from jev_git_graph.groups import build_groups
from jev_git_graph.safety import digest
from jev_git_graph.study import build_study


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
