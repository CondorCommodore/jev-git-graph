import tempfile
import unittest
from pathlib import Path

from jev_git_graph.batches import prepare_batches, collect_batches
from jev_git_graph.errors import JgError
from jev_git_graph.safety import read_json, write_json, digest
from jev_git_graph.jev import payload_for_candidate


class BatchTests(unittest.TestCase):
    def test_partitions_and_skips_attempts_and_facts_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = [{"id": str(i), "endpoints": {"a": {"tip": "a"*40}, "b": {"tip": "b"*40}},
                           "reasons": [], "evidence": {"shared_patch_ids": [], "shared_paths": [],
                           "shared_subject_tokens": [], "a_unique_commit_count": 1, "b_unique_commit_count": 1,
                           "a_merge_base": None, "b_merge_base": None}} for i in range(6)]
            candidates[0]["evidence"]["a_ancestor_of_b"] = True
            write_json(root / "candidates.json", {"candidates": candidates})
            write_json(root / "previous.json", {"attempts": [{"request_sha256": digest(payload_for_candidate(candidates[1])), "candidate_id": candidates[1]["id"], "status": "uncertain"}]})
            target = prepare_batches(root / "candidates.json", root / "batches", 3, [root / "previous.json"])
            result = read_json(target)
            self.assertEqual(result["pending_requests"], 4)
            self.assertEqual([b["request_count"] for b in result["batches"]], [3, 1])
            self.assertEqual(result["fact_only_pairs"], 1)
            self.assertEqual(result["previously_attempted"], 1)
            self.assertFalse(result["network_performed"])
            preview = read_json(root / "batches/batch-0001/jev-preview.json")
            request = preview["requests"][0]
            write_json(root / "batches/batch-0001/relations.json", {
                "source_preview_sha256": preview["payload_sha256"], "network_performed": True,
                "attempts": [{"request_sha256": digest(request), "candidate_id": request["state"]["candidate_id"], "status": "succeeded"}],
                "relations": [{"candidate_id": request["state"]["candidate_id"], "response": {"answers": {}}}],
            })
            combined = read_json(collect_batches(target, root / "candidates.json", root / "combined"))
            self.assertEqual(combined["counts"]["succeeded"], 1)
            self.assertEqual(combined["unattempted_requests"], 3)
            self.assertEqual(combined["missing_batches"], 1)
            self.assertEqual(len(combined["relations"]), 1)
            with self.assertRaises(JgError):
                prepare_batches(root / "candidates.json", root / "batches")
