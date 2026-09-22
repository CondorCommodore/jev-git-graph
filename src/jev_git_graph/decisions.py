"""Conservative, reproducible branch decisions from local facts and Jev signals."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .artifacts import validate_candidates, validate_relations
from .errors import JgError
from .safety import digest, read_json, write_json


def _signal(response: dict[str, Any]) -> str | None:
    answers = response.get("answers", {})
    if not isinstance(answers, dict):
        return None
    relationship = answers.get("relationship")
    if isinstance(relationship, dict):
        choice = relationship.get("choice")
        return choice if isinstance(choice, str) and choice not in {"UNKNOWN", "INSUFFICIENT_EVIDENCE", "UNRELATED"} else None
    evidence = answers.get("evidence_sufficient")
    if not isinstance(evidence, dict) or evidence.get("noul", 0) < 0.75:
        return None
    for field in ("a_supersedes_b", "b_supersedes_a", "same_intent", "partial_overlap", "a_depends_on_b", "b_depends_on_a"):
        answer = answers.get(field)
        if isinstance(answer, dict) and isinstance(answer.get("noul"), (int, float)) and answer["noul"] >= 0.75:
            return field.upper()
    return None


def build_decisions(inventory: dict[str, Any], candidates: dict[str, Any], relations: dict[str, Any], equivalence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify all recorded branches; never authorize a destructive action."""
    if inventory.get("kind") != "inventory" or not isinstance(inventory.get("branches"), list):
        raise JgError("invalid inventory for automatic decisions")
    if candidates.get("inventory_digest") != digest(inventory):
        raise JgError("candidate artifact does not match inventory")
    candidate_info = validate_candidates(candidates)
    if candidates.get("repository_id") != inventory.get("repository", {}).get("id"):
        raise JgError("candidate artifact belongs to a different repository")
    relation_info = validate_relations(relations, candidates)
    if equivalence is not None:
        if (equivalence.get("kind") != "branch-equivalence" or
                equivalence.get("inventory_digest") != digest(inventory) or
                equivalence.get("repository_id") != inventory["repository"]["id"]):
            raise JgError("equivalence artifact does not match inventory")
    content_by_name = {item["name"]: item for item in equivalence["branches"]} if equivalence else {}
    if equivalence is not None:
        expected_tips = {item["name"]: item["tip"] for item in inventory["branches"]}
        if (len(content_by_name) != len(equivalence["branches"]) or
                {name: item.get("tip") for name, item in content_by_name.items()} != expected_tips):
            raise JgError("equivalence branch tips do not match inventory")
    complete = inventory.get("collection", {}).get("complete") is True
    candidate_by_id = {item["id"]: item for item in candidates["candidates"]}
    signals: dict[str, list[dict[str, str]]] = defaultdict(list)
    for relation in relations["relations"]:
        candidate = candidate_by_id.get(relation.get("candidate_id"))
        if candidate is None:
            continue
        label = _signal(relation.get("response", {}))
        if label is None:
            continue
        for side in ("a", "b"):
            endpoint = candidate["endpoints"][side]
            signals[endpoint["branch"]].append({"candidate_id": candidate["id"], "signal": label})
    occupied = {item.get("branch") for item in inventory.get("worktrees", []) if item.get("branch")}
    default = inventory["repository"].get("default_branch")
    records = []
    for branch in inventory["branches"]:
        name = branch["name"]
        unique = branch.get("unique_commits")
        if name == default:
            decision, reason = "RETAIN", "default_branch"
        elif name in occupied:
            decision, reason = "RETAIN", "checked_out_in_worktree"
        elif branch.get("merged_into_default") is True and unique == []:
            decision, reason = ("CLEANUP_CANDIDATE", "merged_without_unique_commits") if complete else ("HOLD", "incomplete_inventory")
        elif unique:
            decision, reason = "HOLD", "unique_work_present"
        else:
            decision, reason = "HOLD", "integration_unproven"
        records.append({
            "name": name, "tip": branch["tip"], "decision": decision, "reason": reason,
            "content_verdict": content_by_name.get(name, {}).get("content_verdict"),
            "content_proof": content_by_name.get(name, {}).get("proof"),
            "content_destination": content_by_name.get(name, {}).get("destination"),
            "equivalence_in_scope": content_by_name.get(name, {}).get("in_scope"),
            "equivalence_scope_reason": content_by_name.get(name, {}).get("scope_reason"),
            "triage": "RELATED_HOLD" if decision == "HOLD" and signals.get(name) else
                      "EVIDENCE_HOLD" if decision == "HOLD" else "FACT_DECISION",
            "jev_signals": signals.get(name, []), "destructive_action_authorized": False,
        })
    counts = {name: sum(item["decision"] == name for item in records) for name in ("RETAIN", "CLEANUP_CANDIDATE", "HOLD")}
    return {
        "kind": "automatic-branch-decisions", "schema_version": 1,
        "repository_id": inventory["repository"]["id"],
        "inventory_digest": digest(inventory), "candidate_digest": digest(candidates),
        "relations_digest": digest(relations), "inventory_complete": complete,
        "equivalence_digest": digest(equivalence) if equivalence else None,
        "candidate_coverage_complete": not candidates.get("coverage", {}).get("truncated", False),
        "candidate_count": len(candidates["candidates"]),
        "relation_count": len(relations["relations"]),
        "counts": counts, "branches": records,
        "limitations": candidate_info["limitations"] + relation_info["limitations"]
        + ([] if complete else ["incomplete_inventory_blocks_cleanup_candidates"]),
        "network_performed": False, "destructive_action_authorized": False,
    }


def write_decisions(inventory_path: str | Path, candidates_path: str | Path, relations_path: str | Path, output: str | Path,
                    equivalence_path: str | Path | None = None) -> Path:
    result = build_decisions(read_json(inventory_path), read_json(candidates_path), read_json(relations_path),
                             read_json(equivalence_path) if equivalence_path else None)
    target = Path(output) / "decisions.json"
    write_json(target, result)
    return target
