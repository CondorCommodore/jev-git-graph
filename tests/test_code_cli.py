import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev_git_graph.cli import parser, run
from jev_git_graph.errors import JgError
from jev_git_graph.safety import opaque_path_id


def git(repo, *args):
    return subprocess.run(("git", "-C", str(repo), *args), check=True,
                          capture_output=True, text=True).stdout.strip()


class CodeCliTests(unittest.TestCase):
    def test_preview_prints_exact_bytes_without_artifact_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            git(repo, "config", "user.name", "Fixture")
            git(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "unit.py").write_text("def result():\n    return 1\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "base")
            main = git(repo, "rev-parse", "HEAD")
            git(repo, "switch", "-qc", "feature")
            (repo / "unit.py").write_text("def result():\n    return 2\n")
            git(repo, "commit", "-qam", "feature")
            feature = git(repo, "rev-parse", "HEAD")
            candidate = {"id": "pair", "endpoints": {"a": {"tip": feature}, "b": {"tip": main}},
                         "reasons": [], "evidence": {"shared_patch_ids": [], "shared_paths": [],
                         "shared_subject_tokens": [], "a_unique_commit_count": 1,
                         "b_unique_commit_count": 0, "a_merge_base": main, "b_merge_base": main}}
            candidates = base / "candidates.json"
            selection = base / "selection.json"
            candidates.write_text(json.dumps({"kind": "candidates", "repository_id": opaque_path_id(repo),
                                              "candidates": [candidate]}))
            selection.write_text(json.dumps({"kind": "jev-code-selection", "candidates": {"pair": {
                "source_ref": "refs/heads/feature", "source_tip": feature,
                "main_ref": "refs/heads/main", "main_tip": main,
                "ranges": [{"path": "unit.py", "start_line": 1, "end_line": 2}]}}}))
            args = parser().parse_args(["code-relate", "--repo", str(repo), "--candidates", str(candidates),
                                        "--selection", str(selection)])
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                shown = json.loads(run(args))
            self.assertIn("return 2", json.dumps(shown["preview"]))
            self.assertEqual(shown["preview"]["payload_sha256"], shown["approval"]["payload_sha256"])
            self.assertFalse((base / "code-relations.json").exists())
            document = json.loads(selection.read_text())
            document["candidates"]["pair"].pop("source_ref")
            document["candidates"]["pair"].pop("main_ref")
            document["candidates"]["pair"]["snapshot_mode"] = "pinned_commits"
            selection.write_text(json.dumps(document))
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                before_move = json.loads(run(args))
            git(repo, "switch", "-q", "main")
            (repo / "unit.py").write_text("def result():\n    return 3\n")
            git(repo, "commit", "-qam", "main moved")
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                pinned = json.loads(run(args))
            self.assertEqual(pinned["preview"]["payload_sha256"], before_move["preview"]["payload_sha256"])
            document["candidates"]["pair"].pop("snapshot_mode")
            document["candidates"]["pair"].update({"source_ref": "refs/heads/feature", "main_ref": "refs/heads/main"})
            selection.write_text(json.dumps(document))
            with self.assertRaisesRegex(JgError, "main ref moved"):
                run(args)
            document["candidates"]["pair"].pop("source_ref")
            document["candidates"]["pair"].pop("main_ref")
            document["candidates"]["pair"]["snapshot_mode"] = "pinned_commits"
            document["candidates"]["pair"]["source_tip"] = main
            selection.write_text(json.dumps(document))
            with self.assertRaisesRegex(JgError, "candidate endpoints"):
                run(args)
            candidates.write_text(json.dumps({"kind": "candidates", "repository_id": "wrong",
                                              "candidates": [candidate]}))
            with self.assertRaisesRegex(JgError, "different local repository"):
                run(args)


if __name__ == "__main__":
    unittest.main()
