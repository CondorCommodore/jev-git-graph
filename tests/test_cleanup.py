import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev_git_graph.cleanup import (_atomic_delete,
                                   approve_cleanup_plan, build_cleanup_plan,
                                   execute_cleanup)
from jev_git_graph.coordinator import (CleanupActionJournal,
                                       CooperativeBranchLeaseAdapter,
                                       REQUIRED_HOME_LAB_CREATORS,
                                       cleanup_action_id,
                                       reconcile_interrupted_cleanup)
from jev_git_graph.errors import JgError
from jev_git_graph.inventory import build_inventory
from jev_git_graph.safety import digest, opaque_path_id


def run(repo: Path, *args: str) -> str:
    result = subprocess.run(("git", "-C", str(repo), *args), check=True,
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
    def integrated_lease(self, directory: Path) -> CooperativeBranchLeaseAdapter:
        lease = CooperativeBranchLeaseAdapter(
            "synthetic-repository", directory / "branch-leases",
        )
        for creator_id in REQUIRED_HOME_LAB_CREATORS:
            lease.register_creator(creator_id)
        self.assertTrue(lease.creator_participation_complete)
        return lease

    def test_planner_rechecks_activity_after_coverage(self):
        repo, topic_tip, main_tip = self.make_repo()
        coverage = coverage_for(repo, [{"name": "topic", "tip": topic_tip, "main_tip": main_tip,
                                       "verdict": "EXACT", "reason": None, "last_activity_epoch": 1,
                                       "paths": [{"path": "topic", "verdict": "EXACT_PRESENT"}]}])
        with tempfile.TemporaryDirectory() as out:
            plan = build_cleanup_plan(repo, coverage, bundle_dir=Path(out))
        self.assertEqual([], plan["candidates"])
        self.assertEqual("recent_or_unverifiable_activity", plan["observed"][1]["reason"])

    def make_repo(self) -> tuple[Path, str, str]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        repo = Path(directory.name) / "repo"
        repo.mkdir()
        run(repo, "init", "-q", "-b", "main")
        run(repo, "config", "user.name", "Fixture")
        run(repo, "config", "user.email", "fixture@example.invalid")
        (repo / "base").write_text("base\n")
        run(repo, "add", ".")
        run(repo, "commit", "-qm", "base")
        run(repo, "switch", "-qc", "topic")
        (repo / "topic").write_text("preserved\n")
        run(repo, "add", ".")
        run(repo, "commit", "-qm", "topic")
        topic_tip = run(repo, "rev-parse", "HEAD")
        run(repo, "switch", "-q", "main")
        (repo / "topic").write_text("preserved\n")
        (repo / "main-only").write_text("main\n")
        run(repo, "add", ".")
        run(repo, "commit", "-qm", "land topic and advance main")
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
            lease = self.integrated_lease(root)
            with patch("jev_git_graph.cleanup.CREATOR_LEASE_INTEGRATED", True), \
                 patch("jev_git_graph.cleanup._live_reproof", return_value=None):
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
            run(repo, "update-ref", "-d", "refs/heads/topic", topic_tip)
            lease = self.integrated_lease(root)
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
            lease = self.integrated_lease(root)
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
            lease = self.integrated_lease(Path(out))
            original_release = lease.release
            lease.release = lambda name, tip: releases.append((name, tip)) or original_release(name, tip)
            with patch("jev_git_graph.cleanup._live_reproof", return_value=None), \
                 patch("jev_git_graph.cleanup.CREATOR_LEASE_INTEGRATED", True), \
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
            lease = self.integrated_lease(Path(out))
            original_release = lease.release
            lease.release = lambda name, tip: (original_release(name, tip), False)[1]
            with patch("jev_git_graph.cleanup.CREATOR_LEASE_INTEGRATED", True), \
                 patch("jev_git_graph.cleanup._live_reproof", return_value=None), \
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
