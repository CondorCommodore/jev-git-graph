"""Deterministic, model-blind evidence-card selection for a small pilot."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from pathlib import Path

from .errors import JgError
from .groups import _family, _validate
from .safety import digest, read_json, write_json, write_private_text


def build_study(contributions: dict, groups: dict, count: int = 32,
                max_per_family: int = 4, excluded_branches: list[str] | None = None,
                project_goals: str = "", selection_policy: str = "candidate-availability-24-8-v2") -> dict:
    if type(count) is not int or not 24 <= count <= 40:
        raise JgError("pilot study must contain 24 to 40 contributions")
    if type(max_per_family) is not int or not 1 <= max_per_family <= 4:
        raise JgError("max per family must be between 1 and 4")
    if selection_policy not in {"candidate-availability-24-8-v2", "dependency-complete-majority-v1"}:
        raise JgError("unsupported study selection policy")
    _, units, destinations = _validate(contributions)
    if (groups.get("contributions_digest") != contributions["contributions_digest"]
            or groups.get("snapshot_digest") != contributions["snapshot_digest"]
            or groups.get("groups_digest") != digest({k: v for k, v in groups.items() if k != "groups_digest"})):
        raise JgError("groups do not match contributions")
    by_unit = {}
    for group in groups["groups"]:
        for uid in group["unit_ids"]:
            if uid in by_unit or uid not in units:
                raise JgError("group membership is duplicate or unknown")
            by_unit[uid] = group
    path_exact = {(p["branch"], p["path"]): p["exact"] for p in contributions["paths"]}
    excluded = set(excluded_branches or [])
    buckets: dict[tuple[str, str], deque] = defaultdict(deque)
    for uid, unit in sorted(units.items()):
        if unit["branch"] in excluded or uid not in by_unit:
            continue
        matches = unit.get("destination_ids", [])
        if unit["kind"] != "python_definition":
            stratum = "file_level_or_unsupported"
        elif path_exact.get((unit["branch"], unit["path"])):
            stratum = "exact_path_present"
        elif len(matches) > 1:
            stratum = "ambiguous_structural_match"
        elif matches:
            stratum = "structural_candidate_distinct_path"
        else:
            stratum = "no_structural_destination"
        # Group incompleteness records cross-partition similarity signals and
        # is intentionally wider than the context needed for one contribution.
        # A contribution has scoped context when its pinned source unit exists
        # and every known destination candidate resolves to a destination row.
        # Units with no destination candidate remain in an explicit uncertainty
        # stratum for review; they are never presented as globally complete.
        candidate_ids = unit.get("destination_ids", [])
        comparison_available = bool(candidate_ids) and all(candidate_id in destinations for candidate_id in candidate_ids)
        if selection_policy == "dependency-complete-majority-v1":
            # A supported comparison requires a source unit with statically
            # resolved references, one destination candidate, and a resolved
            # dependency context for that destination unit. Similarity-only
            # candidates and unknown extraction status stay in the uncertainty
            # arm; group-wide context completeness is reported separately.
            candidate = destinations[candidate_ids[0]] if len(candidate_ids) == 1 and comparison_available else None
            supported = (unit.get("dependency_context_status") == "complete" and candidate is not None
                         and candidate.get("dependency_context_status") == "complete")
            context_stratum = ("dependency_context_supported" if supported
                               else "uncertainty_dependency_or_destination_context")
        else:
            context_stratum = ("comparison_candidates_available" if comparison_available
                               else "uncertainty_no_destination_candidate")
        buckets[(context_stratum, stratum)].append(unit)
    # Fixed round-robin across evidence strata, capped by task-family labels.
    # Signals select review cases; they never supply reference answers.
    family_counts, selected, seen_behaviors = Counter(), [], set()
    def take(context_stratum: str, target: int) -> None:
        picked = 0
        strata = [key for key in sorted(buckets) if key[0] == context_stratum]
        while picked < target and len(selected) < count:
            progressed = False
            for key in strata:
                while buckets[key]:
                    unit = buckets[key].popleft()
                    family = _family(unit["branch"]) or unit["branch"]
                    behavior = (unit.get("source_blob"), unit.get("ast_fingerprint"), unit.get("name"), unit["path"])
                    if family_counts[family] >= max_per_family or behavior in seen_behaviors:
                        continue
                    group = by_unit[unit["id"]]
                    selected.append({"contribution_id": unit["id"], "family": family,
                                     "selection_stratum": key[1], "context_stratum": context_stratum,
                                     "source": {k: unit.get(k) for k in ("branch", "source_tip", "path", "source_blob", "mode", "kind", "name", "range")},
                                     "destination_candidates": [destinations[d] for d in unit.get("destination_ids", [])],
                                     "group_id": group["id"], "neighbor_ids": [u for u in group["unit_ids"] if u != unit["id"]],
                                     "group_context_complete": group.get("context_complete") is True,
                                     "dependency_context_status": unit.get("dependency_context_status", "unknown"),
                                     "destination_dependency_context_statuses": [
                                         destinations[d].get("dependency_context_status", "unknown")
                                         for d in unit.get("destination_ids", []) if d in destinations
                                     ],
                                     "boundary_edges": group.get("boundary_edges", []),
                                     "limitations": group.get("limitations", []), "reference_label_status": "UNREVIEWED"})
                    family_counts[family] += 1
                    seen_behaviors.add(behavior)
                    picked += 1
                    progressed = True
                    break
                if picked >= target or len(selected) >= count:
                    break
            if not progressed:
                break

    if selection_policy == "dependency-complete-majority-v1":
        supported_context = "dependency_context_supported"
        uncertainty_context = "uncertainty_dependency_or_destination_context"
    else:
        supported_context = "comparison_candidates_available"
        uncertainty_context = "uncertainty_no_destination_candidate"
    supported_pool_count = sum(len(v) for (context, _), v in buckets.items() if context == supported_context)
    supported_target = min(count - count // 4, supported_pool_count)
    take(supported_context, supported_target)
    take(uncertainty_context, count - len(selected))
    # If either stratum had too few diverse cases, fill from the other while
    # keeping every selected case's context status explicit.
    take(supported_context, count - len(selected))
    take(uncertainty_context, count - len(selected))
    if len(selected) < count:
        raise JgError(f"only {len(selected)} eligible diverse cases; do not silently shrink the study")
    selected_supported_count = sum(case["context_stratum"] == supported_context for case in selected)
    result = {"kind": "presence-study", "schema_version": 3 if selection_policy == "dependency-complete-majority-v1" else 2,
              "repository_id": contributions["repository_id"], "snapshot_digest": contributions["snapshot_digest"],
              "contributions_digest": contributions["contributions_digest"], "groups_digest": groups["groups_digest"],
              "selection_policy": selection_policy,
              "project_goals": project_goals, "case_count": len(selected), "family_counts": dict(family_counts),
              "context_stratum_counts": dict(Counter(case["context_stratum"] for case in selected)),
              "supported_pool_count": supported_pool_count,
              "supported_case_count": selected_supported_count,
              "supported_majority_available": selected_supported_count > count // 2,
              "context_scope": ("source and unique destination dependency extraction both complete; this does not prove behavior presence or runtime integration"
                                if selection_policy == "dependency-complete-majority-v1"
                                else "destination candidate availability only; this is not dependency completeness or evidence that behavior is present"),
              "dependency_context_status_counts": dict(Counter(case["dependency_context_status"] for case in selected)),
              "excluded_development_branches": sorted(excluded), "cases": selected,
              "selection_uses_model_answers": False, "provider_dispatch_approved": False,
              "expansion_gate": "UNMEASURED", "split_policy": "Keep entire families together; label before viewing model answers"}
    result["study_digest"] = digest(result)
    return result


def write_study(contributions_path: str, groups_path: str, out: str | Path,
                count: int = 32, max_per_family: int = 4,
                excluded_branches: list[str] | None = None, project_goals: str = "",
                selection_policy: str = "candidate-availability-24-8-v2") -> Path:
    result = build_study(read_json(contributions_path), read_json(groups_path), count,
                         max_per_family, excluded_branches, project_goals, selection_policy)
    destination = Path(out)
    if destination.exists():
        raise JgError("study output already exists")
    destination.mkdir(mode=0o700, parents=True)
    write_json(destination / "study.json", result)
    labels_path = destination / "owner-labels.json"
    if not labels_path.exists():
        labels = {
            "kind": "branch-presence-owner-labels",
            "schema_version": 1,
            "snapshot_digest": result["snapshot_digest"],
            "contributions_digest": result["contributions_digest"],
            "groups_digest": result["groups_digest"],
            "accepted_by": None,
            "accepted_at": None,
            "label_source": "owner_review",
            "blinded": True,
            "labels": [
                {
                    "contribution_id": case["contribution_id"],
                    "family_id": case["family"],
                    "disposition": None,
                    "reviewed": False,
                }
                for case in result["cases"]
            ],
        }
        labels["labels_digest"] = digest(labels)
        write_json(labels_path, labels)
    lines = ["# Presence study evidence cards", "", f"Cases: {result['case_count']}. Reference labels remain unreviewed.",
             "Selection signals are not expected answers. Inspect pinned source, destination and dependencies before labeling.", ""]
    for case in result["cases"]:
        source = case["source"]
        lines += [f"## {case['contribution_id']}", "", f"Family: {case['family']}",
                  f"Source: {source['branch']} at {source['source_tip']}",
                  f"Unit: {source['path']} / {source['name'] or source['kind']}",
                  f"Selection signal: {case['selection_stratum']}",
                  f"Destination candidates: {len(case['destination_candidates'])}; neighbors: {len(case['neighbor_ids'])}",
                  f"Limitations: {', '.join(case['limitations']) or 'none recorded'}", "",
                  "Owner label: UNREVIEWED. Evidence and rationale: pending local inspection.", ""]
    write_private_text(destination / "evidence-cards.md", "\n".join(lines))
    return destination / "study.json"
