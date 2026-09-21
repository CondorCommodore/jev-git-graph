import copy
import unittest

from jev_git_graph.preservation import build_preservation_plan
from jev_git_graph.safety import digest


def fixture():
    inventory = {
        "kind": "inventory", "schema_version": 1,
        "repository": {"id": "repo-1", "default_branch": "main"},
        "branches": [
            {"name": "main", "tip": "a" * 40},
            {"name": "topic", "tip": "b" * 40, "unique_commits": [{"sha": "b" * 40}], "merged_into_default": False},
        ],
        "worktrees": [{"path_id": "wt-1", "head": "b" * 40, "branch": "topic", "detached": False, "locked": False, "status": [" M file"]}],
        "stashes": [{"sha": "c" * 40, "reference": "stash@{0}", "subject": "saved"}],
        "collection": {"complete": True},
    }
    candidate = {
        "id": "pair-1",
        "endpoints": {"a": {"branch": "main", "tip": "a" * 40}, "b": {"branch": "topic", "tip": "b" * 40}},
        "reasons": ["CHANGED_PATH_OVERLAP"],
        "evidence": {"identical_tips": False, "shared_patch_ids": ["p" * 40]},
    }
    candidates = {"kind": "candidates", "schema_version": 1, "repository_id": "repo-1", "candidates": [candidate]}
    candidates["content_digest"] = digest({"repository_id": "repo-1", "inventory_digest": None, "candidates": [candidate]})
    relations = {"kind": "relations", "relations": [{"candidate_id": "pair-1", "response": {"answers": {"same_intent": {"noul": 0.9}}}}]}
    return inventory, candidates, relations


class PreservationPlanTests(unittest.TestCase):
    def test_every_object_is_accounted_for_once_and_risks_are_separate(self):
        inventory, candidates, relations = fixture()
        plan = build_preservation_plan(inventory, candidates, relations)
        self.assertEqual(4, plan["object_count"])
        self.assertEqual(4, plan["unique_object_ids"])
        self.assertFalse(any(record["cleanup_authority"] for record in plan["objects"]))
        by_id = {record["object_id"]: record for record in plan["objects"]}
        self.assertTrue(any(item["queue"] == "UNIQUE_COMMIT_REVIEW" for item in by_id["branch:topic"]["suggestions"]))
        self.assertTrue(any(item["queue"] == "DIRTY_WORKTREE" for item in by_id["worktree:wt-1"]["suggestions"]))
        self.assertTrue(any(item["queue"] == "STASH_REVIEW" for item in by_id["stash:stash@{0}:" + "c" * 40]["suggestions"]))
        self.assertEqual("not_available_from_metadata_only", by_id["worktree:wt-1"]["content_verification"])

    def test_factual_and_semantic_suggestions_never_create_decision(self):
        inventory, candidates, relations = fixture()
        plan = build_preservation_plan(inventory, candidates, relations)
        topic = next(record for record in plan["objects"] if record["object_id"] == "branch:topic")
        queues = {item["queue"] for item in topic["suggestions"]}
        self.assertIn("FACT_PATCH_EQUIVALENT", queues)
        self.assertIn("SEMANTIC_SAME_INTENT", queues)
        self.assertIsNone(topic["human_decision"])
        self.assertIsNone(topic["preservation_proof"])

    def test_review_decision_and_proof_are_copied_separately(self):
        inventory, candidates, relations = fixture()
        decision = {"object_id": "branch:topic", "disposition": "PRESERVE_IN_BRANCH", "rationale": "retain", "preservation_proof": {"verified": True}}
        plan = build_preservation_plan(inventory, candidates, relations, {"decisions": [decision]})
        topic = next(record for record in plan["objects"] if record["object_id"] == "branch:topic")
        self.assertEqual(decision, topic["human_decision"])
        self.assertEqual(decision["preservation_proof"], topic["preservation_proof"])

    def test_duplicate_inventory_objects_are_rejected(self):
        inventory, candidates, relations = fixture()
        inventory["branches"].append(copy.deepcopy(inventory["branches"][1]))
        with self.assertRaisesRegex(Exception, "duplicate preservation object"):
            build_preservation_plan(inventory, candidates, relations)


if __name__ == "__main__":
    unittest.main()
