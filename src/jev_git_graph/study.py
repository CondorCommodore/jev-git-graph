"""Deterministic, model-blind evidence-card selection for a small pilot."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from pathlib import Path

from .errors import JgError
from .groups import _family, _validate
from .safety import digest, read_json, write_json, write_private_text


def build_study(contributions: dict, groups: dict, count: int = 32,
                max_per_family: int = 4, excluded_branches: list[str] | None = None,
                project_goals: str = "") -> dict:
    if type(count) is not int or not 24 <= count <= 40:
        raise JgError("pilot study must contain 24 to 40 contributions")
    if type(max_per_family) is not int or not 1 <= max_per_family <= 4:
        raise JgError("max per family must be between 1 and 4")
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
    buckets: dict[str, deque] = defaultdict(deque)
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
        buckets[stratum].append(unit)
    # Fixed round-robin across evidence strata, capped by task-family labels.
    # Signals select review cases; they never supply reference answers.
    family_counts, selected, seen_behaviors = Counter(), [], set()
    while len(selected) < count:
        progressed = False
        for stratum in sorted(buckets):
            while buckets[stratum]:
                unit = buckets[stratum].popleft()
                family = _family(unit["branch"]) or unit["branch"]
                behavior = (unit.get("source_blob"), unit.get("ast_fingerprint"), unit.get("name"), unit["path"])
                if family_counts[family] >= max_per_family or behavior in seen_behaviors:
                    continue
                group = by_unit[unit["id"]]
                selected.append({"contribution_id": unit["id"], "family": family, "selection_stratum": stratum,
                                 "source": {k: unit.get(k) for k in ("branch", "source_tip", "path", "source_blob", "mode", "kind", "name", "range")},
                                 "destination_candidates": [destinations[d] for d in unit.get("destination_ids", [])],
                                 "group_id": group["id"], "neighbor_ids": [u for u in group["unit_ids"] if u != unit["id"]],
                                 "boundary_edges": group.get("boundary_edges", []),
                                 "limitations": group.get("limitations", []), "reference_label_status": "UNREVIEWED"})
                family_counts[family] += 1
                seen_behaviors.add(behavior)
                progressed = True
                break
            if len(selected) == count:
                break
        if not progressed:
            break
    if len(selected) < count:
        raise JgError(f"only {len(selected)} eligible diverse cases; do not silently shrink the study")
    result = {"kind": "presence-study", "schema_version": 1,
              "repository_id": contributions["repository_id"], "snapshot_digest": contributions["snapshot_digest"],
              "contributions_digest": contributions["contributions_digest"], "groups_digest": groups["groups_digest"],
              "project_goals": project_goals, "case_count": len(selected), "family_counts": dict(family_counts),
              "excluded_development_branches": sorted(excluded), "cases": selected,
              "selection_uses_model_answers": False, "provider_dispatch_approved": False,
              "expansion_gate": "UNMEASURED", "split_policy": "Keep entire families together; label before viewing model answers"}
    result["study_digest"] = digest(result)
    return result


def write_study(contributions_path: str, groups_path: str, out: str | Path,
                count: int = 32, max_per_family: int = 4,
                excluded_branches: list[str] | None = None, project_goals: str = "") -> Path:
    result = build_study(read_json(contributions_path), read_json(groups_path), count,
                         max_per_family, excluded_branches, project_goals)
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
