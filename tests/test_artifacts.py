import copy
import json
import tempfile
import unittest
from pathlib import Path

from jev_git_graph.artifacts import candidate_content_digest, validate_artifacts, validate_relation_response
from jev_git_graph.errors import JgError
from jev_git_graph.plan import build_plan
from jev_git_graph.questions import QUESTION_IDS, QUESTION_VERSION
from jev_git_graph.safety import digest, write_json


def make_artifacts() -> tuple[dict, dict, dict]:
    inventory = {
        "kind": "inventory",
        "schema_version": 1,
        "repository": {"id": "repo-1", "default_branch": "main", "head": "a" * 40},
        "branches": [
            {"name": "main", "tip": "a" * 40},
            {"name": "topic", "tip": "b" * 40},
        ],
        "worktrees": [],
        "stashes": [],
        "collection": {"complete": True},
    }
    candidate = {
        "id": "pair-1",
        "endpoints": {
            "a": {"branch": "main", "tip": "a" * 40},
            "b": {"branch": "topic", "tip": "b" * 40},
        },
        "reasons": ["CHANGED_PATH_OVERLAP"],
        "evidence": {"shared_paths": [], "shared_subject_tokens": [], "shared_patch_ids": []},
    }
    candidates = {
        "kind": "candidates",
        "schema_version": 1,
        "repository_id": "repo-1",
        "inventory_digest": digest(inventory),
        "candidate_count": 1,
        "candidates": [candidate],
    }
    candidates["content_digest"] = candidate_content_digest(candidates)
    answers = {question_id: {"noul": 0.5} for question_id in QUESTION_IDS}
    response = {"model": "jev-latest", "usage": {"input_tokens": 2, "output_tokens": 3}, "answers": answers}
    relations = {
        "kind": "relations",
        "question_version": QUESTION_VERSION,
        "repository_id": "repo-1",
        "candidate_digest": digest(candidates),
        "candidate_content_digest": candidates["content_digest"],
        "relations": [{"candidate_id": "pair-1", "question_version": QUESTION_VERSION, "response": response}],
    }
    return inventory, candidates, relations


class ArtifactValidationTests(unittest.TestCase):
    def test_altered_candidate_payload_and_endpoint_tip_are_rejected(self):
        inventory, candidates, relations = make_artifacts()
        altered = copy.deepcopy(candidates)
        altered["candidates"][0]["reasons"].append("ALTERED")
        with self.assertRaisesRegex(JgError, "content digest"):
            validate_artifacts(inventory, altered, relations)
        mismatched = copy.deepcopy(candidates)
        mismatched["candidates"][0]["endpoints"]["b"]["tip"] = "c" * 40
        mismatched["content_digest"] = candidate_content_digest(mismatched)
        with self.assertRaisesRegex(JgError, "tip does not match"):
            validate_artifacts(inventory, mismatched)

    def test_duplicate_candidate_ids_and_incomplete_inventory_are_rejected(self):
        inventory, candidates, relations = make_artifacts()
        duplicated = copy.deepcopy(candidates)
        duplicated["candidates"].append(copy.deepcopy(duplicated["candidates"][0]))
        duplicated["candidate_count"] = 2
        duplicated["content_digest"] = candidate_content_digest(duplicated)
        with self.assertRaisesRegex(JgError, "duplicate candidate id"):
            validate_artifacts(inventory, duplicated, relations)
        incomplete = copy.deepcopy(inventory)
        incomplete["collection"]["complete"] = False
        with self.assertRaisesRegex(JgError, "incomplete"):
            validate_artifacts(incomplete, candidates)

    def test_mismatched_relation_provenance_and_malformed_responses_are_rejected(self):
        inventory, candidates, relations = make_artifacts()
        mismatched = copy.deepcopy(relations)
        mismatched["candidate_content_digest"] = "0" * 64
        with self.assertRaisesRegex(JgError, "different candidate content"):
            validate_artifacts(inventory, candidates, mismatched)
        malformed = copy.deepcopy(relations)
        malformed["relations"][0]["response"]["answers"].pop("same_intent")
        with self.assertRaisesRegex(JgError, "v3 contract"):
            validate_artifacts(inventory, candidates, malformed)

    def test_historical_v2_response_is_readable_but_missing_provenance_is_a_limitation(self):
        inventory, candidates, _relations = make_artifacts()
        choices = {
            "A_SUPERSEDES_B": 0.1,
            "B_SUPERSEDES_A": 0.1,
            "A_DEPENDS_ON_B": 0.1,
            "B_DEPENDS_ON_A": 0.1,
            "PARTIAL_OVERLAP": 0.2,
            "UNRELATED": 0.2,
            "INSUFFICIENT_EVIDENCE": 0.2,
        }
        legacy = {
            "kind": "relations",
            "question_version": "branch-relationship-v2",
            "relations": [{
                "candidate_id": "pair-1",
                "response": {
                    "answers": {
                        "same_intent": {"noul": 0.5},
                        "relationship": {"choice": "PARTIAL_OVERLAP", "confidence": 0.5, "probabilities": choices},
                    }
                },
            }],
        }
        result = validate_artifacts(inventory, candidates, legacy)
        self.assertIn("relations_repository_provenance_missing", result["limitations"])
        self.assertIn("relations_candidate_content_provenance_missing", result["limitations"])
        self.assertIn("historical_relation_contract", result["limitations"])
        self.assertEqual("not_verified", result["cleanup_readiness"])
        with self.assertRaises(JgError):
            validate_relation_response({"answers": {"same_intent": {"noul": 2}}}, "branch-relationship-v2")

    def test_build_plan_surfaces_validation_limitations_without_changing_api(self):
        inventory, candidates, relations = make_artifacts()
        relations.pop("repository_id")
        relations.pop("candidate_content_digest")
        relations.pop("candidate_digest")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory_path = root / "inventory.json"
            candidates_path = root / "candidates.json"
            relations_path = root / "relations.json"
            write_json(inventory_path, inventory)
            write_json(candidates_path, candidates)
            write_json(relations_path, relations)
            plan, _rendered = build_plan(inventory_path, candidates_path, relations_path)
        self.assertEqual("not_verified", plan["cleanup_readiness"])
        self.assertIn("relations_repository_provenance_missing", plan["validation_limitations"])


if __name__ == "__main__":
    unittest.main()
