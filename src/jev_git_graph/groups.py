"""Deterministic, bounded context groups for contribution evidence.

Groups are an advisory index over supplied observations.  They preserve
provenance and omissions; membership never upgrades a candidate to proof.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .errors import JgError
from .safety import digest, read_json, write_json


_GROUPS_SCHEMA_VERSION = 3
_CONTEXT_GAP_LIMITATIONS = {
    "candidate_metadata_missing",
    "candidate_discovery_truncated",
    "python_parse_unsupported",
    "partition_has_required_cross_group_edges",
    "excluded_neighbor_edges_present",
    "edge_output_budget_exhausted",
}
_CANDIDATE_EDGE_KINDS = {
    "ast_fingerprint", "blob", "branch_family", "path", "same_branch", "structural_match", "symbol",
}


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise JgError(f"{label} must be a non-empty string")
    return value


def _digest_text(value: Any, label: str) -> str:
    result = _require_text(value, label)
    if len(result) != 64 or any(c not in "0123456789abcdef" for c in result):
        raise JgError(f"{label} must be a SHA-256 digest")
    return result


def _family(branch: str) -> str | None:
    """Return a narrow hierarchical family, avoiding generic one-word prefixes."""
    parts = branch.split("/")
    if len(parts) < 3:
        return None
    return "/".join(parts[:-1])


def _unit_branch(unit: dict[str, Any]) -> str:
    source = unit.get("source")
    if not isinstance(source, dict):
        raise JgError(f"unit {unit.get('id')} source must be an object")
    return _require_text(source.get("branch"), f"unit {unit.get('id')} source branch")


def _validate(contributions: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(contributions, dict):
        raise JgError("contributions artifact must be an object")
    if contributions.get("kind") != "contributions" or contributions.get("schema_version") not in {1, 2}:
        raise JgError("contributions artifact has an unsupported kind or schema")
    repository_id = _require_text(contributions.get("repository_id"), "repository_id")
    _digest_text(contributions.get("snapshot_digest"), "snapshot_digest")
    recorded = _digest_text(contributions.get("contributions_digest"), "contributions_digest")
    payload = {key: value for key, value in contributions.items() if key != "contributions_digest"}
    if digest(payload) != recorded:
        raise JgError("contributions digest does not match artifact content")
    main = contributions.get("main")
    if not isinstance(main, (str, dict)):
        raise JgError("contributions main must identify the destination branch")
    if isinstance(main, str):
        _require_text(main, "contributions main")
    else:
        _require_text(main.get("branch") or main.get("name") or main.get("ref"), "contributions main branch")

    branches_raw = contributions.get("branches")
    units_raw = contributions.get("units")
    destinations_raw = contributions.get("destination_units")
    edges_raw = contributions.get("edges")
    paths_raw = contributions.get("paths")
    limitations_raw = contributions.get("limitations")
    if not all(isinstance(value, list) for value in (branches_raw, units_raw, destinations_raw, edges_raw, paths_raw, limitations_raw)):
        raise JgError("contributions branches, units, destination_units, paths, edges, and limitations must be arrays")
    if any(not isinstance(item, dict) for item in paths_raw) or any(not isinstance(item, str) for item in limitations_raw):
        raise JgError("contributions paths must contain objects and limitations must contain strings")

    branches: dict[str, dict[str, Any]] = {}
    for index, branch in enumerate(branches_raw):
        if not isinstance(branch, dict):
            raise JgError(f"branch record {index} must be an object")
        name = _require_text(branch.get("name"), f"branch record {index} name")
        _require_text(branch.get("tip"), f"branch {name} tip")
        if name in branches:
            raise JgError(f"duplicate branch name: {name}")
        if not isinstance(branch.get("eligible"), bool):
            raise JgError(f"branch {name} eligible must be boolean")
        branch_unit_ids = branch.get("unit_ids")
        if not isinstance(branch_unit_ids, list) or any(not isinstance(unit_id, str) or not unit_id for unit_id in branch_unit_ids):
            raise JgError(f"branch {name} unit_ids must be an array of non-empty strings")
        if len(branch_unit_ids) != len(set(branch_unit_ids)):
            raise JgError(f"branch {name} has duplicate unit ids")
        exclusions = branch.get("exclusion_reasons", [])
        if not isinstance(exclusions, list) or any(not isinstance(item, str) or not item for item in exclusions):
            raise JgError(f"branch {name} exclusion_reasons must be an array of strings")
        branches[name] = branch

    units: dict[str, dict[str, Any]] = {}
    for index, unit in enumerate(units_raw):
        if not isinstance(unit, dict):
            raise JgError(f"unit record {index} must be an object")
        unit_id = _require_text(unit.get("id"), f"unit record {index} id")
        if unit_id in units:
            raise JgError(f"duplicate unit id: {unit_id}")
        branch_name = _unit_branch(unit)
        if branch_name not in branches:
            raise JgError(f"unit {unit_id} refers to unknown branch: {branch_name}")
        for field in ("destination_ids", "limitations"):
            if field in unit and (not isinstance(unit[field], list) or any(not isinstance(v, str) for v in unit[field])):
                raise JgError(f"unit {unit_id} {field} must be an array of strings")
        units[unit_id] = unit

    destinations: dict[str, dict[str, Any]] = {}
    for index, unit in enumerate(destinations_raw):
        if not isinstance(unit, dict):
            raise JgError(f"destination unit record {index} must be an object")
        unit_id = _require_text(unit.get("id"), f"destination unit record {index} id")
        if unit_id in destinations or unit_id in units:
            raise JgError(f"duplicate destination unit id: {unit_id}")
        destinations[unit_id] = unit

    for unit_id, unit in units.items():
        for destination_id in unit.get("destination_ids", []):
            if destination_id not in destinations:
                raise JgError(f"unit {unit_id} refers to unknown destination id: {destination_id}")

    for branch_name, branch_record in branches.items():
        recorded_units = set(branch_record["unit_ids"])
        actual_units = {unit_id for unit_id, unit in units.items() if _unit_branch(unit) == branch_name}
        if recorded_units != actual_units:
            raise JgError(f"branch {branch_name} unit_ids do not match source units")

    edge_ids: set[str] = set()
    for index, edge in enumerate(edges_raw):
        if not isinstance(edge, dict):
            raise JgError(f"edge record {index} must be an object")
        source_id = _require_text(edge.get("source_id"), f"edge record {index} source_id")
        destination_id = _require_text(edge.get("destination_id"), f"edge record {index} destination_id")
        if source_id not in units:
            raise JgError(f"edge record {index} refers to unknown source unit: {source_id}")
        if destination_id not in destinations and destination_id not in units:
            raise JgError(f"edge record {index} refers to unknown destination unit: {destination_id}")
        _require_text(edge.get("kind", edge.get("type")), f"edge record {index} kind")
        if "id" in edge:
            edge_id = _require_text(edge.get("id"), f"edge record {index} id")
            if edge_id in edge_ids:
                raise JgError(f"duplicate contribution edge id: {edge_id}")
            edge_ids.add(edge_id)
    return branches, units, destinations


def _unit_signals(unit: dict[str, Any]) -> list[tuple[str, str]]:
    source = unit["source"]
    signals: set[tuple[str, str]] = set()
    for field, prefix in (("blob", "blob"), ("source_blob", "blob"), ("ast_fingerprint", "ast_fingerprint"), ("path", "path")):
        value = source.get(field)
        if isinstance(value, str) and value:
            signals.add((prefix, value))
    # Symbol names are useful candidate signals but are intentionally weak.
    for field in ("name", "symbol", "qualified_name"):
        value = source.get(field)
        if isinstance(value, str) and value:
            signals.add(("symbol", value))
    return sorted(signals)


def build_groups(contributions: dict[str, Any], max_units: int = 24, max_edges: int = 1000) -> dict[str, Any]:
    """Build stable primary groups with explicit bounded discovery coverage."""
    if not isinstance(max_units, int) or isinstance(max_units, bool) or max_units < 1:
        raise JgError("max_units must be a positive integer")
    if not isinstance(max_edges, int) or isinstance(max_edges, bool) or max_edges < 1:
        raise JgError("max_edges must be a positive integer")
    branches, units, destinations = _validate(contributions)
    eligible_branches = {name for name, item in branches.items() if item["eligible"]}
    excluded_branches = {name: list(item.get("exclusion_reasons", [])) for name, item in branches.items() if not item["eligible"]}
    for name, reasons in excluded_branches.items():
        if not reasons:
            excluded_branches[name] = ["excluded_reason_unspecified"]

    unit_branches = {unit_id: _unit_branch(unit) for unit_id, unit in units.items()}
    eligible_ids = sorted(unit_id for unit_id, name in unit_branches.items() if name in eligible_branches)
    ineligible_unit_ids = sorted(unit_id for unit_id, name in unit_branches.items() if name not in eligible_branches)
    metadata_unknown_ids = sorted(
        unit_id for unit_id in eligible_ids
        if not any(
            isinstance(units[unit_id]["source"].get(field), str) and units[unit_id]["source"][field]
            for field in ("path", "blob", "ast_fingerprint", "name")
        )
    )

    # Discover relationships through indexed keys and a spanning chain per
    # signal. This is linear in the number of indexed observations, instead
    # of generating every pair in a common-symbol bucket.
    candidate_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    omitted: dict[str, int] = defaultdict(int)
    unexpanded_pairwise: dict[str, int] = defaultdict(int)
    indexes: dict[tuple[str, str], list[str]] = defaultdict(list)
    branch_indexes: dict[str, list[str]] = defaultdict(list)
    for unit_id in eligible_ids:
        branch_indexes[unit_branches[unit_id]].append(unit_id)
        family = _family(unit_branches[unit_id])
        if family:
            indexes[("branch_family", family)].append(unit_id)
        for signal in _unit_signals(units[unit_id]):
            indexes[signal].append(unit_id)

    def add(first: str, second: str, kind: str, signal: str | None = None) -> None:
        if first == second:
            return
        a, b = sorted((first, second))
        candidate_edges.setdefault((a, b, kind), {
            "id": "ge-" + digest({"a": a, "b": b, "kind": kind, "signal": signal})[:24],
            "source_id": a, "destination_id": b, "kind": kind,
            "provenance": "contribution_metadata", "signal": signal,
        })

    for branch, ids in sorted(branch_indexes.items()):
        for left, right in zip(ids, ids[1:]):
            add(left, right, "same_branch", branch)
    for (signal_type, signal), ids in sorted(indexes.items()):
        ids = sorted(set(ids))
        # A chain preserves connectedness without materializing quadratic
        # pair sets. The shared key is retained as edge provenance. Other
        # pairwise edges in the bucket are intentionally unexpanded and are
        # reported separately from truncated or dropped candidates.
        possible_pairs = len(ids) * (len(ids) - 1) // 2
        unexpanded_count = max(0, possible_pairs - max(0, len(ids) - 1))
        if unexpanded_count:
            unexpanded_pairwise[signal_type] += unexpanded_count
        for left, right in zip(ids, ids[1:]):
            add(left, right, signal_type, signal)

    known_source_edges = [
        edge for edge in contributions["edges"]
        if edge["source_id"] in eligible_ids
        or (edge["destination_id"] in units and edge["destination_id"] in eligible_ids)
    ]
    # Input edge IDs may be absent; synthesize a stable identifier while
    # preserving every supplied field verbatim.
    normalized_input_edges = []
    for edge in known_source_edges:
        normalized = dict(edge)
        normalized.setdefault("id", "ge-" + digest(edge)[:24])
        normalized.setdefault("kind", normalized.get("type", "unknown"))
        normalized.setdefault("provenance", "contribution_edge")
        normalized_input_edges.append(normalized)

    edge_priority = {
        "ancestry": 0,
        "dependency": 1,
        "dependency_candidate": 1,
        "structural_match": 2,
        "ast_fingerprint": 3,
        "blob": 4,
        "same_branch": 5,
        "branch_family": 6,
        "path": 7,
        "symbol": 8,
    }
    all_edges = list(candidate_edges.values()) + normalized_input_edges
    all_edges.sort(key=lambda edge: (
        edge_priority.get(edge.get("kind", "unknown"), 20),
        edge.get("kind", "unknown"), edge["source_id"], edge["destination_id"], edge["id"],
    ))

    # Form groups by accepting the strongest source-to-source relationships
    # first, while enforcing the request bound at every merge. Unbounded
    # connected components followed by unit-ID slicing split the strongest
    # dependency edges arbitrarily when weak common-name/path signals make a
    # giant component.
    parent = {unit_id: unit_id for unit_id in eligible_ids}
    component_size = {unit_id: 1 for unit_id in eligible_ids}
    accepted_merges: dict[str, int] = defaultdict(int)
    rejected_merges: dict[str, int] = defaultdict(int)

    def find(unit_id: str) -> str:
        root = unit_id
        while parent[root] != root:
            root = parent[root]
        while parent[unit_id] != unit_id:
            next_id = parent[unit_id]
            parent[unit_id] = root
            unit_id = next_id
        return root

    for edge in all_edges:
        left, right = edge["source_id"], edge["destination_id"]
        if left not in parent or right not in parent:
            continue
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            continue
        kind = str(edge.get("kind", "unknown"))
        if component_size[left_root] + component_size[right_root] > max_units:
            rejected_merges[kind] += 1
            continue
        # The lexical root makes IDs and artifacts independent of edge
        # traversal details while edge order still gives evidence priority.
        root, child = sorted((left_root, right_root))
        parent[child] = root
        component_size[root] += component_size.pop(child)
        accepted_merges[kind] += 1

    grouped: dict[str, list[str]] = defaultdict(list)
    for unit_id in eligible_ids:
        grouped[find(unit_id)].append(unit_id)
    partitions = sorted((sorted(group) for group in grouped.values()), key=lambda group: group[0])
    groups = []
    destination_id_sets: list[set[str]] = []
    group_index_by_unit = {
        unit_id: group_index
        for group_index, partition in enumerate(partitions)
        for unit_id in partition
    }
    for partition in partitions:
        destination_ids = sorted({dest_id for unit_id in partition for dest_id in units[unit_id].get("destination_ids", [])})
        analysis_observations = sorted({
            limitation for unit_id in partition for limitation in units[unit_id].get("limitations", [])
        } | set(contributions.get("limitations", [])))
        limitations = sorted(item for item in analysis_observations
                             if item in _CONTEXT_GAP_LIMITATIONS)
        if any(unit_id in metadata_unknown_ids for unit_id in partition):
            limitations.append("candidate_metadata_missing")
        if omitted:
            limitations.append("candidate_discovery_truncated")
        groups.append({
            "id": "grp-" + digest({"unit_ids": partition, "destination_ids": destination_ids})[:24],
            "unit_ids": partition,
            "destination_ids": destination_ids,
            "edges": [],
            "boundary_edges": [],
            "analysis_observations": analysis_observations,
            "limitations": sorted(set(limitations)),
            "context_complete": not any(item in _CONTEXT_GAP_LIMITATIONS for item in limitations),
        })
        destination_id_sets.append(set(destination_ids))

    # max_edges is a per-group output budget. A global cap made later groups
    # look unrelated, so connectivity is retained and omitted edges are
    # charged to the affected groups. Cross-partition edges are admitted
    # atomically on both sides.
    output_edge_count = 0
    output_budget_omitted: dict[str, int] = defaultdict(int)
    output_omissions_by_group: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    output_edges_by_group: dict[int, int] = defaultdict(int)
    excluded_neighbor_edges: list[dict[str, Any]] = []
    for edge in all_edges:
        source = edge["source_id"]
        target = edge["destination_id"]
        if target in units:
            source_is_grouped = source in group_index_by_unit
            target_is_grouped = target in group_index_by_unit
            if source_is_grouped and not target_is_grouped:
                source_group = group_index_by_unit[source]
                excluded_neighbor = {
                    **edge,
                    "boundary_status": "excluded_neighbor",
                    "excluded_target_branch": unit_branches[target],
                    "exclusion_reasons": excluded_branches.get(unit_branches[target], ["excluded_reason_unspecified"]),
                }
                excluded_neighbor_edges.append(excluded_neighbor)
                placements = [(source_group, "boundary_edges", excluded_neighbor)]
            elif not source_is_grouped and target_is_grouped:
                target_group = group_index_by_unit[target]
                excluded_neighbor = {
                    **edge,
                    "boundary_status": "excluded_neighbor",
                    "excluded_source_branch": unit_branches[source],
                    "exclusion_reasons": excluded_branches.get(unit_branches[source], ["excluded_reason_unspecified"]),
                }
                excluded_neighbor_edges.append(excluded_neighbor)
                placements = [(target_group, "boundary_edges", excluded_neighbor)]
            elif not source_is_grouped and not target_is_grouped:
                continue
            else:
                source_group = group_index_by_unit[source]
                target_group = group_index_by_unit[target]
                placements = ([(source_group, "edges", edge)] if source_group == target_group else [
                    (source_group, "boundary_edges", edge), (target_group, "boundary_edges", edge)
                ])
        elif target in destinations:
            if source not in group_index_by_unit:
                continue
            source_group = group_index_by_unit[source]
            placements = [(source_group, "edges", edge)]
            if target not in destination_id_sets[source_group]:
                groups[source_group]["destination_ids"].append(target)
                destination_id_sets[source_group].add(target)
        else:
            # Validation rejects this, but retain the fail-closed boundary.
            raise JgError(f"edge refers to unknown destination id: {target}")
        affected_groups = {group_index for group_index, _, _ in placements}
        if any(output_edges_by_group[group_index] >= max_edges for group_index in affected_groups):
            output_budget_omitted[edge.get("kind", "unknown")] += 1
            for group_index in affected_groups:
                output_omissions_by_group[group_index][edge.get("kind", "unknown")] += 1
            continue
        output_edge_count += len(placements)
        for group_index, field, placed_edge in placements:
            groups[group_index][field].append(placed_edge)
            output_edges_by_group[group_index] += 1

    for group_index, group in enumerate(groups):
        group["destination_ids"] = sorted(set(group["destination_ids"]))
        group["id"] = "grp-" + digest({"unit_ids": group["unit_ids"], "destination_ids": group["destination_ids"]})[:24]
        group["edges"].sort(key=lambda edge: edge["id"])
        group["boundary_edges"].sort(key=lambda edge: edge["id"])
        required_boundaries = [
            edge for edge in group["boundary_edges"]
            if edge.get("kind", edge.get("type")) not in _CANDIDATE_EDGE_KINDS
        ]
        candidate_boundaries = [
            edge for edge in group["boundary_edges"]
            if edge.get("kind", edge.get("type")) in _CANDIDATE_EDGE_KINDS
        ]
        if required_boundaries:
            group["limitations"].append("partition_has_required_cross_group_edges")
        group["context_scope"] = "dependency_and_non_candidate_relationships"
        group["required_boundary_edge_count"] = len(required_boundaries)
        group["candidate_boundary_edge_count"] = len(candidate_boundaries)
        if any(edge.get("boundary_status") == "excluded_neighbor" for edge in group["boundary_edges"]):
            group["limitations"].append("excluded_neighbor_edges_present")
        if output_omissions_by_group.get(group_index):
            group["omitted_edges_by_type"] = dict(sorted(output_omissions_by_group[group_index].items()))
            if any(kind not in _CANDIDATE_EDGE_KINDS
                   for kind in output_omissions_by_group[group_index]):
                group["limitations"].append("edge_output_budget_exhausted")
        group["limitations"] = sorted(set(group["limitations"]))
        group["analysis_observations"] = sorted(set(group["analysis_observations"]))
        group["context_complete"] = not any(
            item in _CONTEXT_GAP_LIMITATIONS for item in group["limitations"]
        )

    coverage = {
        "eligible_source_units": len(eligible_ids),
        "grouped_source_units": sum(len(group["unit_ids"]) for group in groups),
        "excluded_source_units": ineligible_unit_ids,
        "excluded_branches": [{"branch": name, "exclusion_reasons": reasons} for name, reasons in sorted(excluded_branches.items())],
        "excluded_branch_count": len(excluded_branches),
        "candidate_metadata_unknown_units": metadata_unknown_ids,
        "eligible_branches_without_units": sorted(name for name in eligible_branches if name not in set(unit_branches.values())),
        "candidate_edges_discovered": len(candidate_edges),
        "accepted_group_merges_by_type": dict(sorted(accepted_merges.items())),
        "rejected_group_merges_by_type": dict(sorted(rejected_merges.items())),
        "input_edges_considered": len(normalized_input_edges),
        "excluded_neighbor_edge_ids": sorted(edge["id"] for edge in excluded_neighbor_edges),
        "excluded_neighbor_edge_count": len(excluded_neighbor_edges),
        "omitted_candidates_by_type": dict(sorted(omitted.items())),
        "unexpanded_pairwise_candidates_by_type": dict(sorted(unexpanded_pairwise.items())),
        "discovery_scope": "one deterministic spanning chain per shared indexed signal; remaining pairwise relationships were not enumerated",
        "pairwise_relationships_exhaustive": False,
        "context_scope": "dependency and non-candidate relationships; candidate boundaries remain visible but do not imply dependency",
        "groups_with_required_boundaries": sum(
            group["required_boundary_edge_count"] > 0 for group in groups
        ),
        "groups_with_candidate_boundaries": sum(
            group["candidate_boundary_edge_count"] > 0 for group in groups
        ),
        "omitted_output_edges_by_type": dict(sorted(output_budget_omitted.items())),
        "unresolved_candidate_count": sum(omitted.values()) + sum(output_budget_omitted.values()),
        "output_edges_emitted": output_edge_count,
        "max_units": max_units,
        "max_edges_per_group": max_edges,
        "truncated": bool(omitted) or bool(output_budget_omitted),
    }
    result = {
        "kind": "contribution-groups",
        "schema_version": _GROUPS_SCHEMA_VERSION,
        "repository_id": contributions["repository_id"],
        "snapshot_digest": contributions["snapshot_digest"],
        "contributions_digest": contributions["contributions_digest"],
        "groups": groups,
        "coverage": coverage,
    }
    result["groups_digest"] = digest({key: value for key, value in result.items() if key != "groups_digest"})
    return result


def write_groups(contributions_path: str | Path, out: str | Path, max_units: int = 24, max_edges: int = 1000) -> Path:
    """Read a pinned contributions artifact and write groups.json."""
    contributions = read_json(contributions_path)
    result = build_groups(contributions, max_units=max_units, max_edges=max_edges)
    output_dir = Path(out).expanduser().resolve()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = output_dir / "groups.json"
    if target.exists():
        raise JgError(f"groups artifact already exists: {target}")
    write_json(target, result)
    return target
