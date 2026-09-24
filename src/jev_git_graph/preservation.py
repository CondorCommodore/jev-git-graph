"""Read-only preservation queues derived from Git facts and reviewed evidence."""

from __future__ import annotations

from html import escape
import re
from pathlib import Path
from typing import Any

from .errors import JgError
from .review import DISPOSITIONS, object_fingerprint, object_id
from .safety import digest, read_json, write_json, write_private_text
from .decisions import _signal


LIKELY_TRUE = 0.75
_OUTCOME_LEDGER_RECEIPT_KIND = "branch-presence-outcome-ledger-receipt"
_OUTCOME_REVIEW_APPROVAL_KIND = "outcome-human-review-approval"


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


def _validated_outcomes(inventory: dict[str, Any], outcomes: dict[str, Any] | None,
                        objects: dict[str, tuple[str, dict[str, Any]]]) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    if outcomes is None:
        return {}, {}
    if (outcomes.get("kind") != "object-outcomes" or outcomes.get("schema_version") != 1
            or outcomes.get("outcomes_digest") != digest({
                key: value for key, value in outcomes.items() if key != "outcomes_digest"
            })
            or outcomes.get("repository_id") != inventory["repository"]["id"]
            or outcomes.get("inventory_digest") != digest(inventory)):
        raise JgError("outcomes do not match the complete pinned inventory")
    provenance = outcomes.get("review_provenance")
    if (not isinstance(provenance, dict)
            or provenance.get("repository_id") != inventory["repository"]["id"]
            or provenance.get("inventory_digest") != digest(inventory)
            or provenance.get("snapshot_digest") != outcomes.get("snapshot_digest")
            or provenance.get("contributions_digest") != outcomes.get("contributions_digest")
            or provenance.get("presence_digest") != outcomes.get("presence_digest")
            or set(provenance) != {"repository_id", "inventory_digest", "snapshot_digest",
                                   "contributions_digest", "presence_digest", "coverage_digest"}
            or any(not isinstance(provenance.get(field), str)
                   or re.fullmatch(r"[0-9a-f]{64}", provenance[field]) is None
                   for field in ("inventory_digest", "snapshot_digest", "contributions_digest"))
            or any(provenance[field] is not None and (
                not isinstance(provenance[field], str)
                or re.fullmatch(r"[0-9a-f]{64}", provenance[field]) is None
            ) for field in ("presence_digest", "coverage_digest"))):
        raise JgError("outcomes have invalid pinned review provenance")

    raw_objects = outcomes.get("objects")
    if not isinstance(raw_objects, list):
        raise JgError("outcomes objects must be an array")
    outcome_by_id: dict[str, dict[str, Any]] = {}
    for record in raw_objects:
        if not isinstance(record, dict):
            raise JgError("outcomes contain a malformed object record")
        key = record.get("object_id")
        if not isinstance(key, str) or key not in objects or key in outcome_by_id:
            raise JgError("outcomes contain an unknown or duplicate object")
        kind, item = objects[key]
        if (record.get("kind") != kind
                or record.get("source_fingerprint") != object_fingerprint(kind, item)
                or record.get("review_status") not in ("unreviewed", "current", "stale", "historical-limited")):
            raise JgError("outcome object identity or review status is stale or invalid")
        decision = record.get("human_decision")
        if record.get("review_status") == "current":
            if (not isinstance(decision, dict)
                    or decision.get("object_id") != key
                    or decision.get("source_fingerprint") != record["source_fingerprint"]
                    or decision.get("evidence_fingerprint") != record.get("review_evidence_fingerprint")
                    or decision.get("disposition") not in ("RETAIN", "INTEGRATE", "ARCHIVE_PROPOSED", "UNRESOLVED")
                    or not isinstance(decision.get("rationale"), str) or not decision["rationale"].strip()
                    or not isinstance(decision.get("reviewer_id"), str) or not decision["reviewer_id"].strip()
                    or decision.get("preservation_proof") is not None):
                raise JgError("current outcome review lacks an exact supported human decision")
        if kind == "branch":
            contribution_rows = record.get("contribution_reviews")
            contribution_ids = record.get("contribution_ids")
            if (not isinstance(contribution_rows, list) or not isinstance(contribution_ids, list)
                    or any(not isinstance(cid, str) for cid in contribution_ids)
                    or len(contribution_ids) != len(set(contribution_ids))
                    or {unit.get("contribution_id") for unit in contribution_rows if isinstance(unit, dict)}
                       != set(contribution_ids)):
                raise JgError("outcome branch does not account for its contribution units")
            for unit in contribution_rows:
                if (not isinstance(unit, dict)
                        or not isinstance(unit.get("contribution_id"), str)
                        or not isinstance(unit.get("source"), dict)
                        or not isinstance(unit.get("destination"), dict)
                        or unit.get("disposition") not in ("LIKELY_PRESERVED", "USABLE_WORK_REMAINS", "UNRESOLVED")
                        or unit.get("presence") not in ("PRESENT", "PARTIAL", "ABSENT", "UNKNOWN")
                        or unit.get("routing_scope") not in ("advisory_only", "production_review_candidate")
                        or unit.get("presence_origin") not in (None, "jev", "synthetic", "control")
                        or not isinstance(unit.get("reasons"), list)
                        or any(not isinstance(reason, str) for reason in unit.get("reasons", []))
                        or not isinstance(unit.get("evidence_ids"), list)
                        or any(not isinstance(evidence_id, str) for evidence_id in unit.get("evidence_ids", []))):
                    raise JgError("outcome contribution review is malformed")
                if unit.get("routing_scope") == "production_review_candidate" and (
                    unit.get("presence_origin") != "jev"
                    or unit.get("evidence_sufficient") is not True
                    or unit.get("comparison_context_complete") is not True
                    or unit.get("dependency_context_status") != "complete"
                    or unit.get("dependency_context_sufficient") is not True
                    or (unit.get("disposition") == "USABLE_WORK_REMAINS"
                        and (unit.get("presence") not in {"PARTIAL", "ABSENT"}
                             or unit.get("usable_delta") is not True))
                    or (unit.get("disposition") == "LIKELY_PRESERVED"
                        and (unit.get("presence") != "PRESENT" or unit.get("usable_delta") is not False))
                ):
                    raise JgError("outcome contribution has an invalid production routing claim")
        outcome_by_id[key] = record
    if set(outcome_by_id) != set(objects):
        raise JgError("outcomes do not account for every preservation object")

    tasks_by_object: dict[str, list[dict[str, Any]]] = {}
    raw_tasks = outcomes.get("integration_tasks")
    if not isinstance(raw_tasks, list):
        raise JgError("outcome integration tasks must be an array")
    seen_task_ids: set[str] = set()
    default_branch = inventory["repository"].get("default_branch")
    default_item = next((item for kind, item in objects.values()
                         if kind == "branch" and item.get("name") == default_branch), None)
    if default_item is None:
        raise JgError("outcome destination branch is absent from the inventory")
    for task in raw_tasks:
        if not isinstance(task, dict):
            raise JgError("outcome integration task is malformed")
        task_id = task.get("id")
        key = task.get("object_id")
        cid = task.get("contribution_id")
        if (not isinstance(task_id, str) or not task_id or task_id in seen_task_ids
                or not isinstance(key, str) or key not in outcome_by_id or not isinstance(cid, str)):
            raise JgError("outcome integration task has an invalid or duplicate identity")
        seen_task_ids.add(task_id)
        record = outcome_by_id[key]
        units = {unit.get("contribution_id"): unit
                 for unit in record.get("contribution_reviews", []) if isinstance(unit, dict)}
        unit = units.get(cid)
        source = task.get("source")
        destination = task.get("destination")
        if (record.get("kind") != "branch" or unit is None
                or not isinstance(source, dict) or not isinstance(destination, dict)
                or not isinstance(outcomes.get("presence_digest"), str)
                or re.fullmatch(r"[0-9a-f]{64}", outcomes["presence_digest"]) is None
                or task.get("status") != "PROPOSED"
                or source.get("branch") != record.get("name")
                or source.get("tip") != record.get("tip")
                or source.get("tip") != unit.get("source", {}).get("tip")
                or source.get("blob") != unit.get("source", {}).get("blob")
                or source.get("path") != unit.get("path")
                or destination.get("branch") != default_branch
                or destination.get("tip") != default_item.get("tip")
                or destination.get("candidate_ids") != unit.get("destination", {}).get("candidate_ids")
                or unit.get("disposition") != "USABLE_WORK_REMAINS"
                or unit.get("presence_origin") != "jev"
                or unit.get("routing_scope") != "production_review_candidate"
                or unit.get("evidence_sufficient") is not True
                or unit.get("usable_delta") is not True
                or unit.get("comparison_context_complete") is not True
                or unit.get("dependency_context_status") != "complete"
                or unit.get("dependency_context_sufficient") is not True):
            raise JgError("outcome task lacks matching current trusted contribution evidence")
        tasks_by_object.setdefault(key, []).append(task)

    approval_status = outcomes.get("human_review_approval_status", "none")
    approval = outcomes.get("human_review_approval")
    if approval_status not in ("none", "unverified", "verified"):
        raise JgError("outcome human review approval status is invalid")
    if approval_status == "verified":
        from .presence import _verify_signed_record
        if (not isinstance(approval, dict)
                or not _verify_signed_record(approval, _OUTCOME_REVIEW_APPROVAL_KIND)
                or approval.get("review_sha256") != outcomes.get("review_digest")
                or approval.get("repository_id") != inventory["repository"]["id"]
                or approval.get("provenance") != outcomes.get("review_document_provenance")):
            raise JgError("human review approval receipt is missing or invalid")
    elif approval is not None:
        raise JgError("outcomes contain an approval receipt without verified status")

    trusted_claim = bool(raw_tasks or approval_status == "verified") or any(
        unit.get("routing_scope") == "production_review_candidate" or unit.get("presence_origin") == "jev"
        for record in outcome_by_id.values()
        for unit in record.get("contribution_reviews", []) if isinstance(unit, dict)
    )
    outcome_receipt = outcomes.get("outcome_ledger_receipt")
    if outcome_receipt is not None or trusted_claim:
        from .presence import _verify_signed_record
        body = {key: value for key, value in outcomes.items()
                if key not in {"outcomes_digest", "outcome_ledger_receipt"}}
        receipt_valid = (
            isinstance(outcome_receipt, dict)
            and _verify_signed_record(outcome_receipt, _OUTCOME_LEDGER_RECEIPT_KIND)
            and outcome_receipt.get("outcomes_body_sha256") == digest(body)
            and outcome_receipt.get("repository_id") == inventory["repository"]["id"]
            and outcome_receipt.get("inventory_digest") == digest(inventory)
            and outcome_receipt.get("snapshot_digest") == outcomes.get("snapshot_digest")
            and outcome_receipt.get("contributions_digest") == outcomes.get("contributions_digest")
            and outcome_receipt.get("presence_digest") == provenance.get("presence_digest")
            and outcome_receipt.get("trusted_presence_receipt_digest")
                == outcomes.get("trusted_presence_receipt_digest")
            and outcome_receipt.get("review_approval_digest")
                == (digest(approval) if approval is not None else None)
        )
        if not receipt_valid:
            raise JgError("trusted Jev or human review claims lack a matching signed outcome receipt")
    return outcome_by_id, tasks_by_object


def build_preservation_plan(
    inventory: dict[str, Any],
    candidates: dict[str, Any] | None = None,
    relations: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
    outcomes: dict[str, Any] | None = None,
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
    outcome_by_id, outcome_tasks = _validated_outcomes(inventory, outcomes, objects)
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
        outcome = outcome_by_id.get(key)
        action_tasks = []
        if outcome is not None:
            review_status = outcome.get("review_status", "unreviewed")
            human_decision = outcome.get("human_decision")
            outcome_disposition = human_decision.get("disposition") if isinstance(human_decision, dict) else None
            for task in outcome_tasks.get(key, []):
                if (review_status == "current" and outcome_disposition == "INTEGRATE"
                        and human_decision.get("proposed_destination") == inventory["repository"].get("default_branch")
                        and outcome.get("review_approval_status") == "verified"):
                    state, blocker = "READY_FOR_IMPLEMENTATION", None
                elif (review_status == "current" and outcome_disposition == "INTEGRATE"
                      and outcome.get("review_approval_status") != "verified"):
                    state, blocker = "PROPOSED_AWAITING_HUMAN_VERIFICATION", "explicit_outcome_review_approval_receipt_required"
                else:
                    state = "BLOCKED"
                    blocker = ("outcome_review_stale" if review_status in {"stale", "historical-limited"}
                               else "current_human_integration_review_required" if review_status == "unreviewed"
                               else "integration_destination_not_pinned_default" if (
                                   review_status == "current" and outcome_disposition == "INTEGRATE"
                               )
                               else "current_human_disposition_does_not_select_integration")
                action_tasks.append({**task, "action_state": state, "blocked_reason": blocker})
            contribution_rows = outcome.get("contribution_reviews", [])
            for unit in contribution_rows:
                disposition = unit.get("disposition")
                if disposition == "UNRESOLVED" or unit.get("routing_scope") != "production_review_candidate":
                    suggestions.append(_suggestion(
                        "PRESENCE_EVIDENCE_HOLD",
                        "per-contribution evidence is unresolved or advisory and cannot route work",
                        [f"contribution:{unit.get('contribution_id')}", *unit.get("reasons", [])],
                    ))
            if outcome_tasks.get(key) and review_status != "current":
                suggestions.append(_suggestion(
                    "OUTCOME_REVIEW_REQUIRED",
                    "specific Jev integration tasks are blocked until a current human decision is recorded",
                    [f"outcome_review:{review_status}"],
                ))
        record = {
            "object_id": key,
            "kind": kind,
            "name": item.get("name") or item.get("reference") or item.get("path_id"),
            "source_fingerprint": object_fingerprint(kind, item),
            "suggestions": suggestions,
            "human_decision": decision,
            "preservation_proof": proof,
            "outcome_review": ({
                "status": outcome.get("review_status"),
                "decision": outcome.get("human_decision"),
                "contribution_reviews": outcome.get("contribution_reviews", []),
            } if outcome is not None else None),
            "integration_actions": action_tasks,
            "cleanup_authority": False,
            "content_verification": "not_available_from_metadata_only",
        }
        records.append(record)
        for suggestion in suggestions:
            queues[suggestion["queue"]] = queues.get(suggestion["queue"], 0) + 1

    records.sort(key=lambda record: record["object_id"])
    integration_actions = [action for record in records for action in record["integration_actions"]]
    return {
        "kind": "preservation-plan",
        "schema_version": 1,
        "repository_id": repository["id"],
        "inventory_digest": digest(inventory),
        "candidate_digest": digest(candidates) if candidates is not None else None,
        "relations_digest": digest(relations) if relations is not None else None,
        "review_digest": digest(review) if review is not None else None,
        "outcomes_digest": digest(outcomes) if outcomes is not None else None,
        "object_count": len(records),
        "unique_object_ids": len({record["object_id"] for record in records}),
        "integration_action_count": len(integration_actions),
        "ready_integration_count": sum(action["action_state"] == "READY_FOR_IMPLEMENTATION"
                                        for action in integration_actions),
        "blocked_integration_count": sum(action["action_state"] == "BLOCKED"
                                          for action in integration_actions),
        "awaiting_human_verification_count": sum(
            action["action_state"] == "PROPOSED_AWAITING_HUMAN_VERIFICATION"
            for action in integration_actions
        ),
        "queues": queues,
        "objects": records,
        "cleanup_readiness": "not_verified",
        "network_performed": bool(relations and relations.get("network_performed")),
    }


def render_preservation_plan(plan: dict[str, Any]) -> str:
    """Render the read-only canonical queue without exposing source excerpts."""
    rows = []
    for record in plan["objects"]:
        name = record.get("name") or record.get("reference") or record.get("path_id") or record["object_id"]
        suggestions = "<br>".join(
            escape(f"{item['queue']}: {item['reason']}") for item in record.get("suggestions", [])
        ) or "No queue entries"
        outcome = record.get("outcome_review") or {}
        decision = outcome.get("decision") or {}
        decision_state = f"{outcome.get('status', 'not supplied')} / {decision.get('disposition', 'none')}"
        units = []
        for unit in outcome.get("contribution_reviews", []):
            units.append(
                "<li>" + escape(str(unit.get("name") or unit.get("contribution_id")))
                + " · " + escape(str(unit.get("path") or "path unavailable"))
                + " · " + escape(str(unit.get("disposition", "UNRESOLVED")))
                + " · " + escape(str(unit.get("routing_scope", "advisory_only")))
                + " · " + escape(", ".join(unit.get("reasons", []))) + "</li>"
            )
        tasks = []
        for task in record.get("integration_actions", []):
            tasks.append(
                "<li>" + escape(str(task.get("id"))) + " · "
                + escape(str(task.get("behavior_to_preserve"))) + " · "
                + escape(str(task.get("action_state")))
                + " · destination " + escape(str((task.get("destination") or {}).get("branch", "unknown")))
                + (" · blocked: " + escape(str(task["blocked_reason"])) if task.get("blocked_reason") else "")
                + "<br>Verification: " + escape(str(task.get("verification", "not specified")))
                + "</li>"
            )
        details = "<ul>" + "".join(units) + "</ul>" if units else "No per-contribution findings"
        task_html = "<ul>" + "".join(tasks) + "</ul>" if tasks else "No integration tasks"
        rows.append(
            "<tr><td>" + escape(str(record["kind"])) + "</td><td>" + escape(str(name))
            + "</td><td>" + escape(str(decision_state)) + "</td><td>" + suggestions
            + "</td><td>" + details + "</td><td>" + task_html + "</td></tr>"
        )
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'><title>Preservation queue</title>"
        "<style>body{font:15px system-ui;margin:2rem}table{border-collapse:collapse;width:100%}"
        "td,th{padding:.6rem;text-align:left;border-bottom:1px solid #ccc;vertical-align:top}"
        "li{margin:.4rem 0}</style><h1>Preservation queue</h1>"
        f"<p>{plan['object_count']} inventory objects; {plan.get('integration_action_count', 0)} proposed integration tasks "
        f"({plan.get('ready_integration_count', 0)} ready for implementation, "
        f"{plan.get('blocked_integration_count', 0)} blocked, "
        f"{plan.get('awaiting_human_verification_count', 0)} awaiting explicit approval). Cleanup readiness: "
        f"{escape(str(plan['cleanup_readiness']))}. "
        "This queue proposes no Git or filesystem action; integration tasks marked ready still require implementation and package-outcome verification.</p>"
        "<p>Per-unit judgments show typed status, routing scope, and evidence limits only. Source excerpts are not included.</p>"
        "<table><thead><tr><th>Kind</th><th>Object</th><th>Outcome review</th><th>Queue</th>"
        "<th>Contribution evidence</th><th>Integration plan</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></html>"
    )


def write_preservation_plan(inventory_path: str | Path, out: str | Path,
                            candidates_path: str | Path | None = None,
                            relations_path: str | Path | None = None,
                            review_path: str | Path | None = None,
                            outcomes_path: str | Path | None = None) -> Path:
    inventory = read_json(inventory_path)
    plan = build_preservation_plan(
        inventory,
        read_json(candidates_path) if candidates_path else None,
        read_json(relations_path) if relations_path else None,
        read_json(review_path) if review_path else None,
        read_json(outcomes_path) if outcomes_path else None,
    )
    destination = Path(out).expanduser().resolve()
    if destination.exists():
        raise JgError("preservation output directory already exists; preserve the previous queue")
    destination.mkdir(mode=0o700, parents=True)
    write_json(destination / "preservation-plan.json", plan)
    write_private_text(destination / "index.html", render_preservation_plan(plan))
    return destination / "preservation-plan.json"
