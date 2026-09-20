from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import Any

from .errors import JgError
from . import git
from .git import open_repository
from .inventory import protected_worktree_paths
from .safety import digest, opaque_path_id, read_json, validate_output_path, write_json


def _subject_tokens(branch: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for commit in branch.get("unique_commits", []):
        for token in commit.get("subject", "").lower().replace("/", " ").replace("-", " ").split():
            if len(token) >= 4:
                tokens.add(token)
    return tokens


def build_candidates(repo: str | Path, inventory_path: str | Path, maximum: int = 200) -> dict[str, Any]:
    inventory = read_json(inventory_path)
    root, _common, runner = open_repository(repo)
    if inventory.get("repository", {}).get("id") != opaque_path_id(root):
        raise JgError("inventory belongs to a different local repository")
    branches = [branch for branch in inventory.get("branches", []) if branch.get("name") != inventory["repository"].get("default_branch")]
    candidates: list[dict[str, Any]] = []
    for first, second in combinations(branches, 2):
        first_paths = set(first.get("changed_paths", []))
        second_paths = set(second.get("changed_paths", []))
        shared_paths = sorted(first_paths & second_paths)
        shared_subjects = sorted(_subject_tokens(first) & _subject_tokens(second))
        reasons: list[str] = []
        if shared_paths:
            reasons.append("CHANGED_PATH_OVERLAP")
        if shared_subjects:
            reasons.append("COMMIT_SUBJECT_OVERLAP")
        if not reasons:
            continue
        endpoints = {
            "a": {"branch": first["name"], "tip": first["tip"]},
            "b": {"branch": second["name"], "tip": second["tip"]},
        }
        candidate = {
            "id": digest(endpoints)[:24],
            "endpoints": endpoints,
            "reasons": reasons,
            "evidence": {
                "shared_paths": shared_paths,
                "shared_subject_tokens": shared_subjects,
                "a_unique_commit_count": len(first.get("unique_commits", [])),
                "b_unique_commit_count": len(second.get("unique_commits", [])),
                "a_merge_base": first.get("merge_base"),
                "b_merge_base": second.get("merge_base"),
            },
        }
        candidates.append(candidate)
    candidates.sort(key=lambda item: (-len(item["evidence"]["shared_paths"]), -len(item["evidence"]["shared_subject_tokens"]), item["id"]))
    limited = candidates[:maximum]
    for candidate in limited:
        a_name = candidate["endpoints"]["a"]["branch"]
        b_name = candidate["endpoints"]["b"]["branch"]
        a_branch = next(branch for branch in branches if branch["name"] == a_name)
        b_branch = next(branch for branch in branches if branch["name"] == b_name)
        a_patch_ids = {git.patch_id(runner, commit["sha"]) for commit in a_branch.get("unique_commits", [])}
        b_patch_ids = {git.patch_id(runner, commit["sha"]) for commit in b_branch.get("unique_commits", [])}
        shared_patches = sorted((a_patch_ids & b_patch_ids) - {None})
        candidate["evidence"]["shared_patch_ids"] = shared_patches
        if shared_patches:
            candidate["reasons"].insert(0, "PATCH_EQUIVALENCE")
    return {
        "schema_version": inventory.get("schema_version"),
        "kind": "candidates",
        "repository_id": inventory["repository"]["id"],
        "inventory_digest": digest(inventory),
        "maximum": maximum,
        "candidate_count_before_limit": len(candidates),
        "candidate_count": len(limited),
        "coverage": {"strategy": "pairwise deterministic signals", "truncated": len(candidates) > len(limited)},
        "candidates": limited,
    }


def write_candidates(repo: str | Path, inventory_path: str | Path, output: str | Path, maximum: int = 200) -> Path:
    destination = validate_output_path(output, protected_worktree_paths(repo))
    result = build_candidates(repo, inventory_path, maximum)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "candidates.json"
    write_json(target, result)
    return target
