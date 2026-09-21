from __future__ import annotations

from collections import defaultdict
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


def select_with_coverage(candidates: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    """Give each branch its strongest discovered connection before filling rank order."""
    selected = []
    covered: set[str] = set()
    selected_ids: set[str] = set()
    for candidate in candidates:
        names = {endpoint["branch"] for endpoint in candidate["endpoints"].values()}
        if names - covered:
            selected.append(candidate)
            selected_ids.add(candidate["id"])
            covered.update(names)
            if len(selected) >= maximum:
                return selected
    for candidate in candidates:
        if candidate["id"] not in selected_ids:
            selected.append(candidate)
            if len(selected) >= maximum:
                break
    return selected


def add_ancestry_evidence(runner: git.GitRunner, candidates: list[dict[str, Any]]) -> None:
    """Measure both directions against immutable tips, never branch labels."""
    cache: dict[tuple[str, str], tuple[int, int]] = {}
    for candidate in candidates:
        a = candidate["endpoints"]["a"]["tip"]
        b = candidate["endpoints"]["b"]["tip"]
        if (a, b) not in cache:
            counts = runner.run("rev-list", "--left-right", "--count", f"{a}...{b}").split()
            if len(counts) != 2 or not all(value.isdigit() for value in counts):
                raise JgError("invalid ancestry count from Git")
            cache[(a, b)] = (int(counts[0]), int(counts[1]))
        left, right = cache[(a, b)]
        candidate["evidence"].update({
            "identical_tips": a == b,
            "a_ancestor_of_b": left == 0,
            "b_ancestor_of_a": right == 0,
            "a_commits_not_in_b": left,
            "b_commits_not_in_a": right,
        })


def build_candidates(repo: str | Path, inventory_path: str | Path, maximum: int = 200, allow_incomplete: bool = False) -> dict[str, Any]:
    inventory = read_json(inventory_path)
    root, _common, runner = open_repository(repo)
    if inventory.get("repository", {}).get("id") != opaque_path_id(root):
        raise JgError("inventory belongs to a different local repository")
    if inventory.get("collection", {}).get("complete") is not True and not allow_incomplete:
        raise JgError("inventory is incomplete; cannot build candidates")
    branches = [branch for branch in inventory.get("branches", []) if branch.get("name") != inventory["repository"].get("default_branch")]
    path_sets = [set(branch.get("changed_paths", [])) for branch in branches]
    subject_sets = [_subject_tokens(branch) for branch in branches]
    signal_index: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, (paths, subjects) in enumerate(zip(path_sets, subject_sets)):
        for path in paths:
            signal_index[("path", path)].append(index)
        for subject in subjects:
            signal_index[("subject", subject)].append(index)
    pair_indices: set[tuple[int, int]] = set()
    skipped_common_signals = 0
    pair_budget_exhausted = False
    for _signal, indices in sorted(signal_index.items(), key=lambda item: (len(item[1]), item[0])):
        if len(indices) > 100:
            skipped_common_signals += 1
            continue
        for pair in combinations(indices, 2):
            pair_indices.add(pair)
            if len(pair_indices) >= 50_000:
                pair_budget_exhausted = True
                break
        if pair_budget_exhausted:
            break
    candidates: list[dict[str, Any]] = []
    for first_index, second_index in sorted(pair_indices):
        first, second = branches[first_index], branches[second_index]
        shared_paths = sorted(path_sets[first_index] & path_sets[second_index])
        shared_subjects = sorted(subject_sets[first_index] & subject_sets[second_index])
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
                "a_commit_subjects": [commit.get("subject", "") for commit in first.get("unique_commits", []) if commit.get("subject")],
                "b_commit_subjects": [commit.get("subject", "") for commit in second.get("unique_commits", []) if commit.get("subject")],
                "a_unique_commit_count": len(first.get("unique_commits", [])),
                "b_unique_commit_count": len(second.get("unique_commits", [])),
                "a_merge_base": first.get("merge_base"),
                "b_merge_base": second.get("merge_base"),
            },
        }
        candidates.append(candidate)
    candidates.sort(key=lambda item: (-len(item["evidence"]["shared_paths"]), -len(item["evidence"]["shared_subject_tokens"]), item["id"]))
    limited = select_with_coverage(candidates, maximum)
    branch_by_name = {branch["name"]: branch for branch in branches}
    patch_ids_by_name: dict[str, set[str | None]] = {}
    patch_ids_by_commit: dict[str, str | None] = {}
    for candidate in limited:
        a_name = candidate["endpoints"]["a"]["branch"]
        b_name = candidate["endpoints"]["b"]["branch"]
        for name in (a_name, b_name):
            if name not in patch_ids_by_name:
                for commit in branch_by_name[name].get("unique_commits", []):
                    if commit["sha"] not in patch_ids_by_commit:
                        patch_ids_by_commit[commit["sha"]] = git.patch_id(runner, commit["sha"])
                patch_ids_by_name[name] = {patch_ids_by_commit[commit["sha"]] for commit in branch_by_name[name].get("unique_commits", [])}
        a_patch_ids = patch_ids_by_name[a_name]
        b_patch_ids = patch_ids_by_name[b_name]
        shared_patches = sorted((a_patch_ids & b_patch_ids) - {None})
        candidate["evidence"]["shared_patch_ids"] = shared_patches
        if shared_patches:
            candidate["reasons"].insert(0, "PATCH_EQUIVALENCE")
    discovered_counts: dict[str, int] = defaultdict(int)
    add_ancestry_evidence(runner, limited)
    selected_counts: dict[str, int] = defaultdict(int)
    for candidate in candidates:
        for endpoint in candidate["endpoints"].values():
            discovered_counts[endpoint["branch"]] += 1
    for candidate in limited:
        for endpoint in candidate["endpoints"].values():
            selected_counts[endpoint["branch"]] += 1
    result = {
        "schema_version": inventory.get("schema_version"),
        "kind": "candidates",
        "repository_id": inventory["repository"]["id"],
        "inventory_digest": digest(inventory),
        "maximum": maximum,
        "candidate_count_before_limit": len(candidates),
        "candidate_count": len(limited),
        "coverage": {
            "strategy": "indexed deterministic signals",
            "selection": "strongest uncovered branch connections, then global rank",
            "inventory_complete": inventory.get("collection", {}).get("complete") is True,
            "branches": [{
                "branch": branch["name"], "tip": branch["tip"],
                "discovered_pairs": discovered_counts[branch["name"]],
                "selected_pairs": selected_counts[branch["name"]],
                "status": "selected" if selected_counts[branch["name"]] else "candidate_limit" if discovered_counts[branch["name"]] else "no_discovered_candidate",
            } for branch in inventory.get("branches", [])],
            "truncated": len(candidates) > len(limited) or skipped_common_signals > 0 or pair_budget_exhausted,
            "skipped_common_signals": skipped_common_signals,
            "pair_budget_exhausted": pair_budget_exhausted,
            "pairs_examined": len(pair_indices),
        },
        "candidates": limited,
    }
    result["content_digest"] = digest({
        "repository_id": result["repository_id"],
        "inventory_digest": result["inventory_digest"],
        "candidates": result["candidates"],
    })
    return result


def write_candidates(repo: str | Path, inventory_path: str | Path, output: str | Path, maximum: int = 200, allow_incomplete: bool = False) -> Path:
    destination = validate_output_path(output, protected_worktree_paths(repo))
    result = build_candidates(repo, inventory_path, maximum, allow_incomplete)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = destination / "candidates.json"
    write_json(target, result)
    return target
