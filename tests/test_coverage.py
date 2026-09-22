import subprocess
import tempfile
import unittest
from pathlib import Path

from jev_git_graph.coverage import build_coverage
from jev_git_graph.inventory import build_inventory


def git(repo, *args):
    return subprocess.run(("git", "-C", str(repo), *args), check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()


class CoverageTests(unittest.TestCase):
    def test_stale_ref_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.name", "Fixture")
            git(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "a").write_bytes(b"base")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "base")
            git(repo, "branch", "topic")
            inventory, _, _ = build_inventory(repo)
            git(repo, "switch", "-q", "topic")
            (repo / "a").write_bytes(b"later")
            git(repo, "commit", "-qam", "later")
            record = next(x for x in build_coverage(repo, inventory, 0)["branches"] if x["name"] == "topic")
            self.assertEqual("UNKNOWN", record["verdict"])

    def test_exact_partial_and_mode_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.name", "Fixture")
            git(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "a").write_bytes(b"base\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "base")
            git(repo, "switch", "-qc", "topic")
            (repo / "a").write_bytes(b"topic\n")
            (repo / "b").write_bytes(b"unique\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "topic")
            git(repo, "switch", "-q", "main")
            (repo / "a").write_bytes(b"topic\n")
            git(repo, "commit", "-qam", "land part")
            inventory, _, _ = build_inventory(repo)
            record = next(x for x in build_coverage(repo, inventory, 0)["branches"] if x["name"] == "topic")
            self.assertEqual("DISTINCT", record["verdict"])
            self.assertEqual({"a": "EXACT_PRESENT", "b": "DISTINCT"},
                             {x["path"]: x["verdict"] for x in record["paths"]})
            (repo / "b").write_bytes(b"unique\n")
            git(repo, "add", "b")
            git(repo, "commit", "-qm", "land rest")
            inventory, _, _ = build_inventory(repo)
            record = next(x for x in build_coverage(repo, inventory, 0)["branches"] if x["name"] == "topic")
            self.assertEqual("EXACT", record["verdict"])
            (repo / "b").chmod(0o755)
            git(repo, "add", "b")
            git(repo, "commit", "-qm", "change mode")
            inventory, _, _ = build_inventory(repo)
            record = next(x for x in build_coverage(repo, inventory, 0)["branches"] if x["name"] == "topic")
            self.assertEqual("DISTINCT", record["verdict"])


if __name__ == "__main__":
    unittest.main()
