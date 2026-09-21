"""Human-authoritative review ledger validation and freshness checks."""

from __future__ import annotations

from typing import Any

from .errors import JgError
from .safety import digest


REVIEW_SCHEMA_VERSION = 1
DISPOSITIONS = {
    "ACTIVE",
    "PRESERVE_IN_PR",
    "PRESERVE_IN_BRANCH",
    "PRESERVE_IN_ARCHIVE",
    "CLEANUP_CANDIDATE",
    "UNRESOLVED",
}


def object_id(kind: str, item: dict[str, Any]) -> str:
    if kind == "branch":
        return f"branch:{item.get('name', '')}"
    if kind == "worktree":
        return f"worktree:{item.get('path_id', '')}"
    if kind == "stash":
        return f"stash:{item.get('reference', '')}:{item.get('sha', '')}"
    raise JgError(f"unsupported review object kind: {kind}")


def object_fingerprint(kind: str, item: dict[str, Any]) -> str:
    if kind == "branch":
        value = {"kind": kind, "name": item.get("name"), "tip": item.get("tip")}
    elif kind == "worktree":
        value = {"kind": kind, "path_id": item.get("path_id"), "head": item.get("head"),
                 "branch": item.get("branch"), "status": item.get("status")}
    elif kind == "stash":
        value = {"kind": kind, "reference": item.get("reference"), "sha": item.get("sha")}
    else:
        raise JgError(f"unsupported review object kind: {kind}")
    return digest(value)


def validate_review(review: dict[str, Any], repository_id: str) -> dict[str, dict[str, Any]]:
    if review.get("kind") != "relationship-review" or review.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise JgError("review artifact has an unsupported schema")
    if review.get("repository_id") != repository_id:
        raise JgError("review artifact belongs to a different repository")
    decisions = review.get("decisions")
    if not isinstance(decisions, list):
        raise JgError("review artifact is missing decisions")
    indexed: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        if not isinstance(decision, dict) or not isinstance(decision.get("object_id"), str):
            raise JgError("review decision lacks an object_id")
        if decision["object_id"] in indexed:
            raise JgError("review artifact contains duplicate object decisions")
        if decision.get("disposition") not in DISPOSITIONS:
            raise JgError("review decision has an unsupported disposition")
        if not isinstance(decision.get("rationale"), str) or not decision["rationale"].strip():
            raise JgError("review decision requires a rationale")
        if not isinstance(decision.get("reviewed_at"), str) or not isinstance(decision.get("fingerprint"), str):
            raise JgError("review decision lacks review time or fingerprint")
        indexed[decision["object_id"]] = decision
    return indexed
