"""Typed branch-presence request execution, import, and deterministic routing."""

from __future__ import annotations

import concurrent.futures
import base64
import re
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Mapping

from .errors import JgError
from .group_requests import revalidate_two_sided_evidence
from .jev import _default_transport, validate_response
from .questions import PRESENCE_CHOICES, PRESENCE_QUESTION_VERSION
from .safety import canonical_json, digest, read_json, write_json

RESULT_SCHEMA = "branch-presence-result-v1"
_TRUE = 0.75
_FALSE = 0.25


def _probability(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return None
    return float(value)


def _answer_value(answers: Mapping[str, Any], key: str, kind: str) -> Any:
    answer = answers.get(key)
    if not isinstance(answer, Mapping):
        return None
    return _probability(answer.get("noul")) if kind == "noul" else answer.get("choice")


def _bool_signal(value: Any) -> bool | None:
    probability = _probability(value)
    if probability is None:
        return None
    if probability >= _TRUE:
        return True
    if probability <= _FALSE:
        return False
    return None


def validate_presence_responses(preview: Mapping[str, Any], records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate sanitized request-bound records; unknown/duplicate requests fail closed."""
    if preview.get("kind") != "branch-presence-preview" or preview.get("no_store") is not True:
        raise JgError("presence responses require a no-store branch-presence preview")
    requests = preview.get("requests")
    if not isinstance(requests, list) or preview.get("payload_sha256") != digest(requests):
        raise JgError("presence preview payload digest is invalid")
    index = {digest(request): request for request in requests}
    seen: dict[str, str] = {}
    result = []
    for position, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise JgError(f"presence response record {position} must be an object")
        request_sha = record.get("request_sha256")
        if request_sha not in index:
            raise JgError("presence response does not identify a planned request")
        request = index[request_sha]
        response = record.get("response")
        try:
            validate_response(request, dict(response) if isinstance(response, Mapping) else response)
        except Exception:
            raise JgError("presence response does not match its typed request") from None
        canonical = digest(response)
        if request_sha in seen:
            if seen[request_sha] != canonical:
                raise JgError("conflicting duplicate presence response")
            continue
        seen[request_sha] = canonical
        result.append({"request_sha256": request_sha,
                       "group_id": request.get("state", {}).get("group_id"),
                       "contribution_bindings": _request_bindings(request),
                       "response": _sanitize_response(request, response)})
    return result


def _sanitize_response(request: Mapping[str, Any], response: Mapping[str, Any]) -> dict[str, Any]:
    answers = {}
    for key, question in request["questions"].items():
        item = response["answers"][key]
        if question.get("type") == "noul":
            answers[key] = {"noul": item["noul"]}
        else:
            answers[key] = {"choice": item["choice"], "confidence": item["confidence"],
                            "probabilities": dict(item["probabilities"])}
    return {"model": response["model"],
            "usage": {"input_tokens": response["usage"]["input_tokens"],
                      "output_tokens": response["usage"]["output_tokens"]},
            "answers": answers}


def _request_bindings(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    state = request.get("state", {})
    bindings = []
    for item in state.get("contributions", []):
        binding = {"contribution_id": item.get("contribution_id"),
                   "dependency_edges": [{"id": edge.get("id"), "neighbor_id": edge.get("neighbor_id")}
                                        for edge in item.get("dependency_edges", [])],
                   "context_complete": state.get("context_complete") is True,
                   "context_limitations": list(state.get("limitations", [])),
                   "evidence_ids": sorted({record.get("evidence_id") for record in (item.get("evidence") or {}).get("records", [])
                                           if isinstance(record.get("evidence_id"), str)})}
        if "comparison_context_complete" in item:
            binding["comparison_context_complete"] = item.get("comparison_context_complete") is True
            binding["comparison_context_limitations"] = list(item.get("comparison_context_limitations", []))
        if "dependency_context_status" in item:
            status = item.get("dependency_context_status")
            if status not in {"complete", "unknown", "incomplete"}:
                raise JgError("presence binding has an invalid dependency context status")
            binding["dependency_context_status"] = status
            binding["dependency_context_limitations"] = list(item.get("dependency_context_limitations", []))
        bindings.append(binding)
    return bindings


def _answers_digest(artifact: Mapping[str, Any]) -> str:
    return digest({key: value for key, value in artifact.items() if key != "answers_digest"})


def _validate_sanitized_answers(bindings: list[Mapping[str, Any]], response: Mapping[str, Any]) -> None:
    if not isinstance(response.get("model"), str) or not response["model"]:
        raise JgError("presence response lacks a model identifier")
    usage = response.get("usage")
    if not isinstance(usage, Mapping) or any(
        not isinstance(usage.get(field), int) or isinstance(usage.get(field), bool) or usage[field] < 0
        for field in ("input_tokens", "output_tokens")
    ):
        raise JgError("presence response usage is invalid")
    answers = response.get("answers")
    expected: dict[str, str] = {}
    for binding in bindings:
        cid = binding.get("contribution_id")
        if not isinstance(cid, str) or not cid:
            raise JgError("presence answer has an invalid contribution binding")
        prefix = cid + ":"
        expected[prefix + "evidence_sufficient"] = "noul"
        expected[prefix + "presence"] = "choice"
        expected[prefix + "usable_delta"] = "noul"
        if "dependency_context_status" in binding:
            expected[prefix + "dependency_context_sufficient"] = "noul"
        for edge in binding.get("dependency_edges", []):
            edge_id = edge.get("id")
            if not isinstance(edge_id, str) or not edge_id:
                raise JgError("presence answer has an invalid dependency binding")
            expected[prefix + "dependency:" + edge_id] = "noul"
    if not isinstance(answers, Mapping) or set(answers) != set(expected):
        raise JgError("presence answer IDs do not match contribution bindings")
    for key, kind in expected.items():
        answer = answers[key]
        if not isinstance(answer, Mapping):
            raise JgError("presence answer value must be an object")
        if kind == "noul":
            if _probability(answer.get("noul")) is None:
                raise JgError("presence noul answer is invalid")
        else:
            if answer.get("choice") not in PRESENCE_CHOICES or _probability(answer.get("confidence")) is None:
                raise JgError("presence choice answer is invalid")
            probabilities = answer.get("probabilities")
            if not isinstance(probabilities, Mapping) or set(probabilities) != set(PRESENCE_CHOICES):
                raise JgError("presence choice probabilities are invalid")
            if any(_probability(value) is None for value in probabilities.values()):
                raise JgError("presence choice probabilities are invalid")


def import_synthetic_answers(preview: Mapping[str, Any], answers: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Strict offline importer for known-answer fixtures and package checks."""
    normalized = validate_presence_responses(preview, answers)
    by_sha = {item["request_sha256"]: item for item in normalized}
    expected = {digest(request) for request in preview["requests"]}
    if set(by_sha) != expected:
        raise JgError("synthetic answer import must cover every planned request exactly once")
    artifact = {"kind": "branch-presence-answers", "schema_version": 1,
            "origin": "synthetic", "question_version": PRESENCE_QUESTION_VERSION,
            "snapshot_digest": preview.get("snapshot_digest"),
            "contributions_digest": preview.get("contributions_digest"),
            "groups_digest": preview.get("groups_digest"),
            "preview_sha256": preview.get("payload_sha256"),
            "answers": [by_sha[key] for key in sorted(by_sha)],
            "network_performed": False}
    artifact["answers_digest"] = _answers_digest(artifact)
    return artifact


def import_control_answers(
    preview: Mapping[str, Any],
    answers: list[Mapping[str, Any]],
    *,
    network_performed: bool = False,
) -> dict[str, Any]:
    """Import typed blind-control records; origin is assigned here, never trusted from input."""
    normalized = validate_presence_responses(preview, answers)
    by_sha = {item["request_sha256"]: item for item in normalized}
    expected = {digest(request) for request in preview["requests"]}
    if set(by_sha) != expected:
        raise JgError("control answer import must cover every planned request exactly once")
    artifact = {"kind": "branch-presence-answers", "schema_version": 1,
            "origin": "control", "question_version": PRESENCE_QUESTION_VERSION,
            "snapshot_digest": preview.get("snapshot_digest"),
            "contributions_digest": preview.get("contributions_digest"),
            "groups_digest": preview.get("groups_digest"),
            "preview_sha256": preview.get("payload_sha256"),
            "answers": [by_sha[key] for key in sorted(by_sha)],
            "network_performed": bool(network_performed)}
    artifact["answers_digest"] = _answers_digest(artifact)
    return artifact


def execute_presence_preview(
    preview: Mapping[str, Any],
    approved_payload_sha256: str,
    *,
    approved_approval_sha256: str,
    transport: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
    token: str | None = None,
    max_workers: int = 2,
    checkpoint: str | Path | None = None,
    code_evidence_repo: str | Path | None = None,
) -> dict[str, Any]:
    """Execute an explicitly approved preview with bounded workers/resume.

    Uncertain request identities remain uncertain on resume and are never
    retried implicitly. Checkpoints contain only IDs/statuses/sanitized answers.
    """
    if max_workers < 1 or max_workers > 8:
        raise JgError("max_workers must be between 1 and 8")
    requests = preview.get("requests")
    if (preview.get("kind") != "branch-presence-preview" or preview.get("no_store") is not True
            or preview.get("network_performed") is not False or not isinstance(requests, list)):
        raise JgError("execution requires a transient local branch-presence preview")
    payload_sha = digest(requests)
    if payload_sha != approved_payload_sha256 or preview.get("payload_sha256") != payload_sha:
        raise JgError("approved presence payload digest does not match preview")
    try:
        exact_bytes = base64.b64decode(preview.get("request_bytes_base64", ""), validate=True)
    except Exception:
        raise JgError("presence preview lacks valid exact request bytes") from None
    if exact_bytes != canonical_json(requests) or len(exact_bytes) != preview.get("payload_bytes"):
        raise JgError("presence exact request bytes do not match request records")
    if preview.get("model_settings_digest") != digest(preview.get("model_settings")):
        raise JgError("presence model settings digest is invalid")
    approval_sha = digest({"payload_sha256": payload_sha,
                           "plan_digest": preview.get("plan_digest"),
                           "request_count": len(requests)})
    if preview.get("approval_sha256") != approval_sha or approved_approval_sha256 != approval_sha:
        raise JgError("approved presence manifest digest does not match preview")
    budgets = preview.get("request_budgets")
    if not isinstance(budgets, Mapping) or budgets.get("token_budget_established") is not True:
        raise JgError("presence provider token budget must be established before dispatch")
    if (not isinstance(budgets.get("estimated_input_tokens"), int)
            or not isinstance(budgets.get("max_provider_tokens"), int)
            or budgets["estimated_input_tokens"] > budgets["max_provider_tokens"]):
        raise JgError("presence provider token estimate exceeds its approved budget")
    if preview.get("payload_bytes") != len(canonical_json(requests)):
        raise JgError("presence preview byte count does not match payload")
    if (not isinstance(budgets.get("max_groups"), int) or len(requests) > budgets["max_groups"]
            or not isinstance(budgets.get("max_request_bytes"), int)
            or preview["payload_bytes"] > budgets["max_request_bytes"]):
        raise JgError("presence request exceeds its approved count or byte budget")
    actual_transport = transport
    if actual_transport is None:
        if not token:
            raise JgError("presence execution requires an explicitly supplied transport token")
        actual_transport = _default_transport
    settings = preview.get("model_settings")
    if not isinstance(settings, Mapping):
        raise JgError("presence preview lacks model settings")
    if any(request.get("model") != settings.get("model") for request in requests):
        raise JgError("presence request model differs from approved model settings")
    if actual_transport is _default_transport and any(
        settings.get(field) is not None
        for field in ("reasoning_effort", "max_output_tokens", "temperature", "seed")
    ):
        raise JgError("the pooled Jev SDK adapter does not support the requested model settings")
    if not token:
        raise JgError("presence execution requires an explicitly supplied transport token")

    ledger = {"kind": "branch-presence-execution", "schema_version": 1,
              "origin": "jev",
              "preview_sha256": payload_sha, "question_version": PRESENCE_QUESTION_VERSION,
              "snapshot_digest": preview.get("snapshot_digest"),
              "contributions_digest": preview.get("contributions_digest"),
              "groups_digest": preview.get("groups_digest"),
              "request_budgets": dict(budgets),
              "model_settings_digest": preview.get("model_settings_digest"),
              "network_performed": False, "attempts": [], "answers": []}
    if checkpoint is not None and Path(checkpoint).exists():
        ledger = read_json(checkpoint)
        if (ledger.get("kind") != "branch-presence-execution" or ledger.get("preview_sha256") != payload_sha
                or ledger.get("question_version") != PRESENCE_QUESTION_VERSION
                or ledger.get("contributions_digest") != preview.get("contributions_digest")
                or ledger.get("groups_digest") != preview.get("groups_digest")):
            raise JgError("presence checkpoint belongs to another preview")
        allowed_attempt_fields = {"request_sha256", "group_id", "status", "started_at", "completed_at",
                                  "error_class", "model", "usage"}
        allowed_answer_fields = {"request_sha256", "group_id", "contribution_bindings", "response"}
        if not isinstance(ledger.get("attempts"), list) or not isinstance(ledger.get("answers"), list):
            raise JgError("presence checkpoint has malformed attempt or answer lists")
        valid_shas = {digest(request) for request in requests}
        attempt_ids = set()
        for attempt in ledger["attempts"]:
            if (not isinstance(attempt, Mapping) or set(attempt) - allowed_attempt_fields
                    or attempt.get("request_sha256") not in valid_shas
                    or attempt.get("request_sha256") in attempt_ids
                    or attempt.get("status") not in {"uncertain", "succeeded"}):
                raise JgError("presence checkpoint contains invalid or non-sanitized attempt data")
            attempt_ids.add(attempt["request_sha256"])
        answer_ids = set()
        request_index = {digest(request): request for request in requests}
        for answer in ledger["answers"]:
            if (not isinstance(answer, Mapping) or set(answer) - allowed_answer_fields
                    or answer.get("request_sha256") not in attempt_ids
                    or answer.get("request_sha256") in answer_ids):
                raise JgError("presence checkpoint contains invalid or non-sanitized answers")
            _validate_sanitized_answers(answer.get("contribution_bindings", []), answer.get("response", {}))
            if answer.get("contribution_bindings") != _request_bindings(request_index[answer["request_sha256"]]):
                raise JgError("presence checkpoint bindings differ from the approved request")
            answer_ids.add(answer["request_sha256"])
        if any(attempt.get("status") == "succeeded" for attempt in ledger["attempts"] if attempt["request_sha256"] not in answer_ids):
            raise JgError("presence checkpoint is missing a successful answer")
    attempted = {item.get("request_sha256"): item for item in ledger.get("attempts", [])}
    allowed = [request for request in requests if digest(request) not in attempted]
    known_uncertain = {key for key, item in attempted.items() if item.get("status") == "uncertain"}
    if known_uncertain:
        # This is an operator-visible stop; the remote service may have accepted
        # these requests and replay would duplicate a disclosure/call.
        raise JgError("presence checkpoint has uncertain requests; reconcile them before resuming")

    request_by_sha = {digest(request): request for request in allowed}
    for request_sha, request in request_by_sha.items():
        ledger["attempts"].append({"request_sha256": request_sha, "group_id": request["state"].get("group_id"),
                                   "status": "uncertain", "started_at": datetime.now(UTC).isoformat()})
    if checkpoint is not None:
        write_json(Path(checkpoint), ledger)

    def invoke(item: tuple[str, dict[str, Any]]) -> tuple[str, Any, str | None, bool]:
        request_sha, request = item
        dispatched = False
        try:
            for contribution in request.get("state", {}).get("contributions", []):
                evidence = contribution.get("evidence")
                if evidence is not None:
                    if code_evidence_repo is None:
                        raise JgError("presence code evidence requires immediate pin revalidation")
                    revalidate_two_sided_evidence(code_evidence_repo, evidence)
            dispatched = True
            response = actual_transport(request, token)
            validate_response(request, response)
            return request_sha, response, None, dispatched
        except Exception:
            return request_sha, None, "transport_or_validation_error", dispatched

    ledger["network_performed"] = bool(ledger.get("network_performed"))
    elapsed_started = monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        outcomes = list(pool.map(invoke, request_by_sha.items()))
    attempts = {item["request_sha256"]: item for item in ledger["attempts"]}
    for request_sha, response, error_class, dispatched in outcomes:
        ledger["network_performed"] = bool(ledger["network_performed"] or dispatched)
        attempt = attempts[request_sha]
        if error_class:
            attempt["error_class"] = error_class
            continue
        attempt.update({"status": "succeeded", "completed_at": datetime.now(UTC).isoformat(),
                        "model": response["model"], "usage": dict(response["usage"])})
        ledger["answers"].append({"request_sha256": request_sha,
                                  "group_id": request_by_sha[request_sha]["state"].get("group_id"),
                                  "contribution_bindings": _request_bindings(request_by_sha[request_sha]),
                                  "response": _sanitize_response(request_by_sha[request_sha], response)})
    prior_actual = ledger.get("actual_budgets", {})
    prior_wall = prior_actual.get("wall_time_seconds", 0) if isinstance(prior_actual, Mapping) else 0
    ledger["actual_budgets"] = {
        "attempted_requests": len(ledger["attempts"]),
        "successful_requests": len(ledger["answers"]),
        "input_tokens": sum(item["response"]["usage"]["input_tokens"] for item in ledger["answers"]),
        "output_tokens": sum(item["response"]["usage"]["output_tokens"] for item in ledger["answers"]),
        "wall_time_seconds": prior_wall + monotonic() - elapsed_started,
    }
    if checkpoint is not None:
        write_json(Path(checkpoint), ledger)
    ledger["answers_digest"] = _answers_digest(ledger)
    return ledger


def reconcile_presence(
    contributions: Mapping[str, Any],
    groups: Mapping[str, Any],
    answer_artifact: Mapping[str, Any],
    *,
    origin: str | None = None,
) -> dict[str, Any]:
    """Deduplicate and deterministically reconcile overlap and contradictions."""
    if contributions.get("kind") != "contributions" or groups.get("kind") != "contribution-groups":
        raise JgError("presence reconciliation requires pinned contribution and group artifacts")
    if groups.get("contributions_digest") != contributions.get("contributions_digest"):
        raise JgError("presence groups do not match contributions")
    if contributions.get("contributions_digest") != digest({k: v for k, v in contributions.items() if k != "contributions_digest"}):
        raise JgError("presence contributions digest is invalid")
    if groups.get("groups_digest") != digest({k: v for k, v in groups.items() if k != "groups_digest"}):
        raise JgError("presence groups digest is invalid")
    if answer_artifact.get("question_version") != PRESENCE_QUESTION_VERSION:
        raise JgError("presence answers use an unsupported question version")
    artifact_origin = answer_artifact.get("origin")
    if origin is not None and origin != artifact_origin:
        raise JgError("answer origin override conflicts with importer-assigned provenance")
    effective_origin = artifact_origin
    if effective_origin not in {"jev", "synthetic", "control"}:
        raise JgError("presence answer origin must be explicit")
    if answer_artifact.get("kind") not in {"branch-presence-answers", "branch-presence-execution"}:
        raise JgError("unsupported presence answer artifact")
    if effective_origin == "jev" and answer_artifact.get("kind") != "branch-presence-execution":
        raise JgError("JeV provenance can only come from the approved executor")
    if effective_origin in {"synthetic", "control"} and answer_artifact.get("kind") != "branch-presence-answers":
        raise JgError("synthetic/control provenance requires the strict offline importer")
    if answer_artifact.get("answers_digest") != _answers_digest(answer_artifact):
        raise JgError("presence answer digest is invalid")
    for field in ("snapshot_digest", "contributions_digest", "groups_digest"):
        if answer_artifact.get(field) != ({"snapshot_digest": contributions.get("snapshot_digest"),
                "contributions_digest": contributions.get("contributions_digest"),
                "groups_digest": groups.get("groups_digest")}[field]):
            raise JgError(f"presence answers do not match pinned {field}")
    records_by_sha = {}
    for req_record in answer_artifact.get("answers", []):
        request_sha = req_record.get("request_sha256")
        if not isinstance(request_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", request_sha):
            raise JgError("presence answer record lacks a request digest")
        if request_sha in records_by_sha and digest(records_by_sha[request_sha]) != digest(req_record):
            raise JgError("conflicting duplicate presence answer")
        records_by_sha[request_sha] = req_record
    units = {item.get("id"): item for item in contributions.get("units", [])}
    rows_by_id: dict[str, list[dict[str, Any]]] = {}
    for request_sha, record in records_by_sha.items():
        response = record.get("response")
        bindings = record.get("contribution_bindings")
        if not isinstance(bindings, list) or not isinstance(response, Mapping):
            raise JgError("presence answer lacks sanitized contribution bindings")
        answers = response.get("answers", {})
        _validate_sanitized_answers(bindings, response)
        for contribution in bindings:
            cid = contribution.get("contribution_id")
            if cid not in units:
                raise JgError("presence request names an unknown contribution")
            prefix = f"{cid}:"
            suff = _bool_signal(_answer_value(answers, prefix + "evidence_sufficient", "noul"))
            presence = _answer_value(answers, prefix + "presence", "choice")
            delta = _bool_signal(_answer_value(answers, prefix + "usable_delta", "noul"))
            dependency_context_sufficient = (
                _bool_signal(_answer_value(answers, prefix + "dependency_context_sufficient", "noul"))
                if "dependency_context_status" in contribution else None
            )
            deps = []
            for edge in contribution.get("dependency_edges", []):
                edge_id = edge.get("id")
                relevant = _bool_signal(_answer_value(answers, prefix + "dependency:" + edge_id, "noul"))
                deps.append({"edge_id": edge_id, "neighbor_id": edge.get("neighbor_id"), "relevant": relevant})
            evidence_ids = [item for item in contribution.get("evidence_ids", []) if isinstance(item, str)]
            rows_by_id.setdefault(cid, []).append({"presence": presence if presence in PRESENCE_CHOICES else "UNKNOWN",
                "evidence_sufficient": suff is True, "_suff": suff, "usable_delta": delta,
                "context_complete": contribution.get("context_complete") is True,
                "context_limitations": contribution.get("context_limitations", []),
                "comparison_context_complete": contribution.get("comparison_context_complete") is True,
                "comparison_context_limitations": contribution.get("comparison_context_limitations", []),
                "dependency_context_status": contribution.get("dependency_context_status", "unknown"),
                "dependency_context_limitations": contribution.get("dependency_context_limitations", []),
                "dependency_context_sufficient": dependency_context_sufficient,
                "dependencies": deps, "evidence_ids": evidence_ids, "request_sha256": request_sha,
                "group_id": record.get("group_id")})

    result_rows = []
    for cid in sorted(units):
        observations = rows_by_id.get(cid, [])
        reasons: set[str] = set()
        evidence_ids: set[str] = set()
        dependencies_by_edge: dict[str, set[bool | None]] = {}
        for obs in observations:
            reasons.add("evidence_sufficient" if obs["_suff"] is True else
                        "evidence_insufficient" if obs["_suff"] is False else "evidence_sufficiency_uncertain")
            evidence_ids.update(obs["evidence_ids"])
            for dependency in obs["dependencies"]:
                dependencies_by_edge.setdefault(dependency["edge_id"], set()).add(dependency["relevant"])
        presences = {item["presence"] for item in observations}
        deltas = {item["usable_delta"] for item in observations}
        sufficiencies = {item["_suff"] for item in observations}
        contradiction = len(presences - {"UNKNOWN"}) > 1 or len(deltas - {None}) > 1 or len(sufficiencies - {None}) > 1
        if not observations:
            reasons.add("answer_missing")
        if contradiction:
            reasons.add("overlapping_answers_contradict")
        if any(len(values - {None}) > 1 for values in dependencies_by_edge.values()):
            reasons.add("dependency_answers_contradict")
            contradiction = True
        sufficient = bool(observations) and all(item["_suff"] is True for item in observations)
        presence = next(iter(presences)) if len(presences) == 1 else "UNKNOWN"
        model_delta = next(iter(deltas)) if len(deltas) == 1 else None
        comparison_context_complete = bool(observations) and all(
            item["comparison_context_complete"] for item in observations
        )
        dependency_statuses = {item["dependency_context_status"] for item in observations}
        dependency_context_status = (
            "complete" if observations and dependency_statuses == {"complete"}
            else "incomplete" if "incomplete" in dependency_statuses
            else "unknown"
        )
        dependency_context_sufficient = (
            True if observations and all(item["dependency_context_sufficient"] is True for item in observations)
            else False if any(item["dependency_context_sufficient"] is False for item in observations)
            else None
        )
        delta = model_delta if (
            dependency_context_status == "complete" and dependency_context_sufficient is True
        ) else None
        dependencies = [{"edge_id": edge_id,
                         "relevant": next(iter(values)) if len(values) == 1 else None}
                        for edge_id, values in sorted(dependencies_by_edge.items())]
        if not sufficient:
            reasons.add("insufficient_evidence_unresolved")
        if observations and not comparison_context_complete:
            reasons.add("comparison_context_incomplete")
        if dependency_context_status == "unknown":
            reasons.add("dependency_context_unknown")
        elif dependency_context_status == "incomplete":
            reasons.add("dependency_context_incomplete")
        if dependency_context_status == "complete" and dependency_context_sufficient is not True:
            reasons.add("dependency_context_insufficient")
        if presence == "UNKNOWN":
            reasons.add("presence_unknown")
        if (contradiction or not sufficient or not comparison_context_complete
                or dependency_context_status != "complete" or dependency_context_sufficient is not True
                or presence == "UNKNOWN" or delta is None):
            disposition = "UNRESOLVED"
        elif delta is True and presence in {"PARTIAL", "ABSENT"}:
            disposition = "USABLE_WORK_REMAINS"
        elif presence == "PRESENT" and delta is False:
            disposition = "LIKELY_PRESERVED"
        else:
            disposition = "UNRESOLVED"
            reasons.add("presence_delta_disagreement")
        routeable = effective_origin == "jev" and disposition != "UNRESOLVED"
        suff_value = True if sufficient else False if any(item["_suff"] is False for item in observations) else None
        result_rows.append({"contribution_id": cid, "disposition": disposition,
                            "presence": presence, "evidence_sufficient": suff_value,
                            "usable_delta": delta, "model_usable_delta": model_delta,
                            "comparison_context_complete": comparison_context_complete,
                            "comparison_context_limitations": sorted({reason for item in observations
                                for reason in item["comparison_context_limitations"]}),
                            "dependency_context_status": dependency_context_status,
                            "dependency_context_sufficient": dependency_context_sufficient,
                            "dependency_context_limitations": sorted({reason for item in observations
                                for reason in item["dependency_context_limitations"]}),
                            "group_context_complete": bool(observations) and all(item["context_complete"] for item in observations),
                            "group_context_limitations": sorted({reason for item in observations
                                for reason in item["context_limitations"]}),
                            "reasons": sorted(reasons),
                            "dependencies": dependencies, "evidence_ids": sorted(evidence_ids),
                            "answer_request_ids": sorted({item["request_sha256"] for item in observations}),
                            "routing_scope": "production_review_candidate" if routeable else "advisory_only"})
    result = {"kind": "branch-presence-result", "schema_version": 1,
              "schema": RESULT_SCHEMA, "question_version": PRESENCE_QUESTION_VERSION,
              "origin": effective_origin, "snapshot_digest": contributions.get("snapshot_digest"),
              "groups_digest": groups.get("groups_digest"),
              "contributions_digest": contributions.get("contributions_digest"),
              "contributions": result_rows, "network_performed": bool(answer_artifact.get("network_performed"))}
    result["presence_digest"] = digest(result)
    return result


def validate_outcome_presence(presence: Mapping[str, Any], contributions: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate parent ledger input and expose only production-eligible rows."""
    if presence.get("kind") != "branch-presence-result" or presence.get("schema") != RESULT_SCHEMA:
        raise JgError("outcome presence has an unsupported schema")
    if presence.get("presence_digest") != digest({k: v for k, v in presence.items() if k != "presence_digest"}):
        raise JgError("outcome presence digest is invalid")
    if presence.get("origin") not in {"jev", "synthetic", "control"}:
        raise JgError("outcome presence origin is invalid")
    if contributions.get("contributions_digest") != digest({k: v for k, v in contributions.items() if k != "contributions_digest"}):
        raise JgError("outcome contribution digest is invalid")
    if (presence.get("contributions_digest") != contributions.get("contributions_digest")
            or presence.get("snapshot_digest") != contributions.get("snapshot_digest")):
        raise JgError("outcome presence does not match pinned contributions")
    units = {unit.get("id") for unit in contributions.get("units", [])}
    rows = presence.get("contributions")
    if not isinstance(rows, list):
        raise JgError("outcome presence contributions must be an array")
    result = {}
    for row in rows:
        cid = row.get("contribution_id") if isinstance(row, Mapping) else None
        if cid not in units or cid in result:
            raise JgError("outcome presence has an unknown or duplicate contribution")
        if row.get("disposition") not in {"LIKELY_PRESERVED", "USABLE_WORK_REMAINS", "UNRESOLVED"}:
            raise JgError("outcome presence has an invalid disposition")
        if row.get("presence") not in PRESENCE_CHOICES or not isinstance(row.get("reasons"), list):
            raise JgError("outcome presence row has invalid typed fields")
        if presence.get("origin") in {"synthetic", "control"} and row.get("routing_scope") != "advisory_only":
            raise JgError("synthetic/control evidence cannot route production preservation work")
        if row.get("routing_scope") == "production_review_candidate" and presence.get("origin") not in {"jev", "owner_review"}:
            raise JgError("non-production answer origin cannot route preservation work")
        if row.get("routing_scope") == "production_review_candidate":
            if (presence.get("origin") != "jev" or row.get("evidence_sufficient") is not True
                    or row.get("disposition") == "UNRESOLVED"):
                raise JgError("presence row does not meet production review routing gates")
            if (row.get("comparison_context_complete") is not True
                    or row.get("dependency_context_status") != "complete"
                    or row.get("dependency_context_sufficient") is not True):
                raise JgError("presence row lacks complete comparison and dependency context")
            if row.get("disposition") == "LIKELY_PRESERVED" and not (
                row.get("presence") == "PRESENT" and row.get("usable_delta") is False
            ):
                raise JgError("likely-preserved row has inconsistent typed answers")
            if row.get("disposition") == "USABLE_WORK_REMAINS" and not (
                row.get("presence") in {"PARTIAL", "ABSENT"} and row.get("usable_delta") is True
            ):
                raise JgError("usable-work row has inconsistent typed answers")
        result[cid] = dict(row)
    return {cid: row for cid, row in result.items() if row.get("routing_scope") == "production_review_candidate"}
