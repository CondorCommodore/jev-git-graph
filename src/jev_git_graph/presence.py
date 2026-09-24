"""Typed branch-presence request execution, import, and deterministic routing."""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Mapping

from .errors import JgError
from .group_requests import revalidate_source_only_evidence, revalidate_two_sided_evidence
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


def _project_utility_assessment(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize typed relevance without claiming measured or actionable utility."""
    goal_rows = [row for row in rows if "project_goal_context_conflict" in row]
    if not goal_rows:
        return {"status": "UNKNOWN", "reason": "project_requirements_not_provided",
                "source": "code_only_presence_review"}
    contexts = {(row.get("project_goal_digest"), row.get("project_goal_version"))
                for row in goal_rows}
    if len(contexts) != 1 or any(row.get("project_goal_context_conflict") for row in goal_rows):
        return {"status": "UNKNOWN", "reason": "project_goal_context_conflict",
                "source": "typed_project_relevance_review"}
    goal_digest, goal_version = next(iter(contexts))
    relevant_count = sum(row.get("project_relevance") is True for row in goal_rows)
    not_relevant_count = sum(row.get("project_relevance") is False for row in goal_rows)
    unknown_count = len(goal_rows) - relevant_count - not_relevant_count
    reviewable = any(row.get("evidence_sufficient") is True
                     and row.get("comparison_context_complete") is True
                     and row.get("project_relevance") is not None for row in goal_rows)
    return {
        "status": "ADVISORY_RELEVANCE_AVAILABLE" if reviewable else "UNKNOWN",
        "reason": ("relevance_is_not_measured_utility" if reviewable
                   else "comparison_or_source_evidence_insufficient"),
        "source": "typed_project_relevance_review",
        "project_goal_digest": goal_digest,
        "project_goal_version": goal_version,
        "relevance_counts": {"relevant": relevant_count,
                             "not_relevant": not_relevant_count,
                             "unknown": unknown_count},
    }


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
            _validate_response_scope(request, response)
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
        project_purpose = state.get("project_purpose")
        if project_purpose is not None:
            goal_digest = project_purpose.get("sha256") if isinstance(project_purpose, Mapping) else None
            goal_version = project_purpose.get("version") if isinstance(project_purpose, Mapping) else None
            if not isinstance(goal_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", goal_digest):
                raise JgError("presence binding has an invalid project goal digest")
            if goal_version != "project-purpose-v1":
                raise JgError("presence binding has an unsupported project goal version")
            binding["project_goal_digest"] = goal_digest
            binding["project_goal_version"] = goal_version
        evidence = item.get("evidence") or {}
        if evidence.get("kind") == "branch-presence-source-only-evidence":
            binding["presence_scope"] = "bounded_source_unit_usable_delta_only"
            binding["destination_presence"] = "unknown"
            binding["integration_readiness"] = "unresolved"
            binding["project_decisions"] = "not_assessed"
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


def _validate_response_scope(request: Mapping[str, Any], response: Mapping[str, Any]) -> None:
    """Fail closed if source-only evidence is converted into a destination claim."""
    questions = request.get("questions", {})
    for item in request.get("state", {}).get("contributions", []):
        evidence = item.get("evidence") or {}
        if evidence.get("kind") != "branch-presence-source-only-evidence":
            continue
        if item.get("destination_ids") or any("destination" in record for record in evidence.get("records", [])):
            raise JgError("source-only response has destination evidence")
        answers = response.get("answers", {})
        cid = item.get("contribution_id")
        presence = answers.get(f"{cid}:presence", {})
        if presence.get("choice") != "UNKNOWN":
            raise JgError("source-only response must keep destination presence UNKNOWN")
        dependency = answers.get(f"{cid}:dependency_context_sufficient", {})
        if f"{cid}:dependency_context_sufficient" in questions and _probability(dependency.get("noul")) is None:
            raise JgError("source-only response has an invalid dependency status")
        if f"{cid}:dependency_context_sufficient" in questions and dependency["noul"] > _FALSE:
            raise JgError("source-only response must leave integration readiness unresolved")
        if any(key.startswith(f"{cid}:dependency:") for key in questions):
            raise JgError("source-only response cannot assess dependency integration edges")


def _write_checkpoint(path: Path, ledger: Mapping[str, Any]) -> None:
    """Durably replace a sanitized checkpoint without exposing partial JSON."""
    path = path.expanduser().absolute()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise JgError("presence checkpoint directory must be owner-only")
    if path.is_symlink():
        raise JgError("presence checkpoint must not be a symlink")
    data = json.dumps(ledger, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _answers_digest(artifact: Mapping[str, Any]) -> str:
    return digest({key: value for key, value in artifact.items()
                   if key not in {"answers_digest", "execution_receipt"}})


def _presence_key_path() -> Path:
    return Path.home() / ".local" / "share" / "jev-git-graph" / "presence-execution.key"


def _presence_key(*, create: bool) -> bytes:
    """Load the owner-only local receipt key; it is not a provider credential."""
    path = _presence_key_path()
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif not path.parent.exists():
        raise JgError("trusted presence execution key is unavailable")
    if path.parent.stat().st_mode & 0o077:
        raise JgError("presence receipt key directory must be owner-only")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if not create:
            raise JgError("trusted presence execution key is unavailable") from None
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            fd = os.open(path, flags)
        else:
            key = os.urandom(32)
            try:
                os.write(fd, key)
                os.fsync(fd)
            finally:
                os.close(fd)
            return key
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size != 32):
            raise JgError("trusted presence execution key has unsafe ownership or permissions")
        key = os.read(fd, 33)
    finally:
        os.close(fd)
    if len(key) != 32:
        raise JgError("trusted presence execution key is malformed")
    return key


def _local_signature(payload: Mapping[str, Any], *, create_key: bool = False) -> str:
    return hmac.new(_presence_key(create=create_key), canonical_json(payload), hashlib.sha256).hexdigest()


def _signed_record(kind: str, payload: Mapping[str, Any], *, create_key: bool = False) -> dict[str, Any]:
    record = {"kind": kind, **dict(payload)}
    record["signature"] = _local_signature(record, create_key=create_key)
    return record


def _verify_signed_record(record: Any, kind: str) -> bool:
    if not isinstance(record, Mapping) or record.get("kind") != kind:
        return False
    signature = record.get("signature")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        return False
    unsigned = {key: value for key, value in record.items() if key != "signature"}
    try:
        expected = _local_signature(unsigned)
    except JgError:
        return False
    return hmac.compare_digest(signature, expected)


def _seal_checkpoint(ledger: dict[str, Any], *, create_key: bool = False) -> None:
    body = {key: value for key, value in ledger.items() if key != "checkpoint_signature"}
    ledger["checkpoint_signature"] = _local_signature(body, create_key=create_key)


def _verify_checkpoint(ledger: Mapping[str, Any]) -> bool:
    signature = ledger.get("checkpoint_signature")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        return False
    body = {key: value for key, value in ledger.items() if key != "checkpoint_signature"}
    try:
        expected = _local_signature(body)
    except JgError:
        return False
    return hmac.compare_digest(signature, expected)


def verify_trusted_presence_result(result: Mapping[str, Any]) -> bool:
    """Verify the locally attested normalized Jev result; public hashes are insufficient."""
    if result.get("origin") != "jev" or not _verify_signed_record(
        result.get("trusted_provenance"), "branch-presence-reconciliation-receipt"
    ):
        return False
    if result.get("presence_digest") != digest({key: value for key, value in result.items()
                                                if key != "presence_digest"}):
        return False
    provenance = result["trusted_provenance"]
    body = {key: value for key, value in result.items()
            if key not in {"presence_digest", "trusted_provenance"}}
    return provenance.get("result_body_sha256") == digest(body)


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
        has_goal_digest = "project_goal_digest" in binding
        has_goal_version = "project_goal_version" in binding
        if has_goal_digest != has_goal_version:
            raise JgError("presence answer has incomplete project goal binding")
        if has_goal_digest:
            if (not isinstance(binding.get("project_goal_digest"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", binding["project_goal_digest"])
                    or binding.get("project_goal_version") != "project-purpose-v1"):
                raise JgError("presence answer has an invalid project goal binding")
            expected[prefix + "project_relevance"] = "noul"
        if "dependency_context_status" in binding:
            expected[prefix + "dependency_context_sufficient"] = "noul"
        for edge in binding.get("dependency_edges", []):
            edge_id = edge.get("id")
            if not isinstance(edge_id, str) or not edge_id:
                raise JgError("presence answer has an invalid dependency binding")
            # Source-only requests intentionally carry dependency edges as
            # bounded metadata but do not ask the model to judge them. Keep
            # those edges in the reconciled result as unknown; requiring
            # answers here would make an exact source-only replay impossible.
            if binding.get("presence_scope") != "bounded_source_unit_usable_delta_only":
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


def _safe_failure_metadata(exc: Exception, stage: str) -> dict[str, Any]:
    """Return fixed, non-sensitive diagnostics; never serialize exception text."""
    mro_names = {base.__name__ for base in type(exc).__mro__}
    if stage == "sdk_transport":
        if "TypeSafeAPITimeoutError" in mro_names:
            error_class = "sdk_timeout"
        elif "TypeSafeAPIConnectionError" in mro_names:
            error_class = "sdk_connection_error"
        elif "TypeSafeAPIResponseValidationError" in mro_names:
            error_class = "sdk_response_error"
        elif "TypeSafeAPIError" in mro_names:
            error_class = "sdk_http_error"
        elif "TypeSafeError" in mro_names:
            error_class = "sdk_client_error"
        else:
            error_class = "transport_error"
    elif stage == "evidence_revalidation":
        error_class = "evidence_validation_error"
    elif stage == "response_validation":
        error_class = "response_validation_error"
    else:
        error_class = "response_scope_error"
    result: dict[str, Any] = {"failure_stage": stage, "error_class": error_class}
    if stage == "sdk_transport" and error_class == "sdk_http_error":
        attributes = getattr(exc, "__dict__", None)
        status = attributes.get("status") if isinstance(attributes, dict) else None
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            result["http_status"] = status
    return result


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
    execution_receipt_path: str | Path | None = None,
) -> dict[str, Any]:
    """Execute an explicitly approved preview with bounded workers/resume.

    Uncertain request identities remain uncertain on resume and are never
    retried implicitly. A failed request stops new scheduling; only already
    in-flight calls settle. Checkpoints list remaining request hashes explicitly.
    """
    if max_workers < 1 or max_workers > 8:
        raise JgError("max_workers must be between 1 and 8")
    if checkpoint is None:
        raise JgError("presence execution requires a durable checkpoint path")
    if Path(checkpoint).is_symlink():
        raise JgError("presence checkpoint must not be a symlink")
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
                           "request_count": len(requests),
                           "request_budgets": preview.get("request_budgets")})
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
    if preview.get("request_bytes_by_chunk") != [len(canonical_json(request)) for request in requests]:
        raise JgError("presence per-request byte diagnostics differ from exact request records")
    request_limit = budgets.get("max_request_bytes")
    aggregate_limit = budgets.get("max_aggregate_request_bytes")
    max_groups = budgets.get("max_groups")
    max_requests = budgets.get("max_requests")
    request_sizes = [len(canonical_json(request)) for request in requests]
    group_ids = {request.get("state", {}).get("group_id") for request in requests}
    if (not isinstance(request_limit, int) or request_limit < 1
            or not isinstance(aggregate_limit, int) or aggregate_limit < 1
            or not isinstance(max_groups, int) or max_groups < 1
            or not isinstance(max_requests, int) or max_requests < 1
            or len(request_sizes) != preview.get("request_count")
            or any(size > request_limit for size in request_sizes)
            or len(requests) > max_requests
            or len(group_ids) > max_groups
            or preview["payload_bytes"] > aggregate_limit):
        raise JgError("presence request exceeds its approved count or byte budget")
    trusted_sdk_executor = transport is None
    actual_transport = transport
    if trusted_sdk_executor:
        if not token:
            raise JgError("presence execution requires an explicitly supplied transport token")
        actual_transport = _default_transport
    settings = preview.get("model_settings")
    if not isinstance(settings, Mapping):
        raise JgError("presence preview lacks model settings")
    if any(request.get("model") != settings.get("model") for request in requests):
        raise JgError("presence request model differs from approved model settings")
    if trusted_sdk_executor and any(
        settings.get(field) is not None
        for field in ("reasoning_effort", "max_output_tokens", "temperature", "seed")
    ):
        raise JgError("the pooled Jev SDK adapter does not support the requested model settings")
    if not token:
        raise JgError("presence execution requires an explicitly supplied transport token")

    ledger = {"kind": "branch-presence-execution", "schema_version": 1,
              "origin": "jev" if trusted_sdk_executor else "synthetic",
              "executor": "pooled-jev-sdk-v1" if trusted_sdk_executor else "injected-advisory-transport",
              "preview_sha256": payload_sha, "approval_sha256": approval_sha,
              "question_version": PRESENCE_QUESTION_VERSION,
              "snapshot_digest": preview.get("snapshot_digest"),
              "contributions_digest": preview.get("contributions_digest"),
              "groups_digest": preview.get("groups_digest"),
              "request_budgets": dict(budgets),
              "model_settings_digest": preview.get("model_settings_digest"),
              "network_performed": False, "attempts": [], "answers": [],
              "unattempted_request_sha256s": sorted(digest(request) for request in requests)}
    if checkpoint is not None and Path(checkpoint).exists():
        ledger = read_json(checkpoint)
        if not _verify_checkpoint(ledger):
            raise JgError("presence checkpoint lacks trusted progress authentication")
        if (ledger.get("kind") != "branch-presence-execution" or ledger.get("preview_sha256") != payload_sha
                or ledger.get("approval_sha256") != approval_sha
                or ledger.get("question_version") != PRESENCE_QUESTION_VERSION
                or ledger.get("contributions_digest") != preview.get("contributions_digest")
                or ledger.get("groups_digest") != preview.get("groups_digest")
                or ledger.get("origin") != ("jev" if trusted_sdk_executor else "synthetic")
                or ledger.get("executor") != ("pooled-jev-sdk-v1" if trusted_sdk_executor else "injected-advisory-transport")):
            raise JgError("presence checkpoint belongs to another preview")
        allowed_attempt_fields = {"request_sha256", "group_id", "status", "started_at", "completed_at",
                                  "failure_stage", "error_class", "http_status", "model", "usage"}
        allowed_answer_fields = {"request_sha256", "group_id", "contribution_bindings", "response"}
        if not isinstance(ledger.get("attempts"), list) or not isinstance(ledger.get("answers"), list):
            raise JgError("presence checkpoint has malformed attempt or answer lists")
        valid_shas = {digest(request) for request in requests}
        attempt_ids = set()
        for attempt in ledger["attempts"]:
            if (not isinstance(attempt, Mapping) or set(attempt) - allowed_attempt_fields
                    or attempt.get("request_sha256") not in valid_shas
                    or attempt.get("request_sha256") in attempt_ids
                    or attempt.get("status") not in {"uncertain", "succeeded", "not_dispatched"}):
                raise JgError("presence checkpoint contains invalid or non-sanitized attempt data")
            if ("failure_stage" in attempt and attempt["failure_stage"] not in {
                    "evidence_revalidation", "sdk_transport", "response_validation", "response_scope_validation"}
                    or "error_class" in attempt and attempt["error_class"] not in {
                        "sdk_timeout", "sdk_connection_error", "sdk_response_error", "sdk_http_error",
                        "sdk_client_error", "transport_error", "evidence_validation_error",
                        "response_validation_error", "usage_unavailable", "response_scope_error"}
                    or "http_status" in attempt and (not isinstance(attempt["http_status"], int)
                        or isinstance(attempt["http_status"], bool) or not 100 <= attempt["http_status"] <= 599)):
                raise JgError("presence checkpoint contains invalid failure diagnostics")
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
            _validate_response_scope(request_index[answer["request_sha256"]], answer.get("response", {}))
            answer_ids.add(answer["request_sha256"])
        if any(attempt.get("status") == "succeeded" for attempt in ledger["attempts"] if attempt["request_sha256"] not in answer_ids):
            raise JgError("presence checkpoint is missing a successful answer")
        derived_unattempted = sorted(valid_shas - attempt_ids)
        stored_unattempted = ledger.get("unattempted_request_sha256s")
        if stored_unattempted is not None and (
                not isinstance(stored_unattempted, list)
                or any(not isinstance(item, str) for item in stored_unattempted)
                or len(stored_unattempted) != len(set(stored_unattempted))
                or sorted(stored_unattempted) != derived_unattempted):
            raise JgError("presence checkpoint has invalid unattempted request identities")
        ledger["unattempted_request_sha256s"] = derived_unattempted
    attempted = {item.get("request_sha256"): item for item in ledger.get("attempts", [])}
    request_by_sha = {digest(request): request for request in requests if digest(request) not in attempted}
    request_index = {digest(request): request for request in requests}

    def update_unattempted() -> None:
        done_ids = {item["request_sha256"] for item in ledger["attempts"]}
        ledger["unattempted_request_sha256s"] = sorted(set(request_index) - done_ids)

    def invoke(item: tuple[str, dict[str, Any]]) -> tuple[str, Any, dict[str, Any] | None, bool]:
        request_sha, request = item
        dispatched = False
        try:
            for contribution in request.get("state", {}).get("contributions", []):
                evidence = contribution.get("evidence")
                if evidence is not None:
                    if code_evidence_repo is None:
                        raise JgError("presence code evidence requires immediate pin revalidation")
                    if evidence.get("kind") == "branch-presence-source-only-evidence":
                        revalidate_source_only_evidence(code_evidence_repo, evidence)
                    elif evidence.get("kind") == "branch-presence-code-evidence":
                        revalidate_two_sided_evidence(code_evidence_repo, evidence)
                    else:
                        raise JgError("presence evidence has an unsupported validation kind")
        except Exception as exc:
            return request_sha, None, _safe_failure_metadata(exc, "evidence_revalidation"), dispatched
        dispatched = True
        try:
            response = actual_transport(request, token)
        except Exception as exc:
            return request_sha, None, _safe_failure_metadata(exc, "sdk_transport"), dispatched
        usage = response.get("usage") if isinstance(response, Mapping) else None
        if isinstance(usage, Mapping) and any(usage.get(field) is None for field in ("input_tokens", "output_tokens")):
            return request_sha, None, {"failure_stage": "response_validation", "error_class": "usage_unavailable"}, dispatched
        try:
            validate_response(request, response)
        except Exception as exc:
            return request_sha, None, _safe_failure_metadata(exc, "response_validation"), dispatched
        try:
            _validate_response_scope(request, response)
        except Exception as exc:
            return request_sha, None, _safe_failure_metadata(exc, "response_scope_validation"), dispatched
        return request_sha, response, None, dispatched

    ledger["network_performed"] = bool(ledger.get("network_performed"))
    elapsed_started = monotonic()
    attempts = {item["request_sha256"]: item for item in ledger["attempts"]}

    def persist_progress() -> None:
        prior_actual = ledger.get("actual_budgets", {})
        prior_wall = prior_actual.get("wall_time_seconds", 0) if isinstance(prior_actual, Mapping) else 0
        usage_known = all(item.get("status") != "uncertain" for item in ledger["attempts"])
        ledger["actual_budgets"] = {
            "attempted_requests": sum(item.get("status") != "not_dispatched" for item in ledger["attempts"]),
            "successful_requests": len(ledger["answers"]),
            "input_tokens": (sum(item["response"]["usage"]["input_tokens"] for item in ledger["answers"])
                             if usage_known else None),
            "output_tokens": (sum(item["response"]["usage"]["output_tokens"] for item in ledger["answers"])
                              if usage_known else None),
            "wall_time_seconds": prior_wall + monotonic() - elapsed_started,
        }
        update_unattempted()
        _seal_checkpoint(ledger)
        _write_checkpoint(Path(checkpoint), ledger)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        pending = iter(request_by_sha.items())
        futures: dict[concurrent.futures.Future, str] = {}
        stop_scheduling = False

        def submit_one() -> bool:
            try:
                item = next(pending)
            except StopIteration:
                return False
            request_sha, request = item
            attempt = {"request_sha256": request_sha, "group_id": request["state"].get("group_id"),
                       "status": "uncertain", "started_at": datetime.now(UTC).isoformat()}
            ledger["attempts"].append(attempt)
            attempts[request_sha] = attempt
            update_unattempted()
            _seal_checkpoint(ledger, create_key=True)
            _write_checkpoint(Path(checkpoint), ledger)
            futures[pool.submit(invoke, item)] = request_sha
            return True

        while len(futures) < max_workers and submit_one():
            pass
        while futures:
            done, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                request_sha = futures.pop(future)
                _, response, failure, dispatched = future.result()
                ledger["network_performed"] = bool(ledger.get("network_performed") or dispatched)
                attempt = attempts[request_sha]
                if failure:
                    attempt.update(failure)
                    if not dispatched:
                        attempt["status"] = "not_dispatched"
                    stop_scheduling = True
                else:
                    attempt.update({"status": "succeeded", "completed_at": datetime.now(UTC).isoformat(),
                                    "model": response["model"], "usage": dict(response["usage"])})
                    ledger["answers"].append({"request_sha256": request_sha,
                                              "group_id": request_by_sha[request_sha]["state"].get("group_id"),
                                              "contribution_bindings": _request_bindings(request_by_sha[request_sha]),
                                              "response": _sanitize_response(request_by_sha[request_sha], response)})
                    ledger["answers"].sort(key=lambda item: item["request_sha256"])
                persist_progress()
            if not stop_scheduling:
                while len(futures) < max_workers and submit_one():
                    pass
    prior_actual = ledger.get("actual_budgets", {})
    prior_wall = prior_actual.get("wall_time_seconds", 0) if isinstance(prior_actual, Mapping) else 0
    usage_known = all(item.get("status") != "uncertain" for item in ledger["attempts"])
    ledger["actual_budgets"] = {
        "attempted_requests": sum(item.get("status") != "not_dispatched" for item in ledger["attempts"]),
        "successful_requests": len(ledger["answers"]),
        "input_tokens": (sum(item["response"]["usage"]["input_tokens"] for item in ledger["answers"])
                         if usage_known else None),
        "output_tokens": (sum(item["response"]["usage"]["output_tokens"] for item in ledger["answers"])
                          if usage_known else None),
        "wall_time_seconds": prior_wall + monotonic() - elapsed_started,
    }
    update_unattempted()
    _seal_checkpoint(ledger)
    _write_checkpoint(Path(checkpoint), ledger)
    ledger.pop("checkpoint_signature", None)
    ledger["answers_digest"] = _answers_digest(ledger)
    if trusted_sdk_executor:
        answered = sorted(item["request_sha256"] for item in ledger["answers"])
        receipt_payload = {
            "schema_version": 1,
            "executor": "pooled-jev-sdk-v1",
            "preview_sha256": payload_sha,
            "approval_sha256": approval_sha,
            "request_sha256s": sorted(digest(request) for request in requests),
            "answered_request_sha256s": answered,
            "answers_digest": ledger["answers_digest"],
            "snapshot_digest": preview.get("snapshot_digest"),
            "contributions_digest": preview.get("contributions_digest"),
            "groups_digest": preview.get("groups_digest"),
            "model_settings_digest": preview.get("model_settings_digest"),
            "network_performed": ledger.get("network_performed") is True,
        }
        receipt = _signed_record("branch-presence-executor-receipt", receipt_payload, create_key=True)
        ledger["execution_receipt"] = receipt
        if execution_receipt_path is not None:
            write_json(Path(execution_receipt_path), receipt)
    return ledger


def reconcile_presence(
    contributions: Mapping[str, Any],
    groups: Mapping[str, Any],
    answer_artifact: Mapping[str, Any],
    *,
    origin: str | None = None,
    execution_receipt: Mapping[str, Any] | str | Path | None = None,
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
    if effective_origin == "control" and answer_artifact.get("kind") != "branch-presence-answers":
        raise JgError("control provenance requires the strict offline importer")
    if effective_origin == "synthetic" and answer_artifact.get("kind") not in {
        "branch-presence-answers", "branch-presence-execution"
    }:
        raise JgError("synthetic provenance requires the strict importer or advisory executor")
    if answer_artifact.get("answers_digest") != _answers_digest(answer_artifact):
        raise JgError("presence answer digest is invalid")
    verified_receipt: Mapping[str, Any] | None = None
    if effective_origin == "jev":
        if execution_receipt is None:
            raise JgError("JeV presence requires the separate trusted executor receipt")
        verified_receipt = read_json(execution_receipt) if isinstance(execution_receipt, (str, Path)) else execution_receipt
        # Request digest identities are carried by each sanitized answer record.
        receipt_answer_shas = sorted(item.get("request_sha256") for item in answer_artifact.get("answers", []))
        receipt_valid = _verify_signed_record(verified_receipt, "branch-presence-executor-receipt")
        if (not receipt_valid or answer_artifact.get("executor") != "pooled-jev-sdk-v1"
                or verified_receipt.get("executor") != "pooled-jev-sdk-v1"
                or verified_receipt.get("preview_sha256") != answer_artifact.get("preview_sha256")
                or verified_receipt.get("approval_sha256") != answer_artifact.get("approval_sha256")
                or verified_receipt.get("model_settings_digest") != answer_artifact.get("model_settings_digest")
                or verified_receipt.get("answers_digest") != answer_artifact.get("answers_digest")
                or verified_receipt.get("answered_request_sha256s") != receipt_answer_shas
                or len(set(receipt_answer_shas)) != len(receipt_answer_shas)
                or not set(receipt_answer_shas).issubset(set(verified_receipt.get("request_sha256s", [])))
                or verified_receipt.get("request_sha256s") != sorted(verified_receipt.get("request_sha256s", []))
                or verified_receipt.get("network_performed") is not True
                or answer_artifact.get("network_performed") is not True):
            raise JgError("trusted Jev executor receipt does not match the presence answers")
        for field in ("snapshot_digest", "contributions_digest", "groups_digest"):
            if verified_receipt.get(field) != answer_artifact.get(field):
                raise JgError("trusted Jev executor receipt does not match the pinned inputs")
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
            goal_digest = contribution.get("project_goal_digest")
            goal_version = contribution.get("project_goal_version")
            project_relevance = (
                _bool_signal(_answer_value(answers, prefix + "project_relevance", "noul"))
                if goal_digest is not None and suff is True else None
            )
            deps = []
            for edge in contribution.get("dependency_edges", []):
                edge_id = edge.get("id")
                relevant = _bool_signal(_answer_value(answers, prefix + "dependency:" + edge_id, "noul"))
                deps.append({"edge_id": edge_id, "neighbor_id": edge.get("neighbor_id"), "relevant": relevant})
            evidence_ids = [item for item in contribution.get("evidence_ids", []) if isinstance(item, str)]
            observation = {"presence": presence if presence in PRESENCE_CHOICES else "UNKNOWN",
                "evidence_sufficient": suff is True, "_suff": suff, "usable_delta": delta,
                "context_complete": contribution.get("context_complete") is True,
                "context_limitations": contribution.get("context_limitations", []),
                "comparison_context_complete": contribution.get("comparison_context_complete") is True,
                "comparison_context_limitations": contribution.get("comparison_context_limitations", []),
                "dependency_context_status": contribution.get("dependency_context_status", "unknown"),
                "dependency_context_limitations": contribution.get("dependency_context_limitations", []),
                "dependency_context_sufficient": dependency_context_sufficient,
                "dependencies": deps, "evidence_ids": evidence_ids, "request_sha256": request_sha,
                "group_id": record.get("group_id")}
            if goal_digest is not None:
                observation["project_goal_digest"] = goal_digest
                observation["project_goal_version"] = goal_version
                observation["project_relevance"] = project_relevance
            rows_by_id.setdefault(cid, []).append(observation)

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
        result_row = {"contribution_id": cid, "disposition": disposition,
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
                            "routing_scope": "production_review_candidate" if routeable else "advisory_only"}
        goal_contexts = {(item.get("project_goal_digest"), item.get("project_goal_version"))
                         for item in observations}
        if any(item.get("project_goal_digest") is not None for item in observations):
            goal_conflict = len(goal_contexts) != 1 or any(item.get("project_goal_digest") is None
                                                            for item in observations)
            goal_digest, goal_version = (next(iter(goal_contexts)) if not goal_conflict
                                         else (None, None))
            relevance_values = {item.get("project_relevance") for item in observations}
            evidence_ok = bool(observations) and all(item["_suff"] is True for item in observations)
            relevance_conflict = len(relevance_values - {None}) > 1
            relevance = (next(iter(relevance_values)) if not goal_conflict and evidence_ok
                         and not relevance_conflict and len(relevance_values) == 1 else None)
            if goal_conflict:
                reasons.add("project_goal_context_conflict")
            if relevance_conflict:
                reasons.add("project_relevance_answers_contradict")
            result_row.update({"project_goal_digest": goal_digest,
                               "project_goal_version": goal_version,
                               "project_relevance": relevance,
                               "project_goal_context_conflict": goal_conflict})
        result_row["reasons"] = sorted(reasons)
        result_rows.append(result_row)
    project_utility_assessment = _project_utility_assessment(result_rows)
    result = {"kind": "branch-presence-result", "schema_version": 1,
              "schema": RESULT_SCHEMA, "question_version": PRESENCE_QUESTION_VERSION,
              "origin": effective_origin, "snapshot_digest": contributions.get("snapshot_digest"),
              "groups_digest": groups.get("groups_digest"),
              "contributions_digest": contributions.get("contributions_digest"),
              "contributions": result_rows,
              "project_utility_assessment": project_utility_assessment,
              "network_performed": bool(answer_artifact.get("network_performed"))}
    if verified_receipt is not None:
        result_body_sha = digest(result)
        result["trusted_provenance"] = _signed_record(
            "branch-presence-reconciliation-receipt",
            {"executor_receipt_sha256": digest(verified_receipt),
             "result_body_sha256": result_body_sha},
        )
    result["presence_digest"] = digest(result)
    return result


def validate_presence_observations(
    presence: Mapping[str, Any], contributions: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Validate every normalized observation without granting it action scope.

    Advisory synthetic/control answers and unresolved Jev results must remain
    visible to reviewers. This API validates them but does not promote their
    routing scope. Consumers that create production recommendations must use
    :func:`validate_outcome_presence` instead.
    """
    if presence.get("kind") != "branch-presence-result" or presence.get("schema") != RESULT_SCHEMA:
        raise JgError("outcome presence has an unsupported schema")
    if presence.get("presence_digest") != digest({k: v for k, v in presence.items() if k != "presence_digest"}):
        raise JgError("outcome presence digest is invalid")
    if presence.get("origin") not in {"jev", "synthetic", "control"}:
        raise JgError("outcome presence origin is invalid")
    trusted_provenance = presence.get("trusted_provenance")
    if presence.get("origin") == "jev":
        if not verify_trusted_presence_result(presence):
            raise JgError("JeV outcome lacks a matching trusted reconciliation receipt")
    elif trusted_provenance is not None:
        raise JgError("advisory presence cannot carry trusted Jev provenance")
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
        goal_fields = {"project_goal_digest", "project_goal_version", "project_relevance",
                       "project_goal_context_conflict"}
        present_goal_fields = goal_fields & set(row)
        if present_goal_fields:
            if present_goal_fields != goal_fields or not isinstance(row.get("project_goal_context_conflict"), bool):
                raise JgError("outcome presence row has incomplete project relevance fields")
            if row["project_goal_context_conflict"]:
                if row.get("project_goal_digest") is not None or row.get("project_goal_version") is not None or row.get("project_relevance") is not None:
                    raise JgError("conflicting project goal contexts must remain unknown")
            elif (not isinstance(row.get("project_goal_digest"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", row["project_goal_digest"])
                    or row.get("project_goal_version") != "project-purpose-v1"
                    or (row.get("project_relevance") is not None
                        and type(row.get("project_relevance")) is not bool)):
                raise JgError("outcome presence row has invalid project relevance")
            if row.get("evidence_sufficient") is not True and row.get("project_relevance") is not None:
                raise JgError("insufficient source evidence cannot establish project relevance")
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
    return result


def validate_outcome_presence(
    presence: Mapping[str, Any], contributions: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Validate and expose only rows eligible for human preservation review."""
    observations = validate_presence_observations(presence, contributions)
    return {cid: row for cid, row in observations.items()
            if row.get("routing_scope") == "production_review_candidate"}
