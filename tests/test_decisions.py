import copy
import unittest

from jev_git_graph.decisions import build_decisions
from jev_git_graph.errors import JgError
from jev_git_graph.safety import digest


class AutomaticDecisionTests(unittest.TestCase):
    def fixture(self, complete):
        inventory = {
            "kind": "inventory", "schema_version": 1,
            "repository": {"id": "repo", "default_branch": "main"},
            "collection": {"complete": complete},
            "branches": [
                {"name": "main", "tip": "a" * 40},
                {"name": "merged", "tip": "b" * 40, "merged_into_default": True, "unique_commits": []},
                {"name": "unique", "tip": "c" * 40, "merged_into_default": False,
                 "unique_commits": [{"sha": "c" * 40}]},
            ],
            "worktrees": [], "stashes": [],
        }
        candidates = {
            "kind": "candidates", "schema_version": 1, "repository_id": "repo",
            "inventory_digest": digest(inventory), "candidates": [],
        }
        relations = {"kind": "relations", "repository_id": "repo", "relations": []}
        return inventory, candidates, relations

    def test_complete_facts_classify_without_authorizing_deletion(self):
        inventory, candidates, relations = self.fixture(True)
        result = build_decisions(inventory, candidates, relations)
        by_name = {item["name"]: item for item in result["branches"]}
        self.assertEqual("RETAIN", by_name["main"]["decision"])
        self.assertEqual("CLEANUP_CANDIDATE", by_name["merged"]["decision"])
        self.assertEqual("HOLD", by_name["unique"]["decision"])
        self.assertTrue(all(not item["destructive_action_authorized"] for item in result["branches"]))

    def test_incomplete_inventory_holds_even_merged_branch(self):
        inventory, candidates, relations = self.fixture(False)
        result = build_decisions(inventory, candidates, relations)
        by_name = {item["name"]: item for item in result["branches"]}
        self.assertEqual("HOLD", by_name["merged"]["decision"])
        self.assertIn("incomplete_inventory_blocks_cleanup_candidates", result["limitations"])

    def test_exact_content_does_not_override_worktree_hold(self):
        inventory, candidates, relations = self.fixture(True)
        inventory["worktrees"] = [{"branch": "merged", "path_id": "opaque", "status": []}]
        candidates["inventory_digest"] = digest(inventory)
        equivalence = {
            "kind": "branch-equivalence", "repository_id": "repo", "inventory_digest": digest(inventory),
            "branches": [{"name": item["name"], "tip": item["tip"],
                          "content_verdict": "ALREADY_PRESERVED", "proof": "ANCESTOR",
                          "destination": {"branch": "main", "tip": "a" * 40}}
                         for item in inventory["branches"]],
        }
        result = build_decisions(inventory, candidates, relations, equivalence)
        merged = next(item for item in result["branches"] if item["name"] == "merged")
        self.assertEqual("RETAIN", merged["decision"])
        self.assertEqual("ALREADY_PRESERVED", merged["content_verdict"])
        self.assertEqual("checked_out_in_worktree", merged["reason"])

    def test_rejects_mismatched_inventory(self):
        inventory, candidates, relations = self.fixture(True)
        changed = copy.deepcopy(inventory)
        changed["branches"][0]["tip"] = "d" * 40
        with self.assertRaisesRegex(JgError, "does not match inventory"):
            build_decisions(changed, candidates, relations)

    def test_jev_relationship_routes_unique_work_to_related_hold(self):
        inventory, candidates, relations = self.fixture(True)
        candidates["candidates"] = [{
            "id": "pair", "endpoints": {
                "a": {"branch": "merged", "tip": "b" * 40},
                "b": {"branch": "unique", "tip": "c" * 40}},
            "reasons": [], "evidence": {},
        }]
        relations["relations"] = [{
            "candidate_id": "pair", "question_version": "branch-relationship-v2",
            "response": {"answers": {
                "same_intent": {"noul": 0.8},
                "relationship": {"choice": "PARTIAL_OVERLAP", "confidence": 0.8,
                                 "probabilities": {"PARTIAL_OVERLAP": 0.8}},
            }},
        }]
        result = build_decisions(inventory, candidates, relations)
        unique = next(item for item in result["branches"] if item["name"] == "unique")
        self.assertEqual("HOLD", unique["decision"])
        self.assertEqual("RELATED_HOLD", unique["triage"])
        self.assertEqual("PARTIAL_OVERLAP", unique["jev_signals"][0]["signal"])
        self.assertFalse(unique["destructive_action_authorized"])


if __name__ == "__main__":
    unittest.main()
