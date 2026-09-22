"""Advisory residual analysis in a disposable repository with independent objects."""

from __future__ import annotations

import ast
import hashlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import git
from .errors import JgError
from .inventory import protected_worktree_paths
from .safety import validate_output_path


def _run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(("git", "-C", str(repo), *args), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if check and result.returncode:
        raise JgError("disposable Git analysis failed")
    return result


def _definitions(repo: Path, tip: str, paths: list[str]) -> list[dict[str, Any]]:
    found = []
    for path in paths:
        if not path.endswith(".py"):
            continue
        shown = _run(repo, "show", f"{tip}:{path}", check=False)
        if shown.returncode:
            continue
        try:
            tree = ast.parse(shown.stdout.decode("utf-8"))
        except (UnicodeError, SyntaxError, ValueError):
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            normalized = ast.dump(node, include_attributes=False)
            found.append({"path": path, "name": node.name, "line": node.lineno,
                          "fingerprint": hashlib.sha256(normalized.encode()).hexdigest(),
                          "static_names": sorted({part.id for part in ast.walk(node) if isinstance(part, ast.Name)})})
    return found


def analyze_residual(repo: str | Path, source_tip: str, main_tip: str,
                     changed_paths: list[str]) -> dict[str, Any]:
    """Return advisory merge and AST signals; never exact-preservation proof."""
    _root, _common, runner = git.open_repository(repo)
    for tip in (source_tip, main_tip):
        if runner.run("cat-file", "-t", tip).strip() != "commit":
            raise JgError("residual input is not a pinned commit")
    temp_parent = validate_output_path(tempfile.gettempdir(), protected_worktree_paths(repo))
    with tempfile.TemporaryDirectory(prefix="jg-residual-", dir=temp_parent) as directory:
        temporary = Path(directory)
        bundle = temporary / "snapshot.bundle"
        # Explicit tips make the bundle self-contained even for unreferenced pins.
        subprocess.run(("git", "-C", str(runner.root), "bundle", "create", str(bundle), "--all"),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        isolated = temporary / "repo"
        isolated.mkdir()
        _run(isolated, "init", "-q")
        _run(isolated, "fetch", "-q", str(bundle), "+refs/heads/*:refs/heads/*")
        for tip in (source_tip, main_tip):
            if _run(isolated, "cat-file", "-e", f"{tip}^{{commit}}", check=False).returncode:
                raise JgError("pinned commit unavailable in independent repository")
        merged = _run(isolated, "merge-tree", "--write-tree", main_tip, source_tip, check=False)
        if merged.returncode not in (0, 1):
            raise JgError("merge simulation unavailable")
        source_defs = _definitions(isolated, source_tip, changed_paths)
        main_paths = _run(isolated, "ls-tree", "-r", "--name-only", "-z", main_tip).stdout
        main_python = [name.decode("utf-8", "surrogateescape") for name in main_paths.split(b"\0")
                       if name.endswith(b".py")]
        if len(main_python) > 5000:
            main_python = []  # bounded analysis is uncertain, never proof
        main_defs = _definitions(isolated, main_tip, main_python)
        by_fingerprint = {item["fingerprint"]: item for item in main_defs}
        moved = [{"source_path": item["path"], "main_path": by_fingerprint[item["fingerprint"]]["path"],
                  "name": item["name"], "fingerprint": item["fingerprint"]}
                 for item in source_defs if item["fingerprint"] in by_fingerprint
                 and item["path"] != by_fingerprint[item["fingerprint"]]["path"]]
        references = [{"path": definition["path"], "definition": definition["name"], "name": move["name"]}
                      for move in moved for definition in main_defs
                      if definition["path"] == move["main_path"]
                      and definition["name"] != move["name"]
                      and move["name"] in definition["static_names"]]
        return {"kind": "residual-analysis", "source_tip": source_tip, "main_tip": main_tip,
                "merge_conflicts": merged.returncode == 1,
                "simulated_tree": merged.stdout.decode("ascii", "replace").splitlines()[0] if merged.returncode == 0 else None,
                "moved_definitions": moved, "possible_static_references": references,
                "dynamic_references": "UNKNOWN",
                "other_file_types": "UNKNOWN", "exact_proof": False,
                "repository_isolated": True, "network_performed": False}
