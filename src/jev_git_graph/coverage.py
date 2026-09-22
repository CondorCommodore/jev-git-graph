"""Byte-level coverage of each recorded local branch by pinned main."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import git
from .equivalence import _activity
from .errors import JgError
from .inventory import protected_worktree_paths
from .safety import digest, opaque_path_id, read_json, validate_output_path, write_json


def _tree(runner: git.GitRunner, tip: str) -> dict[str, dict[str, str]]:
    command = ("git", "-C", str(runner.root), "ls-tree", "-rz", "--full-tree", tip)
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise JgError("unable to read pinned Git tree")
    entries = {}
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, kind, blob = meta.decode("ascii").split()
        if kind not in {"blob", "commit"}:
            raise JgError("unexpected Git tree entry")
        entries[os.fsdecode(path)] = {"mode": mode, "blob": blob, "kind": kind}
    return entries


def build_coverage(repo: str | Path, inventory: dict[str, Any], recent_hours: float = 24) -> dict[str, Any]:
    if recent_hours < 0:
        raise JgError("recent hours must be nonnegative")
    root, _common, runner = git.open_repository(repo)
    if inventory.get("kind") != "inventory" or inventory.get("repository", {}).get("id") != opaque_path_id(root):
        raise JgError("inventory belongs to a different repository")
    if inventory.get("collection", {}).get("complete") is not True:
        raise JgError("coverage requires a complete inventory")
    branches = {b["name"]: b for b in inventory["branches"]}
    main_name = inventory["repository"]["default_branch"]
    main_tip = branches[main_name]["tip"]
    live = {b["name"]: b["tip"] for b in git.local_branches(runner)}
    main_tree = _tree(runner, main_tip) if live.get(main_name) == main_tip else None
    now = int(datetime.now(UTC).timestamp())
    cutoff = now - int(recent_hours * 3600)
    records = []
    for branch in inventory["branches"]:
        name, tip = branch["name"], branch["tip"]
        activity = _activity(runner, branch) if name != main_name and recent_hours else None
        reason = None
        paths = []
        if name == main_name:
            verdict = "EXACT"
        elif live.get(name) != tip or main_tree is None:
            verdict, reason = "UNKNOWN", "pinned_ref_changed"
        elif recent_hours and activity is None:
            verdict, reason = "UNKNOWN", "activity_unverifiable"
        elif recent_hours and activity >= cutoff:
            verdict, reason = "UNKNOWN", "recent_activity"
        else:
            base = git.merge_base(runner, tip, main_tip)
            if base is None:
                verdict, reason = "UNKNOWN", "no_merge_base"
            else:
                try:
                    base_tree = _tree(runner, base)
                    source_tree = _tree(runner, tip)
                    changed = sorted(path for path in base_tree.keys() | source_tree.keys()
                                     if base_tree.get(path) != source_tree.get(path))
                    for path in changed:
                        source = source_tree.get(path)
                        destination = main_tree.get(path)
                        paths.append({"path": path, "source": source, "main": destination,
                                      "verdict": "EXACT_PRESENT" if source == destination else "DISTINCT"})
                    verdict = "EXACT" if all(p["verdict"] == "EXACT_PRESENT" for p in paths) else "DISTINCT"
                except JgError:
                    verdict, reason = "UNKNOWN", "tree_unavailable"
        records.append({"name": name, "tip": tip, "main_tip": main_tip,
                        "verdict": verdict, "reason": reason, "last_activity_epoch": activity,
                        "paths": paths})
    after = {b["name"]: b["tip"] for b in git.local_branches(runner)}
    for record in records:
        if after.get(record["name"]) != record["tip"] or after.get(main_name) != main_tip:
            record.update(verdict="UNKNOWN", reason="refs_changed_during_coverage", paths=[])
    return {"kind": "branch-coverage", "schema_version": 1,
            "repository_id": inventory["repository"]["id"], "inventory_digest": digest(inventory),
            "main": {"name": main_name, "tip": main_tip}, "recent_hours": recent_hours,
            "activity_cutoff_epoch": cutoff, "branches": records,
            "network_performed": False, "destructive_action_authorized": False}


def write_coverage(repo: str | Path, inventory_path: str | Path, out: str | Path,
                   recent_hours: float = 24) -> Path:
    target = validate_output_path(out, protected_worktree_paths(repo))
    result = build_coverage(repo, read_json(inventory_path), recent_hours)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = target / "coverage.json"
    write_json(path, result)
    return path
