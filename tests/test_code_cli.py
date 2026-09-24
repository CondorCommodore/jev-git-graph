import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev_git_graph.cli import parser, run
from jev_git_graph.errors import JgError
from jev_git_graph.inventory import build_inventory
from jev_git_graph.review import object_fingerprint, object_id
from jev_git_graph.safety import digest, opaque_path_id


def git(repo, *args):
    return subprocess.run(("git", "-C", str(repo), *args), check=True,
                          capture_output=True, text=True).stdout.strip()


class CodeCliTests(unittest.TestCase):
    def test_outcome_review_cli_requires_and_binds_exact_digest_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            review = {
                "kind": "outcome-review", "schema_version": 2,
                "repository_id": "fixture",
                "provenance": {
                    "repository_id": "fixture", "inventory_digest": "a" * 64,
                    "snapshot_digest": "b" * 64, "contributions_digest": "c" * 64,
                    "presence_digest": None, "coverage_digest": None,
                },
                "decisions": [],
            }
            review_path = base / "review.json"
            private_dir = base / "private"
            private_dir.mkdir(mode=0o700)
            receipt_path = private_dir / "review-approval.json"
            review_path.write_text(json.dumps(review), encoding="utf-8")
            with patch("jev_git_graph.presence._presence_key_path",
                       return_value=base / "private" / "receipt.key"):
                preview = parser().parse_args([
                    "outcome-review-approve", "--review", str(review_path),
                ])
                preview_result = json.loads(run(preview))
                self.assertEqual(preview_result["review_sha256"], digest(review))
                self.assertTrue(preview_result["approval_required"])
                self.assertFalse(receipt_path.exists())

                wrong = parser().parse_args([
                    "outcome-review-approve", "--review", str(review_path),
                    "--approved-review-sha256", "0" * 64, "--out", str(receipt_path),
                ])
                with self.assertRaisesRegex(JgError, "does not match"):
                    run(wrong)

                approved = parser().parse_args([
                    "outcome-review-approve", "--review", str(review_path),
                    "--approved-review-sha256", digest(review), "--out", str(receipt_path),
                ])
                run(approved)
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                self.assertEqual(receipt["review_sha256"], digest(review))
                self.assertEqual(receipt["provenance"], review["provenance"])
                self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)

    def test_preservation_queue_cli_writes_canonical_review_page_offline(self):
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
            inventory, _paths, _runner = build_inventory(repo)
            inventory_path = base / "inventory.json"
            outcomes_path = base / "outcomes.json"
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            outcome_objects = []
            for kind, field in (("branch", "branches"), ("worktree", "worktrees"), ("stash", "stashes")):
                for item in inventory[field]:
                    outcome_objects.append({
                        "object_id": object_id(kind, item), "kind": kind,
                        "source_fingerprint": object_fingerprint(kind, item),
                        "review_status": "unreviewed", "human_decision": None,
                        "contribution_ids": [], "contribution_reviews": [],
                    })
            outcomes = {
                "kind": "object-outcomes", "schema_version": 1,
                "repository_id": inventory["repository"]["id"],
                "inventory_digest": digest(inventory),
                "snapshot_digest": "a" * 64, "contributions_digest": "b" * 64,
                "presence_digest": None,
                "review_provenance": {
                    "repository_id": inventory["repository"]["id"],
                    "inventory_digest": digest(inventory), "snapshot_digest": "a" * 64,
                    "contributions_digest": "b" * 64, "presence_digest": None,
                    "coverage_digest": None,
                },
                "objects": outcome_objects, "integration_tasks": [],
            }
            outcomes["outcomes_digest"] = digest(outcomes)
            outcomes_path.write_text(json.dumps(outcomes), encoding="utf-8")
            args = parser().parse_args([
                "preservation-queue", "--repo", str(repo), "--inventory", str(inventory_path),
                "--outcomes", str(outcomes_path), "--out", str(base / "queue"),
            ])
            output = run(args)
            self.assertTrue(output.endswith("preservation-plan.json"))
            self.assertTrue((base / "queue" / "index.html").exists())
            self.assertTrue((base / "queue" / "preservation-plan.json").exists())

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
