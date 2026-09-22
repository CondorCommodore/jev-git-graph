import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev_git_graph.cleanup import (CooperativeLease, _atomic_delete,
                                   approve_cleanup_plan, build_cleanup_plan,
                                   execute_cleanup)
from jev_git_graph.errors import JgError
from jev_git_graph.inventory import build_inventory
from jev_git_graph.safety import digest, opaque_path_id


def run(repo: Path, *args: str) -> str:
    result = subprocess.run(("git", "-C", str(repo), *args), check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.decode().strip()


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
            lease = CooperativeLease(
                True, lambda *_: True,
                lambda name, tip: releases.append((name, tip)) or True,
            )
            with patch("jev_git_graph.cleanup._live_reproof", return_value=None), \
                 patch("jev_git_graph.cleanup.CREATOR_LEASE_INTEGRATED", True), \
                 patch("jev_git_graph.cleanup.git.local_branches",
                       side_effect=JgError("readback unavailable")):
                result = execute_cleanup(repo, plan,
                                         approved_digest=plan["plan_digest"],
                                         lease_contract=lease)
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
            lease = CooperativeLease(True, lambda *_: True, lambda *_: False)
            with patch("jev_git_graph.cleanup.CREATOR_LEASE_INTEGRATED", True), \
                 patch("jev_git_graph.cleanup._live_reproof", return_value=None), \
                 patch("jev_git_graph.cleanup._ref_presence", return_value=None):
                result = execute_cleanup(repo, approved,
                                         approved_digest=approved["plan_digest"], lease_contract=lease)
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
