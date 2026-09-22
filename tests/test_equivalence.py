import subprocess
import tempfile
import unittest
from pathlib import Path

from jev_git_graph.equivalence import build_equivalence
from jev_git_graph.inventory import build_inventory


def run(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()


class EquivalenceTests(unittest.TestCase):
    def test_exact_content_and_worktree_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            run(repo, "init", "-q", "-b", "main")
            run(repo, "config", "user.name", "Fixture")
            run(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "file.txt").write_text("base\n")
            run(repo, "add", ".")
            run(repo, "commit", "-qm", "base")
            base = run(repo, "rev-parse", "HEAD")
            run(repo, "branch", "same-tip")
            run(repo, "switch", "-qc", "feature")
            (repo / "file.txt").write_text("feature\n")
            run(repo, "commit", "-qam", "feature")
            feature = run(repo, "rev-parse", "HEAD")
            run(repo, "switch", "-q", "main")
            run(repo, "cherry-pick", feature)
            run(repo, "commit", "--amend", "-qm", "landed feature")
            run(repo, "branch", "same-tree")
            (repo / "main-only.txt").write_text("unrelated\n")
            run(repo, "add", ".")
            run(repo, "commit", "-qm", "unrelated main work")
            run(repo, "switch", "-qc", "unique", base)
            (repo / "other.txt").write_text("new\n")
            run(repo, "add", ".")
            run(repo, "commit", "-qm", "unique")
            inventory, _, _ = build_inventory(repo)
            refs_before = run(repo, "for-each-ref", "--format=%(refname):%(objectname)")
            status_before = run(repo, "status", "--porcelain")
            result = build_equivalence(repo, inventory, ["same-tree"])
            self.assertEqual(refs_before, run(repo, "for-each-ref", "--format=%(refname):%(objectname)"))
            self.assertEqual(status_before, run(repo, "status", "--porcelain"))
            by_name = {item["name"]: item for item in result["branches"]}
            self.assertEqual("ALREADY_PRESERVED", by_name["feature"]["content_verdict"])
            self.assertEqual("CHANGED_PATHS_IDENTICAL", by_name["feature"]["proof"])
            self.assertEqual("ALREADY_PRESERVED", by_name["same-tree"]["content_verdict"])
            self.assertEqual("ANCESTOR", by_name["same-tree"]["proof"])
            self.assertEqual("ALREADY_PRESERVED", by_name["same-tip"]["content_verdict"])
            self.assertEqual("ANCESTOR", by_name["same-tip"]["proof"])
            self.assertEqual("UNIQUE_WORK_REMAINS", by_name["unique"]["content_verdict"])
            self.assertTrue(by_name["unique"]["worktrees"])
            self.assertFalse(result["destructive_action_authorized"])

    def test_changed_source_tip_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            run(repo, "init", "-q", "-b", "main")
            run(repo, "config", "user.name", "Fixture")
            run(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "f").write_text("a")
            run(repo, "add", ".")
            run(repo, "commit", "-qm", "base")
            run(repo, "branch", "topic")
            inventory, _, _ = build_inventory(repo)
            run(repo, "switch", "-q", "topic")
            (repo / "f").write_text("b")
            run(repo, "commit", "-qam", "new")
            result = build_equivalence(repo, inventory)
            topic = next(item for item in result["branches"] if item["name"] == "topic")
            self.assertEqual("UNPROVEN", topic["content_verdict"])

    def test_approved_destination_and_rename_are_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            run(repo, "init", "-q", "-b", "main")
            run(repo, "config", "user.name", "Fixture")
            run(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "old.txt").write_text("original\n")
            run(repo, "add", ".")
            run(repo, "commit", "-qm", "base")
            run(repo, "switch", "-qc", "source")
            run(repo, "mv", "old.txt", "new.txt")
            run(repo, "commit", "-qm", "rename")
            source_tip = run(repo, "rev-parse", "HEAD")
            run(repo, "branch", "approved", source_tip)
            run(repo, "switch", "-q", "approved")
            run(repo, "commit", "--amend", "-qm", "same exact tree")
            inventory, _, _ = build_inventory(repo)
            without = build_equivalence(repo, inventory)
            source = next(item for item in without["branches"] if item["name"] == "source")
            self.assertEqual("UNIQUE_WORK_REMAINS", source["content_verdict"])
            with_approval = build_equivalence(repo, inventory, ["approved"])
            source = next(item for item in with_approval["branches"] if item["name"] == "source")
            self.assertEqual("ALREADY_PRESERVED", source["content_verdict"])
            self.assertEqual("IDENTICAL_TREE", source["proof"])
            self.assertEqual("approved_branch", source["destination"]["scope"])

    def test_reverted_branch_has_no_net_content_to_preserve(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            run(repo, "init", "-q", "-b", "main")
            run(repo, "config", "user.name", "Fixture")
            run(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "f").write_text("base")
            run(repo, "add", ".")
            run(repo, "commit", "-qm", "base")
            base = run(repo, "rev-parse", "HEAD")
            run(repo, "switch", "-qc", "reverted")
            (repo / "f").write_text("temporary")
            run(repo, "commit", "-qam", "change")
            (repo / "f").write_text("base")
            run(repo, "commit", "-qam", "revert")
            run(repo, "switch", "-q", "main")
            (repo / "other").write_text("advanced")
            run(repo, "add", ".")
            run(repo, "commit", "-qm", "advance main")
            self.assertNotEqual(base, run(repo, "rev-parse", "HEAD"))
            inventory, _, _ = build_inventory(repo)
            result = build_equivalence(repo, inventory)
            branch = next(item for item in result["branches"] if item["name"] == "reverted")
            self.assertEqual("ALREADY_PRESERVED", branch["content_verdict"])
            self.assertEqual("NO_NET_CHANGE", branch["proof"])

    def test_recent_ref_activity_is_excluded_even_with_old_tip(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            run(repo, "init", "-q", "-b", "main")
            run(repo, "config", "user.name", "Fixture")
            run(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "f").write_text("base")
            run(repo, "add", ".")
            run(repo, "commit", "-qm", "base")
            run(repo, "branch", "recent")
            inventory, _, _ = build_inventory(repo)
            recent = next(item for item in inventory["branches"] if item["name"] == "recent")
            recent["committed_at"] = "2000-01-01T00:00:00+00:00"
            result = build_equivalence(repo, inventory, ignore_recent_hours=24)
            branch = next(item for item in result["branches"] if item["name"] == "recent")
            self.assertFalse(branch["in_scope"])
            self.assertEqual("edited_within_lookback", branch["scope_reason"])
            self.assertEqual("UNPROVEN", branch["content_verdict"])

    def test_missing_activity_proof_is_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            run(repo, "init", "-q", "-b", "main")
            run(repo, "config", "user.name", "Fixture")
            run(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "f").write_text("base")
            run(repo, "add", ".")
            run(repo, "commit", "-qm", "base")
            run(repo, "branch", "unknown")
            inventory, _, _ = build_inventory(repo)
            unknown = next(item for item in inventory["branches"] if item["name"] == "unknown")
            unknown.pop("committed_at")
            result = build_equivalence(repo, inventory, ignore_recent_hours=24)
            branch = next(item for item in result["branches"] if item["name"] == "unknown")
            self.assertEqual("activity_unverifiable", branch["scope_reason"])

    def test_dirty_worktree_is_excluded_when_age_cannot_be_known(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            run(repo, "init", "-q", "-b", "main")
            run(repo, "config", "user.name", "Fixture")
            run(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "f").write_text("base")
            run(repo, "add", ".")
            run(repo, "commit", "-qm", "base")
            run(repo, "switch", "-qc", "dirty")
            (repo / "f").write_text("uncommitted")
            inventory, _, _ = build_inventory(repo)
            result = build_equivalence(repo, inventory, ignore_recent_hours=24)
            branch = next(item for item in result["branches"] if item["name"] == "dirty")
            self.assertEqual("worktree_dirty_or_unavailable", branch["scope_reason"])
            self.assertEqual("UNPROVEN", branch["content_verdict"])


if __name__ == "__main__":
    unittest.main()
