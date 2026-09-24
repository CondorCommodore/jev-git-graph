"""Deterministic, model-blind evidence-card selection for a small pilot."""
from __future__ import annotations

import re
from collections import Counter, defaultdict, deque
from pathlib import Path

from .errors import JgError
from .code_evidence import _path as checked_code_path
from .groups import _family, _validate
from .safety import digest, read_json, write_json, write_private_text


def build_study(contributions: dict, groups: dict, count: int = 32,
                max_per_family: int = 4, excluded_branches: list[str] | None = None,
                project_goals: str = "", selection_policy: str = "candidate-availability-24-8-v2") -> dict:
    if type(count) is not int or not 24 <= count <= 40:
        raise JgError("pilot study must contain 24 to 40 contributions")
    if type(max_per_family) is not int or not 1 <= max_per_family <= 4:
        raise JgError("max per family must be between 1 and 4")
    if selection_policy not in {"candidate-availability-24-8-v2", "dependency-complete-majority-v1",
                                "behavior-focused-v1"}:
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
        elif any(unit.get("destination_candidate_provenance", {}).get(candidate_id) == "same_path_name"
                 and candidate_id in destinations
                 and destinations[candidate_id].get("ast_fingerprint") != unit.get("ast_fingerprint")
                 for candidate_id in matches):
            stratum = "changed_implementation_candidate"
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
        elif selection_policy == "behavior-focused-v1":
            candidate_rows = [destinations[d] for d in candidate_ids if d in destinations]
            dependency_complete = (unit.get("dependency_context_status") == "complete"
                                   and bool(candidate_rows)
                                   and all(row.get("dependency_context_status") == "complete" for row in candidate_rows))
            context_stratum = ("comparison_candidates_available" if comparison_available and dependency_complete
                               else "uncertainty_dependency_or_destination_context")
        else:
            context_stratum = ("comparison_candidates_available" if comparison_available
                               else "uncertainty_no_destination_candidate")
        buckets[(context_stratum, stratum)].append(unit)
    if selection_policy == "behavior-focused-v1":
        return _build_behavior_focused_study(
            contributions, groups, units, destinations, by_unit, buckets, count,
            max_per_family, excluded, project_goals,
        )
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


def _behavior_rank(unit: dict, destinations: dict) -> tuple[int, list[str]]:
    """Rank review candidates from pinned structural metadata, never source text."""
    name = unit.get("name") or ""
    path = unit.get("path") or ""
    line_range = unit.get("range") or {}
    range_start, range_end = line_range.get("start_line"), line_range.get("end_line")
    span = range_end - range_start + 1 if isinstance(range_start, int) and isinstance(range_end, int) else 0
    is_test_path = bool(re.search(r"(^|/)(tests?|__tests__)(/|$)|(^|/)test_[^/]+$", path))
    is_test_body = _is_behavior_test(unit)
    reasons, score = [], 0
    if unit.get("kind") != "python_definition":
        return -100, ["file-level or unsupported unit"]
    source_ast = unit.get("ast_fingerprint")
    destination_ids = unit.get("destination_ids", [])
    provenance = unit.get("destination_candidate_provenance", {})
    changed_same_path = any(provenance.get(destination_id) == "same_path_name"
                            and destination_id in destinations
                            and isinstance(source_ast, str)
                            and isinstance(destinations[destination_id].get("ast_fingerprint"), str)
                            and destinations[destination_id].get("ast_fingerprint") != source_ast
                            for destination_id in destination_ids)
    if changed_same_path and not is_test_path:
        score += 32
        reasons.append("changed production implementation candidate")
    if is_test_body:
        score += 45
        reasons.append("recognized test body from path and definition name")
    elif is_test_path:
        score -= 12
        reasons.append("test-path helper or fixture; eligible but demoted")
    else:
        score += 30
        reasons.append("production Python definition")
    if "." not in name:
        score += 12
        reasons.append("module-level definition")
    else:
        score -= 15
        reasons.append("nested or method definition; lower priority")
    if span < 5:
        score -= 25
        reasons.append("very short range is likely a stub or declaration")
    elif span <= 45:
        score += 16
        reasons.append("moderate complete-definition range")
    elif span <= 80:
        score += 4
        reasons.append("complete-definition range fits one bounded chunk")
    else:
        score -= min(35, 8 + (span - 80) // 4)
        reasons.append("large definition is demoted by range budget")
    destination_rows = [destinations[d] for d in unit.get("destination_ids", []) if d in destinations]
    if destination_rows and span > 0:
        destination_ranges = [row.get("range") for row in destination_rows]
        if all(isinstance(value, dict) for value in destination_ranges):
            estimated_specs, estimated_lines = _complete_range_budget(line_range, destination_ranges)
            if estimated_lines > 240 or estimated_specs > 8:
                score -= 30
                reasons.append("complete source/destination ranges exceed excerpt budget")
            else:
                score += 8
                reasons.append("complete source/destination ranges fit excerpt budget")
    if re.search(r"(?:fixture|stub|fake|dummy|mock|callback|sentinel|constant|error|exception)", name, re.I):
        score -= 24
        reasons.append("name indicates fixture, sentinel, constant, or error helper")
    if name[:1].isupper():
        score -= 12
        reasons.append("class-like definition is demoted pending method-level evidence")
    refs = unit.get("static_references")
    if isinstance(refs, list) and len(refs) >= 2:
        score += 5
        reasons.append("multiple static references indicate a connected body")
    return score, reasons


def _is_behavior_test(unit: dict) -> bool:
    path, name = unit.get("path") or "", unit.get("name") or ""
    test_path = bool(re.search(r"(^|/)(tests?|__tests__)(/|$)|(^|/)test_[^/]+$", path))
    return test_path and bool(re.search(r"(?:^|\.)test_[A-Za-z0-9_]+$", name))


def _complete_range_budget(source_range: dict, destination_ranges: list[dict]) -> tuple[int, int]:
    """Return spec count and combined lines using the evidence manifest chunk rules."""
    def lengths(value: dict) -> list[int]:
        start, end = value.get("start_line"), value.get("end_line")
        if (not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool)
                or start < 1 or end < start):
            return []
        return [min(80, end - line + 1) for line in range(start, end + 1, 80)]

    source_chunks = lengths(source_range)
    if not source_chunks:
        return 0, 0
    if not destination_ranges:
        return len(source_chunks), sum(source_chunks)
    specs = total_lines = 0
    for destination_range in destination_ranges:
        destination_chunks = lengths(destination_range)
        if not destination_chunks:
            return 0, 0
        for index in range(max(len(source_chunks), len(destination_chunks))):
            source_lines = source_chunks[index] if index < len(source_chunks) else 1
            destination_lines = destination_chunks[index] if index < len(destination_chunks) else 1
            specs += 1
            total_lines += source_lines + destination_lines
    return specs, total_lines


def _behavior_identity(unit: dict, destinations: dict) -> tuple:
    """Collapse identical definition ASTs only when destination context also matches."""
    destination_context = tuple(sorted(
        (destinations[d].get("path") or "", destinations[d].get("ast_fingerprint") or "",
         destinations[d].get("blob") or "", destinations[d].get("mode") or "")
        for d in unit.get("destination_ids", []) if d in destinations
    ))
    return (unit.get("ast_fingerprint") or unit.get("source_blob"), unit.get("name"),
            unit.get("path"), destination_context)


def _build_behavior_focused_study(contributions: dict, groups: dict, units: dict,
                                  destinations: dict, by_unit: dict, buckets: dict,
                                  count: int, max_per_family: int, excluded: set,
                                  project_goals: str) -> dict:
    """Select moderate behavior bodies with family diversity and explicit uncertainty."""
    supported = "comparison_candidates_available"
    uncertain = "uncertainty_dependency_or_destination_context"
    ranked = []
    for (context, stratum), queue in buckets.items():
        for unit in queue:
            score, reasons = _behavior_rank(unit, destinations)
            if score > -100:
                ranked.append((unit, context, stratum, score, reasons))
    # A deterministic round-robin keeps narrow branch families from dominating.
    def ordered(context: str) -> list[tuple]:
        candidates = sorted((row for row in ranked if row[1] == context),
                            key=lambda row: (-row[3], row[0]["id"]))
        families: dict[str, deque] = defaultdict(deque)
        for row in candidates:
            family = _family(row[0]["branch"]) or row[0]["branch"]
            families[family].append(row)
        result = []
        family_keys = deque(sorted(families, key=lambda family: (
            -_behavior_rank(families[family][0][0], destinations)[0],
            families[family][0][0]["id"], family)))
        while family_keys:
            family = family_keys.popleft()
            result.append(families[family].popleft())
            if families[family]:
                family_keys.append(family)
        return result

    ordered_supported, ordered_uncertain = ordered(supported), ordered(uncertain)
    # Reserve minimum representation from each context arm when available, then
    # fill globally by rank so dependency uncertainty cannot consume the study.
    context_target = min(max(4, count // 8), count // 2)
    uncertain_target = min(context_target, len(ordered_uncertain))
    supported_target = min(context_target, len(ordered_supported))
    selected, family_counts, seen = [], Counter(), set()
    def take(rows: list[tuple], limit: int) -> int:
        taken = 0
        for unit, context, stratum, score, reasons in rows:
            if taken >= limit or len(selected) >= count:
                break
            family = _family(unit["branch"]) or unit["branch"]
            behavior = _behavior_identity(unit, destinations)
            if family_counts[family] >= max_per_family or behavior in seen:
                continue
            group = by_unit[unit["id"]]
            case = {"contribution_id": unit["id"], "family": family,
                    "selection_stratum": stratum, "context_stratum": context,
                    "source": {k: unit.get(k) for k in ("branch", "source_tip", "path", "source_blob", "mode", "kind", "name", "range")},
                    "destination_candidates": [destinations[d] for d in unit.get("destination_ids", [])],
                    "group_id": group["id"], "neighbor_ids": [u for u in group["unit_ids"] if u != unit["id"]],
                    "group_context_complete": group.get("context_complete") is True,
                    "dependency_context_status": unit.get("dependency_context_status", "unknown"),
                    "destination_dependency_context_statuses": [destinations[d].get("dependency_context_status", "unknown")
                        for d in unit.get("destination_ids", []) if d in destinations],
                    "boundary_edges": group.get("boundary_edges", []), "limitations": group.get("limitations", []),
                    "reference_label_status": "UNREVIEWED",
                    "source_ast_fingerprint": unit.get("ast_fingerprint"),
                    "selection_rank": score, "selection_reasons": reasons}
            selected.append(case)
            family_counts[family] += 1
            seen.add(behavior)
            taken += 1
        return taken
    test_rows = sorted((row for row in ranked if _is_behavior_test(row[0])),
                       key=lambda row: (-row[3], row[0]["id"]))
    test_identities: dict[str, set[tuple]] = defaultdict(set)
    for unit, _context, _stratum, _score, _reasons in test_rows:
        test_identities[_family(unit["branch"]) or unit["branch"]].add(_behavior_identity(unit, destinations))
    feasible_test_capacity = sum(min(max_per_family, len(identities))
                                 for identities in test_identities.values())
    test_target = min(4, count // 4, feasible_test_capacity)
    # Protect reserved test examples from earlier context picks consuming their families.
    take(test_rows, test_target)
    supported_already = sum(case["context_stratum"] == supported for case in selected)
    uncertain_already = sum(case["context_stratum"] == uncertain for case in selected)
    take(ordered_supported, max(0, supported_target - supported_already))
    take(ordered_uncertain, max(0, uncertain_target - uncertain_already))
    combined = sorted(ordered_supported + ordered_uncertain,
                      key=lambda row: (-row[3], row[0]["id"]))
    take(combined, count - len(selected))
    if len(selected) < count:
        raise JgError(f"only {len(selected)} eligible diverse behavior cases; do not silently shrink the study")
    selected_ids = {case["contribution_id"] for case in selected}
    reason_counts = Counter(reason for case in selected for reason in case["selection_reasons"])
    result = {"kind": "presence-study", "schema_version": 3,
              "repository_id": contributions["repository_id"], "snapshot_digest": contributions["snapshot_digest"],
              "contributions_digest": contributions["contributions_digest"], "groups_digest": groups["groups_digest"],
              "selection_policy": "behavior-focused-v1", "project_goals": project_goals,
              "case_count": len(selected), "family_counts": dict(family_counts),
              "context_stratum_counts": dict(Counter(case["context_stratum"] for case in selected)),
              "supported_pool_count": len(ordered_supported),
              "supported_case_count": sum(case["context_stratum"] == supported for case in selected),
              "supported_majority_available": sum(case["context_stratum"] == supported for case in selected) > count // 2,
              "context_scope": "Dependency and destination context are reported for coverage only; structural ranking and moderate ranges do not establish project usefulness or behavior presence.",
              "dependency_context_status_counts": dict(Counter(case["dependency_context_status"] for case in selected)),
              "excluded_development_branches": sorted(excluded), "cases": selected,
              "selection_uses_model_answers": False, "provider_dispatch_approved": False,
              "selection_policy_details": {"ranking": "deterministic metadata-only heuristic; no source text or model answers",
                  "eligible_behavior_units": len(ranked), "selected_units": len(selected_ids),
                  "unselected_contribution_units": len(units) - len(selected_ids),
                  "ledger_contribution_units": len(units), "ranking_reason_counts": dict(reason_counts),
                  "uncertainty_target": uncertain_target, "supported_target": supported_target,
                  "actual_test_body_target": test_target,
                  "actual_test_body_count": sum(_is_behavior_test(case["source"]) for case in selected),
                  "supported_case_count": sum(case["context_stratum"] == supported for case in selected),
                  "uncertainty_case_count": sum(case["context_stratum"] == uncertain for case in selected),
                  "identity_key": "source AST fingerprint, name, path, and destination path/AST/blob/mode signatures",
                  "selection_limits": ["metadata ranking does not establish usefulness", "range length is a proxy, not a semantic test", "family caps and context diversity can override rank"]},
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
    evidence_ranges_digest = digest(normalized_ranges)
    result["evidence_ranges_digest"] = evidence_ranges_digest
    result["study_digest"] = digest(result)
    range_manifest = {"kind": "branch-presence-range-manifest", "schema_version": 1,
                      "snapshot_digest": result["snapshot_digest"],
                      "contributions_digest": result["contributions_digest"],
                      "groups_digest": result["groups_digest"], "selection_digest": selection_digest,
                      "evidence_ranges_digest": evidence_ranges_digest,
                      "ranges": normalized_ranges}
    return result, range_manifest


def validate_selected_range_manifest(study: dict, range_manifest: dict) -> None:
    """Require the exact normalized line ranges selected for this study."""
    if study.get("selection_policy") != "explicit-validated-case-list-v1":
        return
    ranges = range_manifest.get("ranges")
    cases = study.get("cases")
    if (range_manifest.get("selection_digest") != study.get("selection_manifest_digest")
            or not isinstance(ranges, list)
            or digest(ranges) != study.get("evidence_ranges_digest")
            or range_manifest.get("evidence_ranges_digest") != study.get("evidence_ranges_digest")
            or not isinstance(cases, list)):
        raise JgError("explicit study and evidence ranges do not share the pinned range manifest")
    expected_arms = {case.get("contribution_id"): case.get("selection_arm")
                     for case in cases if isinstance(case, dict)}
    range_arms = {item.get("contribution_id"): item.get("arm")
                  for item in ranges if isinstance(item, dict)}
    if len(expected_arms) != len(cases) or len(range_arms) != len(ranges) or range_arms != expected_arms:
        raise JgError("explicit study and evidence range arms do not match")


def build_study_range_manifest(study: dict, main_tip: str) -> dict:
    """Select complete bounded definition ranges; omit cases that exceed a bound."""
    def chunks(value: dict) -> list[dict[str, int]]:
        start, end = value["start_line"], value["end_line"]
        return [{"start_line": line, "end_line": min(line + 79, end)}
                for line in range(start, end + 1, 80)]

    ranges, omissions = [], []
    for case in study["cases"]:
        source = case["source"]
        source_range = source.get("range")
        if not isinstance(source_range, dict):
            omissions.append({"contribution_id": case["contribution_id"], "reason": "source_definition_range_unavailable"})
            continue
        try:
            checked_code_path(source["path"])
            for candidate in case["destination_candidates"]:
                checked_code_path(candidate["path"])
        except JgError:
            omissions.append({"contribution_id": case["contribution_id"],
                              "reason": "code_evidence_path_denied"})
            continue
        source_chunks = chunks(source_range)
        specs = []
        candidates = case["destination_candidates"]
        if not candidates:
            for index, source_chunk in enumerate(source_chunks):
                specs.append({"evidence_id": f"{case['contribution_id']}:source:{index}",
                              "source_path": source["path"], "source_range": source_chunk})
        for candidate in candidates:
            destination_range = candidate.get("range")
            if not isinstance(destination_range, dict):
                specs = []
                break
            destination_chunks = chunks(destination_range)
            for index in range(max(len(source_chunks), len(destination_chunks))):
                source_chunk = source_chunks[min(index, len(source_chunks) - 1)]
                destination_chunk = destination_chunks[min(index, len(destination_chunks) - 1)]
                if index >= len(source_chunks):
                    source_chunk = {"start_line": source_range["end_line"],
                                    "end_line": source_range["end_line"]}
                if index >= len(destination_chunks):
                    destination_chunk = {"start_line": destination_range["end_line"],
                                         "end_line": destination_range["end_line"]}
                specs.append({"evidence_id": f"{case['contribution_id']}:{candidate['id']}:{index}",
                              "source_path": source["path"], "source_range": source_chunk,
                              "destination_path": candidate["path"], "destination_range": destination_chunk})
        total_lines = sum(spec["source_range"]["end_line"] - spec["source_range"]["start_line"] + 1
                          + (spec["destination_range"]["end_line"] - spec["destination_range"]["start_line"] + 1
                             if "destination_range" in spec else 0) for spec in specs)
        if not specs or len(specs) > 8 or total_lines > 240:
            omissions.append({"contribution_id": case["contribution_id"],
                              "reason": "complete_definition_exceeds_evidence_bounds"})
            continue
        ranges.append({"contribution_id": case["contribution_id"],
                       **({"arm": "source_only_unknown"} if not candidates else {}),
                       "source_tip": source["source_tip"], "destination_tip": main_tip,
                       "ranges": specs})
    return {"kind": "branch-presence-range-manifest", "schema_version": 1,
            "snapshot_digest": study["snapshot_digest"],
            "contributions_digest": study["contributions_digest"],
            "groups_digest": study["groups_digest"],
            "ranges": ranges, "omissions": omissions}


def write_study(contributions_path: str, groups_path: str, out: str | Path,
                count: int = 32, max_per_family: int = 4,
                excluded_branches: list[str] | None = None, project_goals: str = "",
                selection_policy: str = "candidate-availability-24-8-v2") -> Path:
    contributions = read_json(contributions_path)
    result = build_study(contributions, read_json(groups_path), count,
                         max_per_family, excluded_branches, project_goals, selection_policy)
    destination = Path(out)
    if destination.exists():
        raise JgError("study output already exists")
    destination.mkdir(mode=0o700, parents=True)
    write_json(destination / "study.json", result)
    write_json(destination / "evidence-ranges.json",
               build_study_range_manifest(result, contributions["main"]["tip"]))
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
