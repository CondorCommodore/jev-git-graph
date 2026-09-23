from __future__ import annotations

import os
import subprocess
import tempfile
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path

from jev_git_graph.contributions import _git_env, build_contributions as _build_contributions, write_contributions
from jev_git_graph.errors import JgError
from jev_git_graph.snapshot import export_pinned_repository


def build_contributions(snapshot, repo):
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
        return _build_contributions(snapshot, store)


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
    assert changed["destination_ids"] and len(changed["destination_ids"]) == 2
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
    assert len(unit["destination_ids"]) == 2
    assert result["branches"][0]["eligible"] is True
    assert result["branches"][0]["exclusion_reasons"] == []


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
