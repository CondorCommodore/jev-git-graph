import copy
import json
import tempfile
import unittest
from pathlib import Path

from jev_git_graph.artifacts import candidate_content_digest, validate_artifacts, validate_inventory, validate_relation_response, validate_relations
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
    def test_relations_without_inventory_record_unavailable_inventory_provenance(self):
        inventory, candidates, relations = make_artifacts()
        relations["inventory_digest"] = digest(inventory)
        result = validate_relations(relations, candidates, None)
        self.assertIn("relations_inventory_provenance_unavailable", result["limitations"])

    def test_inventory_rejects_malformed_worktrees_stashes_and_duplicate_identities(self):
        inventory, _candidates, _relations = make_artifacts()
        inventory["worktrees"] = [None]
        with self.assertRaisesRegex(JgError, "worktree record"):
            validate_inventory(inventory)

        inventory, _candidates, _relations = make_artifacts()
        inventory["stashes"] = [None]
        with self.assertRaisesRegex(JgError, "stash record"):
            validate_inventory(inventory)

        worktree = {"path_id": "path-1", "head": "a" * 40, "branch": "main", "detached": False, "locked": False, "status": []}
        incomplete_status = copy.deepcopy(worktree)
        incomplete_status["status"] = None
        inventory, _candidates, _relations = make_artifacts()
        inventory["worktrees"] = [incomplete_status]
        with self.assertRaisesRegex(JgError, "status must be a list"):
            validate_inventory(inventory)
        missing_status = copy.deepcopy(worktree)
        del missing_status["status"]
        inventory, _candidates, _relations = make_artifacts()
        inventory["worktrees"] = [missing_status]
        with self.assertRaisesRegex(JgError, "status must be a list"):
            validate_inventory(inventory)

        inventory, _candidates, _relations = make_artifacts()
        inventory["worktrees"] = [worktree, copy.deepcopy(worktree)]
        with self.assertRaisesRegex(JgError, "duplicate worktree identity"):
            validate_inventory(inventory)

        stash = {"sha": "c" * 40, "reference": "stash@{0}", "subject": "saved"}
        inventory, _candidates, _relations = make_artifacts()
        inventory["stashes"] = [stash, copy.deepcopy(stash)]
        with self.assertRaisesRegex(JgError, "duplicate stash identity"):
            validate_inventory(inventory)

    def test_inventory_validates_optional_branch_structure_and_counts(self):
        inventory, _candidates, _relations = make_artifacts()
        inventory["branches"][0]["unique_commits"] = None
        with self.assertRaisesRegex(JgError, "unique_commits"):
            validate_inventory(inventory)

        inventory, _candidates, _relations = make_artifacts()
        inventory["branches"][0]["subject"] = ""
        inventory["branches"][0]["unique_commits"] = [{"sha": "c" * 40, "subject": ""}]
        validate_inventory(inventory)

        inventory, _candidates, _relations = make_artifacts()
        inventory["collection"]["counts"] = {"branches": 99}
        with self.assertRaisesRegex(JgError, "count for branches"):
            validate_inventory(inventory)

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

    def test_judgment_history_allows_mixed_versions_but_rejects_identity_conflicts(self):
        inventory, candidates, relations = make_artifacts()
        legacy_response = {
            "answers": {
                "same_intent": {"noul": 0.5},
                "relationship": {
                    "choice": "PARTIAL_OVERLAP",
                    "confidence": 0.5,
                    "probabilities": {"PARTIAL_OVERLAP": 0.8, "UNKNOWN": 0.2},
                },
            }
        }
        v3_response = {"answers": {question_id: {"noul": 0.5} for question_id in QUESTION_IDS}}
        history = copy.deepcopy(relations)
        history["question_version"] = QUESTION_VERSION
        history["relations"] = [
            {"candidate_id": "pair-1", "judgment_id": "judgment-v2", "question_version": "branch-relationship-v2", "response": legacy_response},
            {"candidate_id": "pair-1", "judgment_id": "judgment-v3", "question_version": QUESTION_VERSION, "response": v3_response},
        ]
        result = validate_artifacts(inventory, candidates, history)
        self.assertEqual(["branch-relationship-v2", QUESTION_VERSION], result["relations"]["question_versions"])

        duplicate = copy.deepcopy(history)
        duplicate["relations"].append(copy.deepcopy(history["relations"][0]))
        with self.assertRaisesRegex(JgError, "duplicate judgment identity"):
            validate_artifacts(inventory, candidates, duplicate)

        conflict = copy.deepcopy(history)
        conflicting = copy.deepcopy(history["relations"][1])
        conflicting["response"]["answers"]["same_intent"]["noul"] = 0.9
        conflict["relations"].append(conflicting)
        with self.assertRaisesRegex(JgError, "conflicting duplicate judgment identity"):
            validate_artifacts(inventory, candidates, conflict)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory_path = root / "inventory.json"
            candidates_path = root / "candidates.json"
            relations_path = root / "relations.json"
            write_json(inventory_path, inventory)
            write_json(candidates_path, candidates)
            write_json(relations_path, history)
            plan, rendered = build_plan(inventory_path, candidates_path, relations_path)
        self.assertEqual(2, plan["relation_count"])
        self.assertIn("no single response selected", rendered)

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
