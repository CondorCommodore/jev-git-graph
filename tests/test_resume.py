import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev_git_graph.batches import prepare_batches
from jev_git_graph.errors import JgError
from jev_git_graph.resume import resume_batches
from jev_git_graph.safety import digest, read_json, write_json


class ResumeTests(unittest.TestCase):
    def test_total_budget_rejects_before_any_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = {
                "kind": "candidates", "schema_version": 1, "repository_id": "test",
                "inventory_digest": "0" * 64,
                "candidates": [
                    {"id": str(index), "endpoints": {
                        "a": {"branch": "a", "tip": "a" * 40},
                        "b": {"branch": "b", "tip": "b" * 40}},
                     "evidence": {"shared_patch_ids": [], "shared_paths": [],
                                  "shared_subject_tokens": [], "a_unique_commit_count": 1,
                                  "b_unique_commit_count": 1, "a_merge_base": None,
                                  "b_merge_base": None}, "reasons": []}
                    for index in range(2)
                ],
            }
            source = root / "candidates.json"
            write_json(source, candidates)
            plan_path = prepare_batches(source, root / "batches", 1)
            plan_sha = digest(read_json(plan_path))
            with patch("jev_git_graph.resume.execute_preview") as execute:
                with self.assertRaisesRegex(JgError, "total request budget"):
                    resume_batches(plan_path, plan_sha, max_requests=1,
                                   max_payload_bytes=8192, max_total_requests=1)
                execute.assert_not_called()
                with self.assertRaisesRegex(JgError, "approved plan"):
                    resume_batches(plan_path, "0" * 64, max_requests=1,
                                   max_payload_bytes=8192, max_total_requests=2)
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
