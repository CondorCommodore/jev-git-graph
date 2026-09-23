"""Bounded grouped requests and transient two-sided pinned code evidence."""

from __future__ import annotations

import hashlib
import base64
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import JgError
from .questions import PRESENCE_QUESTION_VERSION, presence_questions
from .safety import canonical_json, digest
from .code_evidence import _path, _reject_sensitive

_OID = re.compile(r"\A[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
DEFAULT_MAX_GROUPS = 32
DEFAULT_MAX_REQUEST_BYTES = 64_000
DEFAULT_MAX_EVIDENCE_BYTES = 24_000


def _git(root: Path, *args: str, optional: bool = False) -> bytes | None:
    result = subprocess.run(("git", "-C", str(root), *args), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        if optional:
            return None
        raise JgError("unable to read pinned presence evidence")
    return result.stdout


def _oid(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise JgError(f"{label} must be a full Git object ID")
    return value.lower()


def _line_range(value: Any, label: str) -> tuple[int, int]:
    if not isinstance(value, Mapping):
        raise JgError(f"{label} range must be an object")
    start, end = value.get("start_line"), value.get("end_line")
    if (not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int)
            or isinstance(end, bool) or start < 1 or end < start or end - start >= 80):
        raise JgError(f"{label} range must contain 1 to 80 inclusive lines")
    return start, end


def _read_excerpt(root: Path, tip: str, path: str, range_value: Any, label: str) -> dict[str, Any]:
    start, end = _line_range(range_value, label)
    oid_bytes = _git(root, "rev-parse", "--verify", f"{tip}:{path}", optional=True)
    if oid_bytes is None:
        raise JgError(f"{label} path does not exist at its pinned tip")
    blob = oid_bytes.decode("ascii", "strict").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", blob):
        raise JgError(f"{label} is not a pinned Git blob")
    kind = _git(root, "cat-file", "-t", blob)
    if kind is None or kind.decode("ascii", "strict").strip() != "blob":
        raise JgError(f"{label} is not a regular source blob")
    raw = _git(root, "cat-file", "blob", blob)
    assert raw is not None
    try:
        source_text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise JgError(f"{label} is not UTF-8 text") from None
    if "\x00" in source_text:
        raise JgError(f"{label} contains a NUL byte")
    lines = source_text.splitlines(keepends=True)
    if end > len(lines):
        raise JgError(f"{label} range is outside its pinned blob")
    excerpt = "".join(lines[start - 1:end])
    _reject_sensitive(excerpt)
    return {"tip": tip, "path": path, "blob": blob,
            "range": {"start_line": start, "end_line": end},
            "text": excerpt, "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest()}


def build_two_sided_evidence(
    repo: str | Path,
    source_tip: str,
    destination_tip: str,
    ranges: Iterable[Mapping[str, Any]],
    *,
    max_total_bytes: int = DEFAULT_MAX_EVIDENCE_BYTES,
    max_excerpt_pairs: int = 8,
    max_total_lines: int = 240,
) -> dict[str, Any]:
    """Build transient source AND destination excerpts from explicit ranges.

    Each range record names source_path/source_range and destination_path/
    destination_range, so moved units can be compared across paths.
    """
    root = Path(repo).expanduser().resolve()
    for label, value in (("max_total_bytes", max_total_bytes), ("max_excerpt_pairs", max_excerpt_pairs),
                         ("max_total_lines", max_total_lines)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise JgError(f"{label} must be a positive integer")
    source_tip, destination_tip = _oid(source_tip, "source_tip"), _oid(destination_tip, "destination_tip")
    for tip, label in ((source_tip, "source"), (destination_tip, "destination")):
        resolved = _git(root, "rev-parse", "--verify", f"{tip}^{{commit}}")
        if resolved is None or resolved.decode("ascii", "strict").strip().lower() != tip:
            raise JgError(f"{label} commit pin is unavailable")
    records = []
    total = 0
    total_lines = 0
    for spec in ranges:
        if not isinstance(spec, Mapping):
            raise JgError("two-sided evidence ranges must be objects")
        source_path = _path(spec.get("source_path"))
        destination_path = _path(spec.get("destination_path"))
        source = _read_excerpt(root, source_tip, source_path, spec.get("source_range"), "source")
        destination = _read_excerpt(root, destination_tip, destination_path,
                                     spec.get("destination_range"), "destination")
        total_lines += (source["range"]["end_line"] - source["range"]["start_line"] + 1
                        + destination["range"]["end_line"] - destination["range"]["start_line"] + 1)
        if len(records) >= max_excerpt_pairs:
            raise JgError("two-sided evidence exceeds the excerpt-pair bound")
        if total_lines > max_total_lines:
            raise JgError("two-sided evidence exceeds the total line bound")
        total += len(source["text"].encode()) + len(destination["text"].encode())
        if total > max_total_bytes:
            raise JgError("two-sided evidence exceeds the approved byte bound")
        records.append({"evidence_id": spec.get("evidence_id"), "source": source,
                        "destination": destination})
    if not records:
        raise JgError("two-sided evidence requires at least one approved range pair")
    result = {"kind": "branch-presence-code-evidence", "schema_version": 1,
              "source_tip": source_tip, "destination_tip": destination_tip,
            "records": records, "total_bytes": total}
    result["evidence_digest"] = digest(result)
    return result


def revalidate_two_sided_evidence(repo: str | Path, evidence: Mapping[str, Any]) -> dict[str, Any]:
    if evidence.get("kind") != "branch-presence-code-evidence" or evidence.get("schema_version") != 1:
        raise JgError("invalid two-sided presence evidence")
    expected = evidence.get("evidence_digest")
    if expected != digest({key: value for key, value in evidence.items() if key != "evidence_digest"}):
        raise JgError("two-sided evidence digest is invalid")
    ranges = []
    for item in evidence.get("records", []):
        ranges.append({"evidence_id": item.get("evidence_id"),
                       "source_path": item["source"]["path"],
                       "source_range": item["source"]["range"],
                       "destination_path": item["destination"]["path"],
                       "destination_range": item["destination"]["range"]})
    rebuilt = build_two_sided_evidence(repo, evidence["source_tip"], evidence["destination_tip"], ranges,
                                       max_total_bytes=max(evidence.get("total_bytes", 1), 1))
    if rebuilt["evidence_digest"] != expected:
        raise JgError("pinned two-sided presence evidence changed; obtain a new approval")
    return rebuilt


def build_group_requests(
    contributions: Mapping[str, Any],
    groups: Mapping[str, Any],
    evidence_by_contribution: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    max_groups: int = DEFAULT_MAX_GROUPS,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    model_settings: Mapping[str, Any] | None = None,
    estimated_input_tokens: int | None = None,
    max_provider_tokens: int | None = None,
    selected_contribution_ids: Iterable[str] | None = None,
    selection_digest: str | None = None,
) -> dict[str, Any]:
    """Build one named-question request per bounded group, retaining omissions."""
    if (not isinstance(max_groups, int) or isinstance(max_groups, bool) or max_groups < 1
            or not isinstance(max_request_bytes, int) or isinstance(max_request_bytes, bool) or max_request_bytes < 1):
        raise JgError("group and request byte budgets must be positive integers")
    if contributions.get("kind") != "contributions" or groups.get("kind") != "contribution-groups":
        raise JgError("group requests require contributions and contribution-groups artifacts")
    if contributions.get("contributions_digest") != digest({k: v for k, v in contributions.items() if k != "contributions_digest"}):
        raise JgError("contributions digest is invalid")
    if contributions.get("contributions_digest") != groups.get("contributions_digest"):
        raise JgError("groups do not match pinned contributions")
    if contributions.get("snapshot_digest") != groups.get("snapshot_digest"):
        raise JgError("groups do not match the pinned snapshot")
    if groups.get("groups_digest") != digest({k: v for k, v in groups.items() if k != "groups_digest"}):
        raise JgError("group digest is invalid")
    units = {unit["id"]: unit for unit in contributions.get("units", [])}
    if len(units) != len(contributions.get("units", [])):
        raise JgError("contributions contain duplicate unit IDs")
    destinations = {unit["id"]: unit for unit in contributions.get("destination_units", [])}
    edges = contributions.get("edges", [])
    if not isinstance(edges, list) or any(not isinstance(edge, Mapping) for edge in edges):
        raise JgError("contributions edges must be objects")
    evidence_by_contribution = evidence_by_contribution or {}
    settings = dict(model_settings or {"model": "jev-latest", "reasoning_effort": None, "max_output_tokens": None})
    if set(settings) - {"model", "reasoning_effort", "max_output_tokens", "temperature", "seed"}:
        raise JgError("model settings contain unsupported or sensitive fields")
    if any(value is not None and not isinstance(value, (str, int, float, bool)) for value in settings.values()):
        raise JgError("model settings must contain scalar values")
    if not isinstance(settings.get("model"), str) or not settings["model"]:
        raise JgError("model settings must name a model")
    for field, value in (("estimated_input_tokens", estimated_input_tokens), ("max_provider_tokens", max_provider_tokens)):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
            raise JgError(f"{field} must be a positive integer")
    if (estimated_input_tokens is None) != (max_provider_tokens is None):
        raise JgError("estimated and maximum provider token budgets must be supplied together")
    if estimated_input_tokens is not None and estimated_input_tokens > max_provider_tokens:
        raise JgError("estimated input tokens exceed the provider token budget")
    group_records = groups.get("groups", [])
    if not isinstance(group_records, list):
        raise JgError("groups artifact lacks groups[]")
    selected_ids = None
    if selected_contribution_ids is not None:
        if isinstance(selected_contribution_ids, (str, bytes)):
            raise JgError("selected contribution IDs must be a sequence of IDs")
        selected_ids = list(selected_contribution_ids)
        if (not selected_ids or any(not isinstance(cid, str) or not cid for cid in selected_ids)
                or len(selected_ids) != len(set(selected_ids))):
            raise JgError("selected contribution IDs must be unique non-empty IDs")
        if set(selected_ids) - set(units):
            raise JgError("selection names contributions absent from the pinned artifact")
        if not isinstance(selection_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", selection_digest):
            raise JgError("a pinned selection digest is required with selected contribution IDs")
    elif selection_digest is not None:
        raise JgError("selection digest requires selected contribution IDs")
    selected_set = set(selected_ids) if selected_ids is not None else None
    selected_groups = []
    selected_group_members: dict[str, list[str]] = {}
    for group in group_records:
        if not isinstance(group, Mapping):
            raise JgError("group record must be an object")
        unit_ids = group.get("unit_ids", [])
        if not isinstance(unit_ids, list):
            raise JgError("group unit_ids must be a list")
        targets = [cid for cid in unit_ids if selected_set is None or cid in selected_set]
        if targets:
            selected_groups.append(group)
            selected_group_members[str(group.get("id"))] = targets
    if selected_ids is not None:
        covered = {cid for targets in selected_group_members.values() for cid in targets}
        if covered != selected_set:
            raise JgError("study selection includes contributions not assigned to an original group")
    if len(selected_groups) > max_groups:
        raise JgError("selected group request plan exceeds max_groups")
    seen_group_ids: set[str] = set()
    requests, omitted = [], []
    for group in selected_groups:
        if not isinstance(group, Mapping):
            raise JgError("group record must be an object")
        group_id = group.get("id")
        unit_ids = group.get("unit_ids", [])
        if not isinstance(group_id, str) or not group_id or group_id in seen_group_ids:
            raise JgError("group IDs must be unique non-empty strings")
        seen_group_ids.add(group_id)
        if not isinstance(unit_ids, list) or any(not isinstance(unit_id, str) or not unit_id for unit_id in unit_ids):
            raise JgError(f"group {group_id} unit_ids must be non-empty strings")
        if len(unit_ids) != len(set(unit_ids)):
            raise JgError(f"group {group_id} has duplicate contribution IDs")
        destination_ids = group.get("destination_ids", [])
        if not isinstance(destination_ids, list) or any(not isinstance(value, str) or not value for value in destination_ids):
            raise JgError(f"group {group_id} destination_ids must be non-empty strings")
        source_items, questions, evidence_ids, group_omitted = [], {}, [], []
        target_ids = selected_group_members[group_id]
        for unit_id in target_ids:
            unit = units.get(unit_id)
            if unit is None:
                omitted.append({"group_id": group_id, "contribution_id": unit_id, "reason": "unit_missing"})
                group_omitted.append({"contribution_id": unit_id, "reason": "unit_missing"})
                continue
            unit_edges = [edge for edge in edges if edge.get("source_id") == unit_id
                          and str(edge.get("kind", edge.get("type", ""))).lower()
                          in {"dependency", "depends_on", "requires", "prerequisite"}]
            if any(not isinstance(edge, Mapping) for edge in unit_edges):
                raise JgError(f"contribution {unit_id} has a malformed dependency edge")
            dependencies = [{"id": edge.get("id") or "edge-" + digest(edge)[:20],
                             "neighbor_id": edge.get("destination_id"),
                             "kind": edge.get("kind", edge.get("type", "unknown"))}
                            for edge in unit_edges]
            unit_questions = presence_questions(
                unit_id, dependencies, dependency_context_status=unit.get("dependency_context_status"))
            for question_id, question in unit_questions.items():
                questions[f"{unit_id}:{question_id}"] = question
            evidence = evidence_by_contribution.get(unit_id)
            if evidence is not None:
                if evidence.get("kind") != "branch-presence-code-evidence":
                    raise JgError(f"invalid approved evidence for {unit_id}")
                if evidence.get("evidence_digest") != digest({k: v for k, v in evidence.items() if k != "evidence_digest"}):
                    raise JgError(f"approved evidence digest mismatch for {unit_id}")
                if evidence.get("source_tip") != unit.get("source_tip") or evidence.get("destination_tip") != unit.get("main_tip"):
                    raise JgError(f"approved evidence pins do not match contribution {unit_id}")
                _validate_evidence_text(evidence)
                evidence_ids.extend(record.get("evidence_id") for record in evidence.get("records", [])
                                    if isinstance(record.get("evidence_id"), str))
            source_metadata = {key: unit.get(key) for key in ("source_tip", "main_tip", "path", "mode", "kind", "name", "range", "source_blob", "limitations") if key in unit}
            source_metadata["identity"] = dict(unit.get("source", {}))
            candidate_ids = unit.get("destination_ids", [])
            comparison_limitations = []
            if evidence is None or not evidence.get("records"):
                comparison_limitations.append("approved_two_sided_evidence_missing")
            if not candidate_ids:
                comparison_limitations.append("destination_candidate_missing")
            missing_candidate_ids = sorted(set(candidate_ids) - set(destinations))
            if missing_candidate_ids:
                comparison_limitations.append("destination_candidate_record_missing")
            evidence_records = evidence.get("records", []) if isinstance(evidence, Mapping) else []
            covered_candidates = set()
            source_evidence_present = False
            for record in evidence_records:
                source_record, destination_record = record.get("source", {}), record.get("destination", {})
                if (source_record.get("path") == unit.get("path")
                        and source_record.get("blob") == unit.get("source_blob")):
                    source_evidence_present = True
                    for destination_id in candidate_ids:
                        candidate = destinations.get(destination_id, {})
                        if (destination_record.get("path") == candidate.get("path")
                                and destination_record.get("blob") == candidate.get("blob")):
                            covered_candidates.add(destination_id)
            if evidence_records and not source_evidence_present:
                comparison_limitations.append("approved_source_range_does_not_match_unit")
            if set(candidate_ids) - covered_candidates:
                comparison_limitations.append("approved_destination_ranges_incomplete")
            source_item = {"contribution_id": unit_id, "source": source_metadata,
                           "destination_ids": candidate_ids,
                           "dependency_edges": dependencies, "evidence": evidence,
                           "comparison_context_complete": bool(evidence_records and source_evidence_present
                               and candidate_ids and not missing_candidate_ids
                               and set(candidate_ids) <= covered_candidates),
                           "comparison_context_limitations": sorted(set(comparison_limitations))}
            if "dependency_context_status" in unit:
                dependency_status = unit.get("dependency_context_status")
                dependency_limitations = list(unit.get("dependency_context_limitations", []))
                if dependency_status not in {"complete", "unknown", "incomplete"}:
                    dependency_status = "unknown"
                    dependency_limitations.append("dependency_context_status_invalid")
                source_item["dependency_context_status"] = dependency_status
                source_item["dependency_context_limitations"] = sorted(set(dependency_limitations))
            source_items.append(source_item)
        context_items = []
        target_set = set(target_ids)
        for context_id in unit_ids:
            if context_id in target_set:
                continue
            context_unit = units.get(context_id)
            if context_unit is None:
                group_omitted.append({"contribution_id": context_id, "reason": "context_unit_missing"})
                continue
            context_items.append({"contribution_id": context_id,
                "source": {key: context_unit.get(key) for key in
                    ("source_tip", "main_tip", "path", "kind", "name", "range", "source_blob", "limitations")
                    if key in context_unit},
                "destination_ids": context_unit.get("destination_ids", [])})
        selected_destination_ids = set(group.get("destination_ids", []))
        for item in source_items:
            selected_destination_ids.update(item.get("destination_ids", []))
        destination_items = []
        for destination_id in sorted(selected_destination_ids):
            destination = destinations.get(destination_id)
            if destination is None:
                group_omitted.append({"destination_id": destination_id, "reason": "destination_missing"})
                continue
            destination_items.append({key: destination.get(key) for key in
                ("id", "path", "blob", "mode", "kind", "name", "range", "ast_fingerprint", "limitations")
                if key in destination})
        group_limitations = list(group.get("limitations", []))
        if group_omitted:
            group_limitations.append("group_evidence_omitted")
        state = {"group_id": group_id, "contributions": source_items,
                 "context_units": context_items,
                 "context_contribution_ids": [item["contribution_id"] for item in context_items],
                 "destination_ids": group.get("destination_ids", []),
                 "destinations": destination_items,
                 "boundary_edges": group.get("boundary_edges", []),
                 "limitations": sorted(set(group_limitations)), "omissions": group_omitted,
                 "context_complete": group.get("context_complete") is True and not group_limitations,
                 "evidence_ids": sorted(set(evidence_ids))}
        requests.append({"state": state, "model": settings["model"], "questions": questions})
    size = len(canonical_json(requests))
    if size > max_request_bytes:
        raise JgError("group request payload exceeds max_request_bytes")
    return {"kind": "branch-presence-request-plan", "schema_version": 1,
            "question_version": PRESENCE_QUESTION_VERSION,
            "snapshot_digest": contributions.get("snapshot_digest"),
            "contributions_digest": contributions.get("contributions_digest"),
            "groups_digest": groups.get("groups_digest"),
            "selected_contribution_ids": selected_ids,
            "selection_digest": selection_digest,
            "request_count": len(requests), "payload_bytes": size,
            "requests": requests, "omitted": omitted,
            "model_settings": settings, "model_settings_digest": digest(settings),
            "request_budgets": {"max_groups": max_groups, "max_request_bytes": max_request_bytes,
                                "estimated_input_tokens": estimated_input_tokens,
                                "max_provider_tokens": max_provider_tokens,
                                "token_estimator": "caller_supplied" if estimated_input_tokens is not None else "unavailable",
                                "token_budget_established": estimated_input_tokens is not None},
            "plan_digest": digest({"requests": requests, "omitted": omitted,
                                    "snapshot_digest": contributions.get("snapshot_digest"),
                                    "contributions_digest": contributions.get("contributions_digest"),
                                    "groups_digest": groups.get("groups_digest"),
                                    "selected_contribution_ids": selected_ids,
                                    "selection_digest": selection_digest,
                                    "model_settings_digest": digest(settings),
                                    "request_budgets": {"max_groups": max_groups,
                                        "max_request_bytes": max_request_bytes,
                                        "estimated_input_tokens": estimated_input_tokens,
                                        "max_provider_tokens": max_provider_tokens,
                                        "token_estimator": "caller_supplied" if estimated_input_tokens is not None else "unavailable",
                                        "token_budget_established": estimated_input_tokens is not None}})}


def approved_presence_preview(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return exact transient payload metadata; request bytes are not persisted."""
    requests = plan.get("requests")
    if not isinstance(requests, list) or plan.get("question_version") != PRESENCE_QUESTION_VERSION:
        raise JgError("invalid branch-presence request plan")
    payload_sha = digest(requests)
    request_bytes = canonical_json(requests)
    return {"kind": "branch-presence-preview", "schema_version": 1,
            "question_version": PRESENCE_QUESTION_VERSION,
            "snapshot_digest": plan.get("snapshot_digest"),
            "contributions_digest": plan.get("contributions_digest"),
            "groups_digest": plan.get("groups_digest"),
            "selected_contribution_ids": plan.get("selected_contribution_ids"),
            "selection_digest": plan.get("selection_digest"),
            "request_count": len(requests), "payload_bytes": len(request_bytes),
            "payload_sha256": payload_sha, "request_bytes_base64": base64.b64encode(request_bytes).decode("ascii"),
            "requests": requests, "model_settings": plan.get("model_settings"),
            "model_settings_digest": plan.get("model_settings_digest"),
            "request_budgets": plan.get("request_budgets"),
            "plan_digest": plan.get("plan_digest"),
            "no_store": True, "network_performed": False,
            "approval_sha256": digest({"payload_sha256": payload_sha,
                                        "plan_digest": plan.get("plan_digest"),
                                        "request_count": len(requests)})}


def _validate_evidence_text(evidence: Mapping[str, Any]) -> None:
    records = evidence.get("records")
    if not isinstance(records, list) or not records or len(records) > 8:
        raise JgError("approved presence evidence must contain source and destination excerpts")
    total = 0
    total_lines = 0
    seen_ids: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise JgError("approved presence evidence record is invalid")
        evidence_id = record.get("evidence_id")
        if evidence_id is not None:
            if not isinstance(evidence_id, str) or not evidence_id or evidence_id in seen_ids:
                raise JgError("approved presence evidence IDs must be unique non-empty strings")
            seen_ids.add(evidence_id)
        for side_name in ("source", "destination"):
            side = record.get(side_name)
            if not isinstance(side, Mapping):
                raise JgError("approved presence evidence is missing a side")
            _path(side.get("path"))
            _oid(side.get("tip"), f"{side_name} evidence tip")
            _oid(side.get("blob"), f"{side_name} evidence blob")
            expected_tip = evidence.get("source_tip" if side_name == "source" else "destination_tip")
            if side.get("tip") != expected_tip:
                raise JgError("presence excerpt tip does not match its evidence pin")
            start, end = _line_range(side.get("range"), f"{side_name} evidence")
            text = side.get("text")
            if not isinstance(text, str) or not text:
                raise JgError("approved presence evidence excerpt is empty")
            encoded = text.encode("utf-8")
            if hashlib.sha256(encoded).hexdigest() != side.get("excerpt_sha256"):
                raise JgError("approved presence excerpt hash is invalid")
            if len(text.splitlines()) != end - start + 1:
                raise JgError("approved presence excerpt does not match its line range")
            total_lines += end - start + 1
            _reject_sensitive(text)
            total += len(encoded)
    if (total != evidence.get("total_bytes") or total > DEFAULT_MAX_EVIDENCE_BYTES
            or total_lines > 240):
        raise JgError("approved presence evidence byte count is invalid")
