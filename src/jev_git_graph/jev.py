from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import fcntl
import atexit
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Mapping

from .errors import JgError
from .questions import QUESTION_IDS, QUESTION_VERSION, relationship_questions
from .safety import canonical_json, digest, read_json, write_json
from .code_evidence import CODE_EVIDENCE_PROFILE, code_evidence_digest, revalidate_code_evidence, sanitize_provider_response, validate_code_evidence


JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
EVIDENCE_PROFILES = ("minimal", "review", CODE_EVIDENCE_PROFILE)
DEFAULT_MAX_JEV_REQUESTS = 1
DEFAULT_MAX_JEV_PAYLOAD_BYTES = 8_192


def _finite_probability(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


@contextmanager
def checkpoint_lock(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise JgError("checkpoint directory must be owner-only")
    descriptor = os.open(path.parent / ".jev.lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JgError("another Jev run holds this checkpoint") from None
        yield
    finally:
        os.close(descriptor)


def update_ledger_statistics(ledger: dict[str, Any], pricing: Any = None) -> None:
    _deduplicate_ledger(ledger)
    ledger["counts"] = {
        "attempted": len(ledger["attempts"]),
        "succeeded": sum(item["status"] == "succeeded" for item in ledger["attempts"]),
        "uncertain": sum(item["status"] == "uncertain" for item in ledger["attempts"]),
    }
    usage = [relation.get("response", {}).get("usage", {}) for relation in ledger.get("relations", [])]
    durations = [item.get("latency_ms") for item in ledger["attempts"] if isinstance(item.get("latency_ms"), int)]
    starts = [item["started_at_epoch_ms"] for item in ledger["attempts"] if isinstance(item.get("started_at_epoch_ms"), int)]
    completions = [item["completed_at_epoch_ms"] for item in ledger["attempts"] if isinstance(item.get("completed_at_epoch_ms"), int)]
    input_tokens = [item.get("input_tokens") for item in usage if isinstance(item.get("input_tokens"), int) and not isinstance(item.get("input_tokens"), bool)]
    output_tokens = [item.get("output_tokens") for item in usage if isinstance(item.get("output_tokens"), int) and not isinstance(item.get("output_tokens"), bool)]
    ledger["statistics"] = {
        "calls": len(ledger["attempts"]),
        "input_tokens": sum(input_tokens) if input_tokens else None,
        "output_tokens": sum(output_tokens) if output_tokens else None,
        "api_time_ms": sum(durations) if durations else None,
        "wall_time_ms": max(completions) - min(starts) if starts and completions else None,
        "estimated_cost_usd": None,
        "pricing_basis": None,
    }
    if pricing is not None:
        quote = estimate_cost(ledger, pricing)
        ledger["statistics"]["estimated_cost_usd"] = quote["estimated_cost_usd"]
        ledger["statistics"]["pricing_basis"] = quote["pricing_provenance"]


def _deduplicate_ledger(ledger: dict[str, Any]) -> None:
    """Deduplicate overlapping checkpoint records by their request identity."""
    unique_attempts: list[dict[str, Any]] = []
    seen_attempts: set[str] = set()
    for attempt in ledger.get("attempts", []):
        request_sha = attempt.get("request_sha256") if isinstance(attempt, dict) else None
        identity = request_sha if isinstance(request_sha, str) else digest(attempt)
        if identity in seen_attempts:
            continue
        seen_attempts.add(identity)
        unique_attempts.append(attempt)
    ledger["attempts"] = unique_attempts

    unique_relations: list[dict[str, Any]] = []
    seen_relations: set[str] = set()
    for relation in ledger.get("relations", []):
        if not isinstance(relation, dict):
            continue
        identity = relation.get("request_sha256") or relation.get("judgment_id")
        identity = identity if isinstance(identity, str) else digest(relation)
        if identity in seen_relations:
            continue
        seen_relations.add(identity)
        unique_relations.append(relation)
    ledger["relations"] = unique_relations


def _pricing_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise JgError(f"{label} must be a finite non-negative number")
    return float(value)


def parse_pricing(pricing: Any) -> dict[str, Any]:
    """Parse an offline local pricing document and retain its provenance."""
    raw = pricing
    if isinstance(pricing, (str, Path)):
        if isinstance(pricing, str) and pricing.lstrip().startswith("{"):
            try:
                raw = json.loads(pricing)
            except json.JSONDecodeError as exc:
                raise JgError("pricing JSON is malformed") from exc
        else:
            raw = read_json(pricing)
    if not isinstance(raw, dict):
        raise JgError("pricing must be a local JSON object")
    rates = raw.get("rates") if isinstance(raw.get("rates"), dict) else raw
    input_rate = next((rates.get(key) for key in ("input_usd_per_million_tokens", "input_usd_per_1m", "input_per_million_tokens") if key in rates), None)
    output_rate = next((rates.get(key) for key in ("output_usd_per_million_tokens", "output_usd_per_1m", "output_per_million_tokens") if key in rates), None)
    if input_rate is None or output_rate is None:
        raise JgError("pricing requires input and output rates per million tokens")
    provenance = raw.get("pricing_provenance") if isinstance(raw.get("pricing_provenance"), dict) else {}
    source = raw.get("source", raw.get("pricing_source", provenance.get("source")))
    if not isinstance(source, str) or not source.strip():
        raise JgError("pricing requires non-empty local source provenance")
    model = raw.get("model", JEV_MODEL)
    if not isinstance(model, str) or not model.strip():
        raise JgError("pricing model must be a non-empty string")
    pricing_digest = provenance.get("pricing_digest") or digest(raw)
    return {
        "kind": "jev-pricing",
        "schema_version": 1,
        "model": model,
        "input_usd_per_million_tokens": _pricing_number(input_rate, "pricing input rate"),
        "output_usd_per_million_tokens": _pricing_number(output_rate, "pricing output rate"),
        "pricing_provenance": {"source": source, "pricing_digest": pricing_digest, "model": model},
    }


def estimate_cost(ledger: dict[str, Any], pricing: Any) -> dict[str, Any]:
    """Estimate local cost without network access; missing usage remains unknown."""
    parsed = parse_pricing(pricing)
    statistics = ledger.get("statistics") if isinstance(ledger.get("statistics"), dict) else {}
    if "input_tokens" not in statistics or "output_tokens" not in statistics:
        update_ledger_statistics(ledger)
        statistics = ledger["statistics"]
    input_tokens = statistics.get("input_tokens")
    output_tokens = statistics.get("output_tokens")
    if not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or not isinstance(output_tokens, int) or isinstance(output_tokens, bool):
        cost = None
        unknown = True
    else:
        cost = (input_tokens * parsed["input_usd_per_million_tokens"] + output_tokens * parsed["output_usd_per_million_tokens"]) / 1_000_000
        unknown = False
    return {
        "estimated_cost_usd": cost,
        "unknown": unknown,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "pricing_provenance": parsed["pricing_provenance"],
    }


def estimate_cost_usd(ledger: dict[str, Any], pricing: Any) -> float | None:
    return estimate_cost(ledger, pricing)["estimated_cost_usd"]


def save_checkpoint(path: Path, ledger: dict[str, Any]) -> None:
    """Replace complete checkpoints atomically; persist before sending requests."""
    update_ledger_statistics(ledger)
    descriptor, name = tempfile.mkstemp(prefix=".jev-checkpoint-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(ledger))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _path_hash(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:20]


def payload_for_candidate(
    candidate: dict[str, Any],
    evidence_profile: str = "minimal",
    code_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if evidence_profile not in EVIDENCE_PROFILES:
        raise JgError(f"unknown evidence profile: {evidence_profile}")
    endpoints = candidate["endpoints"]
    evidence = candidate["evidence"]
    state: dict[str, Any] = {
        "candidate_id": candidate["id"],
        "a_tip": endpoints["a"]["tip"],
        "b_tip": endpoints["b"]["tip"],
        "signals": candidate["reasons"],
        "shared_patch_ids": evidence["shared_patch_ids"],
        "changed_path_hashes": [_path_hash(path) for path in evidence["shared_paths"]],
        "shared_subject_tokens": evidence["shared_subject_tokens"],
        "a_unique_commit_count": evidence["a_unique_commit_count"],
        "b_unique_commit_count": evidence["b_unique_commit_count"],
        "a_merge_base": evidence["a_merge_base"],
        "b_merge_base": evidence["b_merge_base"],
    }
    if evidence_profile == "review":
        state.update({
            "a_branch_label": endpoints["a"]["branch"],
            "b_branch_label": endpoints["b"]["branch"],
            "a_commit_subjects": evidence.get("a_commit_subjects", [])[:20],
            "b_commit_subjects": evidence.get("b_commit_subjects", [])[:20],
            "shared_paths": evidence.get("shared_paths", [])[:40],
            "task_ids": evidence.get("task_ids", [])[:20],
            "pr_ids": evidence.get("pr_ids", [])[:20],
        })
    if evidence_profile == CODE_EVIDENCE_PROFILE:
        approved = code_evidence if code_evidence is not None else candidate.get("code_evidence")
        if not isinstance(approved, dict):
            raise JgError("code evidence profile requires an approved code evidence record")
        validate_code_evidence(approved)
        state["code_evidence"] = approved
    for field in ("identical_tips", "a_ancestor_of_b", "b_ancestor_of_a", "a_commits_not_in_b", "b_commits_not_in_a"):
        if field in evidence:
            state[field] = evidence[field]
    return {
        "state": state,
        "model": JEV_MODEL,
        "questions": relationship_questions(),
    }


def build_preview(
    candidates: dict[str, Any],
    evidence_profile: str = "minimal",
    code_evidence_by_candidate: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    requests = [
        payload_for_candidate(
            candidate,
            evidence_profile,
            (code_evidence_by_candidate or {}).get(candidate.get("id")),
        )
        for candidate in candidates.get("candidates", [])
    ]
    preview = {
        "kind": "jev-preview",
        "endpoint": JEV_ENDPOINT,
        "repository_id": candidates.get("repository_id"),
        "candidate_content_digest": candidates.get("content_digest"),
        "question_version": QUESTION_VERSION,
        "evidence_profile": evidence_profile,
        "request_count": len(requests),
        "requests": requests,
        "payload_sha256": digest(requests),
        "payload_bytes": len(canonical_json(requests)),
        "network_performed": False,
    }
    if evidence_profile == CODE_EVIDENCE_PROFILE:
        records = [
            (code_evidence_by_candidate or {}).get(candidate.get("id"), candidate.get("code_evidence"))
            for candidate in candidates.get("candidates", [])
        ]
        digests = [code_evidence_digest(record) for record in records if isinstance(record, dict)]
        preview.update({
            "storage": "transient",
            "no_store": True,
            "code_evidence_sha256": digests[0] if len(digests) == 1 else digest(sorted(digests)),
        })
    return preview


def write_preview(candidates_path: str | Path, output: str | Path, evidence_profile: str = "minimal") -> Path:
    if evidence_profile == CODE_EVIDENCE_PROFILE:
        raise JgError("code evidence previews are transient and cannot be written to disk")
    candidates = read_json(candidates_path)
    preview = build_preview(candidates, evidence_profile)
    target = Path(output).expanduser().resolve() / "jev-preview.json"
    write_json(target, preview)
    return target


class _TypeSafeTransport:
    """Reuse one official SDK client for a process and never retry a Jev call."""

    def __init__(self) -> None:
        self._client = None
        self._token = None

    def _build_client(self, token: str):
        from typesafe_sdk import RetryPolicy, TypeSafeClient

        return TypeSafeClient(api_key=token, retry=RetryPolicy(max_retries=0), timeout=30)

    def prepare(self, token: str) -> None:
        """Load the local SDK and initialize its client before recording dispatch."""
        if not token:
            raise JgError("Jev credential is unavailable")
        if self._client is None:
            self._client = self._build_client(token)
            self._token = token
        elif token != self._token:
            raise JgError("Jev client cannot change credentials during a run")

    def __call__(self, payload: dict[str, Any], token: str) -> dict[str, Any]:
        from typesafe_sdk import RetryPolicy

        if not token:
            raise JgError("Jev credential is unavailable")
        if self._client is None:
            self._client = self._build_client(token)
            self._token = token
        elif token != self._token:
            raise JgError("Jev client cannot change credentials during a run")

        sdk_logger = logging.getLogger("typesafe_sdk")
        was_disabled = sdk_logger.disabled
        sdk_logger.disabled = True
        try:
            response = self._client.system_one(
                state=payload["state"],
                questions=payload["questions"],
                model=payload["model"],
                retry=RetryPolicy(max_retries=0),
            )
            return response.model_dump(mode="json")
        finally:
            sdk_logger.disabled = was_disabled

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._token = None


_default_transport = _TypeSafeTransport()
atexit.register(_default_transport.close)


class _ResponseValidationError(Exception):
    pass


def validate_response(request: dict[str, Any], response: Any) -> dict[str, Any]:
    """Validate the provider contract without retaining invalid response content."""
    if not isinstance(response, dict) or not isinstance(response.get("model"), str):
        raise _ResponseValidationError
    usage = response.get("usage")
    if not isinstance(usage, dict):
        raise _ResponseValidationError
    for field in ("input_tokens", "output_tokens"):
        if not isinstance(usage.get(field), int) or isinstance(usage.get(field), bool) or usage[field] < 0:
            raise _ResponseValidationError
    answers = response.get("answers")
    questions = request.get("questions")
    if not isinstance(answers, dict) or not isinstance(questions, dict) or set(answers) != set(questions):
        raise _ResponseValidationError
    for question_id, question in questions.items():
        answer = answers.get(question_id)
        if not isinstance(answer, dict):
            raise _ResponseValidationError
        if question.get("type") == "noul":
            value = answer.get("noul")
            if not _finite_probability(value):
                raise _ResponseValidationError
        elif question.get("type") == "choice":
            choice = answer.get("choice")
            confidence = answer.get("confidence")
            probabilities = answer.get("probabilities")
            criteria = question.get("criteria", {})
            if choice not in criteria or not _finite_probability(confidence):
                raise _ResponseValidationError
            if not isinstance(probabilities, dict) or set(probabilities) != set(criteria):
                raise _ResponseValidationError
            if any(not _finite_probability(value) for value in probabilities.values()):
                raise _ResponseValidationError
        else:
            raise _ResponseValidationError
    return response


def execute_preview(
    preview_path: str | Path | Mapping[str, Any],
    approved_payload_sha256: str,
    max_requests: int = DEFAULT_MAX_JEV_REQUESTS,
    max_payload_bytes: int = DEFAULT_MAX_JEV_PAYLOAD_BYTES,
    transport: Callable[[dict[str, Any], str], dict[str, Any]] = _default_transport,
    checkpoint: Path | None = None,
    code_evidence_repo: str | Path | None = None,
    approved_code_evidence_sha256: str | None = None,
    approved_code_batch_sha256: str | None = None,
) -> dict[str, Any]:
    if max_requests < 1:
        raise JgError("--max-jev-requests must be greater than zero")
    if max_payload_bytes < 1:
        raise JgError("--max-jev-payload-bytes must be greater than zero")
    in_memory_preview = isinstance(preview_path, Mapping)
    preview = dict(preview_path) if in_memory_preview else read_json(preview_path)
    if preview.get("kind") != "jev-preview":
        raise JgError("approved preview is not a Jev preview artifact")
    if preview.get("endpoint") != JEV_ENDPOINT:
        raise JgError("approved preview has an unexpected destination host")
    if preview.get("network_performed") is not False:
        raise JgError("approved preview is not a local-only preview")
    evidence_profile = preview.get("evidence_profile", "minimal")
    if evidence_profile == CODE_EVIDENCE_PROFILE:
        if not in_memory_preview:
            raise JgError("code evidence preview must remain in memory and cannot be read from disk")
        if preview.get("storage") != "transient" or preview.get("no_store") is not True:
            raise JgError("code evidence preview must be transient and no-store")
        if code_evidence_repo is None:
            raise JgError("code evidence live execution requires repository revalidation")
        if checkpoint is not None:
            raise JgError("code evidence execution cannot persist a checkpoint containing the preview")
    if preview.get("payload_sha256") != approved_payload_sha256:
        raise JgError("approved payload digest does not match preview; inspect a new preview")
    if digest(preview.get("requests")) != approved_payload_sha256:
        raise JgError("preview payload was modified after it was generated")
    request_count = preview.get("request_count")
    payload_bytes = preview.get("payload_bytes")
    requests = preview.get("requests")
    if not isinstance(requests, list) or request_count != len(requests) or payload_bytes != len(canonical_json(requests)):
        raise JgError("preview request budget metadata does not match its payload")
    if not isinstance(request_count, int) or not isinstance(payload_bytes, int):
        raise JgError("approved preview is missing request budget metadata")
    if request_count > max_requests:
        raise JgError(
            f"approved preview has {request_count} requests; live default permits {max_requests}. "
            "Use --max-jev-requests only after reviewing the larger run."
        )
    if payload_bytes > max_payload_bytes:
        raise JgError(
            f"approved preview has {payload_bytes} payload bytes; live default permits {max_payload_bytes}. "
            "Use --max-jev-payload-bytes only after reviewing the larger run."
        )
    if evidence_profile == CODE_EVIDENCE_PROFILE:
        if not isinstance(preview.get("code_evidence_sha256"), str):
            raise JgError("code evidence preview lacks its batch evidence digest")
        expected_batch = digest({
            "evidence_sha256": preview["code_evidence_sha256"],
            "payload_sha256": approved_payload_sha256,
            "request_count": request_count,
        })
        if approved_code_batch_sha256 != expected_batch:
            raise JgError("approved code batch digest does not match preview")
    from .credential_cache import resolve_provider_token
    token = resolve_provider_token()
    ledger = {
        "kind": "relations", "source_preview_sha256": approved_payload_sha256,
        "question_version": preview.get("question_version"), "network_performed": False,
        "repository_id": preview.get("repository_id"),
        "candidate_content_digest": preview.get("candidate_content_digest"),
        "evidence_profile": evidence_profile,
        "relations": [], "attempts": [],
    }
    if checkpoint is not None and checkpoint.exists():
        ledger = read_json(checkpoint)
        if ledger.get("source_preview_sha256") != approved_payload_sha256:
            raise JgError("checkpoint belongs to a different approved preview")
        if not isinstance(ledger.get("attempts"), list):
            raise JgError("checkpoint lacks an attempt ledger; use a new output directory")
        _deduplicate_ledger(ledger)
    attempted = {item["request_sha256"] for item in ledger["attempts"]}
    for request in preview["requests"]:
        request_digest = digest(request)
        if request_digest in attempted:
            continue
        started = datetime.now(UTC)
        attempt = {
            "request_sha256": request_digest,
            "candidate_id": request["state"]["candidate_id"],
            "question_version": preview.get("question_version"),
            "evidence_profile": evidence_profile,
            "model_requested": request.get("model"),
            "started_at": started.isoformat(),
            "started_at_epoch_ms": int(started.timestamp() * 1000),
            "status": "uncertain",
        }
        ledger["attempts"].append(attempt)
        if checkpoint is not None:
            save_checkpoint(checkpoint, ledger)
        attempted.add(request_digest)
        if evidence_profile == CODE_EVIDENCE_PROFILE:
            record = request.get("state", {}).get("code_evidence")
            if not isinstance(record, dict):
                raise JgError("code evidence request lacks its approved record")
            # Revalidate after any prior request and immediately before this
            # transport call, so an approved batch cannot go stale in flight.
            individual_approval = (
                approved_code_evidence_sha256
                if approved_code_evidence_sha256 == record.get("evidence_sha256")
                else None
            )
            revalidate_code_evidence(code_evidence_repo, record, individual_approval)
        ledger["network_performed"] = True
        timer = monotonic()
        try:
            response = transport(request, token)
            validate_response(request, response)
        except Exception as exc:
            # The server may have processed the request. Never retry implicitly,
            # and never persist an exception message containing credentials.
            attempt["error_class"] = "response_validation_error" if isinstance(exc, _ResponseValidationError) else "transport_error"
            if isinstance(exc, JgError):
                message = str(exc)
                if message.startswith("Jev HTTP error: ") and message.rsplit(" ", 1)[-1].isdigit():
                    attempt["http_status"] = int(message.rsplit(" ", 1)[-1])
            completed = datetime.now(UTC)
            attempt.update({
                "completed_at": completed.isoformat(),
                "completed_at_epoch_ms": int(completed.timestamp() * 1000),
                "latency_ms": round((monotonic() - timer) * 1000),
            })
            if checkpoint is not None:
                save_checkpoint(checkpoint, ledger)
            raise JgError("Jev attempt failed; checkpoint retained, outcome uncertain") from None
        completed = datetime.now(UTC)
        attempt.update({
            "completed_at": completed.isoformat(),
            "completed_at_epoch_ms": int(completed.timestamp() * 1000),
            "latency_ms": round((monotonic() - timer) * 1000),
            "http_status": 200,
            "model": response["model"],
            "input_tokens": response["usage"]["input_tokens"],
            "output_tokens": response["usage"]["output_tokens"],
        })
        recorded_response = sanitize_provider_response(request, response)
        ledger["relations"].append({
            "judgment_id": request_digest,
            "request_sha256": request_digest,
            "candidate_id": request["state"]["candidate_id"],
            "question_version": preview.get("question_version"),
            "evidence_profile": evidence_profile,
            "started_at": attempt["started_at"],
            "completed_at": attempt["completed_at"],
            "completed_at_epoch_ms": attempt["completed_at_epoch_ms"],
            "response": recorded_response,
        })
        attempt["status"] = "succeeded"
        if checkpoint is not None:
            save_checkpoint(checkpoint, ledger)
    update_ledger_statistics(ledger)
    return ledger


def execute_transient_preview(
    preview: Mapping[str, Any],
    approved_payload_sha256: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute an approved in-memory code preview without an artifact path."""
    if preview.get("evidence_profile") != CODE_EVIDENCE_PROFILE:
        raise JgError("transient execution requires the code evidence profile")
    if "checkpoint" in kwargs and kwargs["checkpoint"] is not None:
        raise JgError("transient code execution cannot use a checkpoint")
    return execute_preview(preview, approved_payload_sha256, **kwargs)
