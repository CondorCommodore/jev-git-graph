"""Metadata-only inventory of changes represented by a pinned snapshot."""

from __future__ import annotations

import ast
import builtins
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .errors import JgError
from .safety import digest, write_json
from .analysis_cache import BlobAnalysisCache, checkpoint_key, load_checkpoint, save_checkpoint


CONTRIBUTIONS_SCHEMA_VERSION = 2
EXTRACTOR_VERSION = "python-ast-references-v2"
_OID = re.compile(r"\A[0-9a-f]{40,64}\Z")
_PROCESS_CONTEXT: tuple[Path, str, dict[str, list[str]], BlobAnalysisCache] | None = None


def _git(repo: Path, *args: str) -> bytes:
    from .snapshot import run_snapshot_git

    return run_snapshot_git(repo, *args)


def _oid(value: Any) -> str:
    if not isinstance(value, str) or not _OID.fullmatch(value.lower()):
        raise JgError("snapshot contains an invalid object ID")
    return value.lower()


def _tree_entry(repo: Path, treeish: str, path: str) -> dict[str, str] | None:
    raw = _git(repo, "ls-tree", "-z", treeish, "--", f":(literal){path}")
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
    if not path.endswith(".py") or entry is None or entry["kind"] != "blob" or entry["mode"] not in {"100644", "100755"} or raw is None:
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
    return _definitions_from_tree(ast.parse(text))


def _definitions_from_tree(tree: ast.Module) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    module_imports = []
    module_bindings: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for item in statement.names:
                binding = item.asname or item.name.split(".", 1)[0]
                module_imports.append({"kind": "module", "module": item.name,
                                       "symbol": None,
                                       "binding": binding})
                module_bindings.add(binding)
        elif isinstance(statement, ast.ImportFrom):
            for item in statement.names:
                binding = item.asname or item.name
                module_imports.append({"kind": "from", "module": statement.module or "",
                                       "level": statement.level, "symbol": item.name,
                                       "binding": binding})
                module_bindings.add(binding)
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_bindings.add(statement.name)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                module_bindings.update(child.id for child in ast.walk(target)
                                       if isinstance(child, ast.Name))

    def visit(body: Iterable[ast.stmt], parents: tuple[str, ...] = ()) -> None:
        for node in body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            name = ".".join((*parents, node.name))
            decorators = getattr(node, "decorator_list", ())
            start = min([node.lineno, *(d.lineno for d in decorators)])
            references, dynamic = _static_references(node)
            result.append({
                "name": name,
                "range": {"start_line": start, "end_line": node.end_lineno or node.lineno},
                "ast_fingerprint": _fingerprint(node),
                "static_references": references,
                "dynamic_reference_observations": dynamic,
                "module_imports": module_imports,
                "module_bindings": sorted(module_bindings),
            })
            visit(node.body, (*parents, node.name))

    visit(tree.body)
    return result


def _static_references(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> tuple[list[str], list[str]]:
    """Extract bounded syntactic references without claiming full resolution.

    Only same-module names can later become dependency edges. Attribute and
    computed calls remain explicit unresolved observations; this deliberately
    avoids treating absent edges as proof that a contribution is dependency-free.
    """
    bound: set[str] = set()
    references: set[str] = set()
    dynamic: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, child):
            if child is node:
                for item in child.body:
                    self.visit(item)
            else:
                bound.add(child.name)
                dynamic.add("nested_scope_unanalyzed")

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, child):
            if child is node:
                for item in child.body:
                    self.visit(item)
            else:
                bound.add(child.name)
                dynamic.add("nested_scope_unanalyzed")

        def visit_Lambda(self, child):
            dynamic.add("nested_scope_unanalyzed")

        def visit_ListComp(self, child):
            dynamic.add("comprehension_scope_unanalyzed")

        visit_SetComp = visit_ListComp
        visit_DictComp = visit_ListComp
        visit_GeneratorExp = visit_ListComp

        def visit_arg(self, child):
            bound.add(child.arg)

        def visit_Name(self, child):
            if isinstance(child.ctx, ast.Load):
                references.add(child.id)
            elif isinstance(child.ctx, (ast.Store, ast.Del)):
                bound.add(child.id)

        def visit_Attribute(self, child):
            dynamic.add("attribute_reference")
            self.generic_visit(child)

        def visit_Call(self, child):
            func = child.func
            if isinstance(func, ast.Attribute):
                dynamic.add("attribute_call_unresolved")
            elif isinstance(func, (ast.Subscript, ast.Call, ast.Lambda)):
                dynamic.add("computed_callable")
            elif isinstance(func, ast.Name) and func.id in {
                "eval", "exec", "globals", "locals", "vars", "getattr",
                "setattr", "delattr", "__import__", "super",
            }:
                dynamic.add(f"dynamic_builtin_call:{func.id}")
            self.generic_visit(child)

        def visit_Import(self, child):
            dynamic.add("function_local_import")

        def visit_ImportFrom(self, child):
            dynamic.add("function_local_import")

    visitor = Visitor()
    expressions = list(node.decorator_list)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        expressions.extend(node.args.defaults)
        expressions.extend(item for item in node.args.kw_defaults if item is not None)
        expressions.extend(item.annotation for item in
                           node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                           if item.annotation is not None)
        expressions.append(node.returns)
    else:
        expressions.extend(node.bases)
        expressions.extend(keyword.value for keyword in node.keywords)
    for expression in expressions:
        if expression is not None:
            visitor.visit(expression)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for argument in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
            bound.add(argument.arg)
        if node.args.vararg:
            bound.add(node.args.vararg.arg)
        if node.args.kwarg:
            bound.add(node.args.kwarg.arg)
    for item in node.body:
        visitor.visit(item)
    references.difference_update(bound)
    return sorted(references), sorted(dynamic)


def _identity(prefix: str, value: dict[str, Any]) -> str:
    return prefix + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _destination_index(repo: Path, main_tip: str, limitations: list[str], cache: BlobAnalysisCache,
                       max_blobs: int | None = None) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
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
    if max_blobs is not None and len(rows) > max_blobs:
        limitations.append(f"destination_index_limit:{max_blobs}")
        rows = rows[:max_blobs]
    units: list[dict[str, Any]] = []
    by_fingerprint: dict[str, list[str]] = {}
    for path, blob_id, mode in rows:
        defs, _module_fp = _cached_python(cache, repo, blob_id)
        if defs is None:
            limitations.append(f"destination_python_unparsed:{path}")
            continue
        for definition in defs:
            uid = _identity("du-", {"blob": blob_id, "path": path, "name": definition["name"], "range": definition["range"], "extractor": EXTRACTOR_VERSION})
            record = {"id": uid, "path": path, "blob": blob_id, "mode": mode, "kind": "python_definition", **definition}
            units.append(record)
            by_fingerprint.setdefault(definition["ast_fingerprint"], []).append(uid)
    return units, by_fingerprint


def _cached_python(cache: BlobAnalysisCache, repo: Path, blob_id: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    def parse(raw: bytes):
        try:
            text = raw.decode("utf-8", "strict")
            if "\0" in text:
                raise UnicodeError
            tree = ast.parse(text)
            definitions = _definitions_from_tree(tree)
            tree.body = [node for node in tree.body if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            return definitions, _fingerprint(tree)
        except (UnicodeError, SyntaxError, ValueError):
            return None, None
    return cache.analysis(blob_id, EXTRACTOR_VERSION, lambda: _git(repo, "cat-file", "blob", blob_id), parse)


def _analyze_branch(repo: Path, branch: dict[str, Any], main_tip: str,
                    destination_by_fingerprint: dict[str, list[str]], cache: BlobAnalysisCache) -> dict[str, Any]:
    name = branch.get("name")
    if not isinstance(name, str) or not name:
        raise JgError("snapshot branch name is invalid")
    raw_tip = branch.get("tip")
    tip = raw_tip.lower() if isinstance(raw_tip, str) else None
    base_value = branch.get("merge_base")
    eligibility = bool(branch.get("eligible"))
    output_branch = {"name": name, "tip": tip, "merge_base": base_value, "eligible": eligibility,
                     "exclusion_reasons": list(branch.get("exclusion_reasons") or []),
                     "analysis_status": "excluded" if not eligibility else "pending", "path_count": 0, "unit_ids": []}
    paths: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    limitations: list[str] = []
    if not eligibility:
        return {"branch": output_branch, "paths": paths, "units": units, "edges": edges, "limitations": limitations}
    try:
        tip = _oid(raw_tip)
    except JgError:
        output_branch["analysis_status"] = "unavailable"
        limitations.append(f"source_tip_unavailable:{name}")
        return {"branch": output_branch, "paths": paths, "units": units, "edges": edges, "limitations": limitations}
    try:
        if not base_value:
            raise JgError("missing merge base")
        base = _oid(base_value)
    except JgError:
        output_branch["analysis_status"] = "unavailable"
        limitations.append(f"source_merge_base_missing:{name}")
        return {"branch": output_branch, "paths": paths, "units": units, "edges": edges, "limitations": limitations}
    output_branch["tip"] = tip
    output_branch["merge_base"] = base
    try:
        changed = _changed_paths(repo, base, tip)
    except JgError:
        output_branch["analysis_status"] = "unavailable"
        limitations.append(f"source_diff_unavailable:{name}")
        return {"branch": output_branch, "paths": paths, "units": units, "edges": edges, "limitations": limitations}
    output_branch["analysis_status"] = "complete"
    for path in changed:
        base_entry = _tree_entry(repo, base, path)
        source_entry = _tree_entry(repo, tip, path)
        main_entry = _tree_entry(repo, main_tip, path)
        paths.append({"branch": name, "source_tip": tip, "path": path, "base_entry": base_entry,
                      "source_entry": source_entry, "main_entry": main_entry, "exact": source_entry == main_entry})
        output_branch["path_count"] += 1
        source_bytes = cache.blob(source_entry["blob"], lambda: _blob(repo, source_entry)) if source_entry and source_entry["kind"] == "blob" else None
        base_bytes = cache.blob(base_entry["blob"], lambda: _blob(repo, base_entry)) if base_entry and base_entry["kind"] == "blob" else None
        source_python = _is_text_python(path, source_entry, source_bytes)
        base_python = _is_text_python(path, base_entry, base_bytes)
        created: list[dict[str, Any]] = []
        fallback_reason: str | None = None
        if not source_python:
            fallback_reason = "deleted" if source_entry is None else ("unsupported_kind" if source_entry["kind"] != "blob" or source_entry["mode"] not in {"100644", "100755"} else ("binary_or_non_utf8" if path.endswith(".py") else "non_python"))
        elif source_entry and base_entry and source_entry["mode"] != base_entry["mode"]:
            fallback_reason = "mode_change"
        else:
            src_defs, src_module_fingerprint = _cached_python(cache, repo, source_entry["blob"])
            if base_python and base_entry is not None:
                base_defs, base_module_fingerprint = _cached_python(cache, repo, base_entry["blob"])
            else:
                base_defs, base_module_fingerprint = [], None
            if src_defs is None or (base_python and base_defs is None):
                fallback_reason = "python_parse_unsupported"
            else:
                before = Counter((item["name"], item["ast_fingerprint"]) for item in base_defs)
                changed_defs = []
                for definition in src_defs:
                    key = (definition["name"], definition["ast_fingerprint"])
                    if before[key]:
                        before[key] -= 1
                    else:
                        changed_defs.append(definition)
                for definition in changed_defs:
                    candidates = destination_by_fingerprint.get(definition["ast_fingerprint"], [])
                    identity = {"branch": name, "tip": tip, "path": path, "blob": source_entry["blob"],
                                "range": definition["range"], "name": definition["name"], "extractor": EXTRACTOR_VERSION}
                    unit_limitations = ["binding_resolution_unverified"]
                    if len(candidates) > 1:
                        unit_limitations.append("ambiguous_destination_match")
                    created.append({"id": _identity("cu-", identity), "branch": name, "source_tip": tip,
                                    "main_tip": main_tip, "path": path, "source_blob": source_entry["blob"],
                                    "main_blob": main_entry["blob"] if main_entry and main_entry["kind"] == "blob" else None,
                                    "mode": source_entry["mode"], "kind": "python_definition", "name": definition["name"],
                                    "range": definition["range"], "ast_fingerprint": definition["ast_fingerprint"],
                                    "static_references": definition.get("static_references", []),
                                    "dynamic_reference_observations": definition.get("dynamic_reference_observations", []),
                                    "module_imports": definition.get("module_imports", []),
                                    "module_bindings": definition.get("module_bindings", []),
                                    "dependency_context_status": "unknown",
                                    "dependency_context_limitations": ["module_imports_and_dynamic_resolution_not_exhaustive"],
                                    "destination_ids": list(candidates), "limitations": unit_limitations,
                                    "source": {"branch": name, "path": path, "kind": "python_definition",
                                               "name": definition["name"], "blob": source_entry["blob"],
                                               "ast_fingerprint": definition["ast_fingerprint"]}})
                    limitations.append("binding_resolution_unverified")
                    if len(candidates) > 1:
                        limitations.append("ambiguous_destination_match")
                source_names = {item["name"] for item in src_defs}
                module_changed = base_module_fingerprint is None or src_module_fingerprint != base_module_fingerprint
                if any(item["name"] not in source_names for item in base_defs):
                    fallback_reason = "definition_removal"
                elif module_changed:
                    fallback_reason = "module_change"
                elif not changed_defs and source_bytes != base_bytes:
                    fallback_reason = "non_definition_change"
        if fallback_reason:
            identity = {"branch": name, "tip": tip, "path": path,
                        "blob": source_entry["blob"] if source_entry else None,
                        "range": None, "name": None, "extractor": EXTRACTOR_VERSION}
            created.append({"id": _identity("cu-", identity), "branch": name, "source_tip": tip,
                            "main_tip": main_tip, "path": path,
                            "source_blob": source_entry["blob"] if source_entry else None,
                            "main_blob": main_entry["blob"] if main_entry and main_entry["kind"] == "blob" else None,
                            "mode": source_entry["mode"] if source_entry else None, "kind": "file", "name": None,
                            "range": None, "destination_ids": [], "limitations": [fallback_reason],
                            "static_references": [], "dynamic_reference_observations": [],
                            "dependency_context_status": "unknown",
                            "dependency_context_limitations": ["file_level_dependencies_not_extracted"],
                            "source": {"branch": name, "path": path, "kind": "file", "name": None,
                                       "blob": source_entry["blob"] if source_entry else None,
                                       "ast_fingerprint": None}})
            if fallback_reason == "python_parse_unsupported":
                limitations.append(f"source_python_unparsed:{name}:{path}")
        for unit in created:
            units.append(unit)
            output_branch["unit_ids"].append(unit["id"])
            for destination_id in unit["destination_ids"]:
                edges.append({"source_id": unit["id"], "destination_id": destination_id,
                              "type": "structural_match", "provenance": "ast_fingerprint"})
    return {"branch": output_branch, "paths": paths, "units": units, "edges": edges,
            "limitations": limitations}


def _init_process_worker(object_repo: str, main_tip: str, destination_by_fingerprint: dict[str, list[str]]) -> None:
    """Install read-only snapshot context once in each CPU worker process."""
    global _PROCESS_CONTEXT
    _PROCESS_CONTEXT = (Path(object_repo), main_tip, destination_by_fingerprint, BlobAnalysisCache())


def _analyze_branch_in_process(branch: dict[str, Any]) -> dict[str, Any]:
    if _PROCESS_CONTEXT is None:
        raise JgError("contribution worker was not initialized")
    repo, main_tip, destination_by_fingerprint, cache = _PROCESS_CONTEXT
    return _analyze_branch(repo, branch, main_tip, destination_by_fingerprint, cache)


def build_contributions(snapshot: dict, object_repo: Path, *, workers: int | None = None,
                       progress=None, checkpoint_path: str | Path | None = None,
                       max_destination_blobs: int | None = None) -> dict:
    """Build a deterministic source-to-main contribution inventory.

    Git is read with object plumbing only. No source text or AST body is returned.
    """
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "git-snapshot" or snapshot.get("schema_version") != 1:
        raise JgError("unsupported Git snapshot")
    if workers is None:
        workers = min(8, max(1, os.cpu_count() or 1))
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise JgError("workers must be a positive integer")
    if max_destination_blobs is not None and (
        not isinstance(max_destination_blobs, int) or isinstance(max_destination_blobs, bool) or max_destination_blobs < 1
    ):
        raise JgError("max_destination_blobs must be a positive integer or None")
    repo = Path(object_repo).expanduser().resolve()
    main = snapshot.get("main")
    if not isinstance(main, dict):
        raise JgError("snapshot main pin is missing")
    main_tip = _oid(main.get("tip"))
    branches = snapshot.get("branches")
    if not isinstance(branches, list):
        raise JgError("snapshot branches are invalid")
    if any(not isinstance(branch, dict) for branch in branches):
        raise JgError("snapshot branch record is invalid")
    names = [branch.get("name") for branch in branches]
    if any(not isinstance(name, str) or not name for name in names) or len(names) != len(set(names)):
        raise JgError("snapshot branch names must be non-empty and unique")
    limitations: list[str] = []
    cache = BlobAnalysisCache()
    key = checkpoint_key(snapshot, EXTRACTOR_VERSION)
    completed = load_checkpoint(checkpoint_path, key) if checkpoint_path is not None else {}
    branch_results: dict[str, dict[str, Any]] = {}
    for branch in branches:
        if branch["name"] in completed:
            branch_results[branch["name"]] = completed[branch["name"]]
    pending = [branch for branch in branches if branch["name"] not in branch_results]
    if any(branch.get("eligible") for branch in branches):
        destinations, destination_by_fingerprint = _destination_index(
            repo, main_tip, limitations, cache, max_blobs=max_destination_blobs)
    else:
        destinations, destination_by_fingerprint = [], {}
    total = len(branches)
    done = len(branch_results)
    if progress is not None:
        for index, name in enumerate(sorted(branch_results), start=1):
            progress(index, total, name)
    if workers == 1:
        for branch in pending:
            result = _analyze_branch(repo, branch, main_tip, destination_by_fingerprint, cache)
            branch_results[branch["name"]] = result
            done += 1
            if checkpoint_path is not None:
                save_checkpoint(checkpoint_path, key, branch["name"], result)
            if progress is not None:
                progress(done, total, branch["name"])
    elif pending:
        with ProcessPoolExecutor(
            max_workers=min(workers, len(pending)),
            initializer=_init_process_worker,
            initargs=(str(repo), main_tip, destination_by_fingerprint),
        ) as pool:
            futures = {pool.submit(_analyze_branch_in_process, branch): branch for branch in pending}
            for future in as_completed(futures):
                result = future.result()
                branch_results[result["branch"]["name"]] = result
                done += 1
                if checkpoint_path is not None:
                    save_checkpoint(checkpoint_path, key, result["branch"]["name"], result)
                if progress is not None:
                    progress(done, total, result["branch"]["name"])
    output_branches: list[dict[str, Any]] = []
    paths: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for branch in branches:
        result = branch_results[branch["name"]]
        output_branches.append(result["branch"])
        paths.extend(result["paths"])
        units.extend(result["units"])
        edges.extend(result["edges"])
        limitations.extend(result["limitations"])
    # Resolve only direct same-module name references to a unique immutable
    # source/destination definition. These are syntactic dependency candidates,
    # not proof of behavioral necessity; every unit retains UNKNOWN dependency
    # completeness because imports, attributes, reflection and dynamic calls
    # are not exhaustively modeled by this extractor.
    source_symbols: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    destination_symbols: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for unit in units:
        if unit.get("kind") != "python_definition" or not isinstance(unit.get("name"), str):
            continue
        leaf = unit["name"].rsplit(".", 1)[-1]
        source_symbols.setdefault((unit["branch"], unit["path"], leaf), []).append(unit)
    for unit in destinations:
        if unit.get("kind") == "python_definition" and isinstance(unit.get("name"), str):
            leaf = unit["name"].rsplit(".", 1)[-1]
            destination_symbols.setdefault((unit["path"], leaf), []).append(unit)

    def imported_module_paths(unit: dict[str, Any], record: dict[str, Any]) -> list[str]:
        module = record.get("module", "")
        if not isinstance(module, str):
            return []
        module_parts = [part for part in module.split(".") if part]
        path_parts = Path(unit["path"]).parts[:-1]
        level = record.get("level", 0)
        if isinstance(level, int) and level > 0:
            prefix = path_parts[:max(0, len(path_parts) - level + 1)]
            stem = "/".join((*prefix, *module_parts))
            candidates = [f"{stem}.py", f"{stem}/__init__.py"]
        else:
            stem = "/".join(module_parts)
            candidates = [f"{stem}.py", f"{stem}/__init__.py",
                          f"src/{stem}.py", f"src/{stem}/__init__.py"]
        return candidates

    def reference_targets(unit: dict[str, Any], reference: str) -> list[dict[str, Any]]:
        imports = [item for item in unit.get("module_imports", [])
                   if item.get("binding") == reference]
        if imports:
            targets = []
            unresolved_import = False
            for imported in imports:
                if imported.get("kind") != "from" or imported.get("symbol") == "*":
                    unresolved_import = True
                    continue
                for path in imported_module_paths(unit, imported):
                    branch = unit.get("branch")
                    if branch:
                        targets.extend(source_symbols.get((branch, path, imported["symbol"]), []))
                    if not targets:
                        targets.extend(destination_symbols.get((path, imported["symbol"]), []))
            if targets:
                unique = {item["id"]: item for item in targets}
                return list(unique.values())
            if unresolved_import or imports:
                return []
        branch = unit.get("branch")
        candidates = ([candidate for candidate in source_symbols.get(
            (branch, unit["path"], reference), []) if candidate["id"] != unit["id"]]
            if branch else [])
        if len(candidates) != 1:
            candidates = destination_symbols.get((unit["path"], reference), []) if not candidates else candidates
        return candidates

    resolved_dependencies = 0
    unresolved_reference_observations = 0
    def analyze_static_dependencies(unit: dict[str, Any], *, emit_edges: bool) -> tuple[str, list[str], int]:
        edge_count_before = len(edges)
        unresolved = []
        resolved_reference_count = 0
        for reference in unit.get("static_references", []):
            if reference == unit.get("name", "").rsplit(".", 1)[-1]:
                resolved_reference_count += 1
                continue
            candidates = reference_targets(unit, reference)
            if len(candidates) == 1:
                target = candidates[0]
                if emit_edges and target["id"] != unit["id"]:
                    edges.append({"source_id": unit["id"], "destination_id": target["id"],
                                  "type": "dependency", "provenance": "static_ast_symbol_reference",
                                  "reference_name": reference})
                resolved_reference_count += 1
            else:
                imported_binding = any(item.get("binding") == reference
                                       for item in unit.get("module_imports", []))
                if reference in dir(builtins) and not imported_binding and reference not in unit.get("module_bindings", []):
                    resolved_reference_count += 1
                else:
                    unresolved.append(reference)
        dynamic = unit.get("dynamic_reference_observations", [])
        dependency_status = "complete" if not unresolved and not dynamic else "unknown"
        unit["dependency_observations"] = {
            "resolved_same_module_reference_count": resolved_reference_count,
            "unresolved_reference_count": len(unresolved) + len(dynamic),
            "unresolved_reference_samples": unresolved[:12],
            "dynamic_reference_observations": dynamic,
            "status": dependency_status,
        }
        if unresolved or dynamic:
            limitations = ["unresolved_or_dynamic_references"]
        else:
            limitations = []
        return dependency_status, limitations, len(edges) - edge_count_before

    for unit in destinations:
        if unit.get("kind") == "python_definition":
            status, limitations_for_unit, _resolved = analyze_static_dependencies(unit, emit_edges=False)
            unit["dependency_context_status"] = status
            unit["dependency_context_limitations"] = limitations_for_unit
    destination_by_id = {unit["id"]: unit for unit in destinations}
    for unit in units:
        if unit.get("kind") != "python_definition":
            continue
        source_status, limitations_for_unit, resolved_count = analyze_static_dependencies(unit, emit_edges=True)
        resolved_dependencies += resolved_count
        candidates = [destination_by_id[item] for item in unit.get("destination_ids", [])
                      if item in destination_by_id]
        limitations_for_unit = list(limitations_for_unit)
        if not candidates:
            limitations_for_unit.append("destination_dependency_context_unavailable")
        elif any(item.get("dependency_context_status") != "complete" for item in candidates):
            limitations_for_unit.append("destination_dependency_context_incomplete")
        dependency_status = ("complete" if source_status == "complete" and candidates
                             and not any(item in limitations_for_unit
                                         for item in ("destination_dependency_context_unavailable",
                                                      "destination_dependency_context_incomplete"))
                             else "unknown")
        unit["source_dependency_context_status"] = source_status
        unit["dependency_context_status"] = dependency_status
        unit["dependency_context_limitations"] = sorted(set(limitations_for_unit))
        unresolved_reference_observations += unit["dependency_observations"]["unresolved_reference_count"]
    unresolved_destination_reference_observations = sum(
        unit.get("dependency_observations", {}).get("unresolved_reference_count", 0)
        for unit in destinations if unit.get("kind") == "python_definition")
    dependency_summary = {
        "kind": "static-python-reference-candidates",
        "schema_version": 1,
        "status": "partial_unknown",
        "resolved_static_candidate_edges": resolved_dependencies,
        "source_unresolved_reference_observations": unresolved_reference_observations,
        "destination_unresolved_reference_observations": unresolved_destination_reference_observations,
        "source_context_status_counts": dict(Counter(unit.get("dependency_context_status", "unknown")
                                                       for unit in units)),
        "destination_context_status_counts": dict(Counter(unit.get("dependency_context_status", "unknown")
                                                            for unit in destinations)),
        "limitations": ["cross_module_import_resolution_partial",
                        "attribute_and_reflection_resolution_not_exhaustive",
                        "local_name_binding_not_proven"],
    }
    limitations = sorted(set(limitations))
    result = {"kind": "contributions", "schema_version": CONTRIBUTIONS_SCHEMA_VERSION, "snapshot_digest": snapshot.get("snapshot_digest"), "repository_id": snapshot.get("repository_id"), "main": main, "branches": output_branches, "units": units, "destination_units": destinations, "paths": paths, "edges": edges, "dependency_extraction": dependency_summary, "limitations": limitations}
    result["contributions_digest"] = digest(result)
    return result


def write_contributions(snapshot_path: str | Path, out: str | Path, *, workers: int | None = None,
                        progress=None, checkpoint_path: str | Path | None = None,
                        max_destination_blobs: int | None = None) -> Path:
    """Load a pinned snapshot, write the derived artifact, and return its path."""
    from .snapshot import load_snapshot

    snapshot, object_repo = load_snapshot(snapshot_path)
    destination = Path(out).expanduser()
    if destination.exists() and destination.is_dir():
        destination = destination / "contributions.json"
    if os.path.lexists(destination):
        raise JgError("contributions output already exists")
    destination = destination.resolve(strict=False)
    if os.path.lexists(destination):
        raise JgError("contributions output already exists")
    result = build_contributions(snapshot, Path(object_repo), workers=workers, progress=progress,
                                 checkpoint_path=checkpoint_path,
                                 max_destination_blobs=max_destination_blobs)
    write_json(destination, result)
    return destination
