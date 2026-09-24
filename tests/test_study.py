import pytest

from jev_git_graph.groups import build_groups
from jev_git_graph.errors import JgError
from jev_git_graph.safety import digest
from jev_git_graph.study import (build_selected_study, build_study,
                                 validate_selected_range_manifest)


def _artifact(complete_count=20, total=32):
    branches, units, destinations, paths = [], [], [], []
    for index in range(total):
        branch_name = f"fix/family-{index}/branch"
        unit_id = f"cu-{index}"
        destination_id = f"du-{index}"
        status = "complete" if index < complete_count else "unknown"
        path = f"src/module_{index}.py"
        branches.append({"name": branch_name, "tip": f"{index + 1:040x}", "eligible": True,
                         "exclusion_reasons": [], "unit_ids": [unit_id]})
        units.append({
            "id": unit_id,
            "branch": branch_name,
            "kind": "python_definition",
            "path": path,
            "name": f"subject_{index}",
            "source_tip": f"{index + 1:040x}",
            "source_blob": f"{index + 100:040x}",
            "ast_fingerprint": f"{index + 200:064x}",
            "source": {"branch": branch_name, "kind": "python_definition", "path": path,
                       "blob": f"{index + 100:040x}", "ast_fingerprint": f"{index + 200:064x}",
                       "name": f"subject_{index}"},
            "destination_ids": [destination_id],
            "dependency_context_status": status,
            "limitations": [],
        })
        destinations.append({"id": destination_id, "path": path, "blob": f"{index + 300:040x}",
                             "dependency_context_status": status})
        paths.append({"branch": branch_name, "path": path, "exact": False})
    contributions = {"kind": "contributions", "schema_version": 2, "repository_id": "repo-test",
                     "snapshot_digest": "a" * 64,
                     "main": {"name": "main", "tip": "b" * 40, "tree": "c" * 40},
                     "branches": branches, "units": units, "destination_units": destinations,
                     "paths": paths, "edges": [], "limitations": []}
    contributions["contributions_digest"] = digest(contributions)
    return contributions, build_groups(contributions)


def test_dependency_complete_selection_versions_policy_and_keeps_uncertainty_arm():
    contributions, groups = _artifact()
    study = build_study(contributions, groups, count=24, max_per_family=4,
                        selection_policy="dependency-complete-majority-v1")

    assert study["schema_version"] == 3
    assert study["selection_policy"] == "dependency-complete-majority-v1"
    assert study["supported_pool_count"] == 20
    assert study["supported_case_count"] == 18
    assert study["supported_majority_available"] is True
    assert study["context_stratum_counts"] == {
        "dependency_context_supported": 18,
        "uncertainty_dependency_or_destination_context": 6,
    }
    assert all(case["dependency_context_status"] == "complete"
               for case in study["cases"] if case["context_stratum"] == "dependency_context_supported")
    assert study["selection_uses_model_answers"] is False


def test_study_includes_changed_same_path_implementations():
    contributions, _ = _artifact(complete_count=24, total=32)
    for index in range(8):
        unit = contributions["units"][index]
        unit["destination_candidate_provenance"] = {unit["destination_ids"][0]: "same_path_name"}
        contributions["destination_units"][index]["ast_fingerprint"] = "f" * 64
    contributions["contributions_digest"] = digest({k: v for k, v in contributions.items()
                                                     if k != "contributions_digest"})
    groups = build_groups(contributions)
    study = build_study(contributions, groups, count=32,
                        selection_policy="dependency-complete-majority-v1")
    changed = [case for case in study["cases"] if case["selection_stratum"] == "changed_implementation_candidate"]
    assert changed
    assert all(case["context_stratum"] == "dependency_context_supported" for case in changed)


def _explicit_selection_fixture():
    contributions, _ = _artifact(complete_count=23, total=24)
    destination_by_id = {item["id"]: item for item in contributions["destination_units"]}
    for index, unit in enumerate(contributions["units"]):
        unit.update({"source_tip": f"{index + 1:040x}", "main_tip": contributions["main"]["tip"],
                     "path": unit["source"]["path"], "source_blob": unit["source"]["blob"],
                     "range": {"start_line": 1, "end_line": 3}})
        if index < 23:
            destination_by_id[f"du-{index}"]["range"] = {"start_line": 1, "end_line": 3}
        else:
            unit["destination_ids"] = []
            unit["dependency_context_status"] = "unknown"
    contributions["contributions_digest"] = digest({k: v for k, v in contributions.items()
                                                      if k != "contributions_digest"})
    groups = build_groups(contributions)
    cases = []
    for index, unit in enumerate(contributions["units"]):
        common = {"contribution_id": unit["id"], "dependency_context_status": unit["dependency_context_status"],
                  "destination_dependency_context_statuses": [destination_by_id[d].get("dependency_context_status", "unknown")
                                                               for d in unit["destination_ids"]]}
        if index < 23:
            destination_id = unit["destination_ids"][0]
            destination = destination_by_id[destination_id]
            common.update({"arm": "two_sided_control", "ranges": [{
                "evidence_id": f"{unit['id']}:range-1", "destination_id": destination_id,
                "source_path": unit["path"], "source_range": unit["range"],
                "destination_path": destination["path"], "destination_range": destination["range"],
            }]})
        else:
            common.update({"arm": "source_only_unknown", "ranges": [{
                "evidence_id": f"{unit['id']}:range-1", "source_path": unit["path"],
                "source_range": unit["range"],
            }]})
        cases.append(common)
    selection = {"kind": "presence-study-selection", "schema_version": 1,
                 "repository_id": contributions["repository_id"],
                 "snapshot_digest": contributions["snapshot_digest"],
                 "contributions_digest": contributions["contributions_digest"],
                 "groups_digest": groups["groups_digest"], "cases": cases}
    selection["selection_digest"] = digest(selection)
    return contributions, groups, selection


def test_explicit_selection_keeps_source_only_case_unknown_and_binds_pins():
    contributions, groups, selection = _explicit_selection_fixture()
    study, ranges = build_selected_study(contributions, groups, selection)

    assert study["kind"] == "presence-study"
    assert study["schema_version"] == 3
    assert study["case_count"] == 24
    assert study["contributions_digest"] == contributions["contributions_digest"]
    assert study["groups_digest"] == groups["groups_digest"]
    assert study["cases"][-1]["selection_arm"] == "source_only_unknown"
    assert study["cases"][-1]["dependency_context_status"] == "unknown"
    assert study["cases"][-1]["destination_candidates"] == []
    assert ranges["ranges"][-1]["ranges"][0].get("destination_path") is None
    assert study["evidence_ranges_digest"] == digest(ranges["ranges"])
    validate_selected_range_manifest(study, ranges)
    assert study["context_stratum_counts"]["uncertainty_dependency_or_destination_context"] == 1


def test_explicit_study_rejects_rewritten_line_ranges():
    contributions, groups, selection = _explicit_selection_fixture()
    study, ranges = build_selected_study(contributions, groups, selection)
    ranges["ranges"][0]["ranges"][0]["source_range"]["start_line"] += 1

    with pytest.raises(JgError, match="pinned range manifest"):
        validate_selected_range_manifest(study, ranges)


def test_explicit_selection_rejects_source_only_case_with_a_destination_claim():
    contributions, groups, selection = _explicit_selection_fixture()
    case = selection["cases"][-1]
    case["ranges"][0]["destination_path"] = "src/module_23.py"
    selection["selection_digest"] = digest({k: v for k, v in selection.items() if k != "selection_digest"})

    with pytest.raises(JgError, match="cannot claim destination evidence"):
        build_selected_study(contributions, groups, selection)


def test_explicit_selection_rejects_pinned_dependency_status_drift():
    contributions, groups, selection = _explicit_selection_fixture()
    selection["cases"][0]["dependency_context_status"] = "unknown"
    selection["selection_digest"] = digest({k: v for k, v in selection.items() if k != "selection_digest"})

    with pytest.raises(JgError, match="dependency status changed"):
        build_selected_study(contributions, groups, selection)
