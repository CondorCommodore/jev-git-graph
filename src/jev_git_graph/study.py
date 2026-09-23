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


def build_selected_study(contributions: dict, groups: dict, selection: dict) -> tuple[dict, dict]:
    """Build a normal presence study from a digest-bound, explicit case list.

    This selects review cases only. It never filters or rewrites the pinned
    contribution ledger. Source-only cases are admitted only as explicit
    unknowns with no destination candidate and no destination range.
    """
    _, units, destinations = _validate(contributions)
    if (groups.get("kind") != "contribution-groups"
            or groups.get("contributions_digest") != contributions.get("contributions_digest")
            or groups.get("snapshot_digest") != contributions.get("snapshot_digest")
            or groups.get("groups_digest") != digest({k: v for k, v in groups.items() if k != "groups_digest"})):
        raise JgError("groups do not match pinned contributions")
    if not isinstance(selection, dict) or selection.get("kind") != "presence-study-selection" or selection.get("schema_version") != 1:
        raise JgError("selected study manifest has an unsupported schema")
    selection_digest = selection.get("selection_digest")
    if selection_digest != digest({k: v for k, v in selection.items() if k != "selection_digest"}):
        raise JgError("selected study manifest digest is invalid")
    for key in ("repository_id", "snapshot_digest", "contributions_digest", "groups_digest"):
        if selection.get(key) != contributions.get(key) and key != "groups_digest":
            raise JgError(f"selected study {key} does not match the pinned contribution artifact")
    if selection.get("groups_digest") != groups.get("groups_digest"):
        raise JgError("selected study groups_digest does not match the pinned groups")
    cases_in = selection.get("cases")
    if not isinstance(cases_in, list) or not 24 <= len(cases_in) <= 40:
        raise JgError("explicit study selection must contain 24 to 40 cases")
    ids = [case.get("contribution_id") if isinstance(case, dict) else None for case in cases_in]
    if any(not isinstance(cid, str) or not cid for cid in ids) or len(ids) != len(set(ids)):
        raise JgError("explicit study selection IDs must be unique non-empty strings")
    if set(ids) - set(units):
        raise JgError("explicit study selection contains IDs absent from the full contribution ledger")

    group_for_unit: dict[str, dict] = {}
    for group in groups.get("groups", []):
        for unit_id in group.get("unit_ids", []):
            if unit_id in group_for_unit:
                raise JgError("group membership is duplicate")
            group_for_unit[unit_id] = group

    def checked_range(value: Any, label: str, bounds: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            raise JgError(f"{label} must be an explicit line range")
        start, end = value.get("start_line"), value.get("end_line")
        if (not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool)
                or start < 1 or end < start or end - start >= 80):
            raise JgError(f"{label} must contain 1 to 80 inclusive lines")
        if (not isinstance(bounds, dict) or not isinstance(bounds.get("start_line"), int)
                or not isinstance(bounds.get("end_line"), int)
                or start < bounds["start_line"] or end > bounds["end_line"]):
            raise JgError(f"{label} is outside the pinned contribution definition")
        return {"start_line": start, "end_line": end}

    study_cases, normalized_ranges = [], []
    family_counts, context_counts = Counter(), Counter()
    for case in cases_in:
        cid = case["contribution_id"]
        unit = units[cid]
        branch = unit["source"]["branch"]
        family = _family(branch) or branch
        group = group_for_unit.get(cid)
        if group is None:
            raise JgError(f"selected contribution {cid} has no pinned group")
        arm = case.get("arm")
        if arm not in {"two_sided_control", "source_only_unknown"}:
            raise JgError(f"selected contribution {cid} has an unsupported evidence arm")
        source_status = unit.get("dependency_context_status", "unknown")
        if case.get("dependency_context_status") != source_status:
            raise JgError(f"selected contribution {cid} dependency status changed")
        candidate_ids = list(unit.get("destination_ids", []))
        range_specs = case.get("ranges")
        if not isinstance(range_specs, list) or not 1 <= len(range_specs) <= 8:
            raise JgError(f"selected contribution {cid} must have 1 to 8 bounded ranges")
        checked_specs = []
        covered_destinations = set()
        for index, spec in enumerate(range_specs):
            if not isinstance(spec, dict):
                raise JgError(f"selected contribution {cid} range {index} must be an object")
            source_path = spec.get("source_path")
            if source_path != unit.get("path"):
                raise JgError(f"selected contribution {cid} source path does not match its pinned unit")
            source_range = checked_range(spec.get("source_range"), f"{cid} source", unit.get("range"))
            evidence_id = spec.get("evidence_id")
            if not isinstance(evidence_id, str) or not evidence_id or any(item.get("evidence_id") == evidence_id for item in checked_specs):
                raise JgError(f"selected contribution {cid} evidence IDs must be unique non-empty strings")
            normalized = {"source_path": source_path, "source_range": source_range, "evidence_id": evidence_id}
            if arm == "source_only_unknown":
                if candidate_ids or source_status != "unknown":
                    raise JgError(f"source-only case {cid} must have unknown dependency status and no destination candidates")
                if spec.get("destination_id") is not None or spec.get("destination_path") is not None or spec.get("destination_range") is not None:
                    raise JgError(f"source-only case {cid} cannot claim destination evidence")
            else:
                destination_id = spec.get("destination_id")
                if destination_id not in candidate_ids or destination_id not in destinations:
                    raise JgError(f"two-sided case {cid} must name an actual destination candidate")
                destination = destinations[destination_id]
                if spec.get("destination_path") != destination.get("path"):
                    raise JgError(f"two-sided case {cid} destination path does not match its candidate")
                destination_range = checked_range(spec.get("destination_range"), f"{cid} destination", destination.get("range"))
                normalized.update({"destination_id": destination_id, "destination_path": destination["path"],
                                   "destination_range": destination_range})
                covered_destinations.add(destination_id)
            checked_specs.append(normalized)
        if arm == "two_sided_control" and (not candidate_ids or covered_destinations != set(candidate_ids)):
            raise JgError(f"two-sided case {cid} must provide ranges for every destination candidate")
        destination_statuses = [destinations[d].get("dependency_context_status", "unknown") for d in candidate_ids]
        if case.get("destination_dependency_context_statuses") != destination_statuses:
            raise JgError(f"selected contribution {cid} destination dependency statuses changed")
        source_only = arm == "source_only_unknown"
        context_stratum = ("uncertainty_dependency_or_destination_context" if source_only or
                           source_status != "complete" or any(status != "complete" for status in destination_statuses)
                           else "dependency_context_supported")
        context_counts[context_stratum] += 1
        family_counts[family] += 1
        source = {key: unit.get(key) for key in ("branch", "source_tip", "path", "source_blob", "mode", "kind", "name", "range") if key in unit}
        source["branch"] = branch
        study_cases.append({
            "contribution_id": cid, "family": family,
            "selection_stratum": "no_structural_destination" if source_only else "explicit_two_sided_control",
            "context_stratum": context_stratum, "source": source,
            "destination_candidates": [destinations[d] for d in candidate_ids],
            "group_id": group["id"], "neighbor_ids": [value for value in group.get("unit_ids", []) if value != cid],
            "group_context_complete": group.get("context_complete") is True,
            "dependency_context_status": source_status,
            "destination_dependency_context_statuses": destination_statuses,
            "boundary_edges": group.get("boundary_edges", []), "limitations": group.get("limitations", []),
            "reference_label_status": "UNREVIEWED", "selection_arm": arm,
        })
        normalized_ranges.append({"contribution_id": cid, "arm": arm,
                                  "source_tip": unit.get("source_tip"), "destination_tip": unit.get("main_tip"),
                                  "ranges": checked_specs})
    supported_count = context_counts["dependency_context_supported"]
    result = {
        "kind": "presence-study", "schema_version": 3,
        "repository_id": contributions["repository_id"], "snapshot_digest": contributions["snapshot_digest"],
        "contributions_digest": contributions["contributions_digest"], "groups_digest": groups["groups_digest"],
        "selection_policy": "explicit-validated-case-list-v1", "selection_manifest_digest": selection_digest,
        "project_goals": "", "case_count": len(study_cases), "family_counts": dict(family_counts),
        "context_stratum_counts": dict(context_counts), "supported_pool_count": supported_count,
        "supported_case_count": supported_count, "supported_majority_available": supported_count > len(study_cases) // 2,
        "context_scope": "Explicitly selected source and destination ranges are pinned to the unchanged full contribution ledger; source-only unknown cases contain no destination evidence.",
        "dependency_context_status_counts": dict(Counter(case["dependency_context_status"] for case in study_cases)),
        "excluded_development_branches": [], "cases": study_cases,
        "selection_uses_model_answers": False, "provider_dispatch_approved": False,
        "expansion_gate": "UNMEASURED", "split_policy": "Keep entire families together; label before viewing model answers",
    }
    result["study_digest"] = digest(result)
    range_manifest = {"kind": "branch-presence-range-manifest", "schema_version": 1,
                      "snapshot_digest": result["snapshot_digest"],
                      "contributions_digest": result["contributions_digest"],
                      "groups_digest": result["groups_digest"], "selection_digest": selection_digest,
                      "ranges": normalized_ranges}
    return result, range_manifest


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


def write_selected_study(contributions_path: str, groups_path: str, selection_path: str,
                         out: str | Path) -> Path:
    result, range_manifest = build_selected_study(
        read_json(contributions_path), read_json(groups_path), read_json(selection_path))
    return write_selected_study_artifacts(result, range_manifest, out)


def write_selected_study_artifacts(result: dict, range_manifest: dict, out: str | Path) -> Path:
    destination = Path(out)
    if destination.exists():
        raise JgError("study output already exists")
    destination.mkdir(mode=0o700, parents=True)
    write_json(destination / "study.json", result)
    write_json(destination / "evidence-ranges.json", range_manifest)
    labels = {
        "kind": "branch-presence-owner-labels", "schema_version": 1,
        "snapshot_digest": result["snapshot_digest"],
        "contributions_digest": result["contributions_digest"],
        "groups_digest": result["groups_digest"], "accepted_by": None, "accepted_at": None,
        "label_source": "owner_review", "blinded": True,
        "labels": [{"contribution_id": case["contribution_id"], "family_id": case["family"],
                    "disposition": None, "reviewed": False} for case in result["cases"]],
    }
    labels["labels_digest"] = digest(labels)
    write_json(destination / "owner-labels.json", labels)
    write_private_text(destination / "evidence-cards.md",
                       "# Presence study evidence cards\n\n"
                       f"Cases: {result['case_count']}. Reference labels remain unreviewed.\n"
                       "Selection arms record evidence availability, not expected presence or usefulness.\n")
    return destination / "study.json"
