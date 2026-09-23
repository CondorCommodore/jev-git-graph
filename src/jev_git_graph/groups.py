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


_SCHEMA_VERSION = 1
_SIGNAL_BUCKET_LIMIT = 64
_SIGNAL_EDGE_LIMIT = 256


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
    if contributions.get("kind") != "contributions" or contributions.get("schema_version") != _SCHEMA_VERSION:
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

    # Discover bounded candidate relationships via indexed keys, never an
    # unrestricted all-pairs comparison. Each generated link has its signal.
    candidate_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    omitted: dict[str, int] = defaultdict(int)
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
        # A wide signal's first unit is a deterministic representative. The
        # remaining units are accounted as omitted candidate comparisons.
        allowed = ids[:_SIGNAL_BUCKET_LIMIT]
        possible = len(allowed) * (len(allowed) - 1) // 2
        pairs_added = 0
        for i, first in enumerate(allowed):
            for second in allowed[i + 1:]:
                if pairs_added >= _SIGNAL_EDGE_LIMIT:
                    break
                add(first, second, signal_type, signal)
                pairs_added += 1
            if pairs_added >= _SIGNAL_EDGE_LIMIT:
                break
        if len(ids) > len(allowed):
            omitted[signal_type] += len(ids) - len(allowed)
        omitted[signal_type] += max(0, possible - pairs_added)

    known_source_edges = [edge for edge in contributions["edges"] if edge["source_id"] in eligible_ids]
    # Input edge IDs may be absent; synthesize a stable identifier while
    # preserving every supplied field verbatim.
    normalized_input_edges = []
    for edge in known_source_edges:
        normalized = dict(edge)
        normalized.setdefault("id", "ge-" + digest(edge)[:24])
        normalized.setdefault("kind", normalized.get("type", "unknown"))
        normalized.setdefault("provenance", "contribution_edge")
        normalized_input_edges.append(normalized)

    all_edges = list(candidate_edges.values()) + normalized_input_edges
    all_edges.sort(key=lambda edge: (edge["id"], edge["source_id"], edge["destination_id"]))
    globally_omitted_edges = max(0, len(all_edges) - max_edges)
    if globally_omitted_edges:
        for edge in all_edges[max_edges:]:
            omitted[edge.get("kind", "unknown")] += 1
        all_edges = all_edges[:max_edges]

    # Connected components across source units only. Destination candidates
    # are attached locally and never act as universal main-branch hubs.
    adjacency: dict[str, set[str]] = {unit_id: set() for unit_id in eligible_ids}
    for edge in all_edges:
        left, right = edge["source_id"], edge["destination_id"]
        if left in adjacency and right in adjacency:
            adjacency[left].add(right)
            adjacency[right].add(left)
    components: list[list[str]] = []
    unseen = set(eligible_ids)
    while unseen:
        root = min(unseen)
        stack, component = [root], []
        unseen.remove(root)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in sorted(adjacency[current] & unseen, reverse=True):
                unseen.remove(neighbor)
                stack.append(neighbor)
        components.append(sorted(component))
    components.sort(key=lambda component: component[0])

    partitions: list[list[str]] = []
    for component in components:
        for offset in range(0, len(component), max_units):
            partitions.append(component[offset:offset + max_units])
    groups = []
    for index, partition in enumerate(partitions):
        part_set = set(partition)
        internal_edges = [edge for edge in all_edges if edge["source_id"] in part_set and edge["destination_id"] in part_set]
        boundary_edges = [edge for edge in all_edges if edge["source_id"] in part_set and edge["destination_id"] in units and edge["destination_id"] not in part_set]
        # Include destination edges only where their source belongs to this group.
        destination_ids = sorted({dest_id for unit_id in partition for dest_id in units[unit_id].get("destination_ids", [])})
        for edge in normalized_input_edges:
            if edge["source_id"] in part_set and edge["destination_id"] in destinations and edge not in internal_edges:
                internal_edges.append(edge)
                if edge["destination_id"] not in destination_ids:
                    destination_ids.append(edge["destination_id"])
        internal_edges.sort(key=lambda edge: edge["id"])
        boundary_edges.sort(key=lambda edge: edge["id"])
        limitations = sorted({limitation for unit_id in partition for limitation in units[unit_id].get("limitations", [])})
        if any(unit_id in metadata_unknown_ids for unit_id in partition):
            limitations.append("candidate_metadata_missing")
        if boundary_edges:
            limitations.append("partition_has_known_cross_group_edges")
        if omitted:
            limitations.append("candidate_discovery_truncated")
        group_payload = {"unit_ids": partition, "destination_ids": destination_ids}
        groups.append({
            "id": "grp-" + digest(group_payload)[:24],
            "unit_ids": partition,
            "destination_ids": destination_ids,
            "edges": internal_edges,
            "boundary_edges": boundary_edges,
            "limitations": sorted(set(limitations)),
            "context_complete": not boundary_edges and not omitted,
        })

    coverage = {
        "eligible_source_units": len(eligible_ids),
        "grouped_source_units": sum(len(group["unit_ids"]) for group in groups),
        "excluded_source_units": ineligible_unit_ids,
        "excluded_branches": [{"branch": name, "exclusion_reasons": reasons} for name, reasons in sorted(excluded_branches.items())],
        "excluded_branch_count": len(excluded_branches),
        "candidate_metadata_unknown_units": metadata_unknown_ids,
        "eligible_branches_without_units": sorted(name for name in eligible_branches if name not in set(unit_branches.values())),
        "candidate_edges_discovered": len(candidate_edges),
        "input_edges_considered": len(normalized_input_edges),
        "omitted_candidates_by_type": dict(sorted(omitted.items())),
        "unresolved_candidate_count": sum(omitted.values()),
        "max_units": max_units,
        "max_edges": max_edges,
        "truncated": bool(omitted) or bool(globally_omitted_edges),
    }
    result = {
        "kind": "contribution-groups",
        "schema_version": 1,
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
    write_json(target, result)
    return target
