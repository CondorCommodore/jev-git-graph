from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import JgError
from .safety import digest, read_json, write_json


def build_plan(inventory_path: str | Path, candidates_path: str | Path, relations_path: str | Path | None = None) -> tuple[dict[str, Any], str]:
    inventory = read_json(inventory_path)
    candidates = read_json(candidates_path)
    if inventory.get("repository", {}).get("id") != candidates.get("repository_id"):
        raise JgError("inventory and candidates belong to different repositories")
    relations = read_json(relations_path) if relations_path else None
    relation_by_candidate = {item.get("candidate_id"): item for item in (relations or {}).get("relations", [])}
    default = inventory["repository"].get("default_branch")
    disposition: list[dict[str, Any]] = []
    for branch in inventory.get("branches", []):
        if branch["name"] == default:
            status, reason = "PRESERVE_IN_BRANCH", "canonical default branch"
        elif branch.get("merged_into_default") and not branch.get("unique_commits"):
            status, reason = "CLEANUP_CANDIDATE", "ancestry proves it is merged and it has no unique commits"
        else:
            status, reason = "UNRESOLVED", "requires maintainer review before any cleanup"
        disposition.append({"kind": "branch", "name": branch["name"], "tip": branch["tip"], "disposition": status, "reason": reason})
    for worktree in inventory.get("worktrees", []):
        if worktree.get("status"):
            disposition.append({"kind": "worktree", "path_id": worktree["path_id"], "disposition": "UNRESOLVED", "reason": "dirty or unavailable worktree state must be reviewed"})
    for stash in inventory.get("stashes", []):
        disposition.append({"kind": "stash", "reference": stash["reference"], "sha": stash["sha"], "disposition": "UNRESOLVED", "reason": "stash content must be preserved or explicitly reviewed"})

    plan = {
        "kind": "review-plan",
        "schema_version": inventory.get("schema_version"),
        "repository_id": inventory["repository"]["id"],
        "inventory_digest": digest(inventory),
        "candidate_digest": digest(candidates),
        "relations_digest": digest(relations) if relations else None,
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


def write_plan(inventory_path: str | Path, candidates_path: str | Path, output: str | Path, relations_path: str | Path | None = None) -> tuple[Path, Path]:
    plan, rendered = build_plan(inventory_path, candidates_path, relations_path)
    destination = Path(output).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "plan.json"
    markdown_path = destination / "plan.md"
    write_json(json_path, plan)
    markdown_path.write_text(rendered, encoding="utf-8")
    return json_path, markdown_path
