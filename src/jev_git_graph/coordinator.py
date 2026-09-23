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
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
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
    "scripts/ahc_app/coord_wake.py::ensure_runtime_worktree",
    "scripts/process-safe-prs.sh",
    "scripts/pr_gate/guard_execution.py",
    "scripts/merge_train_parts/prescreen.py",
    "scripts/train_construction_driver.py",
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _branch_key(repository_id: str, branch: str) -> str:
    return hashlib.sha256(f"{repository_id}\0{branch}".encode()).hexdigest()


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

    The adapter only becomes deletion-ready when every reviewed Home Lab
    creator has registered. Creator integrations register once during startup,
    then acquire ``creator_operation`` before their first ref/worktree mutation
    and retain it until publication is complete.
    """

    contract = COOPERATIVE_LEASE_CONTRACT

    def __init__(self, repository_id: str, lease_dir: str | Path | None = None, *,
                 lock_timeout: float = 0.0):
        if not repository_id:
            raise JgError("cooperative lease requires repository identity")
        self.repository_id = repository_id
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

    @property
    def creator_participants(self) -> frozenset[str]:
        return frozenset(self._registered)

    @property
    def creator_participation_complete(self) -> bool:
        return self.creator_participants == REQUIRED_HOME_LAB_CREATORS

    @property
    def established(self) -> bool:
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
                or action_id != cleanup_action_id(plan_digest, index, branch, tip)):
            raise JgError("cleanup journal intent does not match the approved plan")
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
                "event": "reconciled", "action_id": action_id,
                "plan_digest": plan_digest, "branch": branch, "tip": tip,
                "destination": main, "destination_tip": main_tip,
                "observed_destination_tip": _ref_tip(root, main),
                "status": status, "restored": restored,
            }
            journal.append(result)
            results.append(result)
    return results


__all__ = [
    "COOPERATIVE_LEASE_CONTRACT", "REQUIRED_HOME_LAB_CREATORS",
    "CleanupActionJournal", "CooperativeBranchLeaseAdapter",
    "cleanup_action_id", "reconcile_interrupted_cleanup",
]
