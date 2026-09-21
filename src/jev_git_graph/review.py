"""Validation and reconciliation for the human relationship-review ledger."""

from __future__ import annotations

import copy
from typing import Any

from .artifacts import validate_artifacts
from .errors import JgError
from .safety import digest


REVIEW_SCHEMA_VERSION = 2
LEGACY_REVIEW_SCHEMA_VERSION = 1
DISPOSITIONS = {
    "ACTIVE",
    "PRESERVE_IN_PR",
    "PRESERVE_IN_BRANCH",
    "PRESERVE_IN_ARCHIVE",
    "CLEANUP_CANDIDATE",
    "UNRESOLVED",
}
OBJECT_KINDS = {"branch", "worktree", "stash"}
PRESERVATION_REQUIRED_DISPOSITIONS = {
    "PRESERVE_IN_PR",
    "PRESERVE_IN_BRANCH",
    "PRESERVE_IN_ARCHIVE",
    "CLEANUP_CANDIDATE",
}
PROVENANCE_FIELDS = (
    "repository_id",
    "inventory_digest",
    "candidate_digest",
    "relations_digest",
)


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
        value = {
            "kind": kind,
            "path_id": item.get("path_id"),
            "head": item.get("head"),
            "branch": item.get("branch"),
            "status": item.get("status"),
        }
    elif kind == "stash":
        value = {"kind": kind, "reference": item.get("reference"), "sha": item.get("sha")}
    else:
        raise JgError(f"unsupported review object kind: {kind}")
    return digest(value)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise JgError(f"{label} must be a JSON object")
    return value


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        suffix = "" if allow_empty else " non-empty"
        raise JgError(f"{label} must be a{suffix} string")
    return value


def _digest(value: Any, label: str, *, allow_none: bool = False) -> str | None:
    if allow_none and value is None:
        return None
    result = _text(value, label)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise JgError(f"{label} is not a SHA-256 digest")
    return result


def _optional_digest(record: dict[str, Any], field: str, label: str) -> None:
    if field in record and record[field] is not None:
        _digest(record[field], label)


def _provenance_value(review: dict[str, Any], field: str) -> Any:
    nested = review.get("provenance")
    if nested is not None:
        nested = _mapping(nested, "review provenance")
    top_value = review.get(field)
    nested_value = nested.get(field) if nested is not None else None
    if field in review and nested is not None and field in nested and top_value != nested_value:
        raise JgError(f"review provenance has conflicting {field}")
    return top_value if field in review else nested_value


def _review_provenance(
    review: dict[str, Any], schema_version: int,
) -> tuple[dict[str, Any] | None, list[str]]:
    if schema_version == LEGACY_REVIEW_SCHEMA_VERSION:
        limitations = [
            "legacy_review_schema_v1",
            "reviewer_identity_missing",
            "preservation_provenance_missing",
        ]
        if not any(field in review for field in PROVENANCE_FIELDS):
            limitations.append("review_provenance_missing")
        for field in PROVENANCE_FIELDS:
            value = _provenance_value(review, field)
            if field == "repository_id":
                if value is not None:
                    _text(value, "review repository id")
            elif field == "relations_digest":
                _digest(value, "review relations digest", allow_none=True)
            elif value is not None:
                _digest(value, f"review {field}")
        if "candidate_content_digest" in review and review["candidate_content_digest"] is not None:
            _digest(review["candidate_content_digest"], "review candidate content digest")
        return None, limitations

    repository_id = _text(_provenance_value(review, "repository_id"), "review repository id")
    values: dict[str, Any] = {"repository_id": repository_id}
    for field in ("inventory_digest", "candidate_digest"):
        values[field] = _digest(_provenance_value(review, field), f"review {field}")
    nested_provenance = review.get("provenance")
    if "relations_digest" not in review and not (
        isinstance(nested_provenance, dict) and "relations_digest" in nested_provenance
    ):
        raise JgError("review provenance is missing relations_digest")
    values["relations_digest"] = _digest(
        _provenance_value(review, "relations_digest"),
        "review relations digest",
        allow_none=True,
    )
    content_digest = _provenance_value(review, "candidate_content_digest")
    if content_digest is not None:
        values["candidate_content_digest"] = _digest(content_digest, "review candidate content digest")
    return values, []


def _reviewer_identity(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return _text(value, label)
    if isinstance(value, dict):
        return _text(value.get("id"), f"{label} id")
    raise JgError(f"{label} must be a string or JSON object")


def _meaningful_destination(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list)):
        return bool(value)
    return False


def _validate_preservation(decision: dict[str, Any], schema_version: int, limitations: list[str]) -> None:
    if schema_version == LEGACY_REVIEW_SCHEMA_VERSION:
        return
    for field in ("preservation_destination", "preservation_proof"):
        if field not in decision:
            raise JgError(f"review decision is missing {field}")
    destination = decision["preservation_destination"]
    if destination is not None and not isinstance(destination, (str, dict, list)):
        raise JgError("review preservation destination must be null, a string, or a JSON value")
    proof = decision["preservation_proof"]
    if proof is not None:
        proof = _mapping(proof, "review preservation proof")
        if "verified" in proof and not isinstance(proof["verified"], bool):
            raise JgError("review preservation proof verified must be boolean")
        _optional_digest(proof, "source_fingerprint", "preservation proof source fingerprint")
        _optional_digest(proof, "destination_fingerprint", "preservation proof destination fingerprint")
        if "destination" in proof and proof["destination"] != destination:
            raise JgError("review preservation proof destination does not match destination")
    if decision.get("disposition") in PRESERVATION_REQUIRED_DISPOSITIONS:
        if not _meaningful_destination(destination):
            limitations.append("preservation_destination_missing")
        if not isinstance(proof, dict) or not proof:
            limitations.append("preservation_proof_missing")
        elif proof.get("verified") is not True:
            limitations.append("preservation_proof_unverified")
    elif destination is not None and proof is None:
        limitations.append("preservation_proof_missing")


def validate_review_document(
    review: dict[str, Any], repository_id: str | None = None,
) -> dict[str, Any]:
    """Validate a v1 or v2 review without upgrading historical evidence."""

    review = _mapping(review, "review artifact")
    if review.get("kind") != "relationship-review":
        raise JgError("review artifact has an unsupported schema")
    schema_version = review.get("schema_version")
    if schema_version not in {LEGACY_REVIEW_SCHEMA_VERSION, REVIEW_SCHEMA_VERSION}:
        raise JgError("review artifact has an unsupported schema")
    supplied_repository_id = review.get("repository_id")
    if supplied_repository_id is None and schema_version == REVIEW_SCHEMA_VERSION:
        supplied_repository_id = _provenance_value(review, "repository_id")
    supplied_repository_id = _text(supplied_repository_id, "review repository id")
    if repository_id is not None and supplied_repository_id != repository_id:
        raise JgError("review artifact belongs to a different repository")
    provenance, limitations = _review_provenance(review, schema_version)
    decisions = review.get("decisions")
    if not isinstance(decisions, list):
        raise JgError("review artifact is missing decisions")
    indexed: dict[str, dict[str, Any]] = {}
    for index, raw_decision in enumerate(decisions):
        decision = _mapping(raw_decision, f"review decision {index}")
        decision_id = _text(decision.get("object_id"), f"review decision {index} object_id")
        if decision_id in indexed:
            raise JgError("review artifact contains duplicate object decisions")
        kind = decision.get("kind")
        if schema_version == REVIEW_SCHEMA_VERSION and kind is None:
            raise JgError(f"review decision {decision_id} is missing kind")
        if kind is not None and (kind not in OBJECT_KINDS or not decision_id.startswith(f"{kind}:")):
            raise JgError(f"review decision {decision_id} has a malformed kind")
        if decision.get("disposition") not in DISPOSITIONS:
            raise JgError("review decision has an unsupported disposition")
        rationale = decision.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise JgError("review decision requires a rationale")
        status = None
        reconciliation = decision.get("reconciliation")
        if reconciliation is not None:
            reconciliation = _mapping(reconciliation, f"review decision {decision_id} reconciliation")
            status = reconciliation.get("status")
            if status not in {"current", "stale", "unreviewed", "historical-limited"}:
                raise JgError(f"review decision {decision_id} has a malformed reconciliation status")
        historical_or_unreviewed = status in {"unreviewed", "historical-limited"}
        reviewed_at = decision.get("reviewed_at")
        if schema_version == LEGACY_REVIEW_SCHEMA_VERSION:
            if not isinstance(reviewed_at, str) or not reviewed_at:
                raise JgError("review decision lacks review time or fingerprint")
            if not isinstance(decision.get("fingerprint"), str):
                raise JgError("review decision lacks review time or fingerprint")
        else:
            source_fingerprint = decision.get("source_fingerprint", decision.get("fingerprint"))
            if "fingerprint" in decision and "source_fingerprint" in decision and decision["fingerprint"] != source_fingerprint:
                raise JgError(f"review decision {decision_id} has conflicting source fingerprints")
            _digest(source_fingerprint, f"review decision {decision_id} source fingerprint")
            if reviewed_at is None:
                if not historical_or_unreviewed:
                    raise JgError(f"review decision {decision_id} lacks reviewed_at")
            elif not isinstance(reviewed_at, str) or not reviewed_at:
                raise JgError(f"review decision {decision_id} has malformed reviewed_at")
            reviewer = decision.get("reviewer_id", decision.get("reviewer"))
            if "reviewer_id" in decision and "reviewer" in decision:
                left = _reviewer_identity(decision["reviewer_id"], "reviewer_id")
                right = _reviewer_identity(decision["reviewer"], "reviewer")
                if left != right:
                    raise JgError(f"review decision {decision_id} has conflicting reviewer identity")
            if reviewer is None and not historical_or_unreviewed:
                raise JgError(f"review decision {decision_id} lacks reviewer identity")
            _reviewer_identity(reviewer, f"review decision {decision_id} reviewer")
            _validate_preservation(decision, schema_version, limitations)
            source_provenance = decision.get("source_provenance", decision.get("provenance"))
            if source_provenance is None:
                if not historical_or_unreviewed:
                    raise JgError(f"review decision {decision_id} lacks source provenance")
                limitations.append("decision_source_provenance_missing")
            else:
                temp = {"kind": "relationship-review", "schema_version": 2, **source_provenance}
                if isinstance(source_provenance, dict) and "provenance" in source_provenance:
                    temp["provenance"] = source_provenance["provenance"]
                _validate_provenance_fields(temp, 2)
            evidence = decision.get("evidence")
            if evidence is not None and not isinstance(evidence, (dict, list, str)):
                raise JgError(f"review decision {decision_id} has malformed evidence")
            if "evidence_fingerprint" in decision and decision["evidence_fingerprint"] is not None:
                evidence_fingerprint = _digest(decision["evidence_fingerprint"], f"review decision {decision_id} evidence fingerprint")
                if evidence is None:
                    limitations.append("evidence_without_payload")
                elif evidence_fingerprint != digest(evidence):
                    raise JgError(f"review decision {decision_id} evidence fingerprint does not match evidence")
        indexed[decision_id] = decision
    if schema_version == REVIEW_SCHEMA_VERSION:
        top_reviewer = review.get("reviewer_identity", review.get("reviewer"))
        _reviewer_identity(top_reviewer, "review reviewer identity")
    raw_limitations = review.get("limitations", [])
    if not isinstance(raw_limitations, list) or any(not isinstance(item, str) for item in raw_limitations):
        raise JgError("review limitations must be a list of strings")
    return {
        "schema_version": schema_version,
        "repository_id": supplied_repository_id,
        "provenance": provenance,
        "decisions": indexed,
        "limitations": sorted(set(limitations + raw_limitations)),
    }


def validate_review(review: dict[str, Any], repository_id: str) -> dict[str, dict[str, Any]]:
    """Compatibility API used by the existing plan builder."""

    return validate_review_document(review, repository_id)["decisions"]


def _validate_provenance_fields(value: dict[str, Any], schema_version: int = REVIEW_SCHEMA_VERSION) -> dict[str, Any]:
    if schema_version != REVIEW_SCHEMA_VERSION:
        raise JgError("unsupported provenance schema")
    result, _limitations = _review_provenance(value, schema_version)
    if result is None:
        raise JgError("review provenance is missing")
    return result


def _source_provenance(
    inventory: dict[str, Any], candidates: dict[str, Any], relations: dict[str, Any] | None,
    artifact_validation: dict[str, Any],
) -> dict[str, Any]:
    result = {
        "repository_id": inventory["repository"]["id"],
        "inventory_digest": digest(inventory),
        "candidate_digest": digest(candidates),
        "candidate_content_digest": artifact_validation["candidates"]["content_digest"],
        "relations_digest": digest(relations) if relations is not None else None,
    }
    return result


def _inventory_objects(inventory: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any], str]]:
    objects: dict[str, tuple[str, dict[str, Any], str]] = {}
    for kind, records in (
        ("branch", inventory.get("branches")),
        ("worktree", inventory.get("worktrees")),
        ("stash", inventory.get("stashes")),
    ):
        if not isinstance(records, list):
            raise JgError(f"inventory {kind} collection must be a list")
        for index, item in enumerate(records):
            record = _mapping(item, f"inventory {kind} record {index}")
            key = object_id(kind, record)
            if key in objects:
                raise JgError(f"inventory contains duplicate review object: {key}")
            objects[key] = (kind, record, object_fingerprint(kind, record))
    return objects


def _preservation_stale_reasons(decision: dict[str, Any], current_fingerprint: str) -> list[str]:
    reasons: list[str] = []
    requires_preservation = decision.get("disposition") in PRESERVATION_REQUIRED_DISPOSITIONS
    destination = decision.get("preservation_destination")
    proof = decision.get("preservation_proof")
    if requires_preservation and not _meaningful_destination(destination):
        reasons.append("preservation_destination_missing")
    if (requires_preservation and (not isinstance(proof, dict) or not proof)) or (destination is not None and proof is None):
        reasons.append("preservation_proof_missing")
    if isinstance(proof, dict):
        if requires_preservation and proof.get("verified") is not True:
            reasons.append("preservation_proof_unverified")
        elif proof.get("verified") is False:
            reasons.append("preservation_proof_stale")
        if proof.get("source_fingerprint") not in (None, current_fingerprint):
            reasons.append("preservation_proof_stale")
        if proof.get("destination_fingerprint") not in (None, digest(destination)):
            reasons.append("preservation_destination_stale")
        if "destination" in proof and proof["destination"] != destination:
            reasons.append("preservation_destination_stale")
    evidence = decision.get("evidence")
    evidence_fingerprint = decision.get("evidence_fingerprint")
    if evidence is not None and evidence_fingerprint is not None and digest(evidence) != evidence_fingerprint:
        reasons.append("evidence_stale")
    return sorted(set(reasons))


def _new_unreviewed_decision(
    key: str, kind: str, fingerprint: str, provenance: dict[str, Any],
) -> dict[str, Any]:
    return {
        "object_id": key,
        "kind": kind,
        "fingerprint": fingerprint,
        "source_fingerprint": fingerprint,
        "source_provenance": copy.deepcopy(provenance),
        "reviewer_id": None,
        "reviewer": None,
        "rationale": "No prior review decision exists for this snapshot.",
        "reviewed_at": None,
        "disposition": "UNRESOLVED",
        "preservation_destination": None,
        "preservation_proof": None,
        "evidence": None,
        "evidence_fingerprint": None,
        "reconciliation": {"status": "unreviewed", "reasons": ["no_prior_decision"]},
    }


def reconcile_reviews(
    previous_review: dict[str, Any],
    new_inventory: dict[str, Any],
    new_candidates: dict[str, Any],
    new_relations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a v2 ledger while retaining every prior decision."""

    previous = validate_review_document(previous_review)
    artifact_validation = validate_artifacts(new_inventory, new_candidates, new_relations)
    current_repository = new_inventory["repository"]["id"]
    if previous["repository_id"] != current_repository:
        raise JgError("previous review belongs to a different repository")
    provenance = _source_provenance(new_inventory, new_candidates, new_relations, artifact_validation)
    current_objects = _inventory_objects(new_inventory)
    prior_decisions = previous["decisions"]
    decisions: list[dict[str, Any]] = []
    limitations = list(previous["limitations"]) + list(artifact_validation["limitations"])

    for key, (kind, _item, fingerprint) in current_objects.items():
        old = prior_decisions.get(key)
        if old is None:
            decisions.append(_new_unreviewed_decision(key, kind, fingerprint, provenance))
            limitations.append("new_objects_unreviewed")
            continue
        carried = copy.deepcopy(old)
        old_fingerprint = carried.get("source_fingerprint", carried.get("fingerprint"))
        reasons: list[str] = []
        if previous["schema_version"] == LEGACY_REVIEW_SCHEMA_VERSION:
            reasons.append("legacy_review_schema_v1")
        if old_fingerprint != fingerprint:
            reasons.append("source_fingerprint_changed")
        old_provenance = carried.get("source_provenance", carried.get("provenance"))
        if old_provenance is None:
            reasons.append("source_provenance_missing")
        elif old_provenance != provenance:
            reasons.append("source_provenance_changed")
            if old_provenance.get("candidate_content_digest") != provenance.get("candidate_content_digest"):
                reasons.append("evidence_stale")
        reasons.extend(_preservation_stale_reasons(carried, fingerprint))
        carried.setdefault("kind", kind)
        carried.setdefault("source_fingerprint", old_fingerprint)
        carried.setdefault("fingerprint", old_fingerprint)
        carried.setdefault("source_provenance", copy.deepcopy(old_provenance) if old_provenance is not None else None)
        carried.setdefault("reviewer_id", carried.get("reviewer"))
        carried.setdefault("reviewer", carried.get("reviewer_id"))
        carried.setdefault("preservation_destination", None)
        carried.setdefault("preservation_proof", None)
        carried.setdefault("evidence", None)
        carried.setdefault("evidence_fingerprint", None)
        status = "historical-limited" if previous["schema_version"] == LEGACY_REVIEW_SCHEMA_VERSION else ("stale" if reasons else "current")
        carried["reconciliation"] = {
            "status": status,
            "reasons": sorted(set(reasons)),
            "current_source_fingerprint": fingerprint,
            "current_provenance": copy.deepcopy(provenance),
        }
        decisions.append(carried)
        limitations.extend(reasons)

    for key, old in prior_decisions.items():
        if key in current_objects:
            continue
        carried = copy.deepcopy(old)
        reasons = ["object_missing_from_new_inventory"]
        if previous["schema_version"] == LEGACY_REVIEW_SCHEMA_VERSION:
            reasons.append("legacy_review_schema_v1")
        carried.setdefault("kind", key.split(":", 1)[0])
        old_fingerprint = carried.get("source_fingerprint", carried.get("fingerprint"))
        carried.setdefault("source_fingerprint", old_fingerprint)
        carried.setdefault("fingerprint", old_fingerprint)
        carried.setdefault("reviewer_id", carried.get("reviewer"))
        carried.setdefault("reviewer", carried.get("reviewer_id"))
        carried.setdefault("preservation_destination", None)
        carried.setdefault("preservation_proof", None)
        carried.setdefault("evidence", None)
        carried.setdefault("evidence_fingerprint", None)
        carried.setdefault("source_provenance", copy.deepcopy(carried.get("provenance")))
        carried["reconciliation"] = {
            "status": "historical-limited" if previous["schema_version"] == LEGACY_REVIEW_SCHEMA_VERSION else "stale",
            "reasons": reasons,
            "current_source_fingerprint": None,
            "current_provenance": copy.deepcopy(provenance),
        }
        decisions.append(carried)
        limitations.extend(reasons)

    decisions.sort(key=lambda decision: decision["object_id"])
    return {
        "kind": "relationship-review",
        "schema_version": REVIEW_SCHEMA_VERSION,
        "repository_id": current_repository,
        "inventory_digest": provenance["inventory_digest"],
        "candidate_digest": provenance["candidate_digest"],
        "candidate_content_digest": provenance["candidate_content_digest"],
        "relations_digest": provenance["relations_digest"],
        "provenance": copy.deepcopy(provenance),
        "reviewer_identity": None,
        "decisions": decisions,
        "limitations": sorted(set(limitations)),
        "reconciliation": {
            "from_schema_version": previous["schema_version"],
            "previous_review_digest": digest(previous_review),
        },
        "cleanup_readiness": "not_verified",
    }
