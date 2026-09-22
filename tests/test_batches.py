import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from jev_git_graph.batches import prepare_batches, collect_batches
from jev_git_graph.artifacts import candidate_content_digest
from jev_git_graph.errors import JgError
from jev_git_graph.safety import read_json, write_json, digest
from jev_git_graph.jev import payload_for_candidate


class BatchTests(unittest.TestCase):
    def test_exactly_preserved_pair_never_enters_jev_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = {"id": "pair", "endpoints": {
                "a": {"branch": "a", "tip": "a" * 40},
                "b": {"branch": "b", "tip": "b" * 40}},
                "reasons": [], "evidence": {"shared_patch_ids": [], "shared_paths": [],
                "shared_subject_tokens": [], "a_unique_commit_count": 1,
                "b_unique_commit_count": 1, "a_merge_base": None, "b_merge_base": None}}
            source = {"kind": "candidates", "repository_id": "repo", "inventory_digest": "0" * 64,
                      "candidates": [candidate]}
            write_json(root / "candidates.json", source)
            exact = {"kind": "branch-equivalence", "repository_id": "repo", "inventory_digest": "0" * 64,
                     "branches": [{"name": name, "tip": name * 40,
                                   "content_verdict": "ALREADY_PRESERVED"} for name in ("a", "b")]}
            write_json(root / "equivalence.json", exact)
            result = read_json(prepare_batches(root / "candidates.json", root / "batches", equivalence_path=root / "equivalence.json"))
            self.assertEqual(0, result["pending_requests"])
            self.assertEqual(1, result["exactly_preserved_pairs"])
            self.assertEqual([], result["batches"])
            exact["branches"][0]["in_scope"] = False
            write_json(root / "equivalence.json", exact)
            recent = read_json(prepare_batches(root / "candidates.json", root / "recent-batches", equivalence_path=root / "equivalence.json"))
            self.assertEqual(0, recent["out_of_scope_pairs"])
            self.assertEqual(0, recent["exactly_preserved_pairs"])
            self.assertEqual(1, recent["pending_requests"])

    def test_missing_checkpoint_covered_by_another_plan_is_not_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = {"id": "pair", "endpoints": {
                "a": {"branch": "a", "tip": "a" * 40},
                "b": {"branch": "b", "tip": "b" * 40}},
                "reasons": [], "evidence": {"shared_patch_ids": [], "shared_paths": [],
                "shared_subject_tokens": [], "a_unique_commit_count": 1,
                "b_unique_commit_count": 1, "a_merge_base": None, "b_merge_base": None}}
            source = {"kind": "candidates", "schema_version": 1, "repository_id": "repo",
                      "inventory_digest": "0" * 64, "candidates": [candidate]}
            write_json(root / "candidates.json", source)
            plan_path = prepare_batches(root / "candidates.json", root / "completed", 1)
            preview = read_json(root / "completed/batch-0001/jev-preview.json")
            request_sha = digest(preview["requests"][0])
            write_json(root / "completed/batch-0001/relations.json", {
                "source_preview_sha256": preview["payload_sha256"], "network_performed": True,
                "attempts": [{"request_sha256": request_sha, "candidate_id": "pair", "status": "succeeded"}],
                "relations": [{"candidate_id": "pair", "response": {"answers": {}}}],
            })
            shadow = root / "missing"
            shutil.copytree(root / "completed", shadow)
            (shadow / "batch-0001/relations.json").unlink()
            result = read_json(collect_batches(
                [shadow / "batches.json", plan_path], root / "candidates.json", root / "combined"
            ))
            self.assertEqual(0, result["missing_batches"])
            self.assertEqual(0, result["unattempted_requests"])

    def test_partitions_and_skips_attempts_and_facts_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = [{"id": str(i), "endpoints": {"a": {"tip": "a"*40}, "b": {"tip": "b"*40}},
                           "reasons": [], "evidence": {"shared_patch_ids": [], "shared_paths": [],
                           "shared_subject_tokens": [], "a_unique_commit_count": 1, "b_unique_commit_count": 1,
                           "a_merge_base": None, "b_merge_base": None}} for i in range(6)]
            candidates[0]["evidence"]["a_ancestor_of_b"] = True
            source = {
                "kind": "candidates", "schema_version": 1, "repository_id": "repo-1",
                "inventory_digest": "0" * 64, "candidates": candidates,
            }
            source["content_digest"] = candidate_content_digest(source)
            write_json(root / "candidates.json", source)
            write_json(root / "previous.json", {"attempts": [{"request_sha256": digest(payload_for_candidate(candidates[1])), "candidate_id": candidates[1]["id"], "status": "uncertain"}]})
            target = prepare_batches(root / "candidates.json", root / "batches", 3, [root / "previous.json"])
            result = read_json(target)
            self.assertEqual(result["pending_requests"], 4)
            self.assertEqual([b["request_count"] for b in result["batches"]], [3, 1])
            self.assertEqual(result["fact_only_pairs"], 1)
            self.assertEqual(result["previously_attempted"], 1)
            self.assertFalse(result["network_performed"])
            batch_candidates = read_json(root / "batches/batch-0001/candidates.json")
            self.assertEqual(source["content_digest"], batch_candidates["source_content_digest"])
            self.assertEqual(digest(source), batch_candidates["source_candidate_digest"])
            self.assertEqual(candidate_content_digest(batch_candidates), batch_candidates["content_digest"])
            self.assertNotEqual(source["content_digest"], batch_candidates["content_digest"])
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
            overlapping = read_json(collect_batches([target, target], root / "candidates.json", root / "combined-overlap"))
            self.assertEqual(1, overlapping["counts"]["succeeded"])
            self.assertEqual(1, len(overlapping["relations"]))
            self.assertEqual(1, len(overlapping["attempts"]))

            manifest = read_json(target)
            first_batch = manifest["batches"][0]
            conflict_directory = root / "batches-conflict"
            conflict_directory.mkdir(mode=0o700)
            conflict_batch = conflict_directory / first_batch["directory"]
            conflict_batch.mkdir(mode=0o700)
            for filename in ("candidates.json", "jev-preview.json"):
                write_json(conflict_batch / filename, read_json(root / "batches" / first_batch["directory"] / filename))
            conflict_manifest = copy.deepcopy(manifest)
            conflict_manifest["batches"] = [dict(first_batch, directory=first_batch["directory"])]
            conflict_manifest_path = conflict_directory / "batches.json"
            write_json(conflict_manifest_path, conflict_manifest)
            conflicting_attempt = read_json(root / "batches/batch-0001/relations.json")
            conflicting_attempt["attempts"][0]["status"] = "uncertain"
            write_json(conflict_batch / "relations.json", conflicting_attempt)
            with self.assertRaisesRegex(JgError, "conflicting duplicate request attempt"):
                collect_batches([target, conflict_manifest_path], root / "candidates.json", root / "combined-conflict-status")

            conflicting_response = read_json(root / "batches/batch-0001/relations.json")
            conflicting_response["relations"][0]["response"] = {"answers": {"changed": True}}
            write_json(conflict_batch / "relations.json", conflicting_response)
            with self.assertRaisesRegex(JgError, "conflicting duplicate relation response"):
                collect_batches([target, conflict_manifest_path], root / "candidates.json", root / "combined-conflict-response")
            with self.assertRaises(JgError):
                prepare_batches(root / "candidates.json", root / "batches")

    def test_collect_preserves_old_batch_candidates_without_new_provenance_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = {"id": "pair", "endpoints": {"a": {"tip": "a" * 40}, "b": {"tip": "b" * 40}},
                         "reasons": [], "evidence": {"shared_patch_ids": [], "shared_paths": [],
                         "shared_subject_tokens": [], "a_unique_commit_count": 1, "b_unique_commit_count": 1,
                         "a_merge_base": None, "b_merge_base": None}}
            source = {"kind": "candidates", "schema_version": 1, "repository_id": "repo-1",
                      "inventory_digest": "0" * 64, "candidates": [candidate]}
            source["content_digest"] = candidate_content_digest(source)
            write_json(root / "candidates.json", source)
            manifest_path = prepare_batches(root / "candidates.json", root / "batches", 1)

            old_manifest = read_json(manifest_path)
            old_manifest.pop("candidate_content_digest")
            write_json(manifest_path, old_manifest)
            old_batch_path = root / "batches/batch-0001/candidates.json"
            old_batch = read_json(old_batch_path)
            old_batch.pop("source_candidate_digest")
            old_batch.pop("source_content_digest")
            old_batch["content_digest"] = source["content_digest"]
            write_json(old_batch_path, old_batch)

            combined = read_json(collect_batches(manifest_path, root / "candidates.json", root / "combined"))
            self.assertIn("legacy_batch_provenance_missing:batch-0001", combined["limitations"])
