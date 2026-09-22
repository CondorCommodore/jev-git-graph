"""Read-only, exact committed-content comparison against approved destinations."""

from __future__ import annotations

import subprocess
import time
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import git
from .errors import JgError
from .inventory import protected_worktree_paths
from .safety import digest, opaque_path_id, read_json, validate_output_path, write_json


def _probe(runner: git.GitRunner, *args: str) -> bool:
    command = ("git", "-C", str(runner.root), *args)
    runner.commands.append(command)
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    if result.returncode not in (0, 1):
        raise JgError("unable to compare Git objects")
    return result.returncode == 0


def _changed_paths(runner: git.GitRunner, base: str, tip: str) -> list[str]:
    # --no-renames includes both sides of a rename, unlike inventory's display paths.
    command = ("git", "-C", str(runner.root), "diff", "--name-only", "--no-renames", "-z", base, tip)
    runner.commands.append(command)
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise JgError("unable to inspect changed paths")
    return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]


def _same_paths(runner: git.GitRunner, tip: str, destination: str, paths: list[str]) -> bool:
    # Literal pathspecs prevent a filename containing Git pathspec syntax from
    # broadening the comparison. Chunking avoids ARG_MAX on large branches.
    for offset in range(0, len(paths), 128):
        chunk = paths[offset:offset + 128]
        if not _probe(runner, "diff", "--quiet", "--no-ext-diff", "--no-textconv",
                      "--no-renames", tip, destination, "--",
                      *(f":(literal){path}" for path in chunk)):
            return False
    return True


def _activity(runner: git.GitRunner, branch: dict[str, Any]) -> int | None:
    """Latest recorded commit or ref update; missing reflog means unknown activity."""
    try:
        committed = int(datetime.fromisoformat(branch["committed_at"]).timestamp())
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    shown = runner.try_run("reflog", "show", "--date=unix", "-1", "--format=%gd",
                           f"refs/heads/{branch['name']}")
    matched = re.search(r"@\{(\d+)\}$", shown.strip()) if shown else None
    if not matched:
        return None
    return max(committed, int(matched.group(1)))


def build_equivalence(repo: str | Path, inventory: dict[str, Any], destinations: list[str] | None = None,
                      ignore_recent_hours: float = 0) -> dict[str, Any]:
    started = time.monotonic()
    if ignore_recent_hours < 0:
        raise JgError("--ignore-recent-hours must be nonnegative")
    cutoff_epoch = int(datetime.now(timezone.utc).timestamp() - ignore_recent_hours * 3600) if ignore_recent_hours else None
    root, _common, runner = git.open_repository(repo)
    if inventory.get("kind") != "inventory" or inventory.get("repository", {}).get("id") != opaque_path_id(root):
        raise JgError("inventory belongs to a different local repository")
    default = inventory["repository"]["default_branch"]
    recorded = {item["name"]: item for item in inventory["branches"]}
    if default not in recorded:
        raise JgError("default branch missing from inventory")
    allowed = sorted(set(destinations or []))
    if default in allowed or any(name not in recorded for name in allowed):
        raise JgError("approved destinations must be recorded non-default local branches")
    live = {item["name"]: item["tip"] for item in git.local_branches(runner)}
    occupied = defaultdict(list)
    for worktree in inventory.get("worktrees", []):
        if worktree.get("branch"):
            occupied[worktree["branch"]].append({
                "path_id": worktree.get("path_id"), "dirty": None if worktree.get("status") is None else bool(worktree["status"]),
                "locked": bool(worktree.get("locked")),
            })
    destination_names = [default, *allowed]
    valid_destinations = {name: recorded[name]["tip"] for name in destination_names
                          if live.get(name) == recorded[name]["tip"]}
    tip_groups = defaultdict(list)
    tree_cache: dict[str, str] = {}
    def tree(sha: str) -> str:
        if sha not in tree_cache:
            tree_cache[sha] = runner.run("rev-parse", f"{sha}^{{tree}}").strip()
        return tree_cache[sha]
    for branch in inventory["branches"]:
        tip_groups[branch["tip"]].append(branch["name"])
    records = []
    for branch in inventory["branches"]:
        name, tip = branch["name"], branch["tip"]
        verdict, proof, target = "UNPROVEN", None, None
        reason = None
        worktree_hold = any(worktree["dirty"] is not False for worktree in occupied[name])
        activity_epoch = _activity(runner, branch) if cutoff_epoch is not None and name != default and not worktree_hold else None
        scope_reason = ("worktree_dirty_or_unavailable" if worktree_hold else
                        "activity_unverifiable" if activity_epoch is None else
                        "edited_within_lookback" if activity_epoch >= cutoff_epoch else None) if cutoff_epoch is not None and name != default else None
        in_scope = scope_reason is None
        if not in_scope:
            reason = scope_reason
        elif live.get(name) != tip:
            reason = "source_tip_changed_or_missing"
        elif name == default:
            verdict, reason = "ALREADY_PRESERVED", "default_branch"
        elif default not in valid_destinations:
            reason = "destinations_changed_or_missing"
        else:
            if len(valid_destinations) != len(destination_names):
                reason = "some_destinations_changed_or_missing"
            for dest_name in destination_names:
                dest_tip = valid_destinations.get(dest_name)
                if dest_tip is None or dest_name == name:
                    continue
                try:
                    if tip == dest_tip:
                        proof = "IDENTICAL_TIP"
                    elif git.is_ancestor(runner, tip, dest_tip):
                        proof = "ANCESTOR"
                    elif tree(tip) == tree(dest_tip):
                        proof = "IDENTICAL_TREE"
                    else:
                        base = git.merge_base(runner, tip, dest_tip)
                        if base is None:
                            reason = "no_common_ancestor"
                            continue
                        paths = _changed_paths(runner, base, tip)
                        if not paths:
                            proof = "NO_NET_CHANGE"
                        elif _same_paths(runner, tip, dest_tip, paths):
                            proof = "CHANGED_PATHS_IDENTICAL"
                    if proof:
                        verdict = "ALREADY_PRESERVED"
                        target = {"branch": dest_name, "tip": dest_tip, "scope": "main" if dest_name == default else "approved_branch"}
                        break
                except JgError:
                    reason = "git_evidence_unavailable"
            if verdict == "UNPROVEN" and reason is None:
                # Distinct committed state is observed, but intent/integration is not inferred.
                verdict, reason = "UNIQUE_WORK_REMAINS", "no_exact_preservation_proof"
        records.append({
            "name": name, "tip": tip, "content_verdict": verdict, "proof": proof,
            "destination": target, "reason": reason, "worktrees": occupied[name],
            "in_scope": in_scope, "scope_reason": scope_reason, "last_activity_epoch": activity_epoch,
            "duplicate_tip_branches": sorted(other for other in tip_groups[tip] if other != name),
        })
    live_after = {item["name"]: item["tip"] for item in git.local_branches(runner)}
    for item in records:
        target = item["destination"]
        if (live_after.get(item["name"]) != item["tip"] or
                (target and live_after.get(target["branch"]) != target["tip"])):
            item.update(content_verdict="UNPROVEN", proof=None, destination=None,
                        reason="refs_changed_during_equivalence")
    return {
        "kind": "branch-equivalence", "schema_version": 1,
        "repository_id": inventory["repository"]["id"], "inventory_digest": digest(inventory),
        "inventory_complete": inventory.get("collection", {}).get("complete") is True,
        "ignore_recent_hours": ignore_recent_hours,
        "activity_cutoff_epoch": cutoff_epoch,
        "approved_destinations": allowed, "branches": records,
        "counts": dict(Counter(item["content_verdict"] for item in records)),
        "scope_counts": dict(Counter(item["scope_reason"] or "included" for item in records)),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "network_performed": False, "destructive_action_authorized": False,
    }


def write_equivalence(repo: str | Path, inventory_path: str | Path, output: str | Path,
                      destinations: list[str] | None = None, ignore_recent_hours: float = 0) -> Path:
    target = validate_output_path(output, protected_worktree_paths(repo))
    result = build_equivalence(repo, read_json(inventory_path), destinations, ignore_recent_hours)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = target / "equivalence.json"
    write_json(path, result)
    return path
