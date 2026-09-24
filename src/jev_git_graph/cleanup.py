"""Evidence-bound local branch cleanup planning and guarded execution.

The planner consumes the strict, immutable ``coverage.json`` artifact.  It
never treats a semantic relationship, ancestry, or a review disposition as
permission to remove a ref.  Before a plan is approved it creates a bundle
containing every pinned tip and restores each tip in a disposable repository.

Execution is deliberately a separate, narrow step.  It requires approval of
the exact plan digest and a cooperative lease supplied by the caller.  Every
candidate is rechecked immediately before an atomic compare-and-delete; any
uncertainty stops the batch and an already removed ref is restored with an
atomic compare-and-create.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import git
from .coverage import _tree
from .coordinator import (COOPERATIVE_LEASE_CONTRACT, CleanupActionJournal,
                          CooperativeBranchLeaseAdapter, CreatorLeaseCapability,
                          DisposableFixtureLeaseCapability, _fixed_lock_root,
                          _common_dir, capability_receipt_metadata,
                          production_capability_is_current,
                          cleanup_action_id)
from .errors import JgError
from .equivalence import _activity
from .inventory import protected_worktree_paths
from .safety import digest, opaque_path_id, read_json, validate_output_path, write_json


SCHEMA_VERSION = 1
MAX_BRANCHES = 25
# Retained only for compatibility with old offline tests; never grants authority.
CREATOR_LEASE_INTEGRATED = False
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


@dataclass
class CooperativeLease:
    """Concrete lease adapter supplied by an integrated branch coordinator.

    The marker prevents a loose ``{"established": true}`` payload from being
    mistaken for a creator-backed lease.  Tests may construct this adapter
    with local callbacks; production callers must bind the callbacks to their
    cooperative creator/renewal service.
    """

    established: bool
    acquire_fn: Callable[[str, str], bool]
    release_fn: Callable[[str, str], bool]
    contract: str = COOPERATIVE_LEASE_CONTRACT

    def acquire(self, name: str, tip: str) -> bool:
        return bool(self.acquire_fn(name, tip))

    def release(self, name: str, tip: str) -> bool:
        return bool(self.release_fn(name, tip))


def _git(repo: Path, *args: str, check: bool = True,
         input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ("git", "-C", str(repo), *args),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", "replace").splitlines()
        raise JgError(detail[0] if detail else "Git command failed")
    return result


def _out(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", "replace")


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise JgError(f"cleanup coverage has invalid {label}")
    return value


def _plan_digest(plan: Mapping[str, Any]) -> str:
    unsigned = dict(plan)
    unsigned.pop("plan_digest", None)
    return digest(unsigned)


def _validate_coverage(coverage: Mapping[str, Any]) -> tuple[str, str, int, float, list[dict[str, Any]]]:
    if coverage.get("kind") != "branch-coverage" or coverage.get("schema_version") != 1:
        raise JgError("cleanup requires strict branch-coverage schema version 1")
    if coverage.get("network_performed") is not False or coverage.get("destructive_action_authorized") is not False:
        raise JgError("coverage artifact has unsafe execution flags")
    repository_id = coverage.get("repository_id")
    main = coverage.get("main")
    if not isinstance(repository_id, str) or not isinstance(main, Mapping):
        raise JgError("coverage artifact is missing repository identity")
    main_name = main.get("name")
    main_tip = _sha(main.get("tip"), "main tip")
    if not isinstance(main_name, str) or not main_name:
        raise JgError("coverage artifact has invalid main branch")
    cutoff = coverage.get("activity_cutoff_epoch")
    if not isinstance(cutoff, int):
        raise JgError("coverage artifact has no strict activity cutoff")
    recent_hours = coverage.get("recent_hours")
    if not isinstance(recent_hours, (int, float)) or recent_hours < 24:
        raise JgError("cleanup requires coverage with at least a 24-hour age policy")
    records = coverage.get("branches")
    if not isinstance(records, list):
        raise JgError("coverage artifact has no branch records")
    seen: set[str] = set()
    valid: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise JgError("coverage artifact contains a malformed branch record")
        name = record.get("name")
        if not isinstance(name, str) or not name or name in seen:
            raise JgError("coverage artifact contains duplicate or invalid branch names")
        seen.add(name)
        tip = _sha(record.get("tip"), f"tip for {name}")
        if record.get("main_tip") != main_tip:
            raise JgError(f"coverage record for {name} has a different main tip")
        paths = record.get("paths")
        if not isinstance(paths, list):
            raise JgError(f"coverage record for {name} has malformed paths")
        for path in paths:
            if not isinstance(path, Mapping) or path.get("verdict") not in {"EXACT_PRESENT", "DISTINCT"}:
                raise JgError(f"coverage record for {name} has malformed path evidence")
        valid.append(dict(record, tip=tip, name=name))
    if not any(record.get("name") == main_name and record.get("tip") == main_tip for record in valid):
        raise JgError("coverage artifact does not record its main branch")
    return main_name, main_tip, cutoff, float(recent_hours), valid


def _worktree_holds(runner: git.GitRunner) -> tuple[dict[str, list[str]], set[str]]:
    """Return branch -> hold reasons and the branches with linked stashes."""
    holds: dict[str, list[str]] = {}
    for item in git.worktrees(runner):
        branch = item.get("branch")
        if not branch:
            continue
        name = str(branch)
        holds.setdefault(name, []).append("checked_out")
        try:
            status = git.status_for(Path(str(item["path"])))
        except JgError:
            holds[name].append("status_unavailable")
        else:
            if status:
                holds[name].append("dirty_worktree")

    linked_stashes: set[str] = set()
    for stash in git.stashes(runner):
        # Git's default stash subject is ``On <branch>: ...`` or ``WIP on
        # <branch>: ...``.  A branch named in a stash is a preservation hold.
        match = re.search(r"(?:^|\s)(?:WIP )?on ([^:]+):", stash.get("subject", ""))
        if match:
            linked_stashes.add(match.group(1))
    return holds, linked_stashes


def _candidate_reason(record: Mapping[str, Any], cutoff: int, holds: Mapping[str, list[str]], linked_stashes: set[str],
                     main_name: str) -> str | None:
    name = str(record["name"])
    if name == main_name:
        return "default_branch"
    if record.get("verdict") != "EXACT":
        return str(record.get("reason") or "coverage_not_exact")
    activity = record.get("last_activity_epoch")
    if not isinstance(activity, int):
        return "activity_unverifiable"
    if activity >= cutoff:
        return "recent_activity"
    if name in holds:
        return ",".join(dict.fromkeys(holds[name]))
    if name in linked_stashes:
        return "stash_linked"
    paths = record.get("paths", [])
    if any(path.get("verdict") != "EXACT_PRESENT" for path in paths):
        return "path_evidence_not_exact"
    return None


def _bundle_path(bundle_dir: Path) -> Path:
    bundle_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if bundle_dir.stat().st_mode & 0o077:
        raise JgError(f"bundle directory must be owner-only: {bundle_dir}")
    return bundle_dir / "cleanup.bundle"


def _create_and_verify_bundle(repo: Path, tips: Mapping[str, str], bundle_dir: Path) -> dict[str, Any]:
    path = _bundle_path(bundle_dir)
    # ``--all`` supplies named refs to Git's bundle machinery.  The manifest
    # still pins and independently restores only the approved tips below;
    # keeping the source refs in the bundle makes the artifact self-contained
    # on Git versions that reject bare object IDs as bundle revisions.
    if path.exists():
        path.unlink()
    _git(repo, "bundle", "create", str(path), "--all")
    verify = _git(repo, "bundle", "verify", str(path), check=False)
    if verify.returncode:
        raise JgError("cleanup bundle verification failed")
    # Keep the disposable restore checkout under the already validated,
    # owner-only bundle directory so TMPDIR cannot redirect it into a
    # protected worktree.
    restored = tempfile.mkdtemp(prefix="jev-cleanup-restore-", dir=str(bundle_dir))
    restored_path = Path(restored)
    try:
        _git(restored_path, "init", "-q", "--initial-branch=main")
        # Fetch the advertised local heads into independent refs.  Fetching a
        # named ref avoids relying on Git allowing an unadvertised raw SHA.
        fetched = _git(restored_path, "fetch", "-q", str(path),
                       "+refs/heads/*:refs/recovered/*", check=False)
        if fetched.returncode:
            raise JgError("cleanup bundle could not restore local heads")
        for index, (name, tip) in enumerate(tips.items()):
            resolved = _out(_git(restored_path, "rev-parse", f"{tip}^{{commit}}", check=False)).strip()
            if resolved != tip:
                raise JgError(f"cleanup bundle restored the wrong tip for {name}")
            # Keep a numbered ref too, so the proof is independent of source
            # branch naming and remains inspectable if the directory is kept.
            update = _git(restored_path, "update-ref", f"refs/recovered/{index}", tip, check=False)
            if update.returncode:
                raise JgError(f"cleanup bundle could not retain {name}")
    finally:
        shutil.rmtree(restored_path, ignore_errors=True)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "tips": dict(tips),
        "restoration_verified": True,
        "manifest_approved": False,
    }


def build_cleanup_plan(repo: str | Path, coverage: Mapping[str, Any] | str | Path,
                       *, bundle_dir: str | Path | None = None,
                       max_branches: int = MAX_BRANCHES) -> dict[str, Any]:
    """Build a bounded, deletion-ready plan from a strict coverage artifact.

    ``coverage`` may be an in-memory object or a JSON path.  All current Git
    observations are local and read-only.  A bundle is written outside the
    repository and independently restored before the plan receives approval.
    """
    if max_branches <= 0 or max_branches > MAX_BRANCHES:
        raise JgError(f"max_branches must be between 1 and {MAX_BRANCHES}")
    artifact = read_json(coverage) if isinstance(coverage, (str, Path)) else dict(coverage)
    main_name, main_tip, cutoff, recent_hours, records = _validate_coverage(artifact)
    root, _common, runner = git.open_repository(repo)
    if artifact.get("repository_id") != opaque_path_id(root):
        raise JgError("coverage belongs to a different local repository")
    live_records = {item["name"]: item for item in git.local_branches(runner)}
    live = {name: item["tip"] for name, item in live_records.items()}
    if live.get(main_name) != main_tip:
        raise JgError("coverage default branch tip changed")
    holds, linked_stashes = _worktree_holds(runner)
    decisions: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    for record in records:
        name, tip = record["name"], record["tip"]
        current_activity = _activity(runner, live_records[name]) if name in live_records and name != main_name else None
        current_record = dict(record, last_activity_epoch=current_activity)
        reason = _candidate_reason(current_record, cutoff, holds, linked_stashes, main_name)
        if name != main_name and (current_activity is None or current_activity >= int(time.time()) - int(recent_hours * 3600)):
            reason = "recent_or_unverifiable_activity"
        if live.get(name) != tip:
            reason = "source_tip_changed_or_missing"
        if name == main_name and reason is None:
            reason = "default_branch"
        decision = {"name": name, "tip": tip, "main_tip": main_tip,
                    "eligible": reason is None, "reason": reason,
                    "last_activity_epoch": current_activity,
                    "paths": list(record.get("paths", []))}
        decisions.append(decision)
        if reason is None:
            eligible.append(decision)
    # Independently recompute strict exact-content evidence.  A forged or
    # incomplete coverage record cannot promote a branch to a cleanup target.
    independently_exact: list[dict[str, Any]] = []
    for item in eligible:
        reason = _content_reproof(runner, item["tip"], main_tip)
        if reason is None:
            independently_exact.append(item)
        else:
            item["eligible"] = False
            item["reason"] = reason
    eligible = independently_exact
    eligible.sort(key=lambda item: (item["last_activity_epoch"], item["name"]))
    selected = eligible[:max_branches]
    for item in eligible[max_branches:]:
        item["eligible"] = False
        item["reason"] = "plan_limit"

    tips = {main_name: main_tip}
    tips.update((item["name"], item["tip"]) for item in selected)
    protected_paths = protected_worktree_paths(root)
    if bundle_dir is None:
        # Validate TMPDIR before creating anything.  An operator can point
        # tempfile at a path inside the inspected checkout.
        temp_parent = validate_output_path(tempfile.gettempdir(), protected_paths)
        bundle_root = Path(tempfile.mkdtemp(prefix="jev-cleanup-bundle-",
                                            dir=str(temp_parent)))
    else:
        bundle_root = validate_output_path(bundle_dir, protected_paths)
    bundle = _create_and_verify_bundle(root, tips, bundle_root)
    plan: dict[str, Any] = {
        "kind": "cleanup-plan",
        "schema_version": SCHEMA_VERSION,
        "repository_id": artifact["repository_id"],
        "coverage_digest": digest(artifact),
        "main": {"name": main_name, "tip": main_tip},
        "activity_cutoff_epoch": cutoff,
        "recent_hours": recent_hours,
        "max_branches": max_branches,
        "candidates": selected,
        "observed": decisions,
        "bundle": bundle,
        "manifest_approved": False,
        "deletion_ready": False,
        "network_performed": False,
        "destructive_action_authorized": False,
    }
    plan["plan_digest"] = _plan_digest(plan)
    return plan


def approve_cleanup_plan(plan: Mapping[str, Any] | str | Path,
                         *, approved_digest: str | None = None) -> dict[str, Any]:
    """Record explicit operator approval after independent bundle proof.

    Approval is represented in the signed-by-digest plan object.  The input is
    copied and never rewritten on disk; callers should persist the returned
    object and approve its new digest for execution.
    """
    approved = read_json(plan) if isinstance(plan, (str, Path)) else dict(plan)
    original_digest = approved.get("plan_digest")
    if not isinstance(original_digest, str) or _plan_digest(approved) != original_digest:
        raise JgError("cleanup plan has an invalid digest")
    if approved_digest != original_digest:
        raise JgError("exact cleanup plan digest approval is required")
    if approved.get("manifest_approved") is True:
        raise JgError("cleanup plan is already approved")
    bundle = approved.get("bundle")
    if not isinstance(bundle, Mapping) or bundle.get("restoration_verified") is not True:
        raise JgError("cleanup bundle must pass independent restoration before approval")
    approved["bundle"] = dict(bundle, manifest_approved=True)
    approved["manifest_approved"] = True
    approved["deletion_ready"] = True
    approved["plan_digest"] = _plan_digest(approved)
    return approved


def write_cleanup_plan(repo: str | Path, coverage_path: str | Path, out: str | Path,
                       *, bundle_dir: str | Path | None = None,
                       max_branches: int = MAX_BRANCHES) -> Path:
    """Write a private cleanup plan artifact outside the inspected worktrees."""
    root, _common, _runner = git.open_repository(repo)
    destination = validate_output_path(out, protected_worktree_paths(root))
    plan = build_cleanup_plan(root, coverage_path, bundle_dir=bundle_dir, max_branches=max_branches)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = destination / "cleanup-plan.json"
    write_json(path, plan)
    return path


def _lease_established(contract: Any, repository: Path | None = None) -> bool:
    if not isinstance(contract, CooperativeBranchLeaseAdapter):
        return False
    if repository is None or contract.common_dir is None or contract.capability is None:
        return False
    try:
        canonical_common = _common_dir(repository)
        if canonical_common != contract.common_dir:
            return False
        if contract.lease_dir != _fixed_lock_root():
            return False
        if isinstance(contract.capability, CreatorLeaseCapability):
            if not production_capability_is_current(contract.capability, repository):
                return False
        elif isinstance(contract.capability, DisposableFixtureLeaseCapability):
            if not contract.capability.is_current(repository):
                return False
        else:
            return False
    except (JgError, OSError, subprocess.SubprocessError):
        return False
    return True


def _lease_call(contract: Any, method: str, name: str, tip: str) -> bool:
    callback = getattr(contract, method, None)
    if callback is None:
        return True if method == "release" else False
    return bool(callback(name, tip))


def _live_reproof(root: Path, candidate: Mapping[str, Any], main: Mapping[str, Any],
                  runner: git.GitRunner, recent_hours: float) -> str | None:
    name, tip = str(candidate["name"]), str(candidate["tip"])
    main_name, main_tip = str(main["name"]), str(main["tip"])
    live_records = {item["name"]: item for item in git.local_branches(runner)}
    live = {branch: item["tip"] for branch, item in live_records.items()}
    if live.get(name) != tip:
        return "source_tip_changed_or_missing"
    if live.get(main_name) != main_tip:
        return "destination_tip_changed"
    holds, linked = _worktree_holds(runner)
    if name in holds:
        return ",".join(dict.fromkeys(holds[name]))
    if name in linked:
        return "stash_linked"
    activity = _activity(runner, live_records[name]) if recent_hours else None
    if activity is None or activity >= int(time.time()) - int(recent_hours * 3600):
        return "recent_or_unverifiable_activity"
    return _content_reproof(runner, tip, main_tip)


def _content_reproof(runner: git.GitRunner, source_tip: str, destination_tip: str) -> str | None:
    """Recompute exact source coverage from live Git trees.

    The coverage JSON is an input and may be stale or incomplete.  This proof
    derives the merge base and complete net changed path set again, comparing
    blob, mode, kind, and deletion state for every path.
    """
    base = git.merge_base(runner, source_tip, destination_tip)
    if base is None:
        return "no_merge_base"
    try:
        base_tree = _tree(runner, base)
        source_tree = _tree(runner, source_tip)
        destination_tree = _tree(runner, destination_tip)
    except JgError:
        return "tree_unavailable"
    changed = sorted(path for path in base_tree.keys() | source_tree.keys()
                     if base_tree.get(path) != source_tree.get(path))
    if any(source_tree.get(path) != destination_tree.get(path) for path in changed):
        return "live_content_not_exact"
    return None


def _atomic_delete(root: Path, name: str, tip: str,
                   destination: str, destination_tip: str) -> bool:
    """Verify destination and delete source in one ref transaction."""
    commands = (
        "start\n"
        f"verify refs/heads/{destination} {destination_tip}\n"
        f"delete refs/heads/{name} {tip}\n"
        "prepare\n"
        "commit\n"
    ).encode("utf-8")
    result = _git(root, "update-ref", "--stdin", check=False, input_bytes=commands)
    return result.returncode == 0


def _atomic_restore(root: Path, name: str, tip: str) -> bool:
    result = _git(root, "update-ref", f"refs/heads/{name}", tip, "0" * len(tip), check=False)
    return result.returncode == 0


def _ref_presence(root: Path, name: str) -> bool | None:
    """Bounded readback: true present, false absent, None uncertain."""
    try:
        result = subprocess.run(
            ("git", "-C", str(root), "show-ref", "--verify", "--quiet", f"refs/heads/{name}"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _read_ref_tip(root: Path, name: str) -> str | None:
    result = _git(root, "rev-parse", "--verify", f"refs/heads/{name}", check=False)
    if result.returncode:
        return None
    value = _out(result).strip()
    return value or None


def _safe_release(contract: Any, name: str, tip: str) -> bool:
    try:
        return _lease_call(contract, "release", name, tip)
    except Exception:
        return False


def execute_cleanup(repo: str | Path, plan: Mapping[str, Any] | str | Path,
                    *, approved_digest: str | None = None,
                    lease_contract: Any = None,
                    journal_path: str | Path | None = None) -> dict[str, Any]:
    """Execute a plan only after digest, bundle, lease, and live reproof gates.

    With no established cooperative lease this returns a deletion-ready plan
    result and performs no ref mutation.  The caller can use that result to
    establish the lease, then explicitly invoke this function again.
    """
    loaded = read_json(plan) if isinstance(plan, (str, Path)) else dict(plan)
    expected = loaded.get("plan_digest")
    if not isinstance(expected, str) or _plan_digest(loaded) != expected:
        raise JgError("cleanup plan has an invalid digest")
    if approved_digest != expected:
        raise JgError("exact cleanup plan digest approval is required")
    bundle = loaded.get("bundle")
    if not isinstance(bundle, Mapping) or bundle.get("restoration_verified") is not True:
        raise JgError("cleanup bundle manifest is not independently approved")
    bundle_path = Path(str(bundle.get("path", ""))).expanduser().resolve()
    if not bundle_path.is_file() or hashlib.sha256(bundle_path.read_bytes()).hexdigest() != bundle.get("sha256"):
        raise JgError("cleanup bundle is missing or changed")
    root, _common, runner = git.open_repository(repo)
    scope = (lease_contract.capability.scope
             if isinstance(lease_contract, CooperativeBranchLeaseAdapter)
             and lease_contract.capability is not None else "production")
    if loaded.get("repository_id") != opaque_path_id(root):
        raise JgError("cleanup plan belongs to a different local repository")
    if (loaded.get("manifest_approved") is not True
            or bundle.get("manifest_approved") is not True):
        raise JgError("cleanup plan manifest is not approved")
    if not _lease_established(lease_contract, root):
        return {"kind": "cleanup-execution", "plan_digest": expected,
                "scope": scope,
                "deletion_ready": False, "mode": "deletion-ready-plan-only",
                "deleted": [], "stopped": "cooperative_lease_unestablished",
                "network_performed": False, "destructive_action_authorized": False}
    if journal_path is None:
        return {"kind": "cleanup-execution", "plan_digest": expected,
                "scope": scope,
                "deletion_ready": False, "mode": "deletion-ready-plan-only",
                "deleted": [], "stopped": "cleanup_journal_required",
                "network_performed": False, "destructive_action_authorized": False}
    journal_destination = validate_output_path(
        journal_path, [*protected_worktree_paths(root), _common_dir(root)],
    )
    journal = CleanupActionJournal(journal_destination)
    if journal.pending_intents():
        return {"kind": "cleanup-execution", "plan_digest": expected,
                "scope": scope,
                "deletion_ready": False, "mode": "deletion-ready-plan-only",
                "deleted": [], "stopped": "interrupted_cleanup_reconciliation_required",
                "network_performed": False, "destructive_action_authorized": False}
    deleted: list[dict[str, str]] = []
    if isinstance(lease_contract.capability, DisposableFixtureLeaseCapability):
        planned = tuple(sorted(str(item.get("name")) for item in loaded.get("candidates", [])))
        if planned != lease_contract.capability.branches:
            return {"kind": "cleanup-execution", "plan_digest": expected,
                    "scope": scope, "deletion_ready": False, "deleted": [],
                    "stopped": "fixture_branch_inventory_mismatch",
                    "network_performed": False, "destructive_action_authorized": False}
    creator_receipt = capability_receipt_metadata(lease_contract.capability)
    for index, candidate in enumerate(loaded.get("candidates", [])):
        name, tip = str(candidate["name"]), str(candidate["tip"])
        if not _lease_established(lease_contract, root):
            return {"kind": "cleanup-execution", "plan_digest": expected,
                    "scope": scope, "deletion_ready": False, "deleted": deleted,
                    "stopped": "creator_capability_changed", "branch": name,
                    "network_performed": False, "destructive_action_authorized": False}
        if not _lease_call(lease_contract, "acquire", name, tip):
            return {"kind": "cleanup-execution", "plan_digest": expected,
                    "scope": scope,
                    "deletion_ready": False, "deleted": deleted,
                    "stopped": "lease_not_acquired", "branch": name,
                    "network_performed": False, "destructive_action_authorized": False}
        acquired = True
        delete_committed = False
        outcome: dict[str, Any] | None = None
        action_id = cleanup_action_id(expected, index, name, tip)
        intent_written = False
        operation_exception = False
        try:
            reason = (None if _lease_established(lease_contract, root) else "creator_capability_changed")
            if reason is None:
                reason = _live_reproof(root, candidate, loaded["main"], runner,
                                       float(loaded.get("recent_hours", 0)))
            if reason:
                outcome = {"kind": "cleanup-execution", "plan_digest": expected,
                           "deletion_ready": False, "deleted": deleted,
                           "stopped": reason, "branch": name,
                           "network_performed": False, "destructive_action_authorized": False}
            else:
                destination = str(loaded["main"]["name"])
                destination_tip = str(loaded["main"]["tip"])
                journal.append({
                    "event": "intent", "action_id": action_id,
                    "scope": scope,
                    "plan_digest": expected, "approved_digest": approved_digest,
                    "candidate_index": index, "branch": name, "tip": tip,
                    "destination": destination, "destination_tip": destination_tip,
                    "bundle_sha256": bundle.get("sha256"),
                    **creator_receipt,
                })
                intent_written = True
                if not _lease_established(lease_contract, root):
                    outcome = {"kind": "cleanup-execution", "plan_digest": expected,
                               "deletion_ready": False, "deleted": deleted,
                               "stopped": "creator_capability_changed", "branch": name,
                               "network_performed": False, "destructive_action_authorized": False}
                elif not _atomic_delete(root, name, tip, destination, destination_tip):
                    outcome = {"kind": "cleanup-execution", "plan_digest": expected,
                               "deletion_ready": False, "deleted": deleted,
                               "stopped": "source_delete_race", "branch": name,
                               "network_performed": False, "destructive_action_authorized": False}
                else:
                    delete_committed = True
                    # A normal readback proves absence.  If the normal inventory
                    # read fails, probe the exact ref with a bounded command and
                    # restore only after proving it is absent.
                    try:
                        live_after = {item["name"]: item["tip"] for item in git.local_branches(runner)}
                    except JgError:
                        presence = _ref_presence(root, name)
                        if presence is not True:
                            restored = _atomic_restore(root, name, tip)
                            outcome = {"kind": "cleanup-execution", "plan_digest": expected,
                                       "deletion_ready": False, "deleted": deleted,
                                       "stopped": "delete_readback_uncertain",
                                       "restoration_attempted": True, "restored": restored,
                                       "branch": name, "network_performed": False,
                                       "destructive_action_authorized": False}
                        else:
                            outcome = {"kind": "cleanup-execution", "plan_digest": expected,
                                       "deletion_ready": False, "deleted": deleted,
                                       "stopped": "delete_readback_uncertain",
                                       "restoration_attempted": False, "branch": name,
                                       "network_performed": False, "destructive_action_authorized": False}
                    else:
                        if name in live_after:
                            outcome = {"kind": "cleanup-execution", "plan_digest": expected,
                                       "deletion_ready": False, "deleted": deleted,
                                       "stopped": "delete_verification_uncertain",
                                       "restoration_attempted": False, "branch": name,
                                       "network_performed": False, "destructive_action_authorized": False}
                        elif not _lease_established(lease_contract, root):
                            restored = _atomic_restore(root, name, tip)
                            outcome = {"kind": "cleanup-execution", "plan_digest": expected,
                                       "deletion_ready": False, "deleted": deleted,
                                       "stopped": "creator_capability_changed_after_delete",
                                       "restoration_attempted": True, "restored": restored,
                                       "branch": name, "network_performed": False,
                                       "destructive_action_authorized": False}
                        else:
                            deleted.append({"name": name, "tip": tip})
        except BaseException:
            operation_exception = True
            raise
        finally:
            try:
                if not operation_exception:
                    event_status = (
                        str(outcome.get("stopped")) if outcome is not None else
                        ("deleted" if any(item["name"] == name for item in deleted) else
                         ("intent_recorded_no_delete" if intent_written else "precondition_blocked"))
                    )
                    journal.append({
                        "event": "result", "action_id": action_id,
                        "scope": scope,
                        "plan_digest": expected, "branch": name, "tip": tip,
                        "destination": str(loaded["main"]["name"]),
                        "destination_tip": str(loaded["main"]["tip"]),
                        "observed_source_tip": _read_ref_tip(root, name),
                        "observed_destination_tip": _read_ref_tip(root, str(loaded["main"]["name"])),
                        "status": event_status,
                        **creator_receipt,
                    })
            finally:
                released = not acquired or _safe_release(lease_contract, name, tip)
            if acquired and not released:
                presence = _ref_presence(root, name) if delete_committed else True
                restoration_attempted = delete_committed and presence is not True
                restored = _atomic_restore(root, name, tip) if restoration_attempted else False
                if restored:
                    deleted = [item for item in deleted if item["name"] != name]
                if outcome is None:
                    outcome = {"kind": "cleanup-execution", "plan_digest": expected,
                               "deletion_ready": False, "deleted": deleted,
                               "stopped": "lease_release_failed", "branch": name,
                               "restoration_attempted": restoration_attempted,
                               "restored": restored,
                               "network_performed": False, "destructive_action_authorized": False}
                else:
                    outcome = {**outcome, "deletion_ready": False,
                               "stopped": "lease_release_failed",
                               "lease_release_failed": True,
                               "restoration_attempted": restoration_attempted,
                               "restored": restored}
                journal.append({
                    "event": "result", "action_id": action_id,
                    "scope": scope,
                    "plan_digest": expected, "branch": name, "tip": tip,
                    "destination": str(loaded["main"]["name"]),
                    "destination_tip": str(loaded["main"]["tip"]),
                    "observed_source_tip": _read_ref_tip(root, name),
                    "observed_destination_tip": _read_ref_tip(root, str(loaded["main"]["name"])),
                    "status": "lease_release_failed",
                    "restoration_attempted": restoration_attempted,
                    "restored": restored,
                    **creator_receipt,
                })
        if outcome is not None:
            return outcome
    return {"kind": "cleanup-execution", "plan_digest": expected,
            "scope": scope,
            "deletion_ready": True, "deleted": deleted, "stopped": None,
            **creator_receipt,
            "network_performed": False, "destructive_action_authorized": True}


__all__ = ["CooperativeLease", "approve_cleanup_plan", "build_cleanup_plan", "write_cleanup_plan", "execute_cleanup"]
