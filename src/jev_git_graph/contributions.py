"""Metadata-only inventory of changes represented by a pinned snapshot."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .errors import JgError
from .safety import digest, write_json


CONTRIBUTIONS_SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "python-ast-v1"
MAX_DESTINATION_BLOBS = 5000
_OID = re.compile(r"\A[0-9a-f]{40,64}\Z")


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(repo), *args), input=input_bytes,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise JgError("unable to inspect pinned contribution objects")
    return result.stdout


def _oid(value: Any) -> str:
    if not isinstance(value, str) or not _OID.fullmatch(value.lower()):
        raise JgError("snapshot contains an invalid object ID")
    return value.lower()


def _tree_entry(repo: Path, treeish: str, path: str) -> dict[str, str] | None:
    raw = _git(repo, "ls-tree", "-z", treeish, "--", path)
    if not raw:
        return None
    record = raw.rstrip(b"\0").split(b"\t", 1)
    if len(record) != 2:
        return None
    header, actual_path = record
    mode, kind, oid = header.decode("ascii").split(" ")
    if actual_path.decode("utf-8", "surrogateescape") != path:
        return None
    return {"mode": mode, "blob": oid.lower(), "kind": kind}


def _changed_paths(repo: Path, base: str, tip: str) -> list[str]:
    raw = _git(repo, "diff", "--no-renames", "--name-only", "-z", base, tip, "--")
    return sorted({p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p})


def _blob(repo: Path, entry: dict[str, str] | None) -> bytes | None:
    if entry is None or entry["kind"] != "blob":
        return None
    return _git(repo, "cat-file", "blob", entry["blob"])


def _is_text_python(path: str, entry: dict[str, str] | None, raw: bytes | None) -> bool:
    if not path.endswith(".py") or entry is None or entry["kind"] != "blob" or raw is None:
        return False
    if b"\0" in raw:
        return False
    try:
        raw.decode("utf-8", "strict")
        return True
    except UnicodeDecodeError:
        return False


def _fingerprint(node: ast.AST) -> str:
    return hashlib.sha256(ast.dump(node, annotate_fields=True, include_attributes=False).encode()).hexdigest()


def _definitions(text: str) -> list[dict[str, Any]]:
    tree = ast.parse(text)
    result: list[dict[str, Any]] = []

    def visit(body: Iterable[ast.stmt], parents: tuple[str, ...] = ()) -> None:
        for node in body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            name = ".".join((*parents, node.name))
            decorators = getattr(node, "decorator_list", ())
            start = min([node.lineno, *(d.lineno for d in decorators)])
            result.append({
                "name": name,
                "range": {"start_line": start, "end_line": node.end_lineno or node.lineno},
                "ast_fingerprint": _fingerprint(node),
            })
            visit(node.body, (*parents, node.name))

    visit(tree.body)
    return result


def _module_fingerprint(text: str) -> str:
    tree = ast.parse(text)
    tree.body = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    return _fingerprint(tree)


def _identity(prefix: str, value: dict[str, Any]) -> str:
    return prefix + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _destination_index(repo: Path, main_tip: str, limitations: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    listing = _git(repo, "ls-tree", "-r", "-z", "--full-tree", main_tip)
    rows: list[tuple[str, str, str]] = []
    for item in listing.split(b"\0"):
        if not item:
            continue
        header, path_bytes = item.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split(" ")
        path = path_bytes.decode("utf-8", "surrogateescape")
        if path.endswith(".py") and kind == "blob" and mode in {"100644", "100755"}:
            rows.append((path, oid.lower(), mode))
    if len(rows) > MAX_DESTINATION_BLOBS:
        limitations.append({"code": "destination_index_limit", "detail": f"Python destination index capped at {MAX_DESTINATION_BLOBS} blobs"})
        rows = rows[:MAX_DESTINATION_BLOBS]
    units: list[dict[str, Any]] = []
    by_fingerprint: dict[str, list[str]] = {}
    parsed_by_blob: dict[str, list[dict[str, Any]] | None] = {}
    for path, blob_id, mode in rows:
        if blob_id not in parsed_by_blob:
            raw = _git(repo, "cat-file", "blob", blob_id)
            try:
                text = raw.decode("utf-8", "strict")
                if "\0" in text:
                    raise UnicodeError
                parsed_by_blob[blob_id] = _definitions(text)
            except (UnicodeError, SyntaxError, ValueError):
                parsed_by_blob[blob_id] = None
        defs = parsed_by_blob[blob_id]
        if defs is None:
            limitations.append({"code": "destination_python_unparsed", "detail": f"Could not index Python destination blob at {path}"})
            continue
        for definition in defs:
            uid = _identity("du-", {"blob": blob_id, "path": path, "name": definition["name"], "range": definition["range"], "extractor": EXTRACTOR_VERSION})
            record = {"id": uid, "path": path, "blob": blob_id, "mode": mode, "kind": "python_definition", **definition}
            units.append(record)
            by_fingerprint.setdefault(definition["ast_fingerprint"], []).append(uid)
    return units, by_fingerprint


def build_contributions(snapshot: dict, object_repo: Path) -> dict:
    """Build a deterministic source-to-main contribution inventory.

    Git is read with object plumbing only. No source text or AST body is returned.
    """
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "git-snapshot" or snapshot.get("schema_version") != 1:
        raise JgError("unsupported Git snapshot")
    repo = Path(object_repo).expanduser().resolve()
    main = snapshot.get("main")
    if not isinstance(main, dict):
        raise JgError("snapshot main pin is missing")
    main_tip = _oid(main.get("tip"))
    branches = snapshot.get("branches")
    if not isinstance(branches, list):
        raise JgError("snapshot branches are invalid")
    destinations, destination_by_fingerprint = _destination_index(repo, main_tip, limitations := [])
    output_branches: list[dict[str, Any]] = []
    paths: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for branch in branches:
        if not isinstance(branch, dict):
            raise JgError("snapshot branch record is invalid")
        name = branch.get("name")
        if not isinstance(name, str) or not name:
            raise JgError("snapshot branch name is invalid")
        tip = _oid(branch.get("tip"))
        base_value = branch.get("merge_base")
        base = _oid(base_value) if base_value else main_tip
        eligibility = bool(branch.get("eligible"))
        output_branches.append({"name": name, "tip": tip, "merge_base": base, "eligible": eligibility, "exclusion_reasons": list(branch.get("exclusion_reasons") or []), "path_count": 0, "unit_ids": []})
        try:
            changed = _changed_paths(repo, base, tip)
        except JgError:
            limitations.append({"code": "source_diff_unavailable", "detail": f"Cannot enumerate changed paths for {name}"})
            continue
        for path in changed:
            base_entry = _tree_entry(repo, base, path)
            source_entry = _tree_entry(repo, tip, path)
            main_entry = _tree_entry(repo, main_tip, path)
            exact = source_entry == main_entry
            path_record = {"branch": name, "source_tip": tip, "path": path, "base_entry": base_entry, "source_entry": source_entry, "main_entry": main_entry, "exact": exact}
            paths.append(path_record)
            output_branches[-1]["path_count"] += 1
            source_bytes = _blob(repo, source_entry)
            base_bytes = _blob(repo, base_entry)
            source_python = _is_text_python(path, source_entry, source_bytes)
            base_python = _is_text_python(path, base_entry, base_bytes)
            created: list[dict[str, Any]] = []
            fallback_reason: str | None = None
            if not source_python:
                fallback_reason = "deleted" if source_entry is None else ("unsupported_kind" if source_entry["kind"] != "blob" else ("binary_or_non_utf8" if path.endswith(".py") else "non_python"))
            elif source_entry and base_entry and source_entry["mode"] != base_entry["mode"]:
                fallback_reason = "mode_change"
            else:
                try:
                    src_defs = _definitions(source_bytes.decode("utf-8"))
                    base_defs = _definitions(base_bytes.decode("utf-8")) if base_python and base_bytes is not None else []
                    before = Counter((d["name"], d["ast_fingerprint"]) for d in base_defs)
                    changed_defs = []
                    for definition in src_defs:
                        key = (definition["name"], definition["ast_fingerprint"])
                        if before[key]:
                            before[key] -= 1
                        else:
                            changed_defs.append(definition)
                    for definition in changed_defs:
                        candidates = destination_by_fingerprint.get(definition["ast_fingerprint"], [])
                        identity = {"tip": tip, "path": path, "blob": source_entry["blob"], "range": definition["range"], "name": definition["name"], "extractor": EXTRACTOR_VERSION}
                        created.append({"id": _identity("cu-", identity), "branch": name, "source_tip": tip, "main_tip": main_tip, "path": path, "source_blob": source_entry["blob"], "main_blob": main_entry["blob"] if main_entry and main_entry["kind"] == "blob" else None, "mode": source_entry["mode"], "kind": "python_definition", "name": definition["name"], "range": definition["range"], "ast_fingerprint": definition["ast_fingerprint"], "destination_ids": list(candidates), "limitations": [], "source": {"branch": name, "path": path, "kind": "python_definition", "name": definition["name"], "blob": source_entry["blob"], "ast_fingerprint": definition["ast_fingerprint"]}})
                    module_changed = (not base_python) or (_module_fingerprint(source_bytes.decode("utf-8")) != _module_fingerprint(base_bytes.decode("utf-8")))
                    if module_changed:
                        fallback_reason = "module_change"
                except (UnicodeError, SyntaxError, ValueError):
                    fallback_reason = "python_parse_unsupported"
            if fallback_reason:
                identity = {"tip": tip, "path": path, "blob": source_entry["blob"] if source_entry else None, "range": None, "name": None, "extractor": EXTRACTOR_VERSION}
                created.append({"id": _identity("cu-", identity), "branch": name, "source_tip": tip, "main_tip": main_tip, "path": path, "source_blob": source_entry["blob"] if source_entry else None, "main_blob": main_entry["blob"] if main_entry and main_entry["kind"] == "blob" else None, "mode": source_entry["mode"] if source_entry else None, "kind": "file", "name": None, "range": None, "destination_ids": [], "limitations": [fallback_reason], "source": {"branch": name, "path": path, "kind": "file", "name": None, "blob": source_entry["blob"] if source_entry else None, "ast_fingerprint": None}})
                if fallback_reason == "python_parse_unsupported":
                    limitations.append({"code": "source_python_unparsed", "detail": f"Python structure unavailable for {name}:{path}"})
            for unit in created:
                units.append(unit)
                output_branches[-1]["unit_ids"].append(unit["id"])
                for destination_id in unit["destination_ids"]:
                    edges.append({"source_id": unit["id"], "destination_id": destination_id, "type": "structural_match", "provenance": "ast_fingerprint"})
    result = {"kind": "contributions", "schema_version": CONTRIBUTIONS_SCHEMA_VERSION, "snapshot_digest": snapshot.get("snapshot_digest"), "repository_id": snapshot.get("repository_id"), "main": main, "branches": output_branches, "units": units, "destination_units": destinations, "paths": paths, "edges": edges, "limitations": limitations}
    result["contributions_digest"] = digest(result)
    return result


def write_contributions(snapshot_path: str | Path, out: str | Path) -> Path:
    """Load a pinned snapshot, write the derived artifact, and return its path."""
    from .snapshot import load_snapshot

    snapshot, object_repo = load_snapshot(snapshot_path)
    destination = Path(out).expanduser().resolve(strict=False)
    if destination.exists() and destination.is_dir():
        destination = destination / "contributions.json"
    result = build_contributions(snapshot, Path(object_repo))
    write_json(destination, result)
    return destination
