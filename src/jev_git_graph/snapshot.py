"""Immutable, independently stored Git snapshots for advisory analysis."""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import git
from .equivalence import _activity
from .errors import JgError
from .inventory import protected_worktree_paths
from .safety import canonical_json, digest, is_within, opaque_path_id, read_json, write_json


SCHEMA_VERSION = 1
_FULL_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _run(command: tuple[str, ...], *, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise JgError("unable to create or validate pinned Git snapshot")
    return result.stdout


def _check_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) not in {40, 64} or not _FULL_SHA.fullmatch(value):
        raise JgError(f"inventory contains an invalid full {label}")
    return value


def _safe_new_path(path: Path, protected: list[Path]) -> Path:
    raw = path.expanduser()
    # Reject symlinks in every existing component, including a symlinked leaf.
    absolute = Path(os.path.abspath(raw))
    cursor = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        cursor = cursor / component
        if cursor.is_symlink():
            raise JgError("snapshot output path must not contain symlinks")
    destination = absolute.resolve(strict=False)
    for root in protected:
        if is_within(destination, root.resolve()) or is_within(root.resolve(), destination):
            raise JgError("snapshot output must be outside the inspected repository and worktrees")
    if destination.exists():
        raise JgError("snapshot output already exists")
    return destination


def export_pinned_repository(repo: str | Path, tips: list[str], destination: str | Path) -> dict[str, Any]:
    """Export commit closures into a new bare repository, without source refs or writes."""
    root, common_dir, runner = git.open_repository(repo)
    protected = [root, common_dir, *protected_worktree_paths(root)]
    target = _safe_new_path(Path(destination), protected)
    if not isinstance(tips, list) or not tips:
        raise JgError("snapshot requires at least one pinned commit")
    pins = sorted(set(_check_sha(value, "commit pin") for value in tips))
    for pin in pins:
        if runner.try_run("cat-file", "-e", f"{pin}^{{commit}}") is None:
            raise JgError("pinned commit is missing or is not a commit")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.parent.stat().st_mode & 0o077:
        raise JgError("snapshot parent directory must be owner-only")
    target.mkdir(mode=0o700)
    try:
        fmt = runner.run("rev-parse", "--show-object-format").strip()
        if fmt not in {"sha1", "sha256"}:
            raise JgError("unsupported Git object format")
        _run(("git", "init", "--bare", "--quiet", f"--object-format={fmt}", str(target)))
        packed = _run(("git", "-C", str(root), "pack-objects", "--revs", "--stdout", "--delta-base-offset"),
                      input_bytes=("\n".join(pins) + "\n").encode("ascii"))
        if not packed:
            raise JgError("Git produced an empty pinned object pack")
        pack_sha256 = hashlib.sha256(packed).hexdigest()
        _run(("git", "-C", str(target), "index-pack", "--stdin"), input_bytes=packed)
        pack_dir = target / "objects" / "pack"
        packs = sorted(pack_dir.glob("pack-*.pack"))
        if len(packs) != 1 or hashlib.sha256(packs[0].read_bytes()).hexdigest() != pack_sha256:
            raise JgError("installed snapshot pack failed its digest check")
        for pin in pins:
            _run(("git", "-C", str(target), "cat-file", "-e", f"{pin}^{{commit}}"))
        _run(("git", "-C", str(target), "fsck", "--full", "--strict", "--no-reflogs"))
        return {"path": str(target), "pack_path": packs[0].relative_to(target).as_posix(),
                "pack_sha256": pack_sha256, "pins": pins, "object_format": fmt}
    except BaseException:
        # Keep partial output for diagnosis rather than risk deleting a path after
        # ownership or filesystem state changed underneath us.
        raise


def _load_inventory(inventory_path: str | Path, repo: Path) -> dict[str, Any]:
    inventory = read_json(inventory_path)
    if (inventory.get("kind") != "inventory" or inventory.get("schema_version") != 1 or
            inventory.get("repository", {}).get("id") != opaque_path_id(repo)):
        raise JgError("inventory is invalid or belongs to a different repository")
    if inventory.get("collection", {}).get("complete") is not True:
        raise JgError("snapshot requires a complete inventory")
    branches = inventory.get("branches")
    if not isinstance(branches, list) or not branches:
        raise JgError("inventory contains no branches")
    names: set[str] = set()
    for item in branches:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or item["name"] in names:
            raise JgError("inventory contains an invalid or duplicate branch")
        names.add(item["name"])
        _check_sha(item.get("tip"), "branch tip")
        if item.get("merge_base") is not None:
            _check_sha(item.get("merge_base"), "merge base")
    main = inventory.get("repository", {}).get("default_branch")
    if main not in names:
        raise JgError("inventory default branch is missing")
    if not isinstance(inventory.get("worktrees"), list) or not isinstance(inventory.get("stashes"), list):
        raise JgError("inventory worktree or stash metadata is malformed")
    for item in inventory["stashes"]:
        if not isinstance(item, dict):
            raise JgError("inventory stash record is malformed")
        _check_sha(item.get("sha"), "stash pin")
    return inventory


def write_snapshot(repo: str | Path, inventory_path: str | Path, out: str | Path,
                   recent_hours: float = 24) -> Path:
    if (isinstance(recent_hours, bool) or not isinstance(recent_hours, (int, float)) or
            not math.isfinite(recent_hours) or recent_hours < 24):
        raise JgError("snapshot recent_hours must be at least 24")
    root, common_dir, runner = git.open_repository(repo)
    inventory = _load_inventory(inventory_path, root)
    protected = [root, common_dir, *protected_worktree_paths(root)]
    destination = _safe_new_path(Path(out), protected)
    if destination.parent == destination:
        raise JgError("invalid snapshot output path")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.parent.stat().st_mode & 0o077:
        raise JgError("snapshot parent directory must be owner-only")
    destination.mkdir(mode=0o700)

    refs_before = {item["name"]: item["tip"] for item in git.local_branches(runner)}
    recorded = inventory["branches"]
    main_name = inventory["repository"]["default_branch"]
    main_record = next(item for item in recorded if item["name"] == main_name)
    main_tip = main_record["tip"]
    main_tree = runner.try_run("rev-parse", f"{main_tip}^{{tree}}")
    if main_tree is None:
        raise JgError("pinned main commit is missing or invalid")
    now = int(datetime.now(UTC).timestamp())
    cutoff = now - int(recent_hours * 3600)
    records: list[dict[str, Any]] = []
    pin_list: list[str] = [main_tip]
    for branch in recorded:
        name, tip = branch["name"], branch["tip"]
        if name == main_name:
            continue
        base = branch.get("merge_base")
        if base is None:
            base = git.merge_base(runner, tip, main_tip)
        exclusion: list[str] = []
        activity = _activity(runner, branch)
        if activity is None:
            exclusion.append("activity_unverifiable")
        elif activity >= cutoff:
            exclusion.append("recent_activity")
        if base is None:
            exclusion.append("merge_base_unavailable")
        if refs_before.get(name) != tip:
            exclusion.append("pinned_ref_moved_before_capture")
        for sha in (tip, base):
            if sha is None:
                continue
            if runner.try_run("cat-file", "-e", f"{sha}^{{commit}}") is None:
                exclusion.append("pinned_object_missing")
        eligible = not exclusion
        records.append({"name": name, "tip": tip, "merge_base": base,
                        "eligible": eligible, "exclusion_reasons": sorted(set(exclusion))})
        pin_list.append(tip)
        if base:
            pin_list.append(base)
    # Include every commit in the full inventory (including ineligible branches,
    # orphaned tips, main and stashes), so exclusion never means deletion.
    pin_list.extend(item["sha"] for item in inventory["stashes"])
    store_path = destination / "objects.git"
    exported = export_pinned_repository(root, sorted(set(pin_list)), store_path)
    refs_after = {item["name"]: item["tip"] for item in git.local_branches(runner)}
    for record in records:
        if refs_after.get(record["name"]) != refs_before.get(record["name"]):
            record["eligible"] = False
            record["exclusion_reasons"] = sorted(set(record["exclusion_reasons"] + ["source_ref_moved_during_capture"]))
    # Re-export only if the first capture raced and a newly observed pin is needed
    # is unnecessary: all inventory pins were validated and packed above.
    manifest: dict[str, Any] = {
        "kind": "git-snapshot", "schema_version": SCHEMA_VERSION,
        "repository_id": inventory["repository"]["id"], "inventory_digest": digest(inventory),
        "main": {"name": main_name, "tip": main_tip, "tree": main_tree.strip()},
        "branches": records, "worktrees": inventory["worktrees"], "stashes": inventory["stashes"],
        "recent_hours": recent_hours, "activity_cutoff_epoch": cutoff,
        "object_store": {"path": "objects.git", "pack_path": exported["pack_path"],
                         "pack_sha256": exported["pack_sha256"], "object_format": exported["object_format"],
                         "pins": exported["pins"]},
    }
    manifest["snapshot_digest"] = digest(manifest)
    path = destination / "snapshot.json"
    write_json(path, manifest)
    return path


def load_snapshot(snapshot_path: str | Path) -> tuple[dict[str, Any], Path]:
    source = Path(snapshot_path).expanduser()
    absolute = Path(os.path.abspath(source))
    cursor = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        cursor = cursor / component
        if cursor.is_symlink():
            raise JgError("snapshot path must not contain symlinks")
    source = source.resolve(strict=False)
    try:
        if source.stat().st_mode & 0o077 or source.parent.stat().st_mode & 0o077:
            raise JgError("snapshot directory and manifest must be owner-only")
    except FileNotFoundError as exc:
        raise JgError("snapshot manifest does not exist") from exc
    manifest = read_json(source)
    expected_digest = manifest.get("snapshot_digest")
    unsigned = {key: value for key, value in manifest.items() if key != "snapshot_digest"}
    if (manifest.get("kind") != "git-snapshot" or manifest.get("schema_version") != SCHEMA_VERSION or
            not isinstance(expected_digest, str) or not _DIGEST.fullmatch(expected_digest) or
            digest(unsigned) != expected_digest):
        raise JgError("snapshot schema or digest is invalid")
    if not isinstance(manifest.get("inventory_digest"), str) or not _DIGEST.fullmatch(manifest["inventory_digest"]):
        raise JgError("snapshot inventory digest is invalid")
    store_meta = manifest.get("object_store")
    if not isinstance(store_meta, dict) or store_meta.get("path") != "objects.git":
        raise JgError("snapshot object store path is invalid")
    store = source.parent / "objects.git"
    if store.is_symlink() or not store.is_dir() or store.stat().st_mode & 0o077:
        raise JgError("snapshot object store is missing or not private")
    pack_rel = store_meta.get("pack_path")
    if not isinstance(pack_rel, str) or not pack_rel.startswith("objects/pack/pack-") or not pack_rel.endswith(".pack"):
        raise JgError("snapshot pack path is invalid")
    pack = store / pack_rel
    if (pack.is_symlink() or pack.parent.is_symlink() or pack.parent.parent.is_symlink() or
            not pack.is_file() or not _DIGEST.fullmatch(str(store_meta.get("pack_sha256", "")))):
        raise JgError("snapshot pack is missing or invalid")
    if hashlib.sha256(pack.read_bytes()).hexdigest() != store_meta["pack_sha256"]:
        raise JgError("snapshot object pack digest does not match")
    pins = store_meta.get("pins")
    if not isinstance(pins, list) or not pins:
        raise JgError("snapshot pin list is invalid")
    for pin in pins:
        _check_sha(pin, "snapshot pin")
        _run(("git", "-C", str(store), "cat-file", "-e", f"{pin}^{{commit}}"))
    _run(("git", "-C", str(store), "fsck", "--full", "--strict", "--no-reflogs"))
    main = manifest.get("main")
    if not isinstance(main, dict) or main.get("tip") not in pins or main.get("tree") is None:
        raise JgError("snapshot main pin is invalid")
    tree = _run(("git", "-C", str(store), "rev-parse", f"{main['tip']}^{{tree}}" )).decode().strip()
    if tree != main["tree"]:
        raise JgError("snapshot main tree does not match its pin")
    if not isinstance(manifest.get("branches"), list) or not isinstance(manifest.get("worktrees"), list) or not isinstance(manifest.get("stashes"), list):
        raise JgError("snapshot metadata is malformed")
    for branch in manifest["branches"]:
        if (not isinstance(branch, dict) or branch.get("tip") not in pins or
                (branch.get("merge_base") is not None and branch["merge_base"] not in pins) or
                not isinstance(branch.get("eligible"), bool) or
                not isinstance(branch.get("exclusion_reasons"), list)):
            raise JgError("snapshot branch pin metadata is invalid")
    for stash in manifest["stashes"]:
        if not isinstance(stash, dict) or stash.get("sha") not in pins:
            raise JgError("snapshot stash pin metadata is invalid")
    return manifest, store
