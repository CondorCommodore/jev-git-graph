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
        units = [unit(f"cu-{index:03}", "squad/task/part", path=f"src/{index}.py") for index in range(7)]
        result = build_groups(contribution_artifact(branches, units), max_units=3)
        groups = result["groups"]
        self.assertEqual([len(group["unit_ids"]) for group in groups], [3, 3, 1])
        boundary_id_sets = [{edge["id"] for edge in group["boundary_edges"]} for group in groups]
        self.assertTrue(boundary_id_sets[0] & boundary_id_sets[1])
        self.assertTrue(boundary_id_sets[1] & boundary_id_sets[2])
        self.assertTrue(all(group["context_complete"] for group in groups))
        self.assertTrue(all(group["candidate_boundary_edge_count"] > 0 for group in groups))
        self.assertEqual(result["coverage"]["groups_with_candidate_boundaries"], 3)

    def test_common_path_candidate_discovery_uses_linear_edges_and_accounts_for_every_unit(self):
        branches = [branch(f"independent/{index:03}") for index in range(300)]
        units = [unit(f"cu-{index:03}", f"independent/{index:03}", path="shared/common.py") for index in range(300)]
        result = build_groups(contribution_artifact(branches, units), max_units=24)
        self.assertEqual(result["coverage"]["candidate_edges_discovered"], 299)
        self.assertEqual(result["coverage"]["omitted_candidates_by_type"], {})
        self.assertEqual(result["coverage"]["unexpanded_pairwise_candidates_by_type"], {"path": 44551})
        self.assertFalse(result["coverage"]["pairwise_relationships_exhaustive"])
        self.assertFalse(result["coverage"]["truncated"])
        self.assertEqual(sum(len(group["unit_ids"]) for group in result["groups"]), 300)
        self.assertEqual(len(result["groups"]), 13)

    def test_destination_edges_use_per_group_output_budget(self):
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
        self.assertLessEqual(max(len(group["edges"]) + len(group["boundary_edges"]) for group in result["groups"]), 1)
        self.assertFalse(result["coverage"]["truncated"])
        self.assertTrue(all("edge_output_budget_exhausted" not in group["limitations"] for group in result["groups"]))
        for group in result["groups"]:
            self.assertEqual(group["id"], "grp-" + digest({"unit_ids": group["unit_ids"], "destination_ids": group["destination_ids"]})[:24])

    def test_candidate_connectivity_is_not_cut_by_the_output_edge_budget(self):
        branches = [branch(f"feature/{index:03}") for index in range(80)]
        units = [unit(f"cu-{index:03}", f"feature/{index:03}", path="shared/task.py") for index in range(80)]
        result = build_groups(contribution_artifact(branches, units), max_units=20, max_edges=1)
        self.assertEqual([len(group["unit_ids"]) for group in result["groups"]], [20, 20, 20, 20])
        self.assertEqual(result["coverage"]["grouped_source_units"], 80)
        self.assertTrue(result["coverage"]["truncated"])
        self.assertTrue(all(group.get("omitted_edges_by_type") for group in result["groups"]))

    def test_output_budget_keeps_stronger_destination_evidence_first(self):
        branches = [branch("feature/a")]
        units = [unit("cu-a", "feature/a"), unit("cu-b", "feature/a")]
        destinations = [{"id": "du-a"}]
        edges = [{
            "source_id": "cu-a", "destination_id": "du-a",
            "type": "structural_match", "provenance": "ast_fingerprint",
        }]
        result = build_groups(contribution_artifact(branches, units, destinations, edges), max_edges=1)
        self.assertEqual(result["schema_version"], 3)
        self.assertEqual([edge["kind"] for edge in result["groups"][0]["edges"]], ["structural_match"])
        self.assertEqual(result["groups"][0]["omitted_edges_by_type"], {"same_branch": 1})

    def test_group_partition_keeps_strong_dependency_ahead_of_weak_path_signal(self):
        branches = [branch("feature/a"), branch("feature/b"), branch("feature/c")]
        units = [
            unit("cu-a", "feature/a", path="shared.py"),
            unit("cu-b", "feature/b", path="shared.py"),
            unit("cu-c", "feature/c", path="shared.py"),
        ]
        dependency = {
            "source_id": "cu-b", "destination_id": "cu-c",
            "type": "dependency", "provenance": "static_ast_symbol_reference",
        }

        result = build_groups(contribution_artifact(branches, units, edges=[dependency]), max_units=2)

        groups_by_units = {tuple(group["unit_ids"]): group for group in result["groups"]}
        self.assertEqual(set(groups_by_units), {("cu-a",), ("cu-b", "cu-c")})
        strong_group = groups_by_units[("cu-b", "cu-c")]
        self.assertTrue(any(edge["kind"] == "dependency" for edge in strong_group["edges"]))
        self.assertFalse(any(edge["kind"] == "dependency" for group in result["groups"]
                             for edge in group["boundary_edges"]))
        self.assertEqual(result["coverage"]["accepted_group_merges_by_type"], {"dependency": 1})
        self.assertGreater(result["coverage"]["rejected_group_merges_by_type"]["path"], 0)
        self.assertTrue(all(len(group["unit_ids"]) <= 2 for group in result["groups"]))

    def test_group_partition_reports_relationships_cut_by_the_size_bound(self):
        branches = [branch("feature/a")]
        units = [unit(f"cu-{index:03}", "feature/a", path=f"src/{index}.py") for index in range(7)]

        result = build_groups(contribution_artifact(branches, units), max_units=3)

        self.assertEqual(sum(len(group["unit_ids"]) for group in result["groups"]), 7)
        self.assertTrue(all(len(group["unit_ids"]) <= 3 for group in result["groups"]))
        self.assertGreater(result["coverage"]["rejected_group_merges_by_type"]["same_branch"], 0)
        self.assertTrue(all(group["context_complete"] for group in result["groups"]))
        self.assertTrue(all(group["candidate_boundary_edge_count"] > 0 for group in result["groups"]))

    def test_unparsed_python_marks_only_its_group_context_incomplete(self):
        branches = [branch("feature/parsed"), branch("feature/unparsed")]
        units = [
            unit("cu-parsed", "feature/parsed", path="src/parsed.py"),
            unit("cu-unparsed", "feature/unparsed", path="src/unparsed.py",
                 limitations=["python_parse_unsupported"]),
        ]

        result = build_groups(contribution_artifact(branches, units))

        groups_by_unit = {group["unit_ids"][0]: group for group in result["groups"]}
        self.assertTrue(groups_by_unit["cu-parsed"]["context_complete"])
        self.assertFalse(groups_by_unit["cu-unparsed"]["context_complete"])
        self.assertIn("python_parse_unsupported", groups_by_unit["cu-unparsed"]["limitations"])

    def test_dependency_crossing_a_bounded_partition_keeps_context_incomplete(self):
        branches = [branch("feature/a"), branch("feature/b"), branch("feature/c"), branch("feature/d")]
        units = [unit(f"cu-{letter}", f"feature/{letter}", path=f"src/{letter}.py") for letter in "abcd"]
        edges = [
            {"source_id": "cu-a", "destination_id": "cu-b", "type": "dependency", "provenance": "static_ast"},
            {"source_id": "cu-b", "destination_id": "cu-c", "type": "dependency", "provenance": "static_ast"},
            {"source_id": "cu-c", "destination_id": "cu-d", "type": "dependency", "provenance": "static_ast"},
        ]

        result = build_groups(contribution_artifact(branches, units, edges=edges), max_units=2)

        self.assertEqual([group["unit_ids"] for group in result["groups"]], [["cu-a", "cu-b"], ["cu-c", "cu-d"]])
        self.assertEqual(result["coverage"]["groups_with_required_boundaries"], 2)
        self.assertTrue(all(not group["context_complete"] for group in result["groups"]))
        self.assertTrue(all(group["required_boundary_edge_count"] == 1 for group in result["groups"]))

    def test_omitted_dependency_boundary_remains_counted(self):
        branches = [branch(f"feature/{letter}") for letter in "abc"]
        units = [unit(f"cu-{letter}", f"feature/{letter}", path=f"src/{letter}.py") for letter in "abc"]
        edges = [
            {"source_id": "cu-a", "destination_id": "cu-b", "type": "dependency", "provenance": "static_ast"},
            {"source_id": "cu-b", "destination_id": "cu-c", "type": "dependency", "provenance": "static_ast"},
        ]

        result = build_groups(contribution_artifact(branches, units, edges=edges), max_units=2, max_edges=1)

        self.assertEqual(result["coverage"]["groups_with_required_boundaries"], 2)
        self.assertEqual(result["coverage"]["omitted_output_edges_by_type"]["dependency"], 1)
        for group in result["groups"]:
            self.assertEqual(group["required_boundary_edge_count"], 1)
            self.assertEqual(group["omitted_required_boundary_edge_count"], 1)
            self.assertEqual(group["omitted_edges_by_type"]["dependency"], 1)
            self.assertIn("partition_has_required_cross_group_edges", group["limitations"])
            self.assertFalse(group["context_complete"])

    def test_omitted_candidate_boundary_remains_visible_without_dependency_gap(self):
        branches = [branch(f"feature/{letter}") for letter in "abc"]
        units = [unit(f"cu-{letter}", f"feature/{letter}", path=f"src/{letter}.py") for letter in "abc"]
        edges = [
            {"source_id": "cu-a", "destination_id": "cu-b", "type": "dependency", "provenance": "static_ast"},
            {"source_id": "cu-b", "destination_id": "cu-c", "type": "path", "provenance": "path_similarity"},
        ]

        result = build_groups(contribution_artifact(branches, units, edges=edges), max_units=2, max_edges=1)

        self.assertEqual(result["coverage"]["groups_with_candidate_boundaries"], 2)
        self.assertEqual(result["coverage"]["omitted_output_edges_by_type"]["path"], 1)
        for group in result["groups"]:
            self.assertEqual(group["candidate_boundary_edge_count"], 1)
            self.assertEqual(group["omitted_candidate_boundary_edge_count"], 1)
            self.assertNotIn("partition_has_required_cross_group_edges", group["limitations"])
            self.assertNotIn("edge_output_budget_exhausted", group["limitations"])
            self.assertTrue(group["context_complete"])

    def test_structural_match_edge_budget_omission_preserves_destination_context(self):
        branches = [branch("feature/a")]
        destinations = [{"id": "du-a"}, {"id": "du-b"}]
        units = [
            unit("cu-a", "feature/a", path="src/a.py", destination_ids=["du-a"]),
            unit("cu-b", "feature/a", path="src/b.py", destination_ids=["du-b"]),
        ]
        edges = [
            {"source_id": "cu-a", "destination_id": "du-a", "type": "structural_match", "provenance": "ast_fingerprint"},
            {"source_id": "cu-b", "destination_id": "du-b", "type": "structural_match", "provenance": "ast_fingerprint"},
        ]

        result = build_groups(contribution_artifact(branches, units, destinations, edges), max_edges=1)

        group = result["groups"][0]
        self.assertTrue(group["context_complete"])
        self.assertEqual(group["destination_ids"], ["du-a", "du-b"])
        self.assertEqual(group["omitted_edges_by_type"], {"structural_match": 1, "same_branch": 1})
        self.assertNotIn("edge_output_budget_exhausted", group["limitations"])

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

    def test_input_limitations_are_visible_without_claiming_group_context_is_missing(self):
        artifact = contribution_artifact(
            [branch("feature/a")], [unit("cu-a", "feature/a", path="a.py")],
            limitations=["source_diff_unavailable:feature/a"],
        )
        result = build_groups(artifact)
        self.assertIn("source_diff_unavailable:feature/a", result["groups"][0]["analysis_observations"])
        self.assertTrue(result["groups"][0]["context_complete"])
        self.assertIn("source_diff_unavailable:feature/a", result["groups"][0]["analysis_observations"])

    def test_static_ast_limitations_are_visible_without_marking_context_missing(self):
        artifact = contribution_artifact(
            [branch("feature/a")],
            [unit("cu-a", "feature/a", path="a.py", limitations=[
                "binding_resolution_unverified", "ambiguous_destination_match"
            ])],
            limitations=["binding_resolution_unverified", "ambiguous_destination_match"],
        )
        result = build_groups(artifact)
        group = result["groups"][0]
        self.assertTrue(group["context_complete"])
        self.assertEqual(group["analysis_observations"], [
            "ambiguous_destination_match", "binding_resolution_unverified"
        ])
        self.assertEqual(group["limitations"], [])

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
