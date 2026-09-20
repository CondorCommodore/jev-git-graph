from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import git
from .errors import JgError
from .safety import opaque_path_id, validate_output_path, write_json


SCHEMA_VERSION = 1


def protected_worktree_paths(repo: str | Path) -> list[Path]:
    _root, _common_dir, runner = git.open_repository(repo)
    return [Path(str(item["path"])).resolve() for item in git.worktrees(runner) if item.get("path")]


def build_inventory(repo: str | Path) -> tuple[dict[str, Any], list[Path], git.GitRunner]:
    root, common_dir, runner = git.open_repository(repo)
    observed_worktrees = git.worktrees(runner)
    protected_paths = [Path(str(item["path"])).resolve() for item in observed_worktrees if item.get("path")]
    branches = git.local_branches(runner)
    branch_names = {branch["name"] for branch in branches}
    default = git.default_branch(runner, branch_names)

    branch_records: list[dict[str, Any]] = []
    for branch in branches:
        base = git.merge_base(runner, default, branch["name"]) if default and branch["name"] != default else branch["tip"]
        commits = git.commits_since(runner, base, branch["name"]) if branch["name"] != default else []
        paths = git.changed_paths(runner, base, branch["name"]) if branch["name"] != default else []
        branch_records.append(
            {
                **branch,
                "merge_base": base,
                "unique_commits": commits,
                "changed_paths": paths,
                "merged_into_default": bool(default and branch["name"] != default and git.is_ancestor(runner, branch["name"], default)),
            }
        )

    worktree_records: list[dict[str, Any]] = []
    for item in observed_worktrees:
        path = Path(str(item["path"])).resolve()
        worktree_records.append(
            {
                "path_id": opaque_path_id(path),
                "head": item.get("head"),
                "branch": item.get("branch"),
                "detached": bool(item.get("detached")),
                "locked": bool(item.get("locked")),
                "status": git.status_for(path),
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
        "worktrees": worktree_records,
        "stashes": observed_stashes,
        "facts": facts,
        "collection": {"network": False, "remote_operations": False, "commands": [list(command[3:]) for command in runner.commands]},
    }
    return inventory, protected_paths, runner


def write_inventory(repo: str | Path, output: str | Path) -> Path:
    destination = validate_output_path(output, protected_worktree_paths(repo))
    inventory, _protected_paths, _runner = build_inventory(repo)
    destination.mkdir(parents=True, exist_ok=True)
    write_json(destination / "inventory.json", inventory)
    write_json(
        destination / "manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "jev-git-graph-run",
            "repository_id": inventory["repository"]["id"],
            "observed_at": inventory["observed_at"],
            "artifacts": ["inventory.json"],
            "network": False,
            "remote_operations": False,
        },
    )
    return destination / "inventory.json"
