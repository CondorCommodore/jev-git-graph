"""Small, digest-bound caches for deterministic contribution analysis."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from threading import RLock
from typing import Any

from .errors import JgError
from .safety import digest


def checkpoint_key(snapshot: dict[str, Any], extractor_version: str) -> str:
    """Bind resumable work to the exact pinned snapshot and extractor."""
    return digest({
        "snapshot_digest": snapshot.get("snapshot_digest"),
        "repository_id": snapshot.get("repository_id"),
        "main": snapshot.get("main"),
        "branches": snapshot.get("branches"),
        "extractor_version": extractor_version,
    })


def load_checkpoint(path: str | Path, key: str) -> dict[str, dict[str, Any]]:
    target = Path(path).expanduser()
    try:
        mode = target.stat().st_mode
        if target.is_symlink() or not stat.S_ISREG(mode) or mode & 0o077:
            raise JgError("contribution checkpoint must be a private regular file")
        data = target.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise JgError("contribution checkpoint is unreadable") from exc
    lines = data.splitlines(keepends=True)
    if not lines or not lines[0].endswith(b"\n"):
        raise JgError("contribution checkpoint header is incomplete")
    try:
        header = json.loads(lines[0])
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise JgError("contribution checkpoint header is malformed") from exc
    if (not isinstance(header, dict) or header.get("kind") != "contribution-checkpoint"
            or header.get("schema_version") != 1 or header.get("key") != key):
        raise JgError("contribution checkpoint belongs to a different snapshot or extractor")
    branches: dict[str, dict[str, Any]] = {}
    for index, line in enumerate(lines[1:], start=1):
        # A crash during the final append may leave one partial record. It has
        # no commit newline and is safely ignored; complete records are checked.
        if index == len(lines) - 1 and not line.endswith(b"\n"):
            break
        try:
            event = json.loads(line)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise JgError("contribution checkpoint record is malformed") from exc
        name = event.get("branch") if isinstance(event, dict) else None
        record = event.get("result") if isinstance(event, dict) else None
        recorded = event.get("record_digest") if isinstance(event, dict) else None
        unsigned = {field: value for field, value in event.items() if field != "record_digest"} if isinstance(event, dict) else {}
        if (not isinstance(event, dict) or event.get("kind") != "completed-branch"
                or not isinstance(name, str) or not isinstance(record, dict)
                or not isinstance(recorded, str) or digest(unsigned) != recorded or name in branches):
            raise JgError("contribution checkpoint record is invalid")
        branches[name] = record
    return branches


def save_checkpoint(path: str | Path, key: str, branch: str, result: dict[str, Any]) -> None:
    """Durably append one completed branch without rewriting prior results.

    The caller loads the complete journal once before scheduling work and only
    appends names absent from that loaded map, avoiding quadratic checkpoint I/O.
    """
    requested = Path(path).expanduser()
    if os.path.lexists(requested) and requested.is_symlink():
        raise JgError("contribution checkpoint must not be a symlink")
    target = requested.resolve(strict=False)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.parent.stat().st_mode & 0o077:
        raise JgError("contribution checkpoint directory must be owner-only")
    if target.exists():
        try:
            with target.open("rb") as stream:
                header = json.loads(stream.readline())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise JgError("contribution checkpoint header is malformed") from exc
        if (not isinstance(header, dict) or header.get("kind") != "contribution-checkpoint"
                or header.get("schema_version") != 1 or header.get("key") != key):
            raise JgError("contribution checkpoint belongs to a different snapshot or extractor")
        with target.open("r+b") as stream:
            data = stream.read()
            if data and not data.endswith(b"\n"):
                last_complete = data.rfind(b"\n") + 1
                stream.truncate(last_complete)
                stream.flush()
                os.fsync(stream.fileno())
    else:
        header = json.dumps({"kind": "contribution-checkpoint", "schema_version": 1, "key": key},
                            sort_keys=True, separators=(",", ":")).encode() + b"\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(target, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(header)
            stream.flush()
            os.fsync(stream.fileno())
    payload = {"kind": "completed-branch", "branch": branch, "result": result}
    payload["record_digest"] = digest(payload)
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    fd = os.open(target, os.O_WRONLY | os.O_APPEND)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("checkpoint append did not make progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


class BlobAnalysisCache:
    """Thread-safe per-build blob and parsed-AST memoization."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._blobs: dict[str, bytes] = {}
        self._analyses: dict[tuple[str, str], Any] = {}
        self._blob_locks: dict[str, RLock] = {}
        self._analysis_locks: dict[tuple[str, str], RLock] = {}

    def blob(self, blob_id: str, loader) -> bytes:
        with self._lock:
            lock = self._blob_locks.setdefault(blob_id, RLock())
        with lock:
            with self._lock:
                if blob_id in self._blobs:
                    return self._blobs[blob_id]
            value = loader()
            with self._lock:
                self._blobs[blob_id] = value
            return self._blobs[blob_id]

    def analysis(self, blob_id: str, extractor_version: str, loader, parser):
        key = (blob_id, extractor_version)
        with self._lock:
            lock = self._analysis_locks.setdefault(key, RLock())
        with lock:
            with self._lock:
                if key in self._analyses:
                    return self._analyses[key]
            value = parser(self.blob(blob_id, loader))
            with self._lock:
                self._analyses[key] = value
            return self._analyses[key]
