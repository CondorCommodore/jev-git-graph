from __future__ import annotations

import copy
import unittest

from jev_git_graph.artifacts import candidate_content_digest, validate_artifacts
from jev_git_graph.errors import JgError
from jev_git_graph.questions import QUESTION_IDS, QUESTION_VERSION
from jev_git_graph.review import (
    object_fingerprint,
    object_id,
    reconcile_reviews,
    validate_review,
    validate_review_document,
)
from jev_git_graph.safety import digest


def make_artifacts() -> tuple[dict, dict, dict]:
    inventory = {
        "kind": "inventory",
        "schema_version": 1,
        "repository": {"id": "repo-review", "default_branch": "main", "head": "a" * 40},
        "branches": [
            {"name": "main", "tip": "a" * 40},
            {"name": "topic", "tip": "b" * 40},
        ],
        "worktrees": [{
            "path_id": "worktree-1",
            "head": "a" * 40,
            "branch": "main",
            "detached": False,
            "locked": False,
            "status": [" M README.md"],
        }],
        "stashes": [{"sha": "c" * 40, "reference": "stash@{0}", "subject": "preserve"}],
        "collection": {"complete": True, "counts": {"branches": 2, "worktrees": 1, "stashes": 1}},
    }
    candidates = {
        "kind": "candidates",
        "schema_version": 1,
        "repository_id": "repo-review",
        "inventory_digest": digest(inventory),
        "candidate_count": 1,
        "candidates": [{
            "id": "pair-1",
            "endpoints": {
                "a": {"branch": "main", "tip": "a" * 40},
                "b": {"branch": "topic", "tip": "b" * 40},
            },
            "reasons": ["CHANGED_PATH_OVERLAP"],
            "evidence": {"shared_paths": ["src/app.py"], "shared_subject_tokens": [], "shared_patch_ids": []},
        }],
    }
    candidates["content_digest"] = candidate_content_digest(candidates)
    response = {"model": "jev-latest", "usage": {"input_tokens": 2, "output_tokens": 3},
                "answers": {question_id: {"noul": 0.5} for question_id in QUESTION_IDS}}
    relations = {
        "kind": "relations",
        "question_version": QUESTION_VERSION,
        "repository_id": "repo-review",
        "inventory_digest": digest(inventory),
        "candidate_digest": digest(candidates),
        "candidate_content_digest": candidates["content_digest"],
        "relations": [{"candidate_id": "pair-1", "judgment_id": "judgment-1",
                       "question_version": QUESTION_VERSION, "response": response}],
    }
    validate_artifacts(inventory, candidates, relations)
    return inventory, candidates, relations


def provenance(inventory: dict, candidates: dict, relations: dict | None) -> dict:
    return {
        "repository_id": inventory["repository"]["id"],
        "inventory_digest": digest(inventory),
        "candidate_digest": digest(candidates),
        "candidate_content_digest": candidates["content_digest"],
        "relations_digest": digest(relations) if relations is not None else None,
    }


def v2_review(inventory: dict, candidates: dict, relations: dict | None) -> dict:
    source = provenance(inventory, candidates, relations)
    decisions = []
    for kind, records in (("branch", inventory["branches"]), ("worktree", inventory["worktrees"]), ("stash", inventory["stashes"])):
        for item in records:
            key = object_id(kind, item)
            fingerprint = object_fingerprint(kind, item)
            destination = {"kind": "archive", "archive_id": f"archive:{key}", "repository_id": source["repository_id"]}
            evidence = {"review_signal": key}
            decisions.append({
                "object_id": key,
                "kind": kind,
                "fingerprint": fingerprint,
                "source_fingerprint": fingerprint,
                "source_provenance": copy.deepcopy(source),
                "reviewer_id": "reviewer@example.test",
                "rationale": "Maintainer reviewed this object.",
                "reviewed_at": "2026-09-21T12:00:00Z",
                "disposition": "PRESERVE_IN_ARCHIVE",
                "preservation_destination": destination,
                "preservation_proof": {
                    "verified": True,
                    "source_fingerprint": fingerprint,
                    "destination_fingerprint": digest(destination),
                    "destination": destination,
                },
                "evidence": evidence,
                "evidence_fingerprint": digest(evidence),
            })
    return {
        "kind": "relationship-review",
        "schema_version": 2,
        "repository_id": source["repository_id"],
        **source,
        "provenance": copy.deepcopy(source),
        "reviewer_identity": "reviewer@example.test",
        "decisions": decisions,
    }


class ReviewReconciliationTests(unittest.TestCase):
    def test_unchanged_reconciliation_carries_decisions_and_all_objects(self):
        inventory, candidates, relations = make_artifacts()
        previous = v2_review(inventory, candidates, relations)
        result = reconcile_reviews(previous, inventory, candidates, relations)

        self.assertEqual(2, result["schema_version"])
        self.assertEqual(
            {object_id(kind, item) for kind, records in (("branch", inventory["branches"]), ("worktree", inventory["worktrees"]), ("stash", inventory["stashes"])) for item in records},
            {decision["object_id"] for decision in result["decisions"]},
        )
        for decision in result["decisions"]:
            self.assertEqual("current", decision["reconciliation"]["status"])
            self.assertEqual("reviewer@example.test", decision["reviewer_id"])
            self.assertEqual("Maintainer reviewed this object.", decision["rationale"])
            self.assertEqual("PRESERVE_IN_ARCHIVE", decision["disposition"])
        validate_review_document(result, inventory["repository"]["id"])

    def test_changed_source_and_artifact_provenance_are_stale(self):
        inventory, candidates, relations = make_artifacts()
        previous = v2_review(inventory, candidates, relations)
        changed_inventory = copy.deepcopy(inventory)
        changed_inventory["branches"][1]["tip"] = "d" * 40
        changed_candidates = copy.deepcopy(candidates)
        changed_candidates["inventory_digest"] = digest(changed_inventory)
        changed_candidates["candidates"][0]["endpoints"]["b"]["tip"] = "d" * 40
        changed_candidates["content_digest"] = candidate_content_digest(changed_candidates)
        changed_relations = copy.deepcopy(relations)
        changed_relations["inventory_digest"] = digest(changed_inventory)
        changed_relations["candidate_digest"] = digest(changed_candidates)
        changed_relations["candidate_content_digest"] = changed_candidates["content_digest"]

        result = reconcile_reviews(previous, changed_inventory, changed_candidates, changed_relations)
        topic = next(decision for decision in result["decisions"] if decision["object_id"] == "branch:topic")
        self.assertEqual("stale", topic["reconciliation"]["status"])
        self.assertIn("source_fingerprint_changed", topic["reconciliation"]["reasons"])
        self.assertIn("source_provenance_changed", topic["reconciliation"]["reasons"])
        self.assertIn("evidence_stale", topic["reconciliation"]["reasons"])

        changed_candidates["candidates"][0]["reasons"].append("NEW_SIGNAL")
        changed_candidates["content_digest"] = candidate_content_digest(changed_candidates)
        changed_relations["candidate_digest"] = digest(changed_candidates)
        changed_relations["candidate_content_digest"] = changed_candidates["content_digest"]
        result = reconcile_reviews(previous, changed_inventory, changed_candidates, changed_relations)
        main = next(decision for decision in result["decisions"] if decision["object_id"] == "branch:main")
        self.assertEqual("stale", main["reconciliation"]["status"])
        self.assertIn("evidence_stale", main["reconciliation"]["reasons"])

    def test_changed_preservation_destination_and_proof_are_stale(self):
        inventory, candidates, relations = make_artifacts()
        previous = v2_review(inventory, candidates, relations)
        decision = previous["decisions"][0]
        decision["preservation_proof"]["source_fingerprint"] = "0" * 64
        decision["preservation_proof"]["destination_fingerprint"] = "1" * 64
        result = reconcile_reviews(previous, inventory, candidates, relations)
        carried = next(item for item in result["decisions"] if item["object_id"] == decision["object_id"])
        self.assertEqual("stale", carried["reconciliation"]["status"])
        self.assertIn("preservation_proof_stale", carried["reconciliation"]["reasons"])
        self.assertIn("preservation_destination_stale", carried["reconciliation"]["reasons"])

    def test_required_dispositions_cannot_be_current_without_preservation_evidence(self):
        inventory, candidates, relations = make_artifacts()
        for disposition in (
            "PRESERVE_IN_PR",
            "PRESERVE_IN_BRANCH",
            "PRESERVE_IN_ARCHIVE",
            "CLEANUP_CANDIDATE",
        ):
            review = v2_review(inventory, candidates, relations)
            decision = review["decisions"][0]
            decision["disposition"] = disposition
            decision["preservation_destination"] = None
            decision["preservation_proof"] = None
            validated = validate_review_document(review, inventory["repository"]["id"])
            self.assertIn("preservation_destination_invalid", validated["limitations"])
            self.assertIn("preservation_proof_missing", validated["limitations"])
            result = reconcile_reviews(review, inventory, candidates, relations)
            carried = next(item for item in result["decisions"] if item["object_id"] == decision["object_id"])
            self.assertEqual("stale", carried["reconciliation"]["status"])
            self.assertIn("preservation_destination_invalid", carried["reconciliation"]["reasons"])
            self.assertIn("preservation_proof_missing", carried["reconciliation"]["reasons"])
            self.assertEqual("not_verified", result["cleanup_readiness"])

    def test_truthy_meaningless_destination_and_incomplete_proof_are_stale(self):
        inventory, candidates, relations = make_artifacts()
        for disposition in ("PRESERVE_IN_ARCHIVE", "CLEANUP_CANDIDATE"):
            review = v2_review(inventory, candidates, relations)
            decision = review["decisions"][0]
            decision["disposition"] = disposition
            decision["preservation_destination"] = {"meaningless": "value"}
            decision["preservation_proof"] = {"verified": True}
            validated = validate_review_document(review, inventory["repository"]["id"])
            self.assertIn("preservation_destination_invalid", validated["limitations"])
            self.assertIn("preservation_source_fingerprint_missing", validated["limitations"])
            self.assertIn("preservation_destination_fingerprint_missing", validated["limitations"])
            result = reconcile_reviews(review, inventory, candidates, relations)
            carried = next(item for item in result["decisions"] if item["object_id"] == decision["object_id"])
            self.assertEqual("stale", carried["reconciliation"]["status"])
            self.assertIn("preservation_destination_invalid", carried["reconciliation"]["reasons"])
            self.assertIn("preservation_source_fingerprint_missing", carried["reconciliation"]["reasons"])
            self.assertIn("preservation_destination_fingerprint_missing", carried["reconciliation"]["reasons"])

    def test_active_and_unresolved_allow_null_preservation_fields(self):
        inventory, candidates, relations = make_artifacts()
        for disposition in ("ACTIVE", "UNRESOLVED"):
            review = v2_review(inventory, candidates, relations)
            decision = review["decisions"][0]
            decision["disposition"] = disposition
            decision["preservation_destination"] = None
            decision["preservation_proof"] = None
            validated = validate_review_document(review, inventory["repository"]["id"])
            self.assertNotIn("preservation_destination_missing", validated["limitations"])
            result = reconcile_reviews(review, inventory, candidates, relations)
            carried = next(item for item in result["decisions"] if item["object_id"] == decision["object_id"])
            self.assertEqual("current", carried["reconciliation"]["status"])

    def test_v1_is_readable_but_reconciled_as_historical_limited(self):
        inventory, candidates, relations = make_artifacts()
        decisions = []
        for kind, records in (("branch", inventory["branches"]), ("worktree", inventory["worktrees"]), ("stash", inventory["stashes"])):
            for item in records:
                decisions.append({
                    "object_id": object_id(kind, item),
                    "kind": kind,
                    "fingerprint": object_fingerprint(kind, item),
                    "disposition": "ACTIVE",
                    "rationale": "Historical maintainer decision.",
                    "reviewed_at": "2026-09-20T12:00:00Z",
                })
        previous = {"kind": "relationship-review", "schema_version": 1,
                    "repository_id": inventory["repository"]["id"], "decisions": decisions}
        self.assertEqual(len(decisions), len(validate_review(previous, inventory["repository"]["id"])))
        result = reconcile_reviews(previous, inventory, candidates, relations)
        self.assertIn("legacy_review_schema_v1", result["limitations"])
        self.assertIn("reviewer_identity_missing", result["limitations"])
        self.assertTrue(all(item["reconciliation"]["status"] == "historical-limited" for item in result["decisions"]))
        self.assertNotIn("current", {item["reconciliation"]["status"] for item in result["decisions"]})
        validate_review_document(result, inventory["repository"]["id"])

    def test_removed_prior_decision_is_retained_as_stale(self):
        inventory, candidates, relations = make_artifacts()
        previous = v2_review(inventory, candidates, relations)
        removed = previous["decisions"][-1]
        reduced_inventory = copy.deepcopy(inventory)
        reduced_inventory["stashes"] = []
        reduced_inventory["collection"]["counts"]["stashes"] = 0
        reduced_candidates = copy.deepcopy(candidates)
        reduced_candidates["inventory_digest"] = digest(reduced_inventory)
        reduced_candidates["content_digest"] = candidate_content_digest(reduced_candidates)
        reduced_relations = copy.deepcopy(relations)
        reduced_relations["inventory_digest"] = digest(reduced_inventory)
        reduced_relations["candidate_digest"] = digest(reduced_candidates)
        reduced_relations["candidate_content_digest"] = reduced_candidates["content_digest"]
        result = reconcile_reviews(previous, reduced_inventory, reduced_candidates, reduced_relations)
        retained = next(item for item in result["decisions"] if item["object_id"] == removed["object_id"])
        self.assertEqual("stale", retained["reconciliation"]["status"])
        self.assertIn("object_missing_from_new_inventory", retained["reconciliation"]["reasons"])
        self.assertIn(removed["object_id"], {item["object_id"] for item in result["decisions"]})

    def test_malformed_top_level_limitations_and_nested_provenance_fail_as_jg_error(self):
        inventory, candidates, relations = make_artifacts()
        review = v2_review(inventory, candidates, relations)
        review["limitations"] = None
        with self.assertRaisesRegex(JgError, "limitations"):
            validate_review_document(review)
        review = v2_review(inventory, candidates, relations)
        review.pop("relations_digest")
        review["provenance"].pop("relations_digest")
        with self.assertRaisesRegex(JgError, "relations_digest"):
            validate_review_document(review)

    def test_cross_repository_duplicate_and_malformed_reviews_are_rejected(self):
        inventory, candidates, relations = make_artifacts()
        previous = v2_review(inventory, candidates, relations)
        other_inventory = copy.deepcopy(inventory)
        other_inventory["repository"]["id"] = "other-repo"
        other_candidates = copy.deepcopy(candidates)
        other_candidates["repository_id"] = "other-repo"
        other_candidates["inventory_digest"] = digest(other_inventory)
        other_candidates["content_digest"] = candidate_content_digest(other_candidates)
        with self.assertRaisesRegex(JgError, "different repository"):
            reconcile_reviews(previous, other_inventory, other_candidates)

        duplicate = copy.deepcopy(previous)
        duplicate["decisions"].append(copy.deepcopy(duplicate["decisions"][0]))
        with self.assertRaisesRegex(JgError, "duplicate object"):
            validate_review_document(duplicate)

        malformed = copy.deepcopy(previous)
        del malformed["decisions"][0]["reviewer_id"]
        with self.assertRaisesRegex(JgError, "reviewer identity"):
            validate_review_document(malformed)

        malformed = copy.deepcopy(previous)
        malformed["decisions"][0]["preservation_proof"]["destination_fingerprint"] = "not-a-digest"
        with self.assertRaisesRegex(JgError, "destination fingerprint"):
            validate_review_document(malformed)


if __name__ == "__main__":
    unittest.main()
