from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import JgError
from .review import object_fingerprint, object_id, validate_review
from .safety import digest, read_json, write_json, write_private_text


def build_plan(inventory_path: str | Path, candidates_path: str | Path, relations_path: str | Path | None = None, review_path: str | Path | None = None) -> tuple[dict[str, Any], str]:
    inventory = read_json(inventory_path)
    candidates = read_json(candidates_path)
    if inventory.get("repository", {}).get("id") != candidates.get("repository_id"):
        raise JgError("inventory and candidates belong to different repositories")
    if inventory.get("collection", {}).get("complete") is not True:
        raise JgError("inventory is incomplete; cannot build a review plan")
    if candidates.get("inventory_digest") != digest(inventory):
        raise JgError("candidates were built from a different inventory")
    relations = read_json(relations_path) if relations_path else None
    review = read_json(review_path) if review_path else None
    if relations:
        if relations.get("repository_id") and relations["repository_id"] != candidates.get("repository_id"):
            raise JgError("relations belong to a different repository")
        if relations.get("candidate_content_digest") and relations["candidate_content_digest"] != candidates.get("content_digest"):
            raise JgError("relations were built from different candidate content")
    if review:
        if review.get("inventory_digest") and review["inventory_digest"] != digest(inventory):
            raise JgError("review was exported from a different inventory")
        if review.get("candidate_digest") and review["candidate_digest"] != digest(candidates):
            raise JgError("review was exported from different candidates")
        if review.get("relations_digest") and relations and review["relations_digest"] != digest(relations):
            raise JgError("review was exported from different relations")
    reviews = validate_review(review, inventory["repository"]["id"]) if review else {}
    relation_by_candidate = {item.get("candidate_id"): item for item in (relations or {}).get("relations", [])}
    default = inventory["repository"].get("default_branch")
    disposition: list[dict[str, Any]] = []
    stale_reviews = 0

    def reviewed(kind: str, item: dict[str, Any], fallback: str, reason: str) -> tuple[str, str, str]:
        nonlocal stale_reviews
        decision = reviews.get(object_id(kind, item))
        if not decision:
            return fallback, reason, "unreviewed"
        if decision["fingerprint"] != object_fingerprint(kind, item):
            stale_reviews += 1
            return "UNRESOLVED", "prior human review is stale because immutable inputs changed", "stale"
        return decision["disposition"], decision["rationale"], "current"

    for branch in inventory.get("branches", []):
        if branch["name"] == default:
            status, reason = "PRESERVE_IN_BRANCH", "canonical default branch"
        elif branch.get("merged_into_default") and not branch.get("unique_commits"):
            status, reason = "CLEANUP_CANDIDATE", "ancestry proves it is merged and it has no unique commits"
        else:
            status, reason = "UNRESOLVED", "requires maintainer review before any cleanup"
        status, reason, review_status = reviewed("branch", branch, status, reason)
        disposition.append({"kind": "branch", "name": branch["name"], "tip": branch["tip"], "disposition": status, "reason": reason, "review_status": review_status})
    for worktree in inventory.get("worktrees", []):
        status, reason, review_status = reviewed("worktree", worktree, "UNRESOLVED", "worktree state must be reviewed")
        disposition.append({"kind": "worktree", "path_id": worktree["path_id"], "disposition": status, "reason": reason, "review_status": review_status})
    for stash in inventory.get("stashes", []):
        status, reason, review_status = reviewed("stash", stash, "UNRESOLVED", "stash content must be preserved or explicitly reviewed")
        disposition.append({"kind": "stash", "reference": stash["reference"], "sha": stash["sha"], "disposition": status, "reason": reason, "review_status": review_status})

    plan = {
        "kind": "review-plan",
        "schema_version": inventory.get("schema_version"),
        "repository_id": inventory["repository"]["id"],
        "inventory_digest": digest(inventory),
        "candidate_digest": digest(candidates),
        "relations_digest": digest(relations) if relations else None,
        "review_digest": digest(review) if review else None,
        "reviewed_count": sum(item["review_status"] == "current" for item in disposition),
        "stale_review_count": stale_reviews,
        "unreviewed_count": sum(item["review_status"] == "unreviewed" for item in disposition),
        "network_performed": bool(relations and relations.get("network_performed")),
        "dispositions": disposition,
        "candidate_count": len(candidates.get("candidates", [])),
        "relation_count": len(relation_by_candidate),
    }
    lines = ["# Git relationship review plan", "", "This report proposes no destructive action. Review every unresolved item before any cleanup.", "", "## Dispositions", "", "| Kind | Item | Disposition | Reason |", "| --- | --- | --- | --- |"]
    for item in disposition:
        name = item.get("name") or item.get("reference") or item.get("path_id")
        lines.append(f"| {item['kind']} | `{name}` | {item['disposition']} | {item['reason']} |")
    lines.extend(["", "## Candidate relationships", ""])
    if not candidates.get("candidates"):
        lines.append("No candidate relationship met the deterministic evidence threshold.")
    else:
        for candidate in candidates["candidates"]:
            a, b = candidate["endpoints"]["a"]["branch"], candidate["endpoints"]["b"]["branch"]
            answer = relation_by_candidate.get(candidate["id"], {}).get("response")
            relation_note = "No Jev response recorded." if answer is None else "Jev response recorded; maintainer review still required."
            lines.append(f"- `{a}` ↔ `{b}`: {', '.join(candidate['reasons'])}. {relation_note}")
    return plan, "\n".join(lines) + "\n"


def write_plan(inventory_path: str | Path, candidates_path: str | Path, output: str | Path, relations_path: str | Path | None = None, review_path: str | Path | None = None) -> tuple[Path, Path]:
    plan, rendered = build_plan(inventory_path, candidates_path, relations_path, review_path)
    destination = Path(output).expanduser().resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    json_path = destination / "plan.json"
    markdown_path = destination / "plan.md"
    write_json(json_path, plan)
    write_private_text(markdown_path, rendered)
    return json_path, markdown_path
