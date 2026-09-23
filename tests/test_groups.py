from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from jev_git_graph.errors import JgError
from jev_git_graph.groups import build_groups, write_groups
from jev_git_graph.safety import digest


def contribution_artifact(branches, units, destination_units=None, edges=None, limitations=None):
    for branch_record in branches:
        branch_record["unit_ids"] = [
            unit_record["id"] for unit_record in units
            if unit_record["source"]["branch"] == branch_record["name"]
        ]
    artifact = {
        "kind": "contributions",
        "schema_version": 1,
        "repository_id": "repo-test",
        "snapshot_digest": "a" * 64,
        "main": {"name": "main", "tip": "b" * 40, "tree": "c" * 40},
        "branches": branches,
        "units": units,
        "destination_units": destination_units or [],
        "paths": [],
        "edges": edges or [],
        "limitations": limitations or [],
    }
    artifact["contributions_digest"] = digest(artifact)
    return artifact


def branch(name, eligible=True, exclusion_reasons=None):
    return {
        "name": name,
        "tip": "d" * 40,
        "merge_base": "e" * 40,
        "eligible": eligible,
        "analysis_status": "complete" if eligible else "excluded",
        "path_count": 1,
        "exclusion_reasons": exclusion_reasons or [],
        "unit_ids": [],
    }


def unit(unit_id, branch_name, *, path=None, blob=None, ast=None, name=None, destination_ids=None, limitations=None):
    source = {"branch": branch_name, "kind": "python_definition"}
    for key, value in (("path", path), ("blob", blob), ("ast_fingerprint", ast), ("name", name)):
        if value is not None:
            source[key] = value
    return {
        "id": unit_id,
        "source": source,
        "destination_ids": destination_ids or [],
        "limitations": limitations or [],
    }


class TestGroups(unittest.TestCase):
    def test_main_destination_matches_do_not_join_unrelated_sources(self):
        branches = [branch("feature/one"), branch("fix/two")]
        destinations = [{"id": "du-main", "path": "same.py", "blob": "f" * 40}]
        units = [
            unit("cu-one", "feature/one", path="one.py", destination_ids=["du-main"]),
            unit("cu-two", "fix/two", path="two.py", destination_ids=["du-main"]),
        ]
        result = build_groups(contribution_artifact(branches, units, destinations))
        self.assertEqual([group["unit_ids"] for group in result["groups"]], [["cu-one"], ["cu-two"]])
        self.assertTrue(all(group["destination_ids"] == ["du-main"] for group in result["groups"]))

    def test_unrelated_contributions_remain_disconnected_and_exclusions_are_accounted(self):
        branches = [branch("feature/one"), branch("feature/two"), branch("feature/active", False, ["active_within_cutoff"])]
        units = [unit("cu-one", "feature/one"), unit("cu-two", "feature/two"), unit("cu-active", "feature/active")]
        result = build_groups(contribution_artifact(branches, units))
        self.assertEqual([group["unit_ids"] for group in result["groups"]], [["cu-one"], ["cu-two"]])
        self.assertEqual(result["coverage"]["excluded_source_units"], ["cu-active"])
        self.assertEqual(result["coverage"]["excluded_branches"], [
            {"branch": "feature/active", "exclusion_reasons": ["active_within_cutoff"]}
        ])

    def test_missing_candidate_metadata_is_reported_as_unknown(self):
        artifact = contribution_artifact([branch("feature/a")], [unit("cu-a", "feature/a")])
        result = build_groups(artifact)
        self.assertEqual(result["coverage"]["candidate_metadata_unknown_units"], ["cu-a"])
        self.assertIn("candidate_metadata_missing", result["groups"][0]["limitations"])
        self.assertFalse(result["groups"][0]["context_complete"])

    def test_related_branch_family_and_shared_ast_create_candidate_edges(self):
        branches = [branch("team/task/base"), branch("team/task/followup"), branch("other/task")]
        units = [
            unit("cu-a", "team/task/base", ast="ast-x"),
            unit("cu-b", "team/task/followup", ast="ast-x"),
            unit("cu-c", "other/task", ast="ast-x"),
        ]
        result = build_groups(contribution_artifact(branches, units))
        self.assertEqual(result["groups"][0]["unit_ids"], ["cu-a", "cu-b", "cu-c"])
        edge_kinds = {edge["kind"] for edge in result["groups"][0]["edges"]}
        self.assertIn("branch_family", edge_kinds)
        self.assertIn("ast_fingerprint", edge_kinds)
        self.assertTrue(all(edge["provenance"] == "contribution_metadata" for edge in result["groups"][0]["edges"]))

    def test_large_component_has_bidirectional_boundary_edges(self):
        branches = [branch("squad/task/part")]
        units = [unit(f"cu-{index:03}", "squad/task/part") for index in range(7)]
        result = build_groups(contribution_artifact(branches, units), max_units=3)
        groups = result["groups"]
        self.assertEqual([len(group["unit_ids"]) for group in groups], [3, 3, 1])
        boundary_id_sets = [{edge["id"] for edge in group["boundary_edges"]} for group in groups]
        self.assertTrue(boundary_id_sets[0] & boundary_id_sets[1])
        self.assertTrue(boundary_id_sets[1] & boundary_id_sets[2])
        self.assertTrue(all(not group["context_complete"] for group in groups))

    def test_common_path_candidate_discovery_has_a_hard_budget_and_omission_count(self):
        branches = [branch(f"independent/{index:03}") for index in range(300)]
        units = [unit(f"cu-{index:03}", f"independent/{index:03}", path="shared/common.py") for index in range(300)]
        result = build_groups(contribution_artifact(branches, units), max_units=24)
        self.assertLessEqual(result["coverage"]["candidate_edges_discovered"], 256)
        self.assertEqual(result["coverage"]["omitted_candidates_by_type"]["path"], 300 * 299 // 2 - 256)
        self.assertTrue(result["coverage"]["truncated"])
        self.assertEqual(sum(len(group["unit_ids"]) for group in result["groups"]), 300)

    def test_destination_edges_count_against_actual_output_edge_budget(self):
        branches = [branch("feature/a"), branch("feature/b")]
        units = [unit("cu-a", "feature/a"), unit("cu-b", "feature/b")]
        destinations = [{"id": "du-a"}, {"id": "du-b"}]
        edges = [
            {"source_id": "cu-a", "destination_id": "du-a", "type": "structural_match", "provenance": "ast_fingerprint"},
            {"source_id": "cu-b", "destination_id": "du-b", "type": "structural_match", "provenance": "ast_fingerprint"},
        ]
        result = build_groups(contribution_artifact(branches, units, destinations, edges), max_edges=1)
        emitted = sum(len(group["edges"]) + len(group["boundary_edges"]) for group in result["groups"])
        self.assertEqual(emitted, result["coverage"]["output_edges_emitted"])
        self.assertLessEqual(emitted, 1)
        self.assertTrue(result["coverage"]["truncated"])
        self.assertTrue(all(not group["context_complete"] for group in result["groups"]))
        for group in result["groups"]:
            self.assertEqual(group["id"], "grp-" + digest({"unit_ids": group["unit_ids"], "destination_ids": group["destination_ids"]})[:24])

    def test_edge_to_excluded_source_is_retained_as_one_sided_boundary(self):
        branches = [branch("feature/eligible"), branch("feature/excluded", False, ["active_within_cutoff"])]
        units = [unit("cu-live", "feature/eligible"), unit("cu-held", "feature/excluded")]
        edges = [{
            "source_id": "cu-live",
            "destination_id": "cu-held",
            "type": "dependency_candidate",
            "provenance": "static_reference",
        }]
        result = build_groups(contribution_artifact(branches, units, edges=edges))
        group = result["groups"][0]
        self.assertEqual(len(group["boundary_edges"]), 1)
        edge = group["boundary_edges"][0]
        self.assertEqual(edge["boundary_status"], "excluded_neighbor")
        self.assertEqual(edge["excluded_target_branch"], "feature/excluded")
        self.assertEqual(edge["exclusion_reasons"], ["active_within_cutoff"])
        self.assertFalse(group["context_complete"])
        self.assertEqual(result["coverage"]["excluded_neighbor_edge_count"], 1)

    def test_input_limitations_make_context_incomplete(self):
        artifact = contribution_artifact(
            [branch("feature/a")], [unit("cu-a", "feature/a")],
            limitations=["source_diff_unavailable:feature/a"],
        )
        result = build_groups(artifact)
        self.assertIn("source_diff_unavailable:feature/a", result["groups"][0]["limitations"])
        self.assertFalse(result["groups"][0]["context_complete"])

    def test_write_groups_preserves_existing_artifact(self):
        artifact = contribution_artifact([branch("feature/a")], [unit("cu-a", "feature/a", path="a.py")])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "contributions.json"
            input_path.write_text(json.dumps(artifact), encoding="utf-8")
            output_dir = root / "out"
            output_dir.mkdir(mode=0o700)
            target = write_groups(input_path, output_dir)
            original = target.read_bytes()
            with self.assertRaisesRegex(JgError, "already exists"):
                write_groups(input_path, output_dir)
            self.assertEqual(target.read_bytes(), original)

    def test_invalid_digest_duplicate_ids_and_unknown_references_fail_closed(self):
        artifact = contribution_artifact([branch("feature/a")], [unit("cu-a", "feature/a")])
        tampered = deepcopy(artifact)
        tampered["units"][0]["source"]["path"] = "changed.py"
        with self.assertRaisesRegex(JgError, "digest"):
            build_groups(tampered)

        duplicate = contribution_artifact(
            [branch("feature/a")], [unit("cu-a", "feature/a"), unit("cu-a", "feature/a")]
        )
        with self.assertRaisesRegex(JgError, "duplicate unit id"):
            build_groups(duplicate)

        unknown = contribution_artifact(
            [branch("feature/a")], [unit("cu-a", "feature/a", destination_ids=["du-missing"])]
        )
        with self.assertRaisesRegex(JgError, "unknown destination id"):
            build_groups(unknown)
