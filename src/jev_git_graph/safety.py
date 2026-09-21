from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .errors import JgError


def resolve_existing(path: str | Path, label: str) -> Path:
    candidate = Path(path).expanduser().resolve(strict=False)
    if not candidate.exists():
        raise JgError(f"{label} does not exist: {candidate}")
    return candidate.resolve()


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_output_path(output: str | Path, protected_paths: list[Path]) -> Path:
    """Reject output that would dirty an inspected worktree.

    resolve(strict=False) follows existing symlinks and collapses `..`, which
    prevents a path that merely looks external from landing inside a worktree.
    """
    destination = Path(output).expanduser().resolve(strict=False)
    for protected in protected_paths:
        if is_within(destination, protected.resolve()):
            raise JgError(
                "output directory must be outside every inspected worktree; "
                f"{destination} is inside {protected}"
            )
    return destination


def write_json(path: Path, value: Any) -> None:
    write_private_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_private_text(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise JgError(f"artifact directory must be owner-only: {path.parent}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)


def read_json(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        parsed = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise JgError(f"artifact does not exist: {source}") from exc
    except json.JSONDecodeError as exc:
        raise JgError(f"artifact is not valid JSON: {source}") from exc
    if not isinstance(parsed, dict):
        raise JgError(f"artifact must contain a JSON object: {source}")
    return parsed


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def opaque_path_id(path: Path) -> str:
    """A stable local identifier that does not disclose the source path."""
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]
