"""Pinned, metadata-only observations of PR delivery state.

These observations record what an identified observer reports seeing. They are
not provider-verified facts, preservation proof, or integration readiness.
"""
from __future__ import annotations

from datetime import datetime
import re
from typing import Any
from urllib.parse import urlparse

from .errors import JgError


KIND = "delivery-observations"
SCHEMA_VERSION = 1
_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TOP_FIELDS = {"kind", "schema_version", "repository_id", "snapshot_digest",
               "contributions_digest", "observations"}
_ROW_FIELDS = {"contribution_id", "source", "destination", "pull_request",
               "observed_at", "observer", "evidence"}


def _exact_fields(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise JgError(f"delivery observation {label} has unknown or missing fields")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise JgError(f"delivery observation {label} must be non-empty text")
    return value


def _timestamp(value: Any) -> str:
    value = _text(value, "observed_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JgError("delivery observation timestamp is malformed") from exc
    if parsed.tzinfo is None:
        raise JgError("delivery observation timestamp must include a timezone")
    return value


def validate_delivery_observations(
    document: dict[str, Any], repository_id: str, snapshot: dict[str, Any],
    contributions: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Validate exact pins and metadata shape without network or Git access."""
    _exact_fields(document, _TOP_FIELDS, "document")
    if (document["kind"] != KIND or document["schema_version"] != SCHEMA_VERSION
            or document["repository_id"] != repository_id
            or document["snapshot_digest"] != snapshot.get("snapshot_digest")
            or document["contributions_digest"] != contributions.get("contributions_digest")
            or not isinstance(document["snapshot_digest"], str)
            or _DIGEST.fullmatch(document["snapshot_digest"]) is None
            or not isinstance(document["contributions_digest"], str)
            or _DIGEST.fullmatch(document["contributions_digest"]) is None):
        raise JgError("delivery observations do not match repository, snapshot, and contributions pins")
    rows = document["observations"]
    if not isinstance(rows, list):
        raise JgError("delivery observations must be an array")
    units = {unit["id"]: unit for unit in contributions.get("units", [])}
    normalized: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        _exact_fields(row, _ROW_FIELDS, "row")
        cid = _text(row["contribution_id"], "contribution_id")
        unit = units.get(cid)
        if unit is None or cid in by_id:
            raise JgError("delivery observation has unknown or duplicate contribution ID")
        source = _exact_fields(row["source"], {"branch", "tip", "path", "blob"}, "source pin")
        expected_source = {"branch": unit["source"].get("branch"),
                           "tip": unit.get("source_tip"), "path": unit.get("path"),
                           "blob": unit.get("source_blob")}
        if (source != expected_source
                or not isinstance(source.get("branch"), str) or not source["branch"]
                or not isinstance(source.get("path"), str) or not source["path"]
                or not isinstance(source.get("tip"), str) or _SHA.fullmatch(source["tip"]) is None
                or not isinstance(source.get("blob"), str) or _SHA.fullmatch(source["blob"]) is None):
            raise JgError("delivery observation source pin does not match contribution")
        destination = _exact_fields(row["destination"], {"branch", "tip"}, "destination pin")
        main = snapshot.get("main", {})
        if destination != {"branch": main.get("name"), "tip": main.get("tip")} \
                or _SHA.fullmatch(str(destination.get("tip", ""))) is None:
            raise JgError("delivery observation destination pin does not match snapshot main")
        pr = _exact_fields(row["pull_request"],
                           {"url", "repository", "number", "head_sha", "base_sha", "status", "merge_sha"},
                           "pull request")
        repo = _text(pr["repository"], "pull request repository")
        url = _text(pr["url"], "pull request URL")
        parsed_url = urlparse(url)
        number = pr["number"]
        if (not _REPOSITORY.fullmatch(repo) or parsed_url.scheme != "https"
                or parsed_url.netloc != "github.com"
                or parsed_url.path != f"/{repo}/pull/{number}"
                or parsed_url.query or parsed_url.fragment
                or not isinstance(number, int) or isinstance(number, bool) or number < 1
                or not isinstance(pr["head_sha"], str) or _SHA.fullmatch(pr["head_sha"]) is None
                or not isinstance(pr["base_sha"], str) or _SHA.fullmatch(pr["base_sha"]) is None
                or not isinstance(pr["status"], str) or pr["status"] not in {"OPEN", "MERGED"}
                or (pr["status"] == "MERGED"
                    and (not isinstance(pr["merge_sha"], str) or _SHA.fullmatch(pr["merge_sha"]) is None))
                or (pr["status"] == "OPEN" and pr["merge_sha"] is not None)):
            raise JgError("delivery observation pull request metadata is malformed")
        observer = _exact_fields(row["observer"], {"identity", "kind"}, "observer")
        if (not _text(observer["identity"], "observer identity")
                or not isinstance(observer["kind"], str)
                or observer["kind"] not in {"agent_readback", "operator_report"}):
            raise JgError("delivery observation observer provenance is invalid")
        evidence = _exact_fields(row["evidence"], {"reference", "sha256"}, "evidence")
        if (not _text(evidence["reference"], "evidence reference")
                or not isinstance(evidence["sha256"], str)
                or _DIGEST.fullmatch(evidence["sha256"]) is None):
            raise JgError("delivery observation evidence provenance is invalid")
        clean = {"contribution_id": cid, "source": dict(source), "destination": dict(destination),
                 "pull_request": dict(pr), "observed_at": _timestamp(row["observed_at"]),
                 "observer": dict(observer), "evidence": dict(evidence),
                 "interpretation": "reported_observation_only; not provider_verified; not preservation_proof"}
        normalized.append(clean)
        by_id[cid] = clean
    return ({"kind": KIND, "schema_version": SCHEMA_VERSION,
             "repository_id": repository_id, "snapshot_digest": snapshot["snapshot_digest"],
             "contributions_digest": contributions["contributions_digest"],
             "observations": normalized}, by_id)
