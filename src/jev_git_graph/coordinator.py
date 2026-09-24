"""Cooperative creator leases and durable branch-cleanup receipts.

Creator integrations must register their adapter at startup and hold
``creator_operation`` from before any branch/worktree creation or checkout
until the new work is published. Cleanup takes the same per-branch lock and
keeps it through its durable result receipt. The Home Lab creator set is
deliberately closed: omitting or adding an entry requires a reviewed integration
change, not a caller-provided boolean.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import re
import shlex
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .errors import JgError
from .safety import canonical_json, digest


COOPERATIVE_LEASE_CONTRACT = "jev-git-graph/cooperative-branch-lease-v1"
JOURNAL_SCHEMA = "jev-git-graph.cleanup-journal.v1"
REQUIRED_HOME_LAB_CREATORS = frozenset({
    "scripts/bootstrap-worktree.sh",
    "scripts/new-worktree.sh",
    "scripts/overnight-codex-backlog-round.sh::prepare_lane_workspace",
    "scripts/ahc_app/coord_wake.py::ensure_coord_wake_worktree",
    "scripts/merge_train_parts/prescreen.py::run_prescreen",
    "scripts/merge_train_parts/local_guard_verdict.py",
    "scripts/pr_gate/guard_execution.py::_run_guard_suite_in_worktree",
    "scripts/merge_train_parts/candidate_lifecycle.py::construct_candidate_head",
    "scripts/merge_train_parts/verdict_lifecycle.py",
    "scripts/train_builder.py::construct_train",
    "scripts/train_construction_driver.py::construct_next_train",
    "scripts/l1_drain/self_reported.py",
    "scripts/l1_drain/workspace.py::create_worktree",
    "scripts/pr_repair_loop.py::repair_workspace",
    "scripts/deploy_sync.py",
    "scripts/forge-coord-deploy.sh",
})

# Exact physical files from the reviewed Home Lab lease-hook integration.
# Installed runtime metadata and caller-provided creator lists never replace
# these code-owned expected hashes.
_REVIEWED_HOOK_FILES = {
    "scripts/cooperative_branch_lease.py": "e76ea4293ea36f2fbfa6c26f92693c630ab84123f53ad775950123b3edf9953f",
    "scripts/attested_shell_supervisor.py": "fd1b2e6134d30e170dcd989f9e2d37726db1a49bfcb7009c3c2c8310cd127e81",
    "launchd/start-merge-safe-prs-loop.sh": "e9eeeb7aea1ccc6ec51ef6b17dcf09a86d4c3c7e47d8ae8065e0b885c2ddfbc4",
    "launchd/start-autonomy-drain-loop.sh": "2a5bb4fab729056a001e2e1dbc5f775ba12668a087efb5fd0e271dd6c88610bd",
    "scripts/bootstrap-worktree.sh": "c63745dae3652141ac7687a54aa03bf997de145c018a0460a6952100da928664",
    "scripts/new-worktree.sh": "ee28b1e34af1638f6c39948c39248a3015c9ed2f3a6f9cc0b87172247b4a10c5",
    "scripts/overnight-codex-backlog-round.sh": "e188e7415c68ca318a298c704cc87b710404ed912d781b79b85ea3000e64dddd",
    "scripts/ahc_app/coord_wake.py": "616203361a9f4323ab85629165cde1fbd683a75bfdf608b2d24a8b136736cffb",
    "scripts/merge_train_parts/prescreen.py": "bccecab240f597abd0ab6485afa0586e003f7dc11082352a3ec2eea33eee25e9",
    "scripts/merge_train_parts/local_guard_verdict.py": "fc36b0b3544acfd2a43871eb829b4479d3019b765a4e7ad29e5f12686f5319bf",
    "scripts/pr_gate/guard_execution.py": "91dba3571398639d0456a72eae929377b483bab8d376db3daf7c079fcd5b3c7c",
    "scripts/merge_train_parts/candidate_lifecycle.py": "c35db21b44c0d9024b68f802c46f13c9c3e70e0a5ead3eabd45a8c3240d1450e",
    "scripts/merge_train_parts/verdict_lifecycle.py": "eed5e98a608c7998f10d1c9e9a2c8f240d25d08863901438a1dc3e789a4f98c6",
    "scripts/train_builder.py": "289c985aaf9d8b9b00f3934ba767eee9114773240ec67a44207d74625d8ab367",
    "scripts/train_construction_driver.py": "4b9c1631c0b940d6df18ae1987fcef9801776a03ff3a6f1abd98e60059403b70",
    "launchd/start-train-construction.sh": "0f42fc8d72145a5f1845770317e88b677d25fca3d0939633ff79316419903e8f",
    "scripts/l1_drain/self_reported.py": "662d9cbcd86fc021dad1612347e10a9f23f4456e8d482ca3362419ef63e69fae",
    "scripts/l1_drain/workspace.py": "5aecacb06b3322cc64dc729227d45508eb4ffff7cd0504bcf6f46bce20795b70",
    "scripts/pr_repair_loop.py": "2791deaa05197388532898610f3ab3024f617356fcbbfb545724c45782ca108e",
    "scripts/deploy_sync.py": "bb54d9ede1a318951c109b8453c7ace28accc9f38867b3272c4e0891b67ec84d",
    "scripts/forge-coord-deploy.sh": "5ac70ff08333e3d224bbd4d63e10297a3eb1b524ab8f3ba5646ea5e799c36620",
    "scripts/all_health_controller.py": "f2edfe2d03a65c4b8f2d086a4b1aaf5f13137370a9b6904d6bdb50551697197d",
    "scripts/coord_wake_consumer.py": "b59ca92f646ff1e07273db1f49fcd7bcf349db581c2d3c465f0a0616092d2f30",
    "scripts/merge_safe_pr_wake_consumer.py": "fc92956f0c4434e212760400bd22e63974cc3c36caf57fe69e3458aec930ad18",
    "scripts/merge_safe_pr_wake_producer.py": "00bdb553905418ea8c84e340ee59087cfb2210cdcc1a489067cf93b975661caf",
    "scripts/wip_convergence_entrypoint.py": "c63df09d73b0da24fafb87a9de3b5a6234ff8f88f83cdc397b72c6056650cfb5",
    "scripts/ahc_inventory_alarm.py": "1a5f84b7bf227cb8037c37ce0ecaafb0a467484b1f3d00dd3d2980c809bba6be",
    "scripts/merge-safe-prs-loop.sh": "62e7a2183963edf14c41c11db8a6c81287a97b56c875ac04e71bd5eb0cef4a98",
    "watcher/coord-wake.sh": "a1d3fc92ba0709f849b8044030b0fe9260ad066856e3524463bc6b7f4d381c56",
    "launchd/com.mikebook.wip-convergence-loop.plist": "af41fa095438701d1aab7127778550e373c8a1e966738f77644f9df7ed5cc7bd",
    "launchd/start-all-health-controller.sh": "26e5e394a4593ed9c05fa30c278c090ecb328c2efb6bd58fcc7ba4a3bcaad517",
    "launchd/start-all-health-coord-wake-consumer.sh": "f76a72ffdd502ab8abe7d878a5cad6ed97a18b63bb383be791ae4ae0153b4f72",
    "launchd/start-pr-convergence-wake-consumer.sh": "0feea9285380c6992e783cf0af425f4c22d488c9672f7c59d622edebaa09bf57",
    "launchd/start-pr-convergence-wake-producer.sh": "4277f8268de31b878dec72c488fe29b51b62434e257d66a47a35597c8cfd9537",
    "launchd/start-pr-repair-loop.sh": "0ec1816b6c2ed02b59fd4cc2d2508ae22e773dd9cc8f30a947cd9742a6281de1",
    "launchd/start-ahc-inventory-alarm.sh": "dde12dd429a48247549a2eb7159a5acb26577c19bdaa12a0b8af9de8d719ef1b",
}
_RUNTIME_SELECTORS = (
    "code/.runtime/releases/home-lab/stable",
    "code/.runtime/home-lab",
    "code/home-lab",
)
_LOCK_ROOT_SUFFIX = Path(".local/state/jev-git-graph/branch-leases")
_REQUIRED_LAUNCHD_SELECTORS = {
    "com.mikebook.all-health-controller": "code/.runtime/releases/home-lab/stable",
    "com.mikebook.all-health-coord-wake-consumer": "code/.runtime/releases/home-lab/stable",
    "com.mikebook.merge-safe-prs-loop": "code/.runtime/releases/home-lab/stable",
    "com.mikebook.pr-convergence-wake-consumer": "code/.runtime/releases/home-lab/stable",
    "com.mikebook.pr-convergence-wake-producer": "code/.runtime/releases/home-lab/stable",
    "com.mikebook.wip-convergence-loop": "code/.runtime/releases/home-lab/stable",
    "com.mikebook.train-construction": "code/home-lab",
    "com.mikebook.pr-repair-loop": "code/home-lab",
    "com.condor.autonomy-drain-loop.codex": "code/home-lab",
    "com.condor.autonomy-drain-loop.claude": "code/home-lab",
    "com.tradeengine.coord-wake-codex": "code/home-lab",
    "com.mikebook.ahc-inventory-alarm": "code/.runtime/home-lab",
}
_REQUIRED_LAUNCHD_PATHS = {
    "com.mikebook.all-health-controller": ("launchd/start-all-health-controller.sh", "scripts/all_health_controller.py", "runtime"),
    "com.mikebook.all-health-coord-wake-consumer": ("launchd/start-all-health-coord-wake-consumer.sh", "scripts/coord_wake_consumer.py", "runtime"),
    "com.mikebook.merge-safe-prs-loop": ("launchd/start-merge-safe-prs-loop.sh", "scripts/attested_shell_supervisor.py", "runtime"),
    "com.mikebook.pr-convergence-wake-consumer": ("launchd/start-pr-convergence-wake-consumer.sh", "scripts/merge_safe_pr_wake_consumer.py", "runtime"),
    "com.mikebook.pr-convergence-wake-producer": ("launchd/start-pr-convergence-wake-producer.sh", "scripts/merge_safe_pr_wake_producer.py", "runtime"),
    "com.mikebook.wip-convergence-loop": ("scripts/wip_convergence_entrypoint.py", "scripts/wip_convergence_entrypoint.py", "canonical"),
    "com.mikebook.train-construction": ("launchd/start-train-construction.sh", "scripts/attested_shell_supervisor.py", "canonical"),
    "com.mikebook.pr-repair-loop": ("launchd/start-pr-repair-loop.sh", "scripts/pr_repair_loop.py", "canonical"),
    "com.condor.autonomy-drain-loop.codex": ("launchd/start-autonomy-drain-loop.sh", "scripts/attested_shell_supervisor.py", "canonical"),
    "com.condor.autonomy-drain-loop.claude": ("launchd/start-autonomy-drain-loop.sh", "scripts/attested_shell_supervisor.py", "canonical"),
    "com.tradeengine.coord-wake-codex": ("watcher/coord-wake.sh", "scripts/attested_shell_supervisor.py", "canonical"),
    "com.mikebook.ahc-inventory-alarm": ("launchd/start-ahc-inventory-alarm.sh", "scripts/ahc_inventory_alarm.py", "runtime"),
}
_SUPERVISED_SHELL_SOURCES = {
    "com.mikebook.merge-safe-prs-loop": (
        "launchd/start-merge-safe-prs-loop.sh", "scripts/merge-safe-prs-loop.sh"),
    "com.mikebook.train-construction": ("launchd/start-train-construction.sh",),
    "com.condor.autonomy-drain-loop.codex": ("launchd/start-autonomy-drain-loop.sh",),
    "com.condor.autonomy-drain-loop.claude": ("launchd/start-autonomy-drain-loop.sh",),
    "com.tradeengine.coord-wake-codex": ("watcher/coord-wake.sh",),
}
DISPOSABLE_FIXTURE_ROOT = Path.home() / ".local/state/jev-git-graph/disposable-fixtures"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _branch_key(repository_id: str, branch: str) -> str:
    return hashlib.sha256(f"{repository_id}\0{branch}".encode()).hexdigest()


@dataclass(frozen=True)
class CreatorLeaseCapability:
    """Ephemeral production capability derived from installed reviewed files."""

    common_dir: Path
    runtime_roots: tuple[Path, ...]
    hook_digests: tuple[tuple[str, str, str], ...]
    loaded_runtime_jobs: tuple[tuple[str, str, str, str], ...]
    lock_root: Path
    train_construction_runtime: Path
    train_construction_commit: str | None
    scope: str = "production"


@dataclass(frozen=True)
class DisposableFixtureLeaseCapability:
    """Explicit, repository-bound no-automation contract for disposable fixtures."""

    common_dir: Path
    lock_root: Path
    branches: tuple[str, ...]
    branch_tips: tuple[tuple[str, str], ...]
    operator: str
    fixture_root: Path
    scope: str = "disposable_fixture"

    def is_current(self, repository: str | Path) -> bool:
        try:
            inventory = build_disposable_fixture_inventory(
                repository, list(self.branches), operator=self.operator,
                fixture_root=self.fixture_root, recorded_tips=dict(self.branch_tips))
            return (Path(inventory["common_dir"]) == self.common_dir
                    and self.lock_root == _fixed_lock_root())
        except (JgError, OSError):
            return False


def capability_receipt_metadata(
    capability: CreatorLeaseCapability | DisposableFixtureLeaseCapability,
) -> dict[str, str | None]:
    """Return sanitized, stable capability identifiers for cleanup receipts."""
    if isinstance(capability, CreatorLeaseCapability):
        roots = {str(root.resolve()): label for root, label in zip(
            capability.runtime_roots, ("stable", "compat", "canonical"))}
        roots[str(capability.train_construction_runtime.resolve())] = "train-construction"
        hooks: list[tuple[str, str, str]] = []
        for root, relative, sha in capability.hook_digests:
            label = roots.get(str(Path(root).resolve()))
            if label is None:
                raise JgError("creator capability contains an unclassified runtime")
            hooks.append((label, relative, sha))
        runtime_commit: str | None = capability.train_construction_commit
        scope = capability.scope
        payload = {
            "contract": COOPERATIVE_LEASE_CONTRACT,
            "scope": scope,
            "runtime_commit": runtime_commit,
            "hooks": sorted(hooks),
            "jobs": sorted((label, generation) for label, _plist, _root, generation
                           in capability.loaded_runtime_jobs),
        }
    elif isinstance(capability, DisposableFixtureLeaseCapability):
        scope = capability.scope
        runtime_commit = None
        payload = {
            "contract": COOPERATIVE_LEASE_CONTRACT,
            "scope": scope,
            "common_dir_sha256": hashlib.sha256(str(capability.common_dir).encode()).hexdigest(),
            "branches": list(capability.branches),
        }
    else:
        raise JgError("cleanup receipt requires a verified creator capability")
    return {
        "creator_capability_sha256": digest(payload),
        "creator_runtime_commit": runtime_commit,
    }


def production_capability_is_current(capability: CreatorLeaseCapability,
                                     repository: str | Path) -> bool:
    try:
        current = resolve_production_creator_capability(repository)
        return current == capability
    except (JgError, OSError):
        return False


def _common_dir(repository: str | Path) -> Path:
    cp = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, check=False, timeout=15,
    )
    if cp.returncode or not cp.stdout.strip():
        raise JgError("cannot resolve canonical Git common directory for cooperative lease")
    return Path(cp.stdout.strip()).resolve(strict=True)


def _fixed_lock_root() -> Path:
    expected = (Path.home() / _LOCK_ROOT_SUFFIX).resolve(strict=False)
    override = os.environ.get("JEV_BRANCH_LEASE_DIR")
    if override and Path(override).expanduser().resolve(strict=False) != expected:
        raise JgError("cooperative lease lock root differs from the reviewed shared protocol")
    return expected


def default_cleanup_journal_path(common_dir: Path) -> Path:
    key = hashlib.sha256(str(common_dir.resolve()).encode()).hexdigest()
    return Path.home() / ".local/state/jev-git-graph/cleanup-journals" / f"{key}.jsonl"


def _runtime_selector_targets(home: Path | None = None) -> tuple[Path, ...]:
    base = (home or Path.home()).resolve()
    targets: list[Path] = []
    for selector in _RUNTIME_SELECTORS:
        path = (base / selector)
        if selector.endswith("/stable") and not path.is_symlink():
            raise JgError("stable Home Lab runtime selector must be a release symlink")
        if path.is_symlink() and selector.endswith("/stable"):
            target = path.resolve(strict=True)
            metadata_path = target / "release-metadata.json"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise JgError("stable Home Lab runtime release metadata is unavailable") from exc
            release_sha = metadata.get("release_sha") if isinstance(metadata, dict) else None
            release_path = metadata.get("release_path") if isinstance(metadata, dict) else None
            if (release_sha != target.name or Path(str(release_path)).resolve(strict=False) != target
                    or target.parent.resolve() != (base / "code/.runtime/releases/home-lab").resolve()):
                raise JgError("stable Home Lab runtime selector does not match its release metadata")
        else:
            target = path.resolve(strict=True)
        if not target.is_dir():
            raise JgError(f"Home Lab runtime selector is not a directory: {selector}")
        targets.append(target)
    if len(set(targets)) != len(_RUNTIME_SELECTORS):
        raise JgError("Home Lab runtime selectors resolve to duplicate trees")
    return tuple(targets)


def _verify_runtime_hook_files(runtime_root: Path) -> tuple[tuple[str, str, str], ...]:
    records: list[tuple[str, str, str]] = []
    for relative, expected_sha in sorted(_REVIEWED_HOOK_FILES.items()):
        path = runtime_root / relative
        if path.is_symlink() or not path.is_file():
            raise JgError(f"creator runtime hook file missing or symlinked: {relative}")
        try:
            actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise JgError(f"creator runtime hook file unreadable: {relative}") from exc
        if actual_sha != expected_sha:
            raise JgError(f"creator runtime hook digest mismatch: {relative}")
        records.append((str(runtime_root), relative, actual_sha))
    helper = (runtime_root / "scripts/cooperative_branch_lease.py").read_text(encoding="utf-8")
    if f'CONTRACT = "{COOPERATIVE_LEASE_CONTRACT}"' not in helper:
        raise JgError("creator runtime helper contract does not match Jev")
    return tuple(records)


def _verify_train_construction_runtime(
    home: Path, canonical_common_dir: Path,
) -> tuple[Path, str | None, tuple[tuple[str, str, str], ...]]:
    """Prove the mutable-origin/main train constructor is on a reviewed snapshot.

    The launchd wrapper fetches ``origin/main`` into a dedicated linked worktree
    before importing its creator. A source checkout or loaded plist alone cannot
    attest that code. Require the live runtime to be clean, attached to the same
    Git common directory, exactly at its current local ``origin/main`` commit,
    and to contain every code-owned creator-hook digest. The optional sourced
    environment file can redirect the runtime or execute arbitrary shell, so a
    present file keeps production deletion closed without reading its contents.
    An absent dedicated worktree is an inactive state: the reviewed launcher
    refuses to start its creator until that worktree exists. Its appearance
    changes the capability and requires a fresh exact snapshot proof.
    """
    home = home.resolve(strict=True)
    override_file = home / ".config/train-promotion-shadow.env"
    if override_file.exists() or override_file.is_symlink():
        raise JgError("train-construction-runtime_override_unverified")
    runtime = home / ".local/share/home-lab/train-promotion-runtime"
    if runtime.is_symlink():
        raise JgError("train-construction-runtime_unverified: runtime is symlinked")
    if not runtime.exists():
        return runtime, None, ()
    try:
        root = runtime.resolve(strict=True)
    except OSError as exc:
        raise JgError("train-construction-runtime_unavailable") from exc
    if root != runtime:
        raise JgError("train-construction-runtime_unverified: runtime path changed")

    def git_text(*args: str) -> str:
        result = subprocess.run(["git", "-C", str(root), *args],
                                capture_output=True, text=True, check=False, timeout=15)
        if result.returncode:
            raise JgError("train-construction-runtime_unverified: Git state unavailable")
        return result.stdout.strip()

    top = Path(git_text("rev-parse", "--show-toplevel")).resolve(strict=True)
    if top != root or _common_dir(root) != canonical_common_dir:
        raise JgError("train-construction-runtime_unverified: runtime is not a linked Home Lab worktree")
    status = git_text("status", "--porcelain", "--untracked-files=all")
    if status:
        raise JgError("train-construction-runtime_unverified: runtime checkout is dirty")
    head = git_text("rev-parse", "--verify", "HEAD^{commit}").lower()
    origin_main = git_text("rev-parse", "--verify", "origin/main^{commit}").lower()
    if (not re.fullmatch(r"[0-9a-f]{40,64}", head)
            or head != origin_main):
        raise JgError("train-construction-runtime_snapshot_mismatch")
    listing = git_text("worktree", "list", "--porcelain")
    registered = {
        Path(line.removeprefix("worktree ")).resolve(strict=False)
        for line in listing.splitlines() if line.startswith("worktree ")
    }
    if root not in registered:
        raise JgError("train-construction-runtime_unverified: worktree registration missing")
    hook_digests = _verify_runtime_hook_files(root)
    return root, head, hook_digests


def _attest_process_generation(runtime_root: Path, pid: int, started_at: float) -> str:
    """Keep the prior process start-time gate as a conservative freshness check.

    The authoritative loaded-code proof is the per-PID startup receipt checked
    by :func:`_verify_process_startup_attestation`; source mtimes alone cannot
    establish which Python code object is executing.
    """
    hooks = _verify_runtime_hook_files(runtime_root)
    return digest({
        "pid": pid,
        "started_at": datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
        "runtime_root_sha256": hashlib.sha256(str(runtime_root.resolve()).encode()).hexdigest(),
        "hooks": sorted((relative, sha) for _root, relative, sha in hooks),
    })


def _verify_process_startup_attestation(
    home: Path, runtime_root: Path, pid: int, process_start: str,
    label: str, process_rel: str,
) -> str:
    """Require a mode-0600 self-attestation for this exact process generation."""
    if process_rel.endswith(".sh"):
        raise JgError(
            f"runtime_adoption_unverified: shell source receipts are not authoritative: {label}"
        )
    directory = home / ".local/state/jev-git-graph/creator-runtime-attestations"
    receipt_path = directory / f"{pid}.json"
    if (directory.is_symlink() or not directory.is_dir() or receipt_path.is_symlink()
            or not receipt_path.is_file()):
        raise JgError(f"runtime_adoption_unverified: startup attestation missing: {label}")
    try:
        for parent in (directory.parent, *directory.parent.parents):
            if parent == home or parent == Path("/"):
                break
            if (parent.is_symlink() or not parent.is_dir()
                    or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o022):
                raise JgError(f"runtime_adoption_unverified: startup attestation parent unsafe: {label}")
        directory_stat = directory.stat()
        receipt_stat = receipt_path.stat()
        if (directory_stat.st_uid != os.getuid() or directory_stat.st_mode & 0o077
                or receipt_stat.st_uid != os.getuid() or receipt_stat.st_mode & 0o077):
            raise JgError(f"runtime_adoption_unverified: startup attestation permissions invalid: {label}")
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JgError(f"runtime_adoption_unverified: startup attestation unreadable: {label}") from exc
    if (not isinstance(payload, dict)
            or payload.get("contract") != "jev-git-graph/creator-runtime-attestation-v1"
            or payload.get("pid") != pid
            or payload.get("process_start") != process_start
            or payload.get("creator") != label
            or payload.get("runtime_root_sha256") != hashlib.sha256(str(runtime_root.resolve()).encode()).hexdigest()):
        raise JgError(f"runtime_adoption_unverified: startup attestation generation mismatch: {label}")
    runtime_commit = payload.get("runtime_commit")
    try:
        git_head = subprocess.run(["git", "-C", str(runtime_root), "rev-parse", "--verify", "HEAD^{commit}"],
                                  capture_output=True, text=True, check=False, timeout=15)
        if git_head.returncode:
            release = json.loads((runtime_root / "release-metadata.json").read_text(encoding="utf-8"))
            expected_commit = str(release["release_sha"]).lower()
            if (str(release["source_sha"]).lower() != expected_commit
                    or Path(str(release["release_path"])).resolve(strict=True) != runtime_root.resolve(strict=True)):
                raise ValueError("release metadata mismatch")
        else:
            expected_commit = git_head.stdout.strip().lower()
    except (OSError, KeyError, json.JSONDecodeError, ValueError) as exc:
        raise JgError(f"runtime_adoption_unverified: startup runtime commit unavailable: {label}") from exc
    if runtime_commit != expected_commit:
        raise JgError(f"runtime_adoption_unverified: startup runtime commit mismatch: {label}")
    expected_sha = _REVIEWED_HOOK_FILES.get(process_rel)
    if expected_sha is None:
        raise JgError(f"runtime_adoption_unverified: creator process source is not pinned: {label}")
    attestations = payload.get("attestations")
    if not isinstance(attestations, list):
        raise JgError(f"runtime_adoption_unverified: startup source attestations missing: {label}")
    expected_kind = "python_code"
    match = next((item for item in attestations if isinstance(item, dict)
                  and item.get("source_path") == process_rel
                  and item.get("creator") == label
                  and item.get("kind") == expected_kind), None)
    helper_digest = _REVIEWED_HOOK_FILES["scripts/cooperative_branch_lease.py"]
    if (match is None or match.get("source_sha256") != expected_sha
            or match.get("helper_path") != "scripts/cooperative_branch_lease.py"
            or match.get("helper_sha256") != helper_digest
            or not re.fullmatch(r"[0-9a-f]{64}", str(match.get("loaded_code_sha256", "")))):
        raise JgError(f"runtime_adoption_unverified: startup source digest mismatch: {label}")
    if label in _SUPERVISED_SHELL_SOURCES:
        for shell_rel in _SUPERVISED_SHELL_SOURCES[label]:
            shell_sha = _REVIEWED_HOOK_FILES.get(shell_rel)
            shell_match = next((item for item in attestations if isinstance(item, dict)
                                and item.get("source_path") == shell_rel
                                and item.get("creator") == label
                                and item.get("kind") == "shell_fd"), None)
            if (shell_sha is None or shell_match is None
                    or shell_match.get("source_sha256") != shell_sha
                    or shell_match.get("loaded_code_sha256") != shell_sha
                    or shell_match.get("helper_path") != "scripts/cooperative_branch_lease.py"
                    or shell_match.get("helper_sha256") != helper_digest):
                raise JgError(f"runtime_adoption_unverified: reviewed shell descriptor missing: {label}")
    return digest(payload)


def _verify_loaded_runtime_jobs(home: Path, runtime_roots: tuple[Path, ...]) -> tuple[tuple[str, str, str, str], ...]:
    """Verify configured launchd entrypoints and their currently loaded processes."""
    expected_roots = {selector: root for selector, root in zip(_RUNTIME_SELECTORS, runtime_roots)}
    jobs: list[tuple[str, str, str, str]] = []
    uid = os.getuid()
    for label, selector in sorted(_REQUIRED_LAUNCHD_SELECTORS.items()):
        plist_path = home / "Library/LaunchAgents" / f"{label}.plist"
        if plist_path.is_symlink() or not plist_path.is_file():
            raise JgError(f"creator runtime launchd entrypoint missing: {label}")
        try:
            with plist_path.open("rb") as stream:
                plist = plistlib.load(stream)
        except (OSError, plistlib.InvalidFileException) as exc:
            raise JgError(f"creator runtime launchd entrypoint unreadable: {label}") from exc
        root = expected_roots[selector]
        args = plist.get("ProgramArguments")
        wd = plist.get("WorkingDirectory")
        launcher_rel, process_rel, cwd_scope = _REQUIRED_LAUNCHD_PATHS[label]
        launcher_root = root
        expected_launcher = (launcher_root / launcher_rel).resolve(strict=False)
        configured_launcher = False
        if isinstance(args, list):
            command_tokens: list[str] = []
            for arg in args:
                if isinstance(arg, str):
                    command_tokens.extend(shlex.split(arg))
            for token in command_tokens:
                token_path = Path(token)
                candidate = token_path if token_path.is_absolute() else (
                    launcher_root / token_path)
                if candidate.resolve(strict=False) == expected_launcher:
                    configured_launcher = True
                    break
        if (plist.get("Label") != label or not isinstance(args, list) or not configured_launcher
                or (cwd_scope == "runtime" and isinstance(wd, str) and wd and Path(wd).resolve(strict=False) != root)):
            raise JgError(f"creator runtime launchd entrypoint targets an unverified tree: {label}")
        if (label == "com.mikebook.train-construction"
                and isinstance(plist.get("EnvironmentVariables"), dict)
                and any(key.startswith("TRAIN_") for key in plist["EnvironmentVariables"])):
            raise JgError("train-construction-runtime_override_unverified")
        if label == "com.mikebook.train-construction":
            for variable in (
                "TRAIN_PROMOTION_RUNTIME_ROOT", "TRAIN_PROMOTION_REPO",
                "TRAIN_CONSTRUCTION_SCRATCH_ROOT", "TRAIN_PROMOTION_STATE_DIR",
                "TRAIN_PROMOTION_SHADOW_LOCK_DIR", "TRAIN_CONSTRUCTION_ENV_PATH",
            ):
                configured = subprocess.run(
                    ["launchctl", "getenv", variable], capture_output=True,
                    text=True, check=False, timeout=5,
                )
                if configured.returncode or configured.stdout.strip():
                    raise JgError("train-construction-runtime_override_unverified")
        check = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                               capture_output=True, text=True, check=False, timeout=10)
        if check.returncode:
            disabled = subprocess.run(["launchctl", "print-disabled", f"gui/{uid}"],
                                      capture_output=True, text=True, check=False, timeout=10)
            if (disabled.returncode == 0
                    and re.search(rf'(?m)^\s*"{re.escape(label)}"\s*=>\s*disabled\s*$',
                                  disabled.stdout)):
                jobs.append((label, str(plist_path.resolve()), str(root),
                             digest({"disabled": True, "configured_arguments": args})))
                continue
            raise JgError(f"creator runtime launchd job is unavailable and not disabled: {label}")
        if f"path = {plist_path}" not in check.stdout:
            raise JgError(f"creator runtime launchd job is not loaded from the reviewed plist: {label}")
        loaded_arguments = re.search(r"(?m)^\s*arguments = \{\s*\n(.*?)^\s*\}",
                                     check.stdout, re.DOTALL)
        if loaded_arguments is None:
            raise JgError(f"runtime_adoption_unverified: loaded creator arguments unavailable: {label}")
        loaded_tokens = [token for line in loaded_arguments.group(1).splitlines()
                         for token in shlex.split(line.strip())]
        configured_tokens = [token for argument in args for token in shlex.split(argument)]
        loaded_wd = re.search(r"(?m)^\s*working directory = (.+)\s*$", check.stdout)
        if (loaded_tokens != configured_tokens
                or (isinstance(wd, str) and wd
                    and (loaded_wd is None
                         or Path(loaded_wd.group(1).strip()).resolve(strict=False)
                         != Path(wd).resolve(strict=False)))):
            raise JgError(f"runtime_adoption_unverified: loaded creator command differs from reviewed plist: {label}")
        pid = None
        for line in check.stdout.splitlines():
            match = re.match(r"\s*pid = ([0-9]+)\s*$", line)
            if match:
                pid = int(match.group(1))
                break
        if pid is None:
            # Interval jobs may be idle. Their next invocation loads the
            # currently reviewed entrypoint. A start during cleanup changes
            # this capability generation and stops the next ref transaction.
            jobs.append((label, str(plist_path.resolve()), str(root),
                         digest({"idle": True, "loaded_arguments": loaded_tokens})))
            continue
        process = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, check=False, timeout=5)
        start_result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                      capture_output=True, text=True, check=False, timeout=5)
        cwd_result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                    capture_output=True, text=True, check=False, timeout=5)
        cwd = next((line[1:] for line in cwd_result.stdout.splitlines()
                    if line.startswith("n/") and cwd_result.returncode == 0), None)
        cwd_path = Path(cwd).resolve(strict=False) if cwd else None
        argv = shlex.split(process.stdout) if process.returncode == 0 else []
        expected_process = (root / process_rel).resolve(strict=False)
        executable_evidence = False
        for token in argv:
            token_path = Path(token)
            candidate = token_path if token_path.is_absolute() else ((cwd_path / token_path) if cwd_path else Path("/nonexistent"))
            if candidate.is_file() and candidate.resolve(strict=False) == expected_process:
                executable_evidence = True
        expected_cwd = root if cwd_scope == "runtime" else runtime_roots[-1]
        if (not cwd_path or cwd_path != expected_cwd or not executable_evidence
                or process.returncode != 0 or start_result.returncode != 0):
            raise JgError(f"runtime_adoption_unverified: creator process is not running from the reviewed tree: {label}")
        try:
            started_at = datetime.strptime(start_result.stdout.strip(), "%a %b %d %H:%M:%S %Y").timestamp()
        except (ValueError, OverflowError):
            raise JgError(f"runtime_adoption_unverified: creator process start time is unavailable: {label}") from None
        receipt_digest = _verify_process_startup_attestation(
            home, root, pid, start_result.stdout.strip(), label, process_rel,
        )
        generation = digest({
            "mtime_generation": _attest_process_generation(root, pid, started_at),
            "startup_receipt_sha256": receipt_digest,
        })
        jobs.append((label, str(plist_path.resolve()), str(root), generation))
    return tuple(jobs)


def resolve_production_creator_capability(repository: str | Path) -> CreatorLeaseCapability:
    """Derive production readiness from both configured installed runtime trees.

    No serialized attestation or in-memory creator registration is authoritative.
    Every execute attempt calls this resolver again, so symlink/release drift fails
    closed before another branch lease is acquired.
    """
    common_dir = _common_dir(repository)
    lock_root = _fixed_lock_root()
    runtime_roots = _runtime_selector_targets()
    canonical = (Path.home() / "code/home-lab").resolve(strict=True)
    if canonical != runtime_roots[-1]:
        raise JgError("canonical_creator_unverified: Home Lab source selector changed")
    try:
        digests = tuple(record for root in runtime_roots
                        for record in _verify_runtime_hook_files(root))
    except JgError as exc:
        if runtime_roots[-1] in str(exc) or "hook digest mismatch" in str(exc):
            raise JgError("canonical_creator_unverified: canonical Home Lab source does not match reviewed hooks") from exc
        raise
    home = Path.home()
    jobs = _verify_loaded_runtime_jobs(home, runtime_roots)
    train_runtime, train_commit, train_digests = _verify_train_construction_runtime(home, common_dir)
    return CreatorLeaseCapability(common_dir, runtime_roots,
                                  (*digests, *train_digests), jobs, lock_root,
                                  train_runtime, train_commit)


def build_disposable_fixture_inventory(repository: str | Path, branches: list[str],
                                       *, operator: str,
                                       fixture_root: str | Path | None = None,
                                       recorded_tips: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Create an explicit, non-production inventory under a controlled fixture root.

    ``fixture_root`` exists for hermetic tests. Production CLI resolution always
    uses the fixed per-user state directory and cannot be redirected by JSON.
    """
    common_dir = _common_dir(repository)
    root = Path(fixture_root).expanduser().resolve(strict=True) if fixture_root else DISPOSABLE_FIXTURE_ROOT.resolve(strict=False)
    top_result = subprocess.run(["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
                                capture_output=True, text=True, check=False, timeout=10)
    if top_result.returncode:
        raise JgError("cannot resolve disposable fixture worktree")
    top_path = Path(top_result.stdout.strip()).resolve(strict=True)
    try:
        top_path.relative_to(root)
    except ValueError as exc:
        raise JgError("disposable fixture repository is outside the controlled fixture root") from exc
    if root.is_symlink():
        raise JgError("disposable fixture root must not be a symlink")
    if not root.is_dir() or root.stat().st_mode & 0o077:
        raise JgError("disposable fixture root must be an existing owner-only directory")
    if not operator.strip() or len(set(branches)) != len(branches):
        raise JgError("fixture inventory requires an operator and a finite unique branch list")
    remotes = subprocess.run(["git", "-C", str(repository), "remote"],
                             capture_output=True, text=True, check=False, timeout=10)
    worktrees = subprocess.run(["git", "-C", str(repository), "worktree", "list", "--porcelain"],
                               capture_output=True, text=True, check=False, timeout=10)
    if remotes.returncode or remotes.stdout.strip():
        raise JgError("disposable fixture must have no configured remotes")
    hooks = subprocess.run(["git", "-C", str(repository), "config", "--show-origin", "--get", "core.hooksPath"],
                           capture_output=True, text=True, check=False, timeout=10)
    includes = subprocess.run(["git", "-C", str(repository), "config", "--show-origin", "--get-regexp",
                              r"^include(\..*)?\.path$"],
                             capture_output=True, text=True, check=False, timeout=10)
    if hooks.returncode == 0 or includes.returncode == 0:
        raise JgError("disposable fixture has externally configured Git hooks or config includes")
    if worktrees.returncode:
        raise JgError("cannot verify disposable fixture worktree inventory")
    registered = [Path(line[9:]).resolve(strict=False) for line in worktrees.stdout.splitlines()
                  if line.startswith("worktree ")]
    if registered != [top_path]:
        raise JgError("disposable fixture must have exactly one registered worktree")
    hooks_dir = common_dir / "hooks"
    if hooks_dir.exists() and any(path.is_file() and os.access(path, os.X_OK)
                                  and not path.name.endswith(".sample") for path in hooks_dir.iterdir()):
        raise JgError("disposable fixture has an executable repository hook")
    tips: dict[str, str] = {}
    for branch in sorted(branches):
        if not branch or branch.startswith("-"):
            raise JgError("disposable fixture branch list is invalid")
        valid = subprocess.run(["git", "check-ref-format", "--branch", branch],
                               capture_output=True, text=True, check=False, timeout=10)
        if valid.returncode:
            raise JgError("disposable fixture branch list is invalid")
        tip = subprocess.run(["git", "-C", str(repository), "rev-parse", "--verify", f"refs/heads/{branch}"],
                             capture_output=True, text=True, check=False, timeout=10)
        if tip.returncode:
            if not recorded_tips or branch not in recorded_tips:
                raise JgError("disposable fixture branch is missing")
            exists = subprocess.run(["git", "-C", str(repository), "show-ref", "--verify", "--quiet",
                                     f"refs/heads/{branch}"], check=False, timeout=10)
            if exists.returncode != 1:
                raise JgError("disposable fixture branch lookup failed")
            tips[branch] = recorded_tips[branch]
        else:
            tips[branch] = tip.stdout.strip()
    return {"kind": "jev-disposable-cleanup-fixture", "schema_version": 1,
            "common_dir": str(common_dir), "scope": "disposable_fixture",
            "fixture_root": str(root), "worktree": str(top_path),
            "no_known_automation": True, "creators": [], "branches": sorted(branches),
            "branch_tips": tips, "operator": operator.strip(),
            "contract": COOPERATIVE_LEASE_CONTRACT}


def resolve_disposable_fixture_capability(repository: str | Path,
                                          inventory: Mapping[str, Any],
                                          expected_branches: list[str], *,
                                          fixture_root: str | Path | None = None) -> DisposableFixtureLeaseCapability:
    """Validate explicit no-creator fixture evidence; never production authority."""
    common_dir = _common_dir(repository)
    root = Path(fixture_root).resolve(strict=True) if fixture_root else DISPOSABLE_FIXTURE_ROOT.resolve(strict=False)
    if inventory.get("fixture_root") != str(root):
        raise JgError("disposable fixture inventory is outside the controlled fixture root")
    recorded = inventory.get("branch_tips")
    if (not isinstance(recorded, dict)
            or set(recorded) != set(expected_branches)
            or any(not isinstance(tip, str) or not re.fullmatch(r"[0-9a-f]{40,64}", tip)
                   for tip in recorded.values())):
        raise JgError("disposable fixture inventory has invalid pinned branch tips")
    expected = build_disposable_fixture_inventory(repository, expected_branches,
                                                  operator=str(inventory.get("operator", "")),
                                                  fixture_root=root,
                                                  recorded_tips=inventory.get("branch_tips"))
    if dict(inventory) != expected or inventory.get("common_dir") != str(common_dir):
        raise JgError("disposable fixture inventory does not match the live repository scope")
    return DisposableFixtureLeaseCapability(common_dir, _fixed_lock_root(),
                                             tuple(expected_branches),
                                             tuple(sorted((str(k), str(v)) for k, v in inventory["branch_tips"].items())),
                                             str(inventory["operator"]), root)


class CleanupActionJournal:
    """Owner-private append-only JSONL journal for cleanup action receipts."""

    def __init__(self, path: str | Path):
        raw_path = Path(path).expanduser()
        if raw_path.is_symlink():
            raise JgError("cleanup journal must not be a symlink")
        self.path = raw_path.resolve(strict=False)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.stat().st_mode & 0o077:
            raise JgError("cleanup journal directory must be owner-only")
        if self.path.exists() and self.path.is_symlink():
            raise JgError("cleanup journal must not be a symlink")
        if self.path.exists() and self.path.stat().st_mode & 0o077:
            raise JgError("cleanup journal must be owner-only")

    def read_events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                value = json.loads(line)
                if not isinstance(value, dict) or value.get("schema") != JOURNAL_SCHEMA:
                    raise ValueError("invalid journal event")
                events.append(value)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise JgError("cleanup journal is unreadable or malformed") from exc
        return events

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        value = {"schema": JOURNAL_SCHEMA, "recorded_at": _utc_now(), **dict(event)}
        payload = canonical_json(value) + b"\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "ab", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            parent_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except OSError as exc:
            raise JgError("cleanup journal append failed") from exc
        return value

    def pending_intents(self, *, plan_digest: str | None = None) -> list[dict[str, Any]]:
        events = self.read_events()
        pending: dict[str, dict[str, Any]] = {}
        for event in events:
            if plan_digest is not None and event.get("plan_digest") != plan_digest:
                continue
            action_id = event.get("action_id")
            if not isinstance(action_id, str):
                continue
            if event.get("event") == "intent":
                pending[action_id] = event
            elif event.get("event") in {"result", "reconciled"}:
                pending.pop(action_id, None)
        return [pending[key] for key in sorted(pending)]


class CooperativeBranchLeaseAdapter:
    """Cross-process per-branch advisory lock shared by creators and cleanup.

    Production readiness comes only from revalidated installed hook digests;
    manually registered IDs are diagnostic and never grant deletion authority.
    """

    contract = COOPERATIVE_LEASE_CONTRACT

    def __init__(self, repository_id: str, lease_dir: str | Path | None = None, *,
                 lock_timeout: float = 0.0,
                 capability: CreatorLeaseCapability | DisposableFixtureLeaseCapability | None = None):
        if not repository_id:
            raise JgError("cooperative lease requires repository identity")
        self.repository_id = repository_id
        self.capability = capability
        configured_root = lease_dir or os.environ.get("JEV_BRANCH_LEASE_DIR")
        self.lease_dir = Path(configured_root or Path.home() / ".local" / "state" /
                              "jev-git-graph" / "branch-leases").expanduser().resolve(strict=False)
        self.lease_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.lease_dir.stat().st_mode & 0o077:
            raise JgError("cooperative lease directory must be owner-only")
        self.lock_timeout = max(0.0, float(lock_timeout))
        self._registered: set[str] = set()
        self._held: dict[str, tuple[int, int]] = {}
        self._thread_locks: dict[str, threading.RLock] = {}
        self._thread_locks_guard = threading.Lock()
        self.common_dir: Path | None = None

    @classmethod
    def for_repository(cls, repository: str | Path, *, lock_timeout: float = 0.0) -> "CooperativeBranchLeaseAdapter":
        """Bind to Git's canonical common directory and shared Home Lab lock root.

        The constructor computes lock identity itself so an opaque artifact ID
        or linked-checkout path cannot silently miss a creator's branch lock.
        """
        cp = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=False, timeout=15,
        )
        if cp.returncode or not cp.stdout.strip():
            raise JgError("cannot resolve canonical Git common directory for cooperative lease")
        common_dir = Path(cp.stdout.strip()).resolve(strict=True)
        adapter = cls(str(common_dir), lock_timeout=lock_timeout)
        adapter.common_dir = common_dir
        return adapter

    @classmethod
    def for_production_repository(cls, repository: str | Path, *, lock_timeout: float = 0.0) -> "CooperativeBranchLeaseAdapter":
        capability = resolve_production_creator_capability(repository)
        adapter = cls(str(capability.common_dir), capability.lock_root,
                      lock_timeout=lock_timeout, capability=capability)
        adapter.common_dir = capability.common_dir
        return adapter

    @classmethod
    def for_disposable_fixture(cls, repository: str | Path, inventory: Mapping[str, Any],
                                expected_branches: list[str], *, lock_timeout: float = 0.0,
                                fixture_root: str | Path | None = None) -> "CooperativeBranchLeaseAdapter":
        capability = resolve_disposable_fixture_capability(
            repository, inventory, expected_branches, fixture_root=fixture_root)
        adapter = cls(str(capability.common_dir), capability.lock_root,
                      lock_timeout=lock_timeout, capability=capability)
        adapter.common_dir = capability.common_dir
        return adapter

    @property
    def creator_participants(self) -> frozenset[str]:
        return frozenset(self._registered)

    @property
    def creator_participation_complete(self) -> bool:
        return isinstance(self.capability, CreatorLeaseCapability)

    @property
    def established(self) -> bool:
        if isinstance(self.capability, DisposableFixtureLeaseCapability):
            return self.capability.is_current(self.common_dir or "")
        return self.creator_participation_complete

    def register_creator(self, creator_id: str) -> None:
        if creator_id not in REQUIRED_HOME_LAB_CREATORS:
            raise JgError("unknown cooperative creator adapter")
        self._registered.add(creator_id)

    def _lock_path(self, branch: str) -> Path:
        if not branch or branch.startswith("-") or "\0" in branch:
            raise JgError("cooperative lease branch name is invalid")
        return self.lease_dir / f"{_branch_key(self.repository_id, branch)}.lock"

    def _thread_lock(self, branch: str) -> threading.RLock:
        with self._thread_locks_guard:
            return self._thread_locks.setdefault(branch, threading.RLock())

    def acquire(self, name: str, tip: str = "") -> bool:
        local_lock = self._thread_lock(name)
        if not local_lock.acquire(timeout=self.lock_timeout):
            return False
        held = self._held.get(name)
        if held is not None:
            self._held[name] = (held[0], held[1] + 1)
            return True
        path = self._lock_path(name)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
            os.fchmod(fd, 0o600)
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._held[name] = (fd, 1)
                    return True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        os.close(fd)
                        local_lock.release()
                        return False
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        except OSError as exc:
            local_lock.release()
            raise JgError("cooperative branch lease acquisition failed") from exc

    def release(self, name: str, tip: str = "") -> bool:
        held = self._held.get(name)
        if held is None:
            return False
        fd, count = held
        if count > 1:
            self._held[name] = (fd, count - 1)
            self._thread_lock(name).release()
            return True
        self._held.pop(name, None)
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            self._thread_lock(name).release()
            return True
        except OSError:
            try:
                self._thread_lock(name).release()
            except RuntimeError:
                pass
            return False

    @contextmanager
    def creator_operation(self, creator_id: str, branch: str,
                          expected_tip: str = "") -> Iterator[None]:
        """Hold the shared branch lock around the creator's whole operation.

        The lock must wrap branch/ref creation, worktree creation or checkout,
        and publication. Calling this after those operations is not participation.
        """
        if creator_id not in self._registered:
            raise JgError("creator adapter must register before operations begin")
        if not self.acquire(branch, expected_tip):
            raise JgError("cooperative branch lease is busy")
        try:
            yield
        finally:
            if not self.release(branch, expected_tip):
                raise JgError("cooperative branch lease release failed")

    @contextmanager
    def cleanup_operation(self, branch: str, tip: str) -> Iterator[None]:
        if not self.acquire(branch, tip):
            raise JgError("cooperative branch lease is busy")
        try:
            yield
        finally:
            if not self.release(branch, tip):
                raise JgError("cooperative branch lease release failed")


def cleanup_action_id(plan_digest: str, index: int, branch: str, tip: str) -> str:
    return digest({"plan_digest": plan_digest, "index": index,
                   "branch": branch, "tip": tip})


def _ref_tip(repo: Path, name: str) -> str | None:
    import subprocess

    ref = name if name.startswith("refs/") else f"refs/heads/{name}"
    result = subprocess.run(
        ("git", "-C", str(repo), "rev-parse", "--verify", ref),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=5,
    )
    if result.returncode:
        return None
    value = result.stdout.decode("ascii", "replace").strip()
    return value or None


def _restore_from_bundle(repo: Path, branch: str, tip: str, bundle: Path,
                        action_id: str) -> bool:
    """Import a recovery tip, then compare-and-create its original branch ref."""
    import subprocess

    current = _ref_tip(repo, branch)
    if current is not None:
        return current == tip
    recovery_ref = f"refs/jev-git-graph/recovery/{action_id}"
    recovered = _ref_tip(repo, recovery_ref)
    if recovered is None:
        fetch = subprocess.run(
            ("git", "-C", str(repo), "fetch", "--quiet", str(bundle),
             f"refs/heads/{branch}:{recovery_ref}"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=30,
        )
        if fetch.returncode:
            return False
        recovered = _ref_tip(repo, recovery_ref)
    if recovered != tip or _ref_tip(repo, branch) is not None:
        return False
    created = subprocess.run(
        ("git", "-C", str(repo), "update-ref", f"refs/heads/{branch}",
         tip, "0" * len(tip)),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, timeout=5,
    )
    if created.returncode:
        return _ref_tip(repo, branch) == tip
    return _ref_tip(repo, branch) == tip


def reconcile_interrupted_cleanup(repo: str | Path, plan: Mapping[str, Any],
                                  journal: CleanupActionJournal,
                                  lease: CooperativeBranchLeaseAdapter) -> list[dict[str, Any]]:
    """Reconcile unfinished intents without replacing a recreated or moved ref.

    An absent source is restored only from the approved bundle, and only with
    compare-and-create against an absent ref. A present ref at any other SHA is
    preserved and recorded as a conflict. Reconciliation never resumes deletion.
    """
    from .cleanup import _plan_digest

    root = Path(repo).expanduser().resolve()
    if lease.common_dir != _common_dir(root) or lease.lease_dir != _fixed_lock_root():
        raise JgError("cleanup reconciliation lease scope does not match the repository")
    capability = lease.capability
    if isinstance(capability, CreatorLeaseCapability):
        if not production_capability_is_current(capability, root):
            raise JgError("production creator hook capability is stale or unavailable")
    elif isinstance(capability, DisposableFixtureLeaseCapability):
        if not capability.is_current(root):
            raise JgError("disposable fixture capability is stale or unavailable")
    else:
        raise JgError("cleanup reconciliation requires a verified lease capability")
    receipt_metadata = capability_receipt_metadata(capability)
    plan_digest = str(plan.get("plan_digest") or "")
    bundle = plan.get("bundle")
    if (not plan_digest or _plan_digest(plan) != plan_digest
            or plan.get("manifest_approved") is not True
            or not isinstance(bundle, Mapping)
            or bundle.get("restoration_verified") is not True):
        raise JgError("interrupted cleanup requires an approved restored bundle")
    bundle_path = Path(str(bundle.get("path", ""))).expanduser().resolve()
    if not bundle_path.is_file() or hashlib.sha256(bundle_path.read_bytes()).hexdigest() != bundle.get("sha256"):
        raise JgError("cleanup bundle is missing or changed")
    candidates = plan.get("candidates")
    main = plan.get("main")
    if not isinstance(candidates, list) or not isinstance(main, Mapping):
        raise JgError("interrupted cleanup plan is malformed")
    if isinstance(capability, DisposableFixtureLeaseCapability):
        planned = tuple(sorted(str(item.get("name")) for item in candidates))
        if planned != capability.branches:
            raise JgError("fixture reconciliation branch inventory mismatch")
    results: list[dict[str, Any]] = []
    for intent in journal.pending_intents(plan_digest=plan_digest):
        index = intent.get("candidate_index")
        if (not isinstance(index, int) or isinstance(index, bool)
                or index < 0 or index >= len(candidates)
                or not isinstance(candidates[index], Mapping)):
            raise JgError("cleanup journal intent does not match the approved plan")
        candidate = candidates[index]
        branch = str(intent.get("branch") or "")
        tip = str(intent.get("tip") or "")
        main = str(intent.get("destination") or "")
        main_tip = str(intent.get("destination_tip") or "")
        action_id = str(intent.get("action_id") or "")
        if (candidate.get("name") != branch or candidate.get("tip") != tip
                or plan["main"].get("name") != main
                or plan["main"].get("tip") != main_tip
                or intent.get("bundle_sha256") != bundle.get("sha256")
                or intent.get("creator_capability_sha256") != receipt_metadata["creator_capability_sha256"]
                or intent.get("creator_runtime_commit") != receipt_metadata["creator_runtime_commit"]
                or action_id != cleanup_action_id(plan_digest, index, branch, tip)):
            raise JgError("cleanup journal intent does not match the approved plan or creator capability")
        if isinstance(capability, CreatorLeaseCapability) and not production_capability_is_current(capability, root):
            raise JgError("production creator hook capability changed during reconciliation")
        if isinstance(capability, DisposableFixtureLeaseCapability) and not capability.is_current(root):
            raise JgError("disposable fixture capability changed during reconciliation")
        with lease.cleanup_operation(branch, tip):
            current = _ref_tip(root, branch)
            if current == tip:
                status, restored = "source_unchanged", False
            elif current is not None:
                status, restored = "source_recreated_or_moved", False
            else:
                restored = _restore_from_bundle(
                    root, branch, tip, bundle_path, action_id,
                )
                current_after = _ref_tip(root, branch)
                if current_after == tip:
                    status = "source_restored_after_interruption"
                elif current_after is not None:
                    status, restored = "source_recreated_during_reconcile", False
                else:
                    status, restored = "restore_uncertain", False
            result = {
                "event": "reconciled", "scope": capability.scope, "action_id": action_id,
                "plan_digest": plan_digest, "branch": branch, "tip": tip,
                "destination": main, "destination_tip": main_tip,
                "observed_destination_tip": _ref_tip(root, main),
                "status": status, "restored": restored, **receipt_metadata,
            }
            journal.append(result)
            results.append(result)
    return results


__all__ = [
    "COOPERATIVE_LEASE_CONTRACT", "REQUIRED_HOME_LAB_CREATORS",
    "CreatorLeaseCapability", "DisposableFixtureLeaseCapability",
    "resolve_production_creator_capability", "capability_receipt_metadata",
    "build_disposable_fixture_inventory",
    "resolve_disposable_fixture_capability", "default_cleanup_journal_path",
    "CleanupActionJournal", "CooperativeBranchLeaseAdapter",
    "cleanup_action_id", "reconcile_interrupted_cleanup",
]
