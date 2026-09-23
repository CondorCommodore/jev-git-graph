"""Immutable, independently stored Git snapshots for advisory analysis."""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import git
from .equivalence import _activity
from .errors import JgError
from .inventory import protected_worktree_paths
from .safety import digest, is_within, opaque_path_id, read_json, write_json


SCHEMA_VERSION = 1
_FULL_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PACK_PATH = re.compile(r"^objects/pack/pack-[0-9a-f]{40,64}\.pack$")
_UNSAFE_GIT_ENV = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_REPLACE_REF_BASE", "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE",
    "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM", "GIT_TEMPLATE_DIR", "GIT_NO_REPLACE_OBJECTS",
    "GIT_NO_LAZY_FETCH",
}


def _check_git_environment() -> None:
    redirected = sorted(name for name in os.environ
                        if name in _UNSAFE_GIT_ENV or
                        re.fullmatch(r"GIT_CONFIG_(?:KEY|VALUE)_\d+", name))
    if redirected:
        raise JgError("unsafe Git environment redirect is set")


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"})
    return env


def _run(command: tuple[str, ...], *, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False, env=_git_env())
    if result.returncode:
        raise JgError("unable to create or validate pinned Git snapshot")
    return result.stdout


def _validate_store_layout(store: Path, pack_path: str | None = None,
                           pack_sha256: str | None = None,
                           object_format: str | None = None) -> Path:
    """Require a self-contained, ref-free bare store with exactly one pack."""
    if store.is_symlink() or not store.is_dir() or store.stat().st_mode & 0o077:
        raise JgError("snapshot object store is missing or not private")
    for path in store.rglob("*"):
        if path.is_symlink():
            raise JgError("snapshot object store must not contain symlinks")
    if {path.name for path in store.iterdir()} != {"HEAD", "config", "objects", "refs"}:
        raise JgError("snapshot object store contains unexpected files")
    config_path = store / "config"
    if not config_path.is_file() or config_path.is_symlink():
        raise JgError("snapshot object store config is invalid")
    config: dict[str, str] = {}
    section = ""
    try:
        for raw in config_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            match = re.fullmatch(r"\[([A-Za-z0-9]+)\]", line)
            if match:
                section = match.group(1).lower()
                continue
            entry = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*)\s*=\s*(.*?)", line)
            if not entry or not section:
                raise JgError("snapshot object store config is invalid")
            key = f"{section}.{entry.group(1).lower()}"
            if key in config:
                raise JgError("snapshot object store config has duplicate keys")
            config[key] = entry.group(2)
    except (OSError, UnicodeError) as exc:
        raise JgError("snapshot object store config is invalid") from exc
    allowed_config = {"core.repositoryformatversion", "core.filemode", "core.bare",
                      "core.ignorecase", "core.precomposeunicode", "extensions.objectformat"}
    if set(config) - allowed_config or config.get("core.bare") != "true":
        raise JgError("snapshot object store config contains external dependencies")
    fmt = config.get("extensions.objectformat", "sha1")
    if fmt not in {"sha1", "sha256"} or (object_format and fmt != object_format):
        raise JgError("snapshot object format is invalid")
    expected_version = "1" if fmt == "sha256" else "0"
    if config.get("core.repositoryformatversion") != expected_version:
        raise JgError("snapshot object store format is invalid")
    if (config.get("core.filemode") not in {"true", "false"} or
            config.get("core.ignorecase") not in {None, "true", "false"}):
        raise JgError("snapshot object store config is invalid")
    if "core.precomposeunicode" in config and config["core.precomposeunicode"] not in {"true", "false"}:
        raise JgError("snapshot object store config is invalid")
    head = (store / "HEAD").read_text(encoding="ascii")
    if not re.fullmatch(r"ref: refs/heads/[A-Za-z0-9._/-]+\n", head):
        raise JgError("snapshot object store HEAD is invalid")

    refs = store / "refs"
    if not refs.is_dir() or any(path.is_file() for path in refs.rglob("*")):
        raise JgError("snapshot object store must not contain named refs")
    if (store / "logs").exists():
        raise JgError("snapshot object store must not contain reflogs")
    objects = store / "objects"
    if not objects.is_dir() or {path.name for path in objects.iterdir()} != {"info", "pack"}:
        raise JgError("snapshot object store contains unexpected object data")
    info, pack_dir = objects / "info", objects / "pack"
    if not info.is_dir() or any(info.iterdir()) or not pack_dir.is_dir():
        raise JgError("snapshot object store contains alternates or loose objects")
    files = list(pack_dir.iterdir())
    packs = [path for path in files if path.is_file() and _PACK_PATH.fullmatch(path.relative_to(store).as_posix())]
    if len(packs) != 1:
        raise JgError("snapshot object store must contain exactly one pack")
    pack = packs[0]
    if len(pack.name.removeprefix("pack-").removesuffix(".pack")) != (40 if fmt == "sha1" else 64):
        raise JgError("snapshot pack name does not match its object format")
    prefix = pack.name.removesuffix(".pack")
    expected_files = {prefix + ".pack", prefix + ".idx"}
    if (pack.with_suffix(".idx").is_file() is False):
        raise JgError("snapshot pack index is missing")
    if pack.with_suffix(".rev").exists():
        expected_files.add(prefix + ".rev")
    if {path.name for path in files} != expected_files:
        raise JgError("snapshot object store contains unexpected pack data")
    if any(path.stat().st_nlink != 1 for path in files):
        raise JgError("snapshot object store must not use hard-linked objects")
    rel = pack.relative_to(store).as_posix()
    if pack_path is not None and rel != pack_path:
        raise JgError("snapshot pack path does not match the object store")
    if pack_sha256 is not None and hashlib.sha256(pack.read_bytes()).hexdigest() != pack_sha256:
        raise JgError("snapshot object pack digest does not match")
    return pack


def run_snapshot_git(object_repo: str | Path, *args: str) -> bytes:
    """Run a read-only command against a loaded snapshot's isolated object store."""
    if not args or args[0] not in {"cat-file", "diff", "diff-tree", "log", "ls-tree",
                                   "merge-base", "rev-list", "rev-parse", "show"}:
        raise JgError("snapshot Git helper only permits read-only object commands")
    store = Path(object_repo).expanduser()
    _validate_store_layout(store)
    return _run(("git", "-C", str(store), *args))


def _check_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) not in {40, 64} or not _FULL_SHA.fullmatch(value):
        raise JgError(f"inventory contains an invalid full {label}")
    return value


def _has_commit(repo: Path, sha: str) -> bool:
    try:
        _run(("git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"))
        return True
    except JgError:
        return False


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
    _check_git_environment()
    root, common_dir, runner = git.open_repository(repo)
    protected = [root, common_dir, *protected_worktree_paths(root)]
    target = _safe_new_path(Path(destination), protected)
    if not isinstance(tips, list) or not tips:
        raise JgError("snapshot requires at least one pinned commit")
    pins = sorted(set(_check_sha(value, "commit pin") for value in tips))
    for pin in pins:
        if not _has_commit(root, pin):
            raise JgError("pinned commit is missing or is not a commit")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.parent.stat().st_mode & 0o077:
        raise JgError("snapshot parent directory must be owner-only")
    target.mkdir(mode=0o700)
    try:
        fmt = _run(("git", "-C", str(root), "rev-parse", "--show-object-format")).decode().strip()
        if fmt not in {"sha1", "sha256"}:
            raise JgError("unsupported Git object format")
        with tempfile.TemporaryDirectory(prefix="jev-empty-template-", dir=target.parent) as template:
            _run(("git", "init", "--bare", "--quiet", f"--template={template}",
                  f"--object-format={fmt}", str(target)))
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
        _validate_store_layout(target, packs[0].relative_to(target).as_posix(), pack_sha256, fmt)
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
    _check_git_environment()
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
    try:
        main_tree = _run(("git", "-C", str(root), "rev-parse", f"{main_tip}^{{tree}}" )).decode().strip()
    except JgError:
        raise JgError("pinned main commit is missing or invalid")
    now = int(datetime.now(UTC).timestamp())
    cutoff = now - int(recent_hours * 3600)
    records: list[dict[str, Any]] = []
    pin_list: list[str] = [main_tip]
    for worktree in inventory["worktrees"]:
        if not isinstance(worktree, dict):
            raise JgError("inventory worktree record is malformed")
        head = worktree.get("head")
        if head is not None:
            pin_list.append(_check_sha(head, "worktree HEAD"))
    for branch in recorded:
        name, tip = branch["name"], branch["tip"]
        if name == main_name:
            continue
        base = branch.get("merge_base")
        if base is None:
            try:
                base = _run(("git", "-C", str(root), "merge-base", tip, main_tip)).decode().strip()
            except JgError:
                base = None
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
            if not _has_commit(root, sha):
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
        "main": {"name": main_name, "tip": main_tip, "tree": main_tree},
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
    pack_rel = store_meta.get("pack_path")
    if not isinstance(pack_rel, str) or not _PACK_PATH.fullmatch(pack_rel):
        raise JgError("snapshot pack path is invalid")
    if not _DIGEST.fullmatch(str(store_meta.get("pack_sha256", ""))):
        raise JgError("snapshot pack digest is invalid")
    if store_meta.get("object_format") not in {"sha1", "sha256"}:
        raise JgError("snapshot object format is invalid")
    _validate_store_layout(store, pack_rel, store_meta["pack_sha256"], store_meta["object_format"])
    pins = store_meta.get("pins")
    if not isinstance(pins, list) or not pins:
        raise JgError("snapshot pin list is invalid")
    for pin in pins:
        _check_sha(pin, "snapshot pin")
        if len(pin) != (40 if store_meta["object_format"] == "sha1" else 64):
            raise JgError("snapshot pin does not match its object format")
        _run(("git", "-C", str(store), "cat-file", "-e", f"{pin}^{{commit}}"))
    _run(("git", "-C", str(store), "fsck", "--full", "--strict", "--no-reflogs"))
    main = manifest.get("main")
    if not isinstance(main, dict) or main.get("tip") not in pins or main.get("tree") is None:
        raise JgError("snapshot main pin is invalid")
    _check_sha(main["tree"], "main tree")
    if len(main["tree"]) != (40 if store_meta["object_format"] == "sha1" else 64):
        raise JgError("snapshot main tree does not match its object format")
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
    for worktree in manifest["worktrees"]:
        if (not isinstance(worktree, dict) or
                (worktree.get("head") is not None and worktree["head"] not in pins)):
            raise JgError("snapshot worktree pin metadata is invalid")
    return manifest, store
