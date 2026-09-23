from __future__ import annotations

import os
import subprocess
from pathlib import Path

from jev_git_graph.contributions import build_contributions


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
    base = commit(repo, "base")

    git(repo, "checkout", "-q", "-b", "source")
    (repo / "src.py").write_text("def changed():\n    return 9\n", encoding="utf-8")
    (repo / "imports.py").write_text("import os\nimport sys\n", encoding="utf-8")
    (repo / "deleted.py").unlink()
    os.chmod(repo / "mode.py", 0o755)
    (repo / "data.bin").write_bytes(b"new\x00bytes")
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
    expected_paths = {"src.py", "imports.py", "deleted.py", "mode.py", "data.bin"}
    assert {item["path"] for item in result["paths"]} == expected_paths
    assert all(any(unit["path"] == path for unit in result["units"]) for path in expected_paths)
    changed = next(unit for unit in result["units"] if unit["path"] == "src.py" and unit["kind"] == "python_definition")
    assert changed["destination_ids"] and len(changed["destination_ids"]) == 2
    assert all(edge["source_id"] == changed["id"] for edge in result["edges"])
    assert {unit["limitations"][0] for unit in result["units"] if unit["kind"] == "file"} >= {
        "module_change", "deleted", "mode_change", "non_python"
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
    snapshot = {"kind": "git-snapshot", "schema_version": 1, "snapshot_digest": "s", "repository_id": "r", "main": {"name": "main", "tip": main_tip, "tree": git(repo, "rev-parse", f"{main_tip}^{{tree}}")}, "branches": [{"name": "source", "tip": source_tip, "merge_base": base, "eligible": False, "exclusion_reasons": ["active"]}]}
    result = build_contributions(snapshot, repo)
    unit = next(unit for unit in result["units"] if unit["kind"] == "python_definition")
    assert len(unit["destination_ids"]) == 2
    assert result["branches"][0]["eligible"] is False
    assert result["branches"][0]["exclusion_reasons"] == ["active"]
