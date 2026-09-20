from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from .errors import JgError
from .safety import canonical_json, digest, read_json, write_json


JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
QUESTION_VERSION = "branch-relationship-v1"
DEFAULT_MAX_JEV_REQUESTS = 1
DEFAULT_MAX_JEV_PAYLOAD_BYTES = 8_192


def _path_hash(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:20]


def payload_for_candidate(candidate: dict[str, Any], include_branch_labels: bool = False) -> dict[str, Any]:
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
    if include_branch_labels:
        state["a_branch_label"] = endpoints["a"]["branch"]
        state["b_branch_label"] = endpoints["b"]["branch"]
    return {
        "state": state,
        "questions": {
            "same_intent": {
                "type": "noul",
                "question": "Do the two Git tips appear to implement the same intended change from this metadata?"
            },
            "relationship": {
                "type": "choice",
                "question": "What relationship is supported by the supplied metadata?",
                "choices": ["A_SUPERSEDES_B", "B_SUPERSEDES_A", "A_DEPENDS_ON_B", "B_DEPENDS_ON_A", "PARTIAL_OVERLAP", "UNRELATED", "INSUFFICIENT_EVIDENCE"],
            },
        },
        "question_version": QUESTION_VERSION,
    }


def build_preview(candidates: dict[str, Any], include_branch_labels: bool = False) -> dict[str, Any]:
    requests = [payload_for_candidate(candidate, include_branch_labels) for candidate in candidates.get("candidates", [])]
    return {
        "kind": "jev-preview",
        "endpoint": JEV_ENDPOINT,
        "question_version": QUESTION_VERSION,
        "include_branch_labels": include_branch_labels,
        "request_count": len(requests),
        "requests": requests,
        "payload_sha256": digest(requests),
        "payload_bytes": len(canonical_json(requests)),
        "network_performed": False,
    }


def write_preview(candidates_path: str | Path, output: str | Path, include_branch_labels: bool = False) -> Path:
    candidates = read_json(candidates_path)
    preview = build_preview(candidates, include_branch_labels)
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
    with urlopen(request, timeout=30) as response:  # nosec B310: explicit operator opt-in
        return json.loads(response.read().decode("utf-8"))


def execute_preview(
    preview_path: str | Path,
    approved_payload_sha256: str,
    max_requests: int = DEFAULT_MAX_JEV_REQUESTS,
    max_payload_bytes: int = DEFAULT_MAX_JEV_PAYLOAD_BYTES,
    transport: Callable[[dict[str, Any], str], dict[str, Any]] = _default_transport,
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
    answers: list[dict[str, Any]] = []
    for request in preview["requests"]:
        response = transport(request, token)
        answers.append({"candidate_id": request["state"]["candidate_id"], "response": response})
    return {
        "kind": "relations",
        "source_preview_sha256": approved_payload_sha256,
        "question_version": QUESTION_VERSION,
        "network_performed": True,
        "relations": answers,
    }
