"""Read-only preservation queues derived from Git facts and reviewed evidence."""

from __future__ import annotations

from typing import Any

from .errors import JgError
from .review import DISPOSITIONS, object_fingerprint, object_id
from .safety import digest
from .decisions import _signal


LIKELY_TRUE = 0.75


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise JgError(f"{label} must be a list")
    return value


def _object_index(inventory: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    objects: dict[str, tuple[str, dict[str, Any]]] = {}
    for kind in ("branch", "worktree", "stash"):
        for item in _require_list(inventory.get({"branch": "branches", "worktree": "worktrees", "stash": "stashes"}[kind]), f"inventory {kind}s"):
            if not isinstance(item, dict):
                raise JgError(f"inventory {kind} record must be an object")
            required = {
                "branch": ("name", "tip"),
                "worktree": ("path_id", "head"),
                "stash": ("reference", "sha"),
            }[kind]
            if any(not isinstance(item.get(field), str) or not item[field] for field in required):
                raise JgError(f"inventory {kind} record lacks identity fields")
            key = object_id(kind, item)
            if key in objects:
                raise JgError(f"duplicate preservation object: {key}")
            objects[key] = (kind, item)
    return objects


def _suggestion(queue: str, reason: str, evidence: list[str] | None = None) -> dict[str, Any]:
    return {"queue": queue, "reason": reason, "evidence": list(evidence or [])}


def _semantic_suggestions(candidate: dict[str, Any], relation: dict[str, Any]) -> list[dict[str, Any]]:
    response = relation.get("response")
    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict):
        return []
    if _signal(response) is None:
        return []
    suggestions: list[dict[str, Any]] = []
    candidate_id = candidate.get("id", "")
    for question_id, answer in answers.items():
        value = answer.get("noul") if isinstance(answer, dict) else None
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value or not 0 <= value <= 1:
            continue
        if value < LIKELY_TRUE:
            continue
        if question_id == "same_intent":
            queue, reason = "SEMANTIC_SAME_INTENT", "Jev suggested shared intent; unique work still needs human review"
        elif question_id in {"partial_overlap", "a_depends_on_b", "b_depends_on_a", "a_supersedes_b", "b_supersedes_a"}:
            queue, reason = "SEMANTIC_REVIEW", f"Jev suggested {question_id}; no disposition is inferred"
        else:
            continue
        suggestions.append(_suggestion(queue, reason, [f"candidate:{candidate_id}", f"question:{question_id}"]))
    return suggestions


def build_preservation_plan(
    inventory: dict[str, Any],
    candidates: dict[str, Any] | None = None,
    relations: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build non-destructive queues with exactly one record per inventory object.

    Suggestions are derived from immutable Git facts and optional Jev evidence.
    Human decisions and verified preservation proof are copied only from the
    review ledger; neither is synthesized from a suggestion.
    """

    repository = inventory.get("repository")
    if not isinstance(repository, dict) or not isinstance(repository.get("id"), str):
        raise JgError("preservation plan requires repository identity")
    if inventory.get("collection", {}).get("complete") is not True:
        raise JgError("preservation plan requires a complete inventory")
    objects = _object_index(inventory)
    candidate_list = _require_list((candidates or {}).get("candidates", []), "candidates")
    candidate_by_id = {item.get("id"): item for item in candidate_list if isinstance(item, dict) and isinstance(item.get("id"), str)}
    relation_list = _require_list((relations or {}).get("relations", []), "relations")
    relation_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for relation in relation_list:
        if not isinstance(relation, dict) or not isinstance(relation.get("candidate_id"), str):
            raise JgError("relation record must contain candidate_id")
        relation_by_candidate.setdefault(relation["candidate_id"], []).append(relation)

    decisions: dict[str, dict[str, Any]] = {}
    if review is not None:
        raw_decisions = review.get("decisions")
        if not isinstance(raw_decisions, list):
            raise JgError("review decisions must be a list")
        for decision in raw_decisions:
            if not isinstance(decision, dict) or not isinstance(decision.get("object_id"), str):
                raise JgError("review decision lacks object_id")
            if decision["object_id"] in decisions:
                raise JgError(f"duplicate review decision: {decision['object_id']}")
            if decision["object_id"] not in objects:
                raise JgError(f"review decision references unknown object: {decision['object_id']}")
            if decision.get("disposition") not in DISPOSITIONS:
                raise JgError(f"unsupported review disposition: {decision.get('disposition')}")
            decisions[decision["object_id"]] = decision

    queues: dict[str, int] = {}
    records: list[dict[str, Any]] = []
    for key, (kind, item) in objects.items():
        suggestions: list[dict[str, Any]] = []
        if kind == "branch":
            if item.get("name") == repository.get("default_branch"):
                suggestions.append(_suggestion("DEFAULT_BRANCH", "canonical default branch is retained"))
            elif item.get("merged_into_default") and not item.get("unique_commits"):
                suggestions.append(_suggestion("FACT_MERGED_NO_UNIQUE_COMMITS", "Git ancestry shows no unique commits; human review still required"))
            elif item.get("unique_commits"):
                suggestions.append(_suggestion("UNIQUE_COMMIT_REVIEW", "branch contains commits not represented by the default branch"))
        elif kind == "worktree":
            if item.get("status"):
                suggestions.append(_suggestion("DIRTY_WORKTREE", "worktree has uncommitted metadata; content preservation is not verified"))
            else:
                suggestions.append(_suggestion("WORKTREE_REVIEW", "linked worktree requires an explicit human decision"))
        else:
            suggestions.append(_suggestion("STASH_REVIEW", "stash is retained until a preservation destination is verified"))

        for candidate in candidate_list:
            if not isinstance(candidate, dict):
                raise JgError("candidate record must be an object")
            endpoints = candidate.get("endpoints", {})
            if not isinstance(endpoints, dict):
                raise JgError("candidate endpoints must be an object")
            endpoint_ids: set[str] = set()
            for endpoint in endpoints.values():
                if not isinstance(endpoint, dict) or not isinstance(endpoint.get("branch"), str) or not endpoint["branch"] or not isinstance(endpoint.get("tip"), str) or not endpoint["tip"]:
                    raise JgError("candidate endpoint lacks branch or tip identity")
                endpoint_ids.add(object_id("branch", {"name": endpoint["branch"], "tip": endpoint["tip"]}))
            if key not in endpoint_ids:
                continue
            evidence = candidate.get("evidence", {})
            if evidence.get("identical_tips"):
                suggestions.append(_suggestion("FACT_IDENTICAL_TIP", "branches point to the same immutable tip", [f"candidate:{candidate.get('id')}"]))
            if evidence.get("shared_patch_ids"):
                suggestions.append(_suggestion("FACT_PATCH_EQUIVALENT", "candidate contains exact patch-equivalent commits", [f"candidate:{candidate.get('id')}"]))
            for relation in relation_by_candidate.get(candidate.get("id"), []):
                suggestions.extend(_semantic_suggestions(candidate, relation))

        decision = decisions.get(key)
        proof = decision.get("preservation_proof") if decision else None
        record = {
            "object_id": key,
            "kind": kind,
            "source_fingerprint": object_fingerprint(kind, item),
            "suggestions": suggestions,
            "human_decision": decision,
            "preservation_proof": proof,
            "cleanup_authority": False,
            "content_verification": "not_available_from_metadata_only",
        }
        records.append(record)
        for suggestion in suggestions:
            queues[suggestion["queue"]] = queues.get(suggestion["queue"], 0) + 1

    records.sort(key=lambda record: record["object_id"])
    return {
        "kind": "preservation-plan",
        "schema_version": 1,
        "repository_id": repository["id"],
        "inventory_digest": digest(inventory),
        "candidate_digest": digest(candidates) if candidates is not None else None,
        "relations_digest": digest(relations) if relations is not None else None,
        "review_digest": digest(review) if review is not None else None,
        "object_count": len(records),
        "unique_object_ids": len({record["object_id"] for record in records}),
        "queues": queues,
        "objects": records,
        "cleanup_readiness": "not_verified",
        "network_performed": bool(relations and relations.get("network_performed")),
    }
