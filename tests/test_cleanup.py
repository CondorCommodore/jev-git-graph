import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev_git_graph.cleanup import (_atomic_delete,
                                   approve_cleanup_plan, build_cleanup_plan,
                                   execute_cleanup, _lease_established)
from jev_git_graph.cli import main as jg_main
from jev_git_graph.coordinator import (CleanupActionJournal,
                                       CooperativeBranchLeaseAdapter,
                                       build_disposable_fixture_inventory,
                                       cleanup_action_id,
                                       reconcile_interrupted_cleanup)
from jev_git_graph.errors import JgError
from jev_git_graph.inventory import build_inventory
from jev_git_graph.safety import digest, opaque_path_id


def run(repo: Path, *args: str, env=None) -> str:
    result = subprocess.run(("git", "-C", str(repo), *args), check=True, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.decode().strip()


def _ref_exists(repo: Path, name: str) -> bool:
    result = subprocess.run(
        ("git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{name}"),
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def coverage_for(repo: Path, records: list[dict]) -> dict:
    main_tip = run(repo, "rev-parse", "main")
    branches = [{"name": "main", "tip": main_tip, "main_tip": main_tip,
                 "verdict": "EXACT", "reason": None,
                 "last_activity_epoch": None, "paths": []}]
    branches.extend(records)
    return {
        "kind": "branch-coverage", "schema_version": 1,
        "repository_id": opaque_path_id(repo), "inventory_digest": "0" * 64,
        "main": {"name": "main", "tip": main_tip}, "recent_hours": 24,
        "activity_cutoff_epoch": 100,
        "branches": branches, "network_performed": False,
        "destructive_action_authorized": False,
    }


def build_old_plan(*args, **kwargs):
    # Fixture commits are created now; simulate a verified old reflog epoch.
    with patch("jev_git_graph.cleanup._activity", return_value=1):
        return build_cleanup_plan(*args, **kwargs)


class CleanupTests(unittest.TestCase):
    def integrated_lease(self, repository: Path) -> CooperativeBranchLeaseAdapter:
        fixture_root = repository.parent
        inventory = build_disposable_fixture_inventory(
            repository, ["topic"], operator="cleanup fixture test", fixture_root=fixture_root)
        return CooperativeBranchLeaseAdapter.for_disposable_fixture(
            repository, inventory, ["topic"], fixture_root=fixture_root)

    def test_planner_rechecks_activity_after_coverage(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_cleanup_plan(repo, coverage, bundle_dir=Path(out))
        self.assertEqual([], plan["candidates"])
        self.assertEqual("recent_or_unverifiable_activity", plan["observed"][1]["reason"])

    def make_repo(self, *, old_commits: bool = False) -> tuple[Path, str, str]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        repo = Path(directory.name) / "repo"
        repo.mkdir()
        run(repo, "init", "-q", "-b", "main")
        run(repo, "config", "user.name", "Fixture")
        run(repo, "config", "user.email", "fixture@example.invalid")
        commit_env = ({**os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                       "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"} if old_commits else None)
        (repo / "base").write_text("base\n")
        run(repo, "add", ".")
        run(repo, "commit", "-qm", "base", env=commit_env)
        run(repo, "switch", "-qc", "topic")
        (repo / "topic").write_text("preserved\n")
        run(repo, "add", ".")
        run(repo, "commit", "-qm", "topic", env=commit_env)
        topic_tip = run(repo, "rev-parse", "HEAD")
        run(repo, "switch", "-q", "main")
        (repo / "topic").write_text("preserved\n")
        (repo / "main-only").write_text("main\n")
        run(repo, "add", ".")
        run(repo, "commit", "-qm", "land topic and advance main", env=commit_env)
        return repo, topic_tip, run(repo, "rev-parse", "main")

    def test_plan_selects_old_exact_and_restores_every_tip(self):
        repo, topic_tip, main_tip = self.make_repo()
        records = [
            {"name": "topic", "tip": topic_tip, "main_tip": main_tip,
             "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
             "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]},
            {"name": "recent", "tip": main_tip, "main_tip": main_tip,
             "verdict": "UNKNOWN", "reason": "recent_activity", "last_activity_epoch": 200,
             "paths": []},
            {"name": "unknown", "tip": main_tip, "main_tip": main_tip,
             "verdict": "UNKNOWN", "reason": "activity_unverifiable", "last_activity_epoch": None,
             "paths": []},
        ]
        run(repo, "branch", "recent", main_tip)
        run(repo, "branch", "unknown", main_tip)
        coverage = coverage_for(repo, records)
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out) / "bundle")
            self.assertEqual(["topic"], [item["name"] for item in plan["candidates"]])
            observed = {item["name"]: item for item in plan["observed"]}
            self.assertEqual("recent_activity", observed["recent"]["reason"])
            self.assertEqual("activity_unverifiable", observed["unknown"]["reason"])
            self.assertFalse(plan["manifest_approved"])
            self.assertTrue(plan["bundle"]["restoration_verified"])
            self.assertEqual(hashlib.sha256(Path(plan["bundle"]["path"]).read_bytes()).hexdigest(), plan["bundle"]["sha256"])

    def test_checked_out_branch_is_held(self):
        repo, topic_tip, main_tip = self.make_repo()
        worktree = repo.parent / "linked"
        run(repo, "worktree", "add", "-q", str(worktree), "topic")
        self.addCleanup(lambda: run(repo, "worktree", "remove", "-f", str(worktree)))
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out))
        self.assertEqual([], plan["candidates"])
        self.assertEqual("checked_out", plan["observed"][1]["reason"])

    def test_executor_requires_exact_approval_and_lease(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out))
            with self.assertRaisesRegex(JgError, "digest"):
                execute_cleanup(repo, plan, approved_digest="wrong")
            with self.assertRaisesRegex(JgError, "manifest"):
                execute_cleanup(repo, plan, approved_digest=plan["plan_digest"])
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            result = execute_cleanup(repo, approved, approved_digest=approved["plan_digest"],
                                     lease_contract={"established": True,
                                                     "acquire": lambda *_: True,
                                                     "release": lambda *_: True})
        self.assertEqual("cooperative_lease_unestablished", result["stopped"])
        self.assertEqual(topic_tip, run(repo, "rev-parse", "topic"))

    def test_cli_fixture_execute_journals_and_reconcile_restores(self, capsys=None):
        repo, topic_tip, main_tip = self.make_repo(old_commits=True)
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            plan_path = root / "plan.json"
            fixture_root = repo.parent
            inventory_path = fixture_root / "fixture-inventory.json"
            journal_path = root / "cleanup.jsonl"
            plan_path.write_text(json.dumps(approved), encoding="utf-8")
            inventory = build_disposable_fixture_inventory(
                repo, ["topic"], operator="explicit disposable fixture", fixture_root=fixture_root)
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            original_append = CleanupActionJournal.append

            def interrupted_append(journal, event):
                if event.get("event") == "result":
                    raise JgError("simulated interruption after ref delete")
                return original_append(journal, event)

            with patch("jev_git_graph.cli.DISPOSABLE_FIXTURE_ROOT", fixture_root), \
                 patch.object(CleanupActionJournal, "append", interrupted_append):
                exit_code = jg_main([
                    "cleanup", "execute", "--repo", str(repo), "--plan", str(plan_path),
                    "--approved-digest", approved["plan_digest"], "--journal", str(journal_path),
                    "--fixture-inventory", str(inventory_path),
                ])
            self.assertEqual(2, exit_code)
            self.assertFalse(_ref_exists(repo, "topic"))
            intent = CleanupActionJournal(journal_path).pending_intents(plan_digest=approved["plan_digest"])
            self.assertEqual(1, len(intent))
            self.assertEqual("disposable_fixture", intent[0]["scope"])

            with patch("jev_git_graph.cli.DISPOSABLE_FIXTURE_ROOT", fixture_root):
                reconcile_exit = jg_main([
                    "cleanup", "reconcile", "--repo", str(repo), "--plan", str(plan_path),
                    "--journal", str(journal_path), "--fixture-inventory", str(inventory_path),
                ])
            self.assertEqual(0, reconcile_exit)
            self.assertEqual(topic_tip, run(repo, "rev-parse", "topic"))
            events = CleanupActionJournal(journal_path).read_events()
            self.assertEqual(["intent", "reconciled"], [event["event"] for event in events])
            self.assertEqual("source_restored_after_interruption", events[-1]["status"])
            self.assertEqual("disposable_fixture", events[-1]["scope"])

    def test_executor_journals_intent_and_result_while_holding_integrated_lease(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            journal_path = root / "actions.jsonl"
            lease = self.integrated_lease(repo)
            with patch("jev_git_graph.cleanup._live_reproof", return_value=None):
                result = execute_cleanup(
                    repo, approved, approved_digest=approved["plan_digest"],
                    lease_contract=lease, journal_path=journal_path,
                )
            events = CleanupActionJournal(journal_path).read_events()
        self.assertEqual(["topic"], [item["name"] for item in result["deleted"]])
        self.assertEqual(["intent", "result"], [event["event"] for event in events])
        self.assertEqual("deleted", events[-1]["status"])
        self.assertEqual(topic_tip, events[0]["tip"])
        self.assertEqual(main_tip, events[-1]["observed_destination_tip"])
        self.assertFalse(_ref_exists(repo, "topic"))

    def test_capability_drift_after_branch_lock_blocks_ref_transaction(self):
        repo, topic_tip, main_tip = self.make_repo(old_commits=True)
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            lease = self.integrated_lease(repo)
            journal_path = root / "actions.jsonl"
            original = _lease_established
            calls = 0

            def drift_after_intent(contract, repository):
                nonlocal calls
                calls += 1
                return False if calls == 4 else original(contract, repository)

            with patch("jev_git_graph.cleanup._lease_established", drift_after_intent):
                result = execute_cleanup(
                    repo, approved, approved_digest=approved["plan_digest"],
                    lease_contract=lease, journal_path=journal_path,
                )
            events = CleanupActionJournal(journal_path).read_events()
        self.assertEqual("creator_capability_changed", result["stopped"])
        self.assertEqual(4, calls)
        self.assertEqual(topic_tip, run(repo, "rev-parse", "topic"))
        self.assertEqual(["intent", "result"], [event["event"] for event in events])

    def test_post_delete_capability_drift_restores_exact_tip_without_overwriting_recreation(self):
        # Exercise both outcomes after a real CAS deletion: restore the pinned
        # source only while the ref is still absent, and preserve a concurrent
        # recreation at a different tip.
        for recreate in (False, True):
            with self.subTest(recreate=recreate):
                repo, topic_tip, main_tip = self.make_repo(old_commits=True)
                coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                               "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                               "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
                with tempfile.TemporaryDirectory() as out:
                    root = Path(out)
                    plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
                    approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
                    lease = self.integrated_lease(repo)
                    original = _lease_established
                    calls = 0

                    def drift_after_delete(contract, repository):
                        nonlocal calls
                        calls += 1
                        if calls == 5:
                            if recreate:
                                run(repo, "update-ref", "refs/heads/topic", main_tip)
                            return False
                        return original(contract, repository)

                    with patch("jev_git_graph.cleanup._live_reproof", return_value=None), \
                         patch("jev_git_graph.cleanup._lease_established", drift_after_delete):
                        result = execute_cleanup(
                            repo, approved, approved_digest=approved["plan_digest"],
                            lease_contract=lease, journal_path=root / "actions.jsonl",
                        )
                    observed_tip = run(repo, "rev-parse", "refs/heads/topic")
                self.assertEqual(5, calls)
                self.assertEqual("creator_capability_changed_after_delete", result["stopped"])
                self.assertTrue(result["restoration_attempted"])
                if recreate:
                    self.assertFalse(result["restored"])
                    self.assertEqual(main_tip, observed_tip)
                else:
                    self.assertTrue(result["restored"])
                    self.assertEqual(topic_tip, observed_tip)

    def test_interrupted_reconciliation_restores_absent_ref_from_bundle(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            action_id = cleanup_action_id(approved["plan_digest"], 0, "topic", topic_tip)
            journal = CleanupActionJournal(root / "actions.jsonl")
            journal.append({
                "event": "intent", "action_id": action_id,
                "plan_digest": approved["plan_digest"], "candidate_index": 0,
                "branch": "topic",
                "tip": topic_tip, "destination": "main", "destination_tip": main_tip,
                "bundle_sha256": approved["bundle"]["sha256"],
            })
            fixture_root = repo.parent
            inventory = build_disposable_fixture_inventory(
                repo, ["topic"], operator="interrupted fixture", fixture_root=fixture_root)
            run(repo, "update-ref", "-d", "refs/heads/topic", topic_tip)
            lease = CooperativeBranchLeaseAdapter.for_disposable_fixture(
                repo, inventory, ["topic"], fixture_root=fixture_root)
            results = reconcile_interrupted_cleanup(repo, approved, journal, lease)
            restored_tip = run(repo, "rev-parse", "refs/heads/topic")
            events = journal.read_events()
        self.assertEqual("source_restored_after_interruption", results[0]["status"])
        self.assertTrue(results[0]["restored"])
        self.assertEqual(topic_tip, restored_tip)
        self.assertEqual("reconciled", events[-1]["event"])
        self.assertEqual([], journal.pending_intents(plan_digest=approved["plan_digest"]))

    def test_interrupted_reconciliation_preserves_recreated_ref(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            plan = build_old_plan(repo, coverage, bundle_dir=root / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            action_id = cleanup_action_id(approved["plan_digest"], 0, "topic", topic_tip)
            journal = CleanupActionJournal(root / "actions.jsonl")
            journal.append({
                "event": "intent", "action_id": action_id,
                "plan_digest": approved["plan_digest"], "candidate_index": 0,
                "branch": "topic",
                "tip": topic_tip, "destination": "main", "destination_tip": main_tip,
                "bundle_sha256": approved["bundle"]["sha256"],
            })
            run(repo, "branch", "--force", "topic", main_tip)
            lease = self.integrated_lease(repo)
            results = reconcile_interrupted_cleanup(repo, approved, journal, lease)
            current_tip = run(repo, "rev-parse", "refs/heads/topic")
        self.assertEqual("source_recreated_or_moved", results[0]["status"])
        self.assertFalse(results[0]["restored"])
        self.assertEqual(main_tip, current_tip)

    def test_destination_verify_and_source_delete_are_one_transaction(self):
        repo, topic_tip, main_tip = self.make_repo()
        self.assertFalse(_atomic_delete(repo, "topic", topic_tip, "main", "0" * 40))
        self.assertEqual(topic_tip, run(repo, "rev-parse", "topic"))
        self.assertEqual(main_tip, run(repo, "rev-parse", "main"))

    def test_failed_post_delete_readback_restores_only_confirmed_absence(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out))
            plan = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            releases = []
            lease = self.integrated_lease(repo)
            original_release = lease.release
            lease.release = lambda name, tip: releases.append((name, tip)) or original_release(name, tip)
            with patch("jev_git_graph.cleanup._live_reproof", return_value=None), \
                 patch("jev_git_graph.cleanup.git.local_branches",
                       side_effect=JgError("readback unavailable")):
                result = execute_cleanup(repo, plan,
                                         approved_digest=plan["plan_digest"],
                                         lease_contract=lease,
                                         journal_path=Path(out) / "actions.jsonl")
        self.assertEqual("delete_readback_uncertain", result["stopped"])
        self.assertTrue(result["restored"])
        self.assertEqual([("topic", topic_tip)], releases)
        self.assertEqual(topic_tip, run(repo, "rev-parse", "topic"))

    def test_executor_rejects_approved_plan_from_another_clone(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out) / "bundle")
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            clone = Path(out) / "clone"
            subprocess.run(("git", "clone", "--quiet", "--no-hardlinks", str(repo), str(clone)), check=True)
            with self.assertRaisesRegex(JgError, "different local repository"):
                execute_cleanup(clone, approved, approved_digest=approved["plan_digest"])

    def test_failed_lease_release_restores_deleted_fixture_ref(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out))
            approved = approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])
            lease = self.integrated_lease(repo)
            original_release = lease.release
            lease.release = lambda name, tip: (original_release(name, tip), False)[1]
            with patch("jev_git_graph.cleanup._live_reproof", return_value=None), \
                 patch("jev_git_graph.cleanup._ref_presence", return_value=None):
                result = execute_cleanup(repo, approved,
                                         approved_digest=approved["plan_digest"],
                                         lease_contract=lease,
                                         journal_path=Path(out) / "actions.jsonl")
        self.assertEqual("lease_release_failed", result["stopped"])
        self.assertTrue(result["restoration_attempted"])
        self.assertTrue(result["restored"])
        self.assertEqual(topic_tip, run(repo, "rev-parse", "topic"))

    def test_manifest_approval_rejects_tampered_plan(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out))
            plan["candidates"][0]["tip"] = "0" * 40
            with self.assertRaisesRegex(JgError, "invalid digest"):
                approve_cleanup_plan(plan, approved_digest=plan["plan_digest"])

    def test_plan_digest_is_stable_and_json_round_trips(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_old_plan(repo, coverage, bundle_dir=Path(out))
            encoded = json.loads(json.dumps(plan))
        self.assertEqual(plan["plan_digest"], digest({key: value for key, value in encoded.items() if key != "plan_digest"}))


if __name__ == "__main__":
    unittest.main()
