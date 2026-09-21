from __future__ import annotations

import hashlib
import json
import os
import tempfile
import fcntl
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import JgError
from .questions import QUESTION_IDS, QUESTION_VERSION, relationship_questions
from .safety import canonical_json, digest, read_json, write_json


JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
EVIDENCE_PROFILES = ("minimal", "review")
DEFAULT_MAX_JEV_REQUESTS = 1
DEFAULT_MAX_JEV_PAYLOAD_BYTES = 8_192


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


def update_ledger_statistics(ledger: dict[str, Any]) -> None:
    ledger["counts"] = {
        "attempted": len(ledger["attempts"]),
        "succeeded": sum(item["status"] == "succeeded" for item in ledger["attempts"]),
        "uncertain": sum(item["status"] == "uncertain" for item in ledger["attempts"]),
    }
    usage = [relation.get("response", {}).get("usage", {}) for relation in ledger.get("relations", [])]
    durations = [item.get("latency_ms") for item in ledger["attempts"] if isinstance(item.get("latency_ms"), int)]
    starts = [item["started_at_epoch_ms"] for item in ledger["attempts"] if isinstance(item.get("started_at_epoch_ms"), int)]
    completions = [item["completed_at_epoch_ms"] for item in ledger["attempts"] if isinstance(item.get("completed_at_epoch_ms"), int)]
    ledger["statistics"] = {
        "calls": len(ledger["attempts"]),
        "input_tokens": sum(item.get("input_tokens", 0) for item in usage),
        "output_tokens": sum(item.get("output_tokens", 0) for item in usage),
        "api_time_ms": sum(durations),
        "wall_time_ms": max(completions) - min(starts) if starts and completions else None,
        "estimated_cost_usd": None,
        "pricing_basis": None,
    }


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


def payload_for_candidate(candidate: dict[str, Any], evidence_profile: str = "minimal") -> dict[str, Any]:
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
    for field in ("identical_tips", "a_ancestor_of_b", "b_ancestor_of_a", "a_commits_not_in_b", "b_commits_not_in_a"):
        if field in evidence:
            state[field] = evidence[field]
    return {
        "state": state,
        "model": JEV_MODEL,
        "questions": relationship_questions(),
    }


def build_preview(candidates: dict[str, Any], evidence_profile: str = "minimal") -> dict[str, Any]:
    requests = [payload_for_candidate(candidate, evidence_profile) for candidate in candidates.get("candidates", [])]
    return {
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


def write_preview(candidates_path: str | Path, output: str | Path, evidence_profile: str = "minimal") -> Path:
    candidates = read_json(candidates_path)
    preview = build_preview(candidates, evidence_profile)
    target = Path(output).expanduser().resolve() / "jev-preview.json"
    write_json(target, preview)
    return target


def _default_transport(payload: dict[str, Any], token: str) -> dict[str, Any]:
    data = canonical_json(payload)
    request = Request(
        JEV_ENDPOINT,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:  # nosec B310: explicit operator opt-in
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise JgError(f"Jev HTTP error: {exc.code}") from None
    except URLError as exc:
        raise JgError("Jev network error") from None


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
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
                raise _ResponseValidationError
        elif question.get("type") == "choice":
            choice = answer.get("choice")
            confidence = answer.get("confidence")
            probabilities = answer.get("probabilities")
            criteria = question.get("criteria", {})
            if choice not in criteria or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise _ResponseValidationError
            if not isinstance(probabilities, dict) or set(probabilities) != set(criteria):
                raise _ResponseValidationError
            if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1 for value in probabilities.values()):
                raise _ResponseValidationError
        else:
            raise _ResponseValidationError
    return response


def execute_preview(
    preview_path: str | Path,
    approved_payload_sha256: str,
    max_requests: int = DEFAULT_MAX_JEV_REQUESTS,
    max_payload_bytes: int = DEFAULT_MAX_JEV_PAYLOAD_BYTES,
    transport: Callable[[dict[str, Any], str], dict[str, Any]] = _default_transport,
    checkpoint: Path | None = None,
) -> dict[str, Any]:
    if max_requests < 1:
        raise JgError("--max-jev-requests must be greater than zero")
    if max_payload_bytes < 1:
        raise JgError("--max-jev-payload-bytes must be greater than zero")
    preview = read_json(preview_path)
    if preview.get("kind") != "jev-preview":
        raise JgError("approved preview is not a Jev preview artifact")
    if preview.get("endpoint") != JEV_ENDPOINT:
        raise JgError("approved preview has an unexpected destination host")
    if preview.get("network_performed") is not False:
        raise JgError("approved preview is not a local-only preview")
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
    token = os.environ.get("TYPESAFE_API_KEY")
    if not token:
        raise JgError("--use-jev requires TYPESAFE_API_KEY in the process environment")
    ledger = {
        "kind": "relations", "source_preview_sha256": approved_payload_sha256,
        "question_version": preview.get("question_version"), "network_performed": False,
        "repository_id": preview.get("repository_id"),
        "candidate_content_digest": preview.get("candidate_content_digest"),
        "evidence_profile": preview.get("evidence_profile", "minimal"),
        "relations": [], "attempts": [],
    }
    if checkpoint is not None and checkpoint.exists():
        ledger = read_json(checkpoint)
        if ledger.get("source_preview_sha256") != approved_payload_sha256:
            raise JgError("checkpoint belongs to a different approved preview")
        if not isinstance(ledger.get("attempts"), list):
            raise JgError("checkpoint lacks an attempt ledger; use a new output directory")
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
            "evidence_profile": preview.get("evidence_profile", "minimal"),
            "model_requested": request.get("model"),
            "started_at": started.isoformat(),
            "started_at_epoch_ms": int(started.timestamp() * 1000),
            "status": "uncertain",
        }
        ledger["attempts"].append(attempt)
        if checkpoint is not None:
            save_checkpoint(checkpoint, ledger)
        attempted.add(request_digest)
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
        ledger["relations"].append({
            "judgment_id": request_digest,
            "candidate_id": request["state"]["candidate_id"],
            "question_version": preview.get("question_version"),
            "evidence_profile": preview.get("evidence_profile", "minimal"),
            "response": response,
        })
        attempt["status"] = "succeeded"
        if checkpoint is not None:
            save_checkpoint(checkpoint, ledger)
    update_ledger_statistics(ledger)
    return ledger
