"""Validation for the local Git-graph artifact chain.

Artifacts are inputs to a review plan, not proof that any cleanup is safe.  The
validators deliberately retain limitations for historical artifacts whose
provenance fields predate the current chain instead of upgrading them to a
verified result.
"""

from __future__ import annotations

from typing import Any

from .errors import JgError
from .jev import validate_response as validate_v3_response
from .questions import QUESTION_VERSION, relationship_questions
from .safety import digest


LEGACY_QUESTION_VERSIONS = frozenset({"branch-relationship-v1", "branch-relationship-v2"})
V2_RELATION_CHOICES = frozenset(
    {
        "A_SUPERSEDES_B",
        "B_SUPERSEDES_A",
        "A_DEPENDS_ON_B",
        "B_DEPENDS_ON_A",
        "PARTIAL_OVERLAP",
        "UNRELATED",
        "INSUFFICIENT_EVIDENCE",
    }
)
V2_RELATION_CHOICES_WITH_UNKNOWN = V2_RELATION_CHOICES | {"UNKNOWN"}
V3_QUESTION_IDS = frozenset(relationship_questions())


def candidate_content_digest(candidates: dict[str, Any]) -> str:
    """Return the digest written by the candidate builder.

    The count and coverage metadata are intentionally outside this digest.  It
    identifies the repository, source inventory, and ordered candidate records
    that a Jev response actually describes.
    """

    return digest(
        {
            "repository_id": candidates.get("repository_id"),
            "inventory_digest": candidates.get("inventory_digest"),
            "candidates": candidates.get("candidates"),
        }
    )


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise JgError(f"{label} must be a JSON object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise JgError(f"{label} must be a non-empty string")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise JgError(f"{label} must be a string")
    return value


def _optional_string(record: dict[str, Any], field: str, label: str, allow_none: bool = False) -> None:
    if field not in record:
        return
    value = record[field]
    if allow_none and value is None:
        return
    _require_string(value, label)


def _optional_text(record: dict[str, Any], field: str, label: str, allow_none: bool = False) -> None:
    if field not in record:
        return
    value = record[field]
    if allow_none and value is None:
        return
    _require_text(value, label)


def _require_digest(value: Any, label: str) -> str:
    result = _require_string(value, label)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise JgError(f"{label} is not a SHA-256 digest")
    return result


def _validate_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    if inventory.get("kind") != "inventory" or inventory.get("schema_version") != 1:
        raise JgError("inventory artifact has an unsupported schema")
    repository = _require_mapping(inventory.get("repository"), "inventory repository")
    repository_id = _require_string(repository.get("id"), "inventory repository id")
    default_branch = _require_string(repository.get("default_branch"), "inventory default branch")
    _optional_string(repository, "head", "inventory repository head")
    _optional_string(repository, "common_dir_id", "inventory common directory id")

    branches = inventory.get("branches")
    if not isinstance(branches, list):
        raise JgError("inventory is missing branches")
    branch_by_name: dict[str, dict[str, Any]] = {}
    for branch in branches:
        record = _require_mapping(branch, "inventory branch record")
        name = _require_string(record.get("name"), "inventory branch name")
        tip = _require_string(record.get("tip"), f"inventory branch {name} tip")
        if name in branch_by_name:
            raise JgError(f"inventory contains duplicate branch: {name}")
        _optional_string(record, "committed_at", f"inventory branch {name} committed_at")
        _optional_text(record, "subject", f"inventory branch {name} subject")
        _optional_string(record, "merge_base", f"inventory branch {name} merge_base", allow_none=True)
        if "unique_commits" in record:
            unique_commits = record["unique_commits"]
            if not isinstance(unique_commits, list):
                raise JgError(f"inventory branch {name} unique_commits must be a list")
            for commit_index, commit in enumerate(unique_commits):
                commit_record = _require_mapping(commit, f"inventory branch {name} unique commit {commit_index}")
                _require_string(commit_record.get("sha"), f"inventory branch {name} unique commit {commit_index} sha")
                _optional_text(commit_record, "subject", f"inventory branch {name} unique commit {commit_index} subject")
        if "changed_paths" in record:
            changed_paths = record["changed_paths"]
            if not isinstance(changed_paths, list) or any(not isinstance(path, str) for path in changed_paths):
                raise JgError(f"inventory branch {name} changed_paths must be a list of strings")
        if "merged_into_default" in record and not isinstance(record["merged_into_default"], bool):
            raise JgError(f"inventory branch {name} merged_into_default must be boolean")
        branch_by_name[name] = {"name": name, "tip": tip}
    if default_branch not in branch_by_name:
        raise JgError("inventory default branch is not present in branches")

    for field in ("worktrees", "stashes"):
        if not isinstance(inventory.get(field), list):
            raise JgError(f"inventory is missing {field}")
    worktree_ids: set[str] = set()
    for index, worktree in enumerate(inventory["worktrees"]):
        record = _require_mapping(worktree, f"inventory worktree record {index}")
        path_id = _require_string(record.get("path_id"), f"inventory worktree {index} path_id")
        if path_id in worktree_ids:
            raise JgError(f"inventory contains duplicate worktree identity: {path_id}")
        worktree_ids.add(path_id)
        _require_string(record.get("head"), f"inventory worktree {index} head")
        branch = record.get("branch")
        if branch is not None:
            _require_string(branch, f"inventory worktree {index} branch")
        for field in ("detached", "locked"):
            if not isinstance(record.get(field), bool):
                raise JgError(f"inventory worktree {index} {field} must be boolean")
        if "status" not in record or not isinstance(record["status"], list) or any(not isinstance(item, str) for item in record["status"]):
            raise JgError(f"inventory worktree {index} status must be a list of strings")
    stash_ids: set[tuple[str, str]] = set()
    for index, stash in enumerate(inventory["stashes"]):
        record = _require_mapping(stash, f"inventory stash record {index}")
        sha = _require_string(record.get("sha"), f"inventory stash {index} sha")
        reference = _require_string(record.get("reference"), f"inventory stash {index} reference")
        _require_text(record.get("subject"), f"inventory stash {index} subject")
        identity = (reference, sha)
        if identity in stash_ids:
            raise JgError(f"inventory contains duplicate stash identity: {reference}:{sha}")
        stash_ids.add(identity)
    collection = _require_mapping(inventory.get("collection"), "inventory collection")
    if collection.get("complete") is not True:
        raise JgError("inventory is incomplete; cannot build a review plan")
    counts = collection.get("counts")
    if counts is not None:
        counts = _require_mapping(counts, "inventory collection counts")
        for field, records in (
            ("branches", inventory["branches"]),
            ("worktrees", inventory["worktrees"]),
            ("stashes", inventory["stashes"]),
        ):
            if field in counts:
                count = counts[field]
                if not isinstance(count, int) or isinstance(count, bool) or count < 0 or count != len(records):
                    raise JgError(f"inventory collection count for {field} does not match records")
        if "remote_tracking_refs" in counts:
            remote_refs = inventory.get("remote_tracking_refs")
            remote_count = counts["remote_tracking_refs"]
            if (
                not isinstance(remote_count, int)
                or isinstance(remote_count, bool)
                or remote_count < 0
                or not isinstance(remote_refs, list)
                or remote_count != len(remote_refs)
            ):
                raise JgError("inventory collection count for remote_tracking_refs does not match records")
    return {"repository_id": repository_id, "branches": branch_by_name}


def validate_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    """Validate an inventory and return its immutable endpoint index."""

    return _validate_inventory(_require_mapping(inventory, "inventory artifact"))


def _validate_candidate_record(
    candidate: Any,
    index: int,
    branches: dict[str, dict[str, Any]] | None,
) -> str:
    record = _require_mapping(candidate, f"candidate record {index}")
    candidate_id = _require_string(record.get("id"), f"candidate record {index} id")
    endpoints = _require_mapping(record.get("endpoints"), f"candidate {candidate_id} endpoints")
    endpoint_values: dict[str, tuple[str, str]] = {}
    for side in ("a", "b"):
        endpoint = _require_mapping(endpoints.get(side), f"candidate {candidate_id} endpoint {side}")
        branch = _require_string(endpoint.get("branch"), f"candidate {candidate_id} endpoint {side} branch")
        tip = _require_string(endpoint.get("tip"), f"candidate {candidate_id} endpoint {side} tip")
        if branches is not None:
            inventory_branch = branches.get(branch)
            if inventory_branch is None:
                raise JgError(f"candidate {candidate_id} endpoint {side} is not in the inventory")
            if inventory_branch["tip"] != tip:
                raise JgError(f"candidate {candidate_id} endpoint {side} tip does not match the inventory")
        endpoint_values[side] = (branch, tip)
    if endpoint_values["a"] == endpoint_values["b"]:
        raise JgError(f"candidate {candidate_id} has identical endpoints")

    reasons = record.get("reasons")
    if not isinstance(reasons, list) or any(not isinstance(reason, str) or not reason for reason in reasons):
        raise JgError(f"candidate {candidate_id} has malformed reasons")
    evidence = record.get("evidence")
    if not isinstance(evidence, dict):
        raise JgError(f"candidate {candidate_id} has malformed evidence")
    for field in ("shared_paths", "shared_subject_tokens", "shared_patch_ids"):
        if field in evidence and (
            not isinstance(evidence[field], list)
            or any(not isinstance(value, str) for value in evidence[field])
        ):
            raise JgError(f"candidate {candidate_id} has malformed evidence.{field}")
    for field in ("a_unique_commit_count", "b_unique_commit_count"):
        if field in evidence and (
            not isinstance(evidence[field], int)
            or isinstance(evidence[field], bool)
            or evidence[field] < 0
        ):
            raise JgError(f"candidate {candidate_id} has malformed evidence.{field}")
    return candidate_id


def validate_candidates(
    candidates: dict[str, Any],
    inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate candidates, optionally against their source inventory."""

    candidates = _require_mapping(candidates, "candidates artifact")
    if candidates.get("kind") != "candidates" or candidates.get("schema_version") != 1:
        raise JgError("candidates artifact has an unsupported schema")
    repository_id = _require_string(candidates.get("repository_id"), "candidates repository id")
    inventory_digest = _require_digest(candidates.get("inventory_digest"), "candidates inventory digest")
    records = candidates.get("candidates")
    if not isinstance(records, list):
        raise JgError("candidates artifact is missing candidates")

    branch_index: dict[str, dict[str, Any]] | None = None
    if inventory is not None:
        inventory_info = validate_inventory(inventory)
        if repository_id != inventory_info["repository_id"]:
            raise JgError("candidates belong to a different repository than inventory")
        if inventory_digest != digest(inventory):
            raise JgError("candidates were built from a different inventory")
        branch_index = inventory_info["branches"]

    candidate_ids: set[str] = set()
    for index, candidate in enumerate(records):
        candidate_id = _validate_candidate_record(candidate, index, branch_index)
        if candidate_id in candidate_ids:
            raise JgError(f"candidates contain duplicate candidate id: {candidate_id}")
        candidate_ids.add(candidate_id)

    if "candidate_count" in candidates and candidates["candidate_count"] != len(records):
        raise JgError("candidates candidate_count does not match candidates")
    if "candidate_count_before_limit" in candidates:
        count_before_limit = candidates["candidate_count_before_limit"]
        if not isinstance(count_before_limit, int) or isinstance(count_before_limit, bool) or count_before_limit < len(records):
            raise JgError("candidates candidate_count_before_limit is malformed")

    limitations: list[str] = []
    supplied_content_digest = candidates.get("content_digest")
    if supplied_content_digest is None:
        limitations.append("candidate_content_digest_missing")
    elif _require_digest(supplied_content_digest, "candidates content digest") != candidate_content_digest(candidates):
        raise JgError("candidates content digest does not match its payload")
    return {
        "repository_id": repository_id,
        "candidate_ids": sorted(candidate_ids),
        "content_digest": candidate_content_digest(candidates),
        "limitations": limitations,
    }


def _bounded_probability(value: Any, label: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
        raise JgError(f"{label} must be a number between zero and one")


def _validate_v2_response(response: Any) -> None:
    response = _require_mapping(response, "historical v2 relation response")
    answers = _require_mapping(response.get("answers"), "historical v2 relation answers")
    if set(answers) != {"same_intent", "relationship"}:
        raise JgError("historical v2 relation response has the wrong answer set")
    same_intent = _require_mapping(answers["same_intent"], "historical v2 same_intent answer")
    _bounded_probability(same_intent.get("noul"), "historical v2 same_intent")
    relationship = _require_mapping(answers["relationship"], "historical v2 relationship answer")
    choice = relationship.get("choice")
    if choice not in V2_RELATION_CHOICES_WITH_UNKNOWN:
        raise JgError("historical v2 relation response has an invalid choice")
    if "confidence" in relationship:
        _bounded_probability(relationship["confidence"], "historical v2 relationship confidence")
    probabilities = relationship.get("probabilities")
    if not isinstance(probabilities, dict) or not probabilities or not set(probabilities) <= V2_RELATION_CHOICES_WITH_UNKNOWN or choice not in probabilities:
        raise JgError("historical v2 relation response has malformed probabilities")
    for option, probability in probabilities.items():
        _bounded_probability(probability, f"historical v2 probability {option}")
    if "model" in response:
        _require_string(response["model"], "historical v2 response model")
    if "usage" in response:
        usage = _require_mapping(response["usage"], "historical v2 response usage")
        for field in ("input_tokens", "output_tokens"):
            value = usage.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise JgError(f"historical v2 response usage.{field} is malformed")


def _validate_stored_v3_response(response: Any) -> bool:
    """Validate a v3 response, allowing historical normalized records.

    Live v3 responses include model and usage metadata. Older exported judgment
    records may retain only the typed answers; those remain readable but are
    reported as missing runtime provenance by the artifact-chain validator.
    """

    if isinstance(response, dict) and "model" not in response and "usage" not in response:
        answers = response.get("answers")
        if not isinstance(answers, dict) or set(answers) != V3_QUESTION_IDS:
            raise JgError("relation response does not satisfy the v3 contract")
        for question_id, answer in answers.items():
            answer_record = _require_mapping(answer, f"v3 answer {question_id}")
            _bounded_probability(answer_record.get("noul"), f"v3 answer {question_id}")
        return False
    try:
        validate_v3_response({"questions": relationship_questions()}, response)
    except Exception as exc:
        raise JgError("relation response does not satisfy the v3 contract") from exc
    return True


def validate_relation_response(response: Any, question_version: str | None = None) -> str:
    """Validate either the current v3 response or a readable historical v2 response."""

    if question_version is None:
        answers = response.get("answers") if isinstance(response, dict) else None
        question_version = QUESTION_VERSION if isinstance(answers, dict) and set(answers) == V3_QUESTION_IDS else "branch-relationship-v2"
    if question_version == QUESTION_VERSION:
        _validate_stored_v3_response(response)
        return question_version
    if question_version in LEGACY_QUESTION_VERSIONS:
        _validate_v2_response(response)
        return question_version
    raise JgError(f"relation response has unsupported question version: {question_version}")


def validate_relations(
    relations: dict[str, Any],
    candidates: dict[str, Any] | None = None,
    inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate relation provenance and every stored provider response."""

    relations = _require_mapping(relations, "relations artifact")
    if relations.get("kind") != "relations":
        raise JgError("relations artifact has an unsupported schema")
    records = relations.get("relations")
    if not isinstance(records, list):
        raise JgError("relations artifact is missing relations")

    limitations: list[str] = []
    candidate_info: dict[str, Any] | None = None
    if candidates is not None:
        candidate_info = validate_candidates(candidates, inventory)
        expected_repository_id = candidate_info["repository_id"]
        expected_content_digest = candidate_info["content_digest"]
        supplied_repository_id = relations.get("repository_id")
        if supplied_repository_id is None:
            limitations.append("relations_repository_provenance_missing")
        elif supplied_repository_id != expected_repository_id:
            raise JgError("relations belong to a different repository than candidates")
        supplied_content_digest = relations.get("candidate_content_digest")
        if supplied_content_digest is None:
            limitations.append("relations_candidate_content_provenance_missing")
        elif supplied_content_digest != expected_content_digest:
            raise JgError("relations were built from different candidate content")
        supplied_candidate_digest = relations.get("candidate_digest")
        if supplied_candidate_digest is None:
            limitations.append("relations_candidate_digest_missing")
        elif supplied_candidate_digest != digest(candidates):
            raise JgError("relations were built from a different candidate artifact")
        supplied_inventory_digest = relations.get("inventory_digest")
        if supplied_inventory_digest is not None and supplied_inventory_digest != digest(inventory):
            raise JgError("relations were built from a different inventory")
        candidate_by_id = {
            candidate["id"]: candidate for candidate in candidates.get("candidates", [])
        }
    else:
        candidate_by_id = {}
        limitations.append("relation_candidate_provenance_unavailable")

    root_question_version = relations.get("question_version")
    if root_question_version is None and records:
        limitations.append("relations_question_version_missing")
    seen_ids: set[str] = set()
    seen_judgment_identities: dict[tuple[Any, ...], str] = {}
    versions: set[str] = set()
    for index, relation in enumerate(records):
        record = _require_mapping(relation, f"relation record {index}")
        candidate_id = _require_string(record.get("candidate_id"), f"relation record {index} candidate id")
        seen_ids.add(candidate_id)
        candidate = candidate_by_id.get(candidate_id)
        if candidates is not None and candidate is None:
            raise JgError(f"relation references unknown candidate: {candidate_id}")
        if candidate is not None:
            for side in ("a", "b"):
                supplied_tip = record.get(f"{side}_tip")
                if supplied_tip is not None and supplied_tip != candidate["endpoints"][side]["tip"]:
                    raise JgError(f"relation {candidate_id} {side} tip does not match its candidate")
            supplied_endpoints = record.get("endpoints")
            if supplied_endpoints is not None and supplied_endpoints != candidate["endpoints"]:
                raise JgError(f"relation {candidate_id} endpoints do not match its candidate")

        relation_version = record.get("question_version", root_question_version)
        if relation_version is None:
            answers = record.get("response", {}).get("answers") if isinstance(record.get("response"), dict) else None
            relation_version = QUESTION_VERSION if isinstance(answers, dict) and set(answers) == V3_QUESTION_IDS else "branch-relationship-v2"
        validated_version = validate_relation_response(record.get("response"), relation_version)
        versions.add(validated_version)
        evidence_profile = record.get("evidence_profile", relations.get("evidence_profile"))
        if evidence_profile is not None:
            _require_string(evidence_profile, f"relation {candidate_id} evidence profile")
        judgment_id = record.get("judgment_id")
        request_sha = record.get("request_sha256")
        if judgment_id is not None:
            _require_string(judgment_id, f"relation {candidate_id} judgment id")
        if request_sha is not None:
            _require_digest(request_sha, f"relation {candidate_id} request sha256")
        identities: list[tuple[Any, ...]] = []
        if judgment_id is not None:
            identities.append(("judgment", judgment_id))
        if request_sha is not None:
            identities.append(("request", request_sha))
        if not identities:
            response_value = record.get("response")
            model = response_value.get("model") if isinstance(response_value, dict) else None
            identities.append(("derived", candidate_id, validated_version, model, evidence_profile))
        record_digest = digest(record)
        for identity in identities:
            prior_digest = seen_judgment_identities.get(identity)
            if prior_digest is not None:
                if prior_digest == record_digest:
                    raise JgError(f"relations contain duplicate judgment identity: {identity[-1]}")
                raise JgError(f"relations contain conflicting duplicate judgment identity: {identity[-1]}")
        for identity in identities:
            seen_judgment_identities[identity] = record_digest
        if validated_version == QUESTION_VERSION and isinstance(record.get("response"), dict):
            response = record["response"]
            if "model" not in response and "usage" not in response:
                limitations.append("relation_runtime_provenance_missing")

    if records and root_question_version is None:
        limitations.append("historical_relations_question_version_inferred")
    if any(version in LEGACY_QUESTION_VERSIONS for version in versions):
        limitations.append("historical_relation_contract")
    return {
        "relation_count": len(records),
        "candidate_ids": sorted(seen_ids),
        "question_versions": sorted(versions),
        "mixed_question_versions": len(versions) > 1,
        "limitations": limitations,
    }


def validate_artifacts(
    inventory: dict[str, Any],
    candidates: dict[str, Any],
    relations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the complete inventory -> candidates -> relations chain."""

    inventory_info = validate_inventory(inventory)
    candidate_info = validate_candidates(candidates, inventory)
    limitations = list(candidate_info["limitations"])
    relation_info = None
    if relations is not None:
        relation_info = validate_relations(relations, candidates, inventory)
        limitations.extend(relation_info["limitations"])
    return {
        "inventory": inventory_info,
        "candidates": candidate_info,
        "relations": relation_info,
        "limitations": sorted(set(limitations)),
        "cleanup_readiness": "not_verified",
    }


validate_artifact_chain = validate_artifacts
