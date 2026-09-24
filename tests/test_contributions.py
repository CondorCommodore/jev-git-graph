from __future__ import annotations

import os
import subprocess
import tempfile
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path

from jev_git_graph.contributions import build_contributions as _build_contributions, write_contributions
from jev_git_graph.contributions import _definitions
from jev_git_graph.errors import JgError
from jev_git_graph.snapshot import export_pinned_repository, _git_env


def build_contributions(snapshot, repo, **kwargs):
    """Use the production independent object-store contract in every fixture."""
    pins = [snapshot['main']['tip']]
    for branch in snapshot['branches']:
        if branch.get('eligible'):
            pins.append(branch['tip'])
            if branch.get('merge_base'):
                pins.append(branch['merge_base'])
    with tempfile.TemporaryDirectory(dir='/private/tmp') as temporary:
        store = Path(temporary) / 'objects.git'
        export_pinned_repository(repo, pins, store)
        return _build_contributions(snapshot, store, **kwargs)


def git(repo: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *args), input=input_text,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return result.stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def test_inventory_accounts_for_changed_files_and_matches_all_destinations(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src.py").write_text("def changed():\n    return 1\n", encoding="utf-8")
    (repo / "imports.py").write_text("import os\n", encoding="utf-8")
    (repo / "deleted.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "mode.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "data.bin").write_bytes(b"old\x00bytes")
    (repo / "link.py").symlink_to("src.py")
    base = commit(repo, "base")

    git(repo, "checkout", "-q", "-b", "source")
    (repo / "src.py").write_text("def changed():\n    return 9\n", encoding="utf-8")
    (repo / "imports.py").write_text("import os\nimport sys\n", encoding="utf-8")
    (repo / "deleted.py").unlink()
    os.chmod(repo / "mode.py", 0o755)
    (repo / "data.bin").write_bytes(b"new\x00bytes")
    (repo / "link.py").unlink()
    (repo / "link.py").symlink_to("imports.py")
    source_tip = commit(repo, "source changes")

    git(repo, "checkout", "-q", "main")
    (repo / "dest_a.py").write_text("def changed():\n    return 9\n", encoding="utf-8")
    (repo / "dest_b.py").write_text("def changed():\n    return 9\n", encoding="utf-8")
    main_tip = commit(repo, "destinations")
    snapshot = {
        "kind": "git-snapshot", "schema_version": 1,
        "snapshot_digest": "snapshot-hash", "repository_id": "repo-id",
        "main": {"name": "main", "tip": main_tip, "tree": git(repo, "rev-parse", f"{main_tip}^{{tree}}")},
        "branches": [{"name": "source", "tip": source_tip, "merge_base": base, "eligible": True, "exclusion_reasons": []}],
    }
    before = git(repo, "status", "--porcelain=v1")
    result = build_contributions(snapshot, repo)
    after = git(repo, "status", "--porcelain=v1")

    assert before == after == ""
    expected_paths = {"src.py", "imports.py", "deleted.py", "mode.py", "data.bin", "link.py"}
    assert {item["path"] for item in result["paths"]} == expected_paths
    assert all(any(unit["path"] == path for unit in result["units"]) for path in expected_paths)
    changed = next(unit for unit in result["units"] if unit["path"] == "src.py" and unit["kind"] == "python_definition")
    assert changed["destination_ids"] and len(changed["destination_ids"]) == 3
    assert set(changed["destination_candidate_provenance"].values()) == {"same_path_name", "ast_fingerprint"}
    assert "binding_resolution_unverified" in changed["limitations"]
    assert "ambiguous_destination_match" in changed["limitations"]
    assert all(edge["source_id"] == changed["id"] for edge in result["edges"])
    assert {unit["limitations"][0] for unit in result["units"] if unit["kind"] == "file"} >= {
        "module_change", "deleted", "mode_change", "non_python", "unsupported_kind"
    }
    assert result["branches"][0]["eligible"] is True
    assert result["contributions_digest"]
    assert "return 9" not in repr(result)


def test_ast_ambiguity_keeps_every_candidate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    base = commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "source")
    (repo / "src.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    source_tip = commit(repo, "source")
    git(repo, "checkout", "-q", "main")
    body = "def f():\n    return 2\n"
    (repo / "one.py").write_text(body, encoding="utf-8")
    (repo / "two.py").write_text(body, encoding="utf-8")
    main_tip = commit(repo, "destinations")
    snapshot = {"kind": "git-snapshot", "schema_version": 1, "snapshot_digest": "s", "repository_id": "r", "main": {"name": "main", "tip": main_tip, "tree": git(repo, "rev-parse", f"{main_tip}^{{tree}}")}, "branches": [{"name": "source", "tip": source_tip, "merge_base": base, "eligible": True, "exclusion_reasons": []}]}
    result = build_contributions(snapshot, repo)
    unit = next(unit for unit in result["units"] if unit["kind"] == "python_definition")
    assert len(unit["destination_ids"]) == 3
    assert unit["source_dependency_context_status"] == "complete"
    assert unit["dependency_context_status"] == "complete"
    assert result["branches"][0]["eligible"] is True
    assert result["branches"][0]["exclusion_reasons"] == []


def test_static_same_module_references_are_explicit_and_incomplete_references_abstain(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src.py").write_text(
        "def len(value):\n    return value\n\ndef run(value):\n    return value\n",
        encoding="utf-8")
    base = commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "source")
    (repo / "src.py").write_text(
        "def len(value):\n    return value\n\ndef run(value):\n    return len(value)\n",
        encoding="utf-8")
    source_tip = commit(repo, "source")
    snapshot = {"kind": "git-snapshot", "schema_version": 1, "snapshot_digest": "s",
                "repository_id": "r", "main": {"name": "main", "tip": base},
                "branches": [{"name": "source", "tip": source_tip, "merge_base": base,
                              "eligible": True, "exclusion_reasons": []}]}
    result = build_contributions(snapshot, repo)
    caller = next(unit for unit in result["units"] if unit.get("name") == "run")
    helper = next(unit for unit in result["destination_units"] if unit.get("name") == "len")
    dependency = next(edge for edge in result["edges"] if edge.get("type") == "dependency")
    assert dependency["source_id"] == caller["id"]
    assert dependency["destination_id"] == helper["id"]
    assert dependency["provenance"] == "static_ast_symbol_reference"
    assert caller["source_dependency_context_status"] == "complete"
    assert caller["dependency_context_status"] == "complete"
    assert caller["destination_candidate_provenance"][caller["destination_ids"][0]] == "same_path_name"
    assert result["dependency_extraction"]["status"] == "partial_unknown"


def test_attribute_calls_keep_dependency_context_unknown(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src.py").write_text("def run(value):\n    return value\n", encoding="utf-8")
    base = commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "source")
    (repo / "src.py").write_text("def run(value):\n    return value.process()\n", encoding="utf-8")
    source_tip = commit(repo, "source")
    snapshot = {"kind": "git-snapshot", "schema_version": 1, "snapshot_digest": "s",
                "repository_id": "r", "main": {"name": "main", "tip": base},
                "branches": [{"name": "source", "tip": source_tip, "merge_base": base,
                              "eligible": True, "exclusion_reasons": []}]}
    result = build_contributions(snapshot, repo)
    unit = next(unit for unit in result["units"] if unit.get("name") == "run")
    assert unit["dependency_context_status"] == "unknown"
    assert "attribute_call_unresolved" in unit["dynamic_reference_observations"]


def test_nested_dynamic_references_cannot_be_shadowed_by_nested_bindings(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    base = commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "source")
    (repo / "src.py").write_text(
        "def run():\n"
        "    def inner(module, name):\n"
        "        return getattr(module, name)\n"
        "    return inner(object(), 'work')\n",
        encoding="utf-8")
    source_tip = commit(repo, "source")
    snapshot = {"kind": "git-snapshot", "schema_version": 1, "snapshot_digest": "s",
                "repository_id": "r", "main": {"name": "main", "tip": base},
                "branches": [{"name": "source", "tip": source_tip, "merge_base": base,
                              "eligible": True, "exclusion_reasons": []}]}
    result = build_contributions(snapshot, repo)
    unit = next(unit for unit in result["units"] if unit.get("name") == "run")
    assert unit["dependency_context_status"] == "unknown"
    assert "nested_scope_unanalyzed" in unit["dynamic_reference_observations"]


def test_removal_and_comment_only_change_always_have_file_units(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "removed.py").write_text("def gone():\n    return 1\n", encoding="utf-8")
    (repo / "comment.py").write_text("VALUE = 1\n", encoding="utf-8")
    base = commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "source")
    (repo / "removed.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "comment.py").write_text("# formatting only\nVALUE = 1\n", encoding="utf-8")
    source_tip = commit(repo, "source")
    git(repo, "checkout", "-q", "main")
    main_tip = git(repo, "rev-parse", "HEAD")
    snapshot = {"kind": "git-snapshot", "schema_version": 1, "snapshot_digest": "s", "repository_id": "r", "main": {"name": "main", "tip": main_tip, "tree": git(repo, "rev-parse", f"{main_tip}^{{tree}}")}, "branches": [{"name": "source", "tip": source_tip, "merge_base": base, "eligible": True, "exclusion_reasons": []}]}
    result = build_contributions(snapshot, repo)
    file_units = {(unit["path"], unit["limitations"][0]) for unit in result["units"] if unit["kind"] == "file"}
    assert ("removed.py", "definition_removal") in file_units
    assert ("comment.py", "non_definition_change") in file_units
    assert {item["path"] for item in result["paths"]} == {"removed.py", "comment.py"}


def test_alias_ids_are_branch_distinct_and_excluded_or_unpinned_refs_are_not_analyzed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    base = commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "source")
    (repo / "src.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    source_tip = commit(repo, "source")
    git(repo, "replace", base, source_tip)
    git(repo, "checkout", "-q", "main")
    main_tip = git(repo, "rev-parse", "HEAD")
    snapshot = {"kind": "git-snapshot", "schema_version": 1, "snapshot_digest": "s", "repository_id": "r", "main": {"name": "main", "tip": main_tip, "tree": git(repo, "rev-parse", f"{main_tip}^{{tree}}")}, "branches": [
        {"name": "source", "tip": source_tip, "merge_base": base, "eligible": True, "exclusion_reasons": []},
        {"name": "source-alias", "tip": source_tip, "merge_base": base, "eligible": True, "exclusion_reasons": []},
        {"name": "excluded", "tip": "invalid-pin", "merge_base": None, "eligible": False, "exclusion_reasons": ["recent"]},
        {"name": "unbased", "tip": source_tip, "merge_base": None, "eligible": True, "exclusion_reasons": []},
    ]}
    result = build_contributions(snapshot, repo)
    f_units = [unit for unit in result["units"] if unit["kind"] == "python_definition"]
    assert {unit["branch"] for unit in f_units} == {"source", "source-alias"}
    assert len({unit["id"] for unit in f_units}) == 2
    assert not any(path["branch"] in {"excluded", "unbased"} for path in result["paths"])
    by_name = {branch["name"]: branch for branch in result["branches"]}
    assert by_name["excluded"]["analysis_status"] == "excluded"
    assert by_name["excluded"]["unit_ids"] == []
    assert by_name["unbased"]["analysis_status"] == "unavailable"
    assert "source_merge_base_missing:unbased" in result["limitations"]
    assert all(isinstance(item, str) for item in result["limitations"])


def test_parallel_build_checkpoint_resume_and_extractor_binding(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    base = commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "source")
    (repo / "src.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    source_tip = commit(repo, "source")
    git(repo, "checkout", "-q", "main")
    snapshot = {
        "kind": "git-snapshot", "schema_version": 1, "snapshot_digest": "snapshot-1",
        "repository_id": "repo-id",
        "main": {"name": "main", "tip": base, "tree": git(repo, "rev-parse", f"{base}^{{tree}}")},
        "branches": [
            {"name": "source", "tip": source_tip, "merge_base": base, "eligible": True, "exclusion_reasons": []},
            {"name": "source-alias", "tip": source_tip, "merge_base": base, "eligible": True, "exclusion_reasons": []},
        ],
    }
    pins = [base, source_tip]
    object_repo = tmp_path / "objects.git"
    export_pinned_repository(repo, pins, object_repo)
    checkpoint = tmp_path / "contribution-checkpoint.json"
    checkpoint.parent.chmod(0o700)
    progress = []
    parallel = _build_contributions(snapshot, object_repo, workers=2, progress=lambda *item: progress.append(item), checkpoint_path=checkpoint)
    assert len(progress) == 2
    serial_resumed = _build_contributions(snapshot, object_repo, workers=1, checkpoint_path=checkpoint)
    assert parallel == serial_resumed
    assert checkpoint.stat().st_mode & 0o077 == 0
    changed_snapshot = {**snapshot, "snapshot_digest": "snapshot-2"}
    with unittest.TestCase().assertRaisesRegex(JgError, "different snapshot or extractor"):
        _build_contributions(changed_snapshot, object_repo, checkpoint_path=checkpoint)


def test_conditional_module_builtin_shadow_and_star_import_stay_unknown(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src.py").write_text(
        "def subject(values):\n    return len(values)\n", encoding="utf-8")
    base = commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "source")
    (repo / "src.py").write_text(
        "if configure_custom_builtins():\n    len = custom_length\n"
        "def subject(values):\n    return len(values) + 1\n", encoding="utf-8")
    source_tip = commit(repo, "conditional module binding")
    snapshot = {"kind": "git-snapshot", "schema_version": 1, "snapshot_digest": "s",
                "repository_id": "r", "main": {"name": "main", "tip": base},
                "branches": [{"name": "source", "tip": source_tip, "merge_base": base,
                              "eligible": True, "exclusion_reasons": []}]}
    result = build_contributions(snapshot, repo)
    unit = next(unit for unit in result["units"] if unit.get("name") == "subject")
    assert "len" in unit["module_value_bindings"]
    assert "conditional_module_binding_unanalyzed" in unit["module_dynamic_reference_observations"]
    assert unit["source_dependency_context_status"] == "unknown"
    assert unit["dependency_context_status"] == "unknown"

    (repo / "src.py").write_text(
        "from package import *\n\ndef subject(values):\n    return len(values) + 2\n",
        encoding="utf-8")
    star_tip = commit(repo, "star import")
    star_snapshot = {**snapshot, "snapshot_digest": "star",
                     "branches": [{"name": "source", "tip": star_tip, "merge_base": base,
                                   "eligible": True, "exclusion_reasons": []}]}
    star_result = build_contributions(star_snapshot, repo)
    star_unit = next(unit for unit in star_result["units"] if unit.get("name") == "subject")
    assert "module_star_import_unresolved" in star_unit["module_dynamic_reference_observations"]
    assert star_unit["source_dependency_context_status"] == "unknown"


def test_module_value_reference_does_not_fall_back_to_destination_definition(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src.py").write_text(
        "def helper():\n    return 1\ndef subject():\n    return 1\n", encoding="utf-8")
    base = commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "source")
    (repo / "src.py").write_text(
        "helper = 3\ndef subject():\n    return helper()\n", encoding="utf-8")
    source_tip = commit(repo, "module value reference")
    snapshot = {"kind": "git-snapshot", "schema_version": 1, "snapshot_digest": "s",
                "repository_id": "r", "main": {"name": "main", "tip": base},
                "branches": [{"name": "source", "tip": source_tip, "merge_base": base,
                              "eligible": True, "exclusion_reasons": []}]}
    result = build_contributions(snapshot, repo)
    unit = next(unit for unit in result["units"] if unit.get("name") == "subject")
    assert "helper" in unit["module_value_bindings"]
    assert "helper" in unit["dependency_observations"]["unresolved_reference_samples"]
    assert unit["source_dependency_context_status"] == "unknown"
    assert unit["dependency_context_status"] == "unknown"


def test_unknown_dependency_status_propagates_to_callers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src.py").write_text(
        "import builtins\n"
        "def mutate():\n    return 0\n"
        "def subject(values):\n    return values\n", encoding="utf-8")
    base = commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "source")
    subject = "def subject(values):\n    mutate()\n    return len(values) + 1\n"
    (repo / "src.py").write_text(
        "import builtins\n"
        "def mutate():\n    builtins.len = custom_length\n    return 0\n"
        + subject, encoding="utf-8")
    source_tip = commit(repo, "dynamic dependency")
    git(repo, "checkout", "-q", "main")
    (repo / "dst.py").write_text(subject, encoding="utf-8")
    commit(repo, "destination subject candidate")
    main_tip = git(repo, "rev-parse", "HEAD")
    snapshot = {"kind": "git-snapshot", "schema_version": 1, "snapshot_digest": "s",
                "repository_id": "r", "main": {"name": "main", "tip": main_tip},
                "branches": [{"name": "source", "tip": source_tip, "merge_base": base,
                              "eligible": True, "exclusion_reasons": []}]}
    result = build_contributions(snapshot, repo)
    caller = next(unit for unit in result["units"] if unit.get("name") == "subject")
    mutator = next(unit for unit in result["units"] if unit.get("name") == "mutate")
    assert any(edge["source_id"] == caller["id"] and edge["destination_id"] == mutator["id"]
               for edge in result["edges"])
    assert mutator["source_dependency_context_status"] == "unknown"
    assert caller["source_dependency_context_status"] == "unknown"
    assert "transitive_dependency_context_unknown" in caller["dependency_context_limitations"]


def test_match_capture_and_global_statement_keep_module_resolution_unknown() -> None:
    match_unit = _definitions(
        "match value:\n"
        "    case len:\n"
        "        pass\n"
        "def subject(values):\n"
        "    return len(values) + 1\n")[0]
    assert "len" in match_unit["module_value_bindings"]
    assert "conditional_module_binding_unanalyzed" in match_unit["module_dynamic_reference_observations"]

    global_unit = _definitions(
        "def mutate():\n"
        "    global len\n"
        "    len = custom_length\n"
        "def subject(values):\n"
        "    return len(values) + 1\n")[1]
    assert "module_global_write_unanalyzed" in global_unit["module_dynamic_reference_observations"]

    for expression, signal in (
        ("exec('len = custom_length')", "module_dynamic_namespace_call:exec"),
        ("globals()['len'] = custom_length", "module_dynamic_namespace_call:globals"),
        ("builtins.len = custom_length", "module_attribute_binding_write_unanalyzed"),
        ("__builtins__['len'] = custom_length", "module_subscript_binding_write_unanalyzed"),
    ):
        dynamic_unit = _definitions(
            f"{expression}\n"
            "def subject(values):\n"
            "    return len(values) + 1\n")[0]
        assert signal in dynamic_unit["module_dynamic_reference_observations"]

    class_unit = _definitions(
        "import builtins\n"
        "class C:\n"
        "    builtins.len = custom_length\n"
        "def subject(values):\n"
        "    return len(values) + 1\n")[1]
    assert "module_attribute_binding_write_unanalyzed" in class_unit["module_dynamic_reference_observations"]


class ContributionTests(unittest.TestCase):
    """Make the hermetic fixture cases available to unittest discovery."""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.tmp_path = Path(self._tempdir.name)

    def test_git_environment_drops_ambient_repository_redirection(self) -> None:
        with patch.dict(os.environ, {"GIT_DIR": "/wrong", "GIT_INDEX_FILE": "/wrong/index", "GIT_OBJECT_DIRECTORY": "/wrong/objects"}):
            env = _git_env()
        self.assertNotIn("GIT_DIR", env)
        self.assertNotIn("GIT_INDEX_FILE", env)
        self.assertNotIn("GIT_OBJECT_DIRECTORY", env)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(env["GIT_NO_LAZY_FETCH"], "1")

    def test_writer_preserves_an_existing_artifact(self) -> None:
        repo = self.tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / "file.txt").write_text("x\n", encoding="utf-8")
        tip = commit(repo, "base")
        snapshot = {"kind": "git-snapshot", "schema_version": 1, "snapshot_digest": "s", "repository_id": "r", "main": {"name": "main", "tip": tip, "tree": git(repo, "rev-parse", f"{tip}^{{tree}}")}, "branches": []}
        snapshot_path = self.tmp_path / "snapshot.json"
        snapshot_path.write_text("snapshot placeholder", encoding="utf-8")
        output = self.tmp_path / "contributions.json"
        output.write_text("preserve this", encoding="utf-8")
        snapshot_module = types.ModuleType("jev_git_graph.snapshot")
        snapshot_module.load_snapshot = lambda _path: (snapshot, repo)
        with patch.dict(sys.modules, {"jev_git_graph.snapshot": snapshot_module}):
            with self.assertRaises(JgError):
                write_contributions(snapshot_path, output)
        self.assertEqual(output.read_text(encoding="utf-8"), "preserve this")

    def test_inventory_accounts_for_changed_files_and_matches_all_destinations(self) -> None:
        test_inventory_accounts_for_changed_files_and_matches_all_destinations(self.tmp_path)

    def test_ast_ambiguity_keeps_every_candidate(self) -> None:
        test_ast_ambiguity_keeps_every_candidate(self.tmp_path)

    def test_removal_and_comment_only_change_always_have_file_units(self) -> None:
        test_removal_and_comment_only_change_always_have_file_units(self.tmp_path)

    def test_alias_ids_are_branch_distinct_and_excluded_or_unpinned_refs_are_not_analyzed(self) -> None:
        test_alias_ids_are_branch_distinct_and_excluded_or_unpinned_refs_are_not_analyzed(self.tmp_path)
