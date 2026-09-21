from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import git
from .errors import JgError
from .safety import digest, opaque_path_id, validate_output_path, write_json


SCHEMA_VERSION = 1


def protected_worktree_paths(repo: str | Path) -> list[Path]:
    _root, _common_dir, runner = git.open_repository(repo)
    return [Path(str(item["path"])).resolve() for item in git.worktrees(runner) if item.get("path")]


def build_inventory(repo: str | Path) -> tuple[dict[str, Any], list[Path], git.GitRunner]:
    root, common_dir, runner = git.open_repository(repo)
    refs_before = git.ref_snapshot(runner)
    observed_worktrees = git.worktrees(runner)
    protected_paths = [Path(str(item["path"])).resolve() for item in observed_worktrees if item.get("path")]
    branches = git.local_branches(runner)
    remote_refs = git.remote_tracking_refs(runner)
    branch_names = {branch["name"] for branch in branches}
    default = git.default_branch(runner, branch_names)
    default_tip = next((branch["tip"] for branch in branches if branch["name"] == default), None)
    if default_tip is None:
        raise JgError("unable to identify a local default branch")

    branch_records: list[dict[str, Any]] = []
    for branch in branches:
        tip = branch["tip"]
        base = git.merge_base(runner, default_tip, tip) if branch["name"] != default else tip
        commits = git.commits_since(runner, base, tip) if branch["name"] != default else []
        paths = git.changed_paths(runner, base, tip) if branch["name"] != default else []
        branch_records.append(
            {
                **branch,
                "merge_base": base,
                "unique_commits": commits,
                "changed_paths": paths,
                "merged_into_default": bool(branch["name"] != default and git.is_ancestor(runner, tip, default_tip)),
            }
        )

    worktree_records: list[dict[str, Any]] = []
    collection_errors: list[dict[str, str]] = []
    for item in observed_worktrees:
        path = Path(str(item["path"])).resolve()
        try:
            status = git.status_for(path)
        except JgError:
            status = None
            collection_errors.append({"kind": "worktree_status_unavailable", "path_id": opaque_path_id(path)})
        worktree_records.append(
            {
                "path_id": opaque_path_id(path),
                "head": item.get("head"),
                "branch": item.get("branch"),
                "detached": bool(item.get("detached")),
                "locked": bool(item.get("locked")),
                "status": status,
            }
        )

    facts: list[dict[str, Any]] = []
    for branch in branch_records:
        facts.append({"type": "POINTS_TO", "branch": branch["name"], "commit": branch["tip"]})
    for worktree in worktree_records:
        if worktree.get("branch"):
            facts.append({"type": "CHECKED_OUT_AT", "branch": worktree["branch"], "worktree_path_id": worktree["path_id"]})
    for branch in branch_records:
        if branch["name"] != default and branch["merged_into_default"]:
            facts.append({"type": "MERGED_INTO", "branch": branch["name"], "target": default})
    observed_stashes = git.stashes(runner)
    for stash in observed_stashes:
        facts.append({"type": "STASH_PRESENT", "stash": stash["reference"], "commit": stash["sha"]})

    refs_after = git.ref_snapshot(runner)
    worktrees_after = git.worktrees(runner)
    stashes_after = git.stashes(runner)
    if refs_before != refs_after:
        collection_errors.append({"kind": "refs_changed_during_inventory"})
    if observed_worktrees != worktrees_after:
        collection_errors.append({"kind": "worktrees_changed_during_inventory"})
    if observed_stashes != stashes_after:
        collection_errors.append({"kind": "stashes_changed_during_inventory"})

    inventory = {
        "schema_version": SCHEMA_VERSION,
        "kind": "inventory",
        "observed_at": datetime.now(UTC).isoformat(),
        "repository": {
            "id": opaque_path_id(root),
            "common_dir_id": opaque_path_id(common_dir),
            "default_branch": default,
            "head": runner.run("rev-parse", "HEAD").strip(),
        },
        "branches": branch_records,
        "remote_tracking_refs": remote_refs,
        "worktrees": worktree_records,
        "stashes": observed_stashes,
        "facts": facts,
        "collection": {
            "network": False,
            "remote_operations": False,
            "complete": not collection_errors,
            "errors": collection_errors,
            "ref_snapshot_digest": digest(refs_before),
            "counts": {"branches": len(branches), "remote_tracking_refs": len(remote_refs), "worktrees": len(worktree_records), "stashes": len(observed_stashes)},
            "commands": [list(command[3:]) for command in runner.commands],
        },
    }
    return inventory, protected_paths, runner


def write_inventory(repo: str | Path, output: str | Path) -> Path:
    destination = validate_output_path(output, protected_worktree_paths(repo))
    inventory, _protected_paths, _runner = build_inventory(repo)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(destination / "inventory.json", inventory)
    complete = inventory["collection"]["complete"]
    write_json(
        destination / "manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "jev-git-graph-run",
            "repository_id": inventory["repository"]["id"],
            "observed_at": inventory["observed_at"],
            "artifacts": ["inventory.json"],
            "complete": complete,
            "counts": inventory["collection"]["counts"],
            "errors": inventory["collection"]["errors"],
            "network": False,
            "remote_operations": False,
        },
    )
    if not complete:
        raise JgError(f"inventory incomplete; diagnostic artifacts written to {destination}")
    return destination / "inventory.json"
