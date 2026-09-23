from __future__ import annotations

from copy import deepcopy

import pytest

from jev_git_graph.errors import JgError
from jev_git_graph.groups import build_groups
from jev_git_graph.safety import digest


def contribution_artifact(branches, units, destination_units=None, edges=None):
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
        "limitations": [],
    }
    artifact["contributions_digest"] = digest(artifact)
    return artifact


def branch(name, eligible=True, exclusion_reasons=None):
    return {
        "name": name,
        "tip": ("d" * 40),
        "merge_base": "e" * 40,
        "eligible": eligible,
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


def test_main_destination_matches_do_not_join_unrelated_sources():
    branches = [branch("feature/one"), branch("fix/two")]
    destinations = [{"id": "du-main", "path": "same.py", "blob": "f" * 40}]
    units = [
        unit("cu-one", "feature/one", path="one.py", destination_ids=["du-main"]),
        unit("cu-two", "fix/two", path="two.py", destination_ids=["du-main"]),
    ]
    result = build_groups(contribution_artifact(branches, units, destinations))
    assert [group["unit_ids"] for group in result["groups"]] == [["cu-one"], ["cu-two"]]
    assert all(group["destination_ids"] == ["du-main"] for group in result["groups"])


def test_unrelated_contributions_remain_disconnected_and_exclusions_are_accounted():
    branches = [branch("feature/one"), branch("feature/two"), branch("feature/active", False, ["active_within_cutoff"])]
    units = [unit("cu-one", "feature/one"), unit("cu-two", "feature/two"), unit("cu-active", "feature/active")]
    result = build_groups(contribution_artifact(branches, units))
    assert [group["unit_ids"] for group in result["groups"]] == [["cu-one"], ["cu-two"]]
    assert result["coverage"]["excluded_source_units"] == ["cu-active"]
    assert result["coverage"]["excluded_branches"] == [
        {"branch": "feature/active", "exclusion_reasons": ["active_within_cutoff"]}
    ]


def test_missing_candidate_metadata_is_reported_as_unknown():
    artifact = contribution_artifact([branch("feature/a")], [unit("cu-a", "feature/a")])
    result = build_groups(artifact)
    assert result["coverage"]["candidate_metadata_unknown_units"] == ["cu-a"]
    assert "candidate_metadata_missing" in result["groups"][0]["limitations"]


def test_related_branch_family_and_shared_ast_create_candidate_edges():
    branches = [branch("team/task/base"), branch("team/task/followup"), branch("other/task")]
    units = [
        unit("cu-a", "team/task/base", ast="ast-x"),
        unit("cu-b", "team/task/followup", ast="ast-x"),
        unit("cu-c", "other/task", ast="ast-x"),
    ]
    result = build_groups(contribution_artifact(branches, units))
    assert result["groups"][0]["unit_ids"] == ["cu-a", "cu-b", "cu-c"]
    edge_kinds = {edge["kind"] for edge in result["groups"][0]["edges"]}
    assert "branch_family" in edge_kinds
    assert "ast_fingerprint" in edge_kinds
    assert all(edge["provenance"] == "contribution_metadata" for edge in result["groups"][0]["edges"])


def test_large_component_is_partitioned_with_boundary_edge_ids():
    branches = [branch("squad/task/part")]
    units = [unit(f"cu-{index:03}", "squad/task/part") for index in range(7)]
    result = build_groups(contribution_artifact(branches, units), max_units=3)
    groups = result["groups"]
    assert [len(group["unit_ids"]) for group in groups] == [3, 3, 1]
    boundary_ids = {edge["id"] for group in groups for edge in group["boundary_edges"]}
    assert boundary_ids
    assert all("partition_has_known_cross_group_edges" in group["limitations"] for group in groups[:2])


def test_common_path_candidate_discovery_has_a_hard_budget_and_omission_count():
    branches = [branch(f"independent/{index:03}") for index in range(300)]
    units = [unit(f"cu-{index:03}", f"independent/{index:03}", path="shared/common.py") for index in range(300)]
    result = build_groups(contribution_artifact(branches, units), max_units=24)
    assert result["coverage"]["candidate_edges_discovered"] <= 256
    assert result["coverage"]["omitted_candidates_by_type"]["path"] > 0
    assert result["coverage"]["truncated"] is True
    assert sum(len(group["unit_ids"]) for group in result["groups"]) == 300


def test_invalid_digest_duplicate_ids_and_unknown_references_fail_closed():
    artifact = contribution_artifact([branch("feature/a")], [unit("cu-a", "feature/a")])
    tampered = deepcopy(artifact)
    tampered["units"][0]["source"]["path"] = "changed.py"
    with pytest.raises(JgError, match="digest"):
        build_groups(tampered)

    duplicate = contribution_artifact(
        [branch("feature/a")], [unit("cu-a", "feature/a"), unit("cu-a", "feature/a")]
    )
    with pytest.raises(JgError, match="duplicate unit id"):
        build_groups(duplicate)

    unknown = contribution_artifact(
        [branch("feature/a")], [unit("cu-a", "feature/a", destination_ids=["du-missing"])]
    )
    with pytest.raises(JgError, match="unknown destination id"):
        build_groups(unknown)
