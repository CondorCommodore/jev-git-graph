from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from jev_git_graph.errors import JgError
from jev_git_graph.inventory import build_inventory, write_inventory
from jev_git_graph.snapshot import export_pinned_repository, load_snapshot, write_snapshot


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(("git", "-C", str(repo), *args), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result.stdout.decode().strip()


def make_repo(parent: Path) -> tuple[Path, str, str, str]:
    repo = parent / "source"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Snapshot Fixture")
    git(repo, "config", "user.email", "snapshot@example.invalid")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    main = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-qc", "topic")
    (repo / "topic.txt").write_text("topic\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "topic")
    topic = git(repo, "rev-parse", "HEAD")
    # Preserve a reachable commit whose only named source ref is then removed.
    git(repo, "switch", "-q", "main")
    git(repo, "switch", "--orphan", "orphan")
    (repo / "orphan.txt").write_text("orphan\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "orphan")
    orphan = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-q", "main")
    git(repo, "branch", "-D", "orphan")
    return repo, main, topic, orphan


class SnapshotTests(unittest.TestCase):
    def test_snapshot_restores_pins_in_independent_bare_store_and_ignores_later_main_move(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            repo, main, topic, orphan = make_repo(root)
            inv = write_inventory(repo, root / "inventory-run")
            before_status = git(repo, "status", "--porcelain=v1")
            before_refs = git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
            before_objects = git(repo, "count-objects", "-v")
            artifact = write_snapshot(repo, inv, root / "snapshot")
            manifest, objects = load_snapshot(artifact)
            self.assertEqual(main, manifest["main"]["tip"])
            self.assertEqual(topic, next(b["tip"] for b in manifest["branches"] if b["name"] == "topic"))
            for pin in manifest["object_store"]["pins"]:
                git(objects, "cat-file", "-e", f"{pin}^{{commit}}")
            self.assertNotIn(orphan, manifest["object_store"]["pins"])
            # A caller can pin an orphaned SHA directly; export does not depend on refs.
            orphan_store = root / "orphan-store.git"
            exported = export_pinned_repository(repo, [orphan], orphan_store)
            git(orphan_store, "cat-file", "-e", f"{orphan}^{{commit}}")
            self.assertEqual([orphan], exported["pins"])
            self.assertEqual(before_status, git(repo, "status", "--porcelain=v1"))
            self.assertEqual(before_refs, git(repo, "for-each-ref", "--format=%(refname) %(objectname)"))
            self.assertEqual(before_objects, git(repo, "count-objects", "-v"))
            git(repo, "switch", "main")
            (repo / "main-only.txt").write_text("advance main\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "advance main")
            loaded, same_objects = load_snapshot(artifact)
            self.assertEqual(manifest["snapshot_digest"], loaded["snapshot_digest"])
            self.assertEqual(objects, same_objects)

    def test_rejects_output_inside_worktree_or_common_git_directory(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            repo, _, _, _ = make_repo(root)
            inv = write_inventory(repo, root / "inventory-run")
            with self.assertRaises(JgError):
                write_snapshot(repo, inv, repo / "snapshot")
            with self.assertRaises(JgError):
                write_snapshot(repo, inv, repo / ".git" / "snapshot")

    def test_rejects_manifest_digest_and_pack_tampering_or_missing_pack(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            repo, _, _, _ = make_repo(root)
            inv = write_inventory(repo, root / "inventory-run")
            artifact = write_snapshot(repo, inv, root / "snapshot")
            manifest = json.loads(artifact.read_text())
            manifest["main"]["tip"] = "f" * 40
            artifact.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(JgError, "digest"):
                load_snapshot(artifact)

            artifact = write_snapshot(repo, inv, root / "second-snapshot")
            manifest, objects = load_snapshot(artifact)
            pack = objects / manifest["object_store"]["pack_path"]
            pack.chmod(0o600)
            pack.write_bytes(pack.read_bytes() + b"tamper")
            with self.assertRaisesRegex(JgError, "pack digest"):
                load_snapshot(artifact)
            pack.unlink()
            with self.assertRaises(JgError):
                load_snapshot(artifact)

    def test_rejects_incomplete_inventory_and_recent_hours_under_24(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            repo, _, _, _ = make_repo(root)
            inv = root / "inventory.json"
            inventory, _, _ = build_inventory(repo)
            inventory["collection"]["complete"] = False
            inv.write_text(json.dumps(inventory))
            with self.assertRaisesRegex(JgError, "complete"):
                write_snapshot(repo, inv, root / "snapshot")
            inventory["collection"]["complete"] = True
            inv.write_text(json.dumps(inventory))
            with self.assertRaisesRegex(JgError, "at least 24"):
                write_snapshot(repo, inv, root / "snapshot", recent_hours=23)


if __name__ == "__main__":
    unittest.main()
