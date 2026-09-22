import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev_git_graph.residual import analyze_residual
from jev_git_graph.errors import JgError


def git(repo, *args):
    return subprocess.run(("git", "-C", str(repo), *args), check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()


class ResidualTests(unittest.TestCase):
    def test_rejects_temp_directory_inside_inspected_repo(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.name", "Fixture")
            git(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "file.py").write_text("value = 1\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "base")
            tip = git(repo, "rev-parse", "HEAD")
            with patch("jev_git_graph.residual.tempfile.gettempdir", return_value=str(repo)):
                with self.assertRaises(JgError):
                    analyze_residual(repo, tip, tip, ["file.py"])

    def test_moved_definition_is_advisory(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.name", "Fixture")
            git(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "old.py").write_text("def value():\n    return 1\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "base")
            git(repo, "switch", "-qc", "topic")
            (repo / "old.py").write_text("def value():\n    return 2\n")
            git(repo, "commit", "-qam", "change")
            source = git(repo, "rev-parse", "HEAD")
            git(repo, "switch", "-q", "main")
            (repo / "moved.py").write_text("def value():\n    return 2\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "move")
            result = analyze_residual(repo, source, git(repo, "rev-parse", "HEAD"), ["old.py"])
            self.assertEqual("moved.py", result["moved_definitions"][0]["main_path"])
            self.assertFalse(result["exact_proof"])

    def test_simulation_keeps_source_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.name", "Fixture")
            git(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "one.py").write_text("def value():\n    return 1\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "base")
            git(repo, "switch", "-qc", "topic")
            (repo / "one.py").write_text("def value():\n    return 2\n")
            git(repo, "commit", "-qam", "topic")
            source = git(repo, "rev-parse", "HEAD")
            git(repo, "switch", "-q", "main")
            before = git(repo, "status", "--porcelain")
            result = analyze_residual(repo, source, git(repo, "rev-parse", "HEAD"), ["one.py"])
            self.assertFalse(result["exact_proof"])
            self.assertTrue(result["repository_isolated"])
            self.assertEqual(before, git(repo, "status", "--porcelain"))


if __name__ == "__main__":
    unittest.main()
