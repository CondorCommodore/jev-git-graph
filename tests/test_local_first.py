from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev_git_graph.candidates import build_candidates, write_candidates
from jev_git_graph.errors import JgError
from jev_git_graph.inventory import build_inventory, write_inventory
from jev_git_graph import git as git_module
from jev_git_graph.jev import build_preview, execute_preview
from jev_git_graph.questions import QUESTION_IDS
from jev_git_graph.plan import build_plan, write_plan
from jev_git_graph.review import object_fingerprint
from jev_git_graph.safety import canonical_json, digest, opaque_path_id


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(("git", "-C", str(repo), *args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return completed.stdout.decode("utf-8")


def make_fixture(parent: Path) -> tuple[Path, Path]:
    repo = parent / "private-repository"
    worktree = parent / "linked-worktree"
    git(parent, "init", "-b", "main", str(repo))
    git(repo, "config", "user.name", "Private Maintainer")
    git(repo, "config", "user.email", "private@example.test")
    git(repo, "remote", "add", "origin", "https://example.invalid/private-token-should-not-leak")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "base")

    git(repo, "switch", "-c", "auth-v1")
    (repo / "auth.txt").write_text("private secret payload\nversion one\n", encoding="utf-8")
    git(repo, "add", "auth.txt")
    git(repo, "commit", "-m", "AUTH-42 add session validation")

    git(repo, "switch", "main")
    git(repo, "switch", "-c", "auth-v2")
    git(repo, "cherry-pick", "auth-v1")
    (repo / "auth.txt").write_text("private secret payload\nversion two\n", encoding="utf-8")
    git(repo, "add", "auth.txt")
    git(repo, "commit", "-m", "AUTH-42 refine session validation")

    git(repo, "switch", "main")
    git(repo, "switch", "-c", "rebase-topic")
    (repo / "rebase.txt").write_text("topic\n", encoding="utf-8")
    git(repo, "add", "rebase.txt")
    git(repo, "commit", "-m", "topic before rebase")
    git(repo, "switch", "main")
    (repo / "README.md").write_text("base\nmain changed\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "main update")
    git(repo, "switch", "rebase-topic")
    git(repo, "rebase", "main")

    git(repo, "switch", "auth-v1")
    (repo / "stash.txt").write_text("uncommitted private stash material\n", encoding="utf-8")
    git(repo, "add", "stash.txt")
    git(repo, "stash", "push", "-m", "preserve auth investigation")
    git(repo, "worktree", "add", str(worktree), "auth-v2")
    (worktree / "worktree-note.txt").write_text("uncommitted worktree material\n", encoding="utf-8")
    return repo, worktree


class LocalFirstTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name)
        self.repo, self.worktree = make_fixture(self.parent)
        self.output = self.parent / "private-artifacts"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_inventory_is_read_only_and_covers_worktree_stash_and_rebase(self) -> None:
        before_status = git(self.repo, "status", "--porcelain=v1")
        before_refs = git(self.repo, "show-ref")
        path = write_inventory(self.repo, self.output)
        after_status = git(self.repo, "status", "--porcelain=v1")
        after_refs = git(self.repo, "show-ref")

        self.assertEqual(before_status, after_status)
        self.assertEqual(before_refs, after_refs)
        inventory = json.loads(path.read_text())
        self.assertFalse(inventory["collection"]["network"])
        self.assertFalse(inventory["collection"]["remote_operations"])
        commands = [part for command in inventory["collection"]["commands"] for part in command]
        self.assertNotIn("fetch", commands)
        self.assertNotIn("push", commands)
        self.assertNotIn("pull", commands)
        self.assertEqual({"auth-v1", "auth-v2", "rebase-topic", "main"}, {branch["name"] for branch in inventory["branches"]})
        self.assertEqual(1, len(inventory["stashes"]))
        self.assertEqual(2, len(inventory["worktrees"]))
        self.assertTrue(any(worktree["status"] for worktree in inventory["worktrees"]))
        self.assertTrue(inventory["collection"]["complete"])
        self.assertEqual(len(inventory["branches"]), inventory["collection"]["counts"]["branches"])
        self.assertEqual(2, inventory["collection"]["counts"]["worktrees"])
        self.assertEqual(0, len(inventory["remote_tracking_refs"]))
        self.assertEqual(0, path.stat().st_mode & 0o077)
        self.assertEqual(0, self.output.stat().st_mode & 0o077)

    def test_inventory_records_changed_refs_as_incomplete(self) -> None:
        original = git_module.ref_snapshot
        calls = 0

        def changed(runner):
            nonlocal calls
            calls += 1
            return original(runner) if calls == 1 else original(runner) + "changed"

        with patch.object(git_module, "ref_snapshot", side_effect=changed):
            with self.assertRaisesRegex(JgError, "inventory incomplete"):
                write_inventory(self.repo, self.output)
        inventory = json.loads((self.output / "inventory.json").read_text())
        self.assertFalse(inventory["collection"]["complete"])
        self.assertIn({"kind": "refs_changed_during_inventory"}, inventory["collection"]["errors"])
        with self.assertRaisesRegex(JgError, "incomplete"):
            write_candidates(self.repo, self.output / "inventory.json", self.output)

    def test_inventory_rejects_unavailable_worktree(self) -> None:
        with patch.object(git_module, "status_for", side_effect=JgError("unable to inspect worktree status")):
            with self.assertRaisesRegex(JgError, "inventory incomplete"):
                write_inventory(self.repo, self.output)
        inventory = json.loads((self.output / "inventory.json").read_text())
        self.assertFalse(inventory["collection"]["complete"])
        self.assertEqual(2, len(inventory["collection"]["errors"]))

    def test_rejects_output_inside_repository_before_creating_it(self) -> None:
        nested = self.repo / "reports"
        with self.assertRaises(JgError):
            write_inventory(self.repo, nested)
        self.assertFalse(nested.exists())

    def test_candidates_find_cherry_pick_and_partial_overlap(self) -> None:
        inventory_path = write_inventory(self.repo, self.output)
        candidates_path = write_candidates(self.repo, inventory_path, self.output)
        candidates = json.loads(candidates_path.read_text())
        pairs = {(item["endpoints"]["a"]["branch"], item["endpoints"]["b"]["branch"]): item for item in candidates["candidates"]}
        candidate = pairs[("auth-v1", "auth-v2")]
        self.assertIn("PATCH_EQUIVALENCE", candidate["reasons"])
        self.assertIn("CHANGED_PATH_OVERLAP", candidate["reasons"])
        self.assertTrue(candidate["evidence"]["shared_patch_ids"])
        self.assertIn("auth.txt", candidate["evidence"]["shared_paths"])

    def test_large_common_signal_is_explicitly_truncated(self) -> None:
        inventory = {
            "schema_version": 1,
            "repository": {"id": opaque_path_id(self.repo), "default_branch": "main"},
            "collection": {"complete": True},
            "branches": [
                {"name": f"topic-{index}", "tip": f"{index:040x}", "changed_paths": ["common.txt"], "unique_commits": [], "merge_base": None}
                for index in range(101)
            ],
        }
        source = self.parent / "large-inventory.json"
        source.write_text(json.dumps(inventory), encoding="utf-8")
        result = build_candidates(self.repo, source)
        self.assertEqual(0, result["candidate_count"])
        self.assertTrue(result["coverage"]["truncated"])
        self.assertEqual(1, result["coverage"]["skipped_common_signals"])

    def test_preview_excludes_private_content_paths_and_remote_urls(self) -> None:
        inventory_path = write_inventory(self.repo, self.output)
        candidates_path = write_candidates(self.repo, inventory_path, self.output)
        preview = build_preview(json.loads(candidates_path.read_text()))
        serialized = json.dumps(preview)
        self.assertFalse(preview["network_performed"])
        self.assertNotIn(str(self.repo), serialized)
        self.assertNotIn("example.invalid", serialized)
        self.assertNotIn("private secret payload", serialized)
        self.assertNotIn("uncommitted private stash material", serialized)
        self.assertNotIn("auth.txt", serialized)
        self.assertNotIn("Private Maintainer", serialized)

    def test_preview_uses_documented_system_one_request_shape(self) -> None:
        inventory_path = write_inventory(self.repo, self.output)
        candidates_path = write_candidates(self.repo, inventory_path, self.output)
        preview = build_preview(json.loads(candidates_path.read_text()))
        self.assertGreater(preview["request_count"], 0)
        for request in preview["requests"]:
            self.assertEqual({"state", "model", "questions"}, set(request))
            self.assertEqual("jev-latest", request["model"])
            self.assertEqual(set(QUESTION_IDS), set(request["questions"]))
            for question in request["questions"].values():
                self.assertEqual({"type", "instructions", "criteria"}, set(question))
                self.assertEqual("noul", question["type"])
                self.assertEqual({"true", "false"}, set(question["criteria"]))

    def test_review_profile_is_explicit_and_minimal_remains_private(self) -> None:
        inventory_path = write_inventory(self.repo, self.output)
        candidates_path = write_candidates(self.repo, inventory_path, self.output)
        source = json.loads(candidates_path.read_text())
        minimal = json.dumps(build_preview(source, "minimal"))
        review = build_preview(source, "review")
        self.assertNotIn("auth-v1", minimal)
        self.assertNotIn("auth.txt", minimal)
        self.assertEqual("review", review["evidence_profile"])
        self.assertTrue(any("a_branch_label" in request["state"] for request in review["requests"]))

    def test_human_review_applies_only_to_matching_fingerprint(self) -> None:
        inventory_path = write_inventory(self.repo, self.output)
        candidates_path = write_candidates(self.repo, inventory_path, self.output)
        inventory = json.loads(inventory_path.read_text())
        branch = next(item for item in inventory["branches"] if item["name"] == "auth-v1")
        review = {
            "kind": "relationship-review", "schema_version": 1,
            "repository_id": inventory["repository"]["id"],
            "decisions": [{"object_id": "branch:auth-v1", "kind": "branch",
                           "fingerprint": object_fingerprint("branch", branch),
                           "disposition": "ACTIVE", "rationale": "owner confirmed active work",
                           "reviewed_at": "2026-09-21T00:00:00Z"}],
        }
        review_path = self.output / "review.json"
        review_path.write_text(json.dumps(review), encoding="utf-8")
        plan, _ = build_plan(inventory_path, candidates_path, review_path=review_path)
        decision = next(item for item in plan["dispositions"] if item.get("name") == "auth-v1")
        self.assertEqual("ACTIVE", decision["disposition"])
        self.assertEqual("current", decision["review_status"])
        review["decisions"][0]["fingerprint"] = "0" * 64
        review_path.write_text(json.dumps(review), encoding="utf-8")
        plan, _ = build_plan(inventory_path, candidates_path, review_path=review_path)
        decision = next(item for item in plan["dispositions"] if item.get("name") == "auth-v1")
        self.assertEqual("UNRESOLVED", decision["disposition"])
        self.assertEqual("stale", decision["review_status"])

    def test_plan_rejects_review_exported_from_different_inventory(self) -> None:
        inventory_path = write_inventory(self.repo, self.output)
        candidates_path = write_candidates(self.repo, inventory_path, self.output)
        inventory = json.loads(inventory_path.read_text())
        review_path = self.output / "review.json"
        review_path.write_text(json.dumps({
            "kind": "relationship-review", "schema_version": 1,
            "repository_id": inventory["repository"]["id"],
            "inventory_digest": "0" * 64, "decisions": [],
        }), encoding="utf-8")
        with self.assertRaisesRegex(JgError, "different inventory"):
            build_plan(inventory_path, candidates_path, review_path=review_path)

    def test_plan_downgrades_tampered_preservation_proof(self) -> None:
        inventory_path = write_inventory(self.repo, self.output)
        candidates_path = write_candidates(self.repo, inventory_path, self.output)
        inventory = json.loads(inventory_path.read_text())
        candidates = json.loads(candidates_path.read_text())
        branch = next(item for item in inventory["branches"] if item["name"] == "auth-v1")
        destination = {"kind": "branch", "name": "auth-v1"}
        fingerprint = object_fingerprint("branch", branch)
        review = {
            "kind": "relationship-review", "schema_version": 2,
            "repository_id": inventory["repository"]["id"],
            "inventory_digest": digest(inventory), "candidate_digest": digest(candidates), "relations_digest": None,
            "provenance": {"repository_id": inventory["repository"]["id"], "inventory_digest": digest(inventory), "candidate_digest": digest(candidates), "relations_digest": None},
            "reviewer_identity": "reviewer@example.test",
            "decisions": [{
                "object_id": "branch:auth-v1", "kind": "branch", "fingerprint": fingerprint, "source_fingerprint": fingerprint,
                "source_provenance": {"repository_id": inventory["repository"]["id"], "inventory_digest": digest(inventory), "candidate_digest": digest(candidates), "relations_digest": None}, "reviewer_id": "reviewer@example.test",
                "reviewed_at": "2026-09-21T00:00:00Z", "disposition": "PRESERVE_IN_BRANCH", "rationale": "retain",
                "preservation_destination": destination,
                "preservation_proof": {"verified": True, "source_fingerprint": fingerprint, "destination_fingerprint": "0" * 64, "destination": destination},
            }], "limitations": [],
        }
        review_path = self.output / "tampered-review.json"
        review_path.write_text(json.dumps(review), encoding="utf-8")
        plan, _ = build_plan(inventory_path, candidates_path, review_path=review_path)
        decision = next(item for item in plan["dispositions"] if item.get("name") == "auth-v1")
        self.assertEqual("UNRESOLVED", decision["disposition"])
        self.assertEqual("stale", decision["review_status"])

    def test_plan_accounts_for_each_branch_dirty_worktree_and_stash(self) -> None:
        inventory_path = write_inventory(self.repo, self.output)
        candidates_path = write_candidates(self.repo, inventory_path, self.output)
        plan, rendered = build_plan(inventory_path, candidates_path)
        kinds = [item["kind"] for item in plan["dispositions"]]
        self.assertGreaterEqual(kinds.count("branch"), 4)
        self.assertIn("worktree", kinds)
        self.assertIn("stash", kinds)
        self.assertIn("UNRESOLVED", rendered)
        self.assertIn("destructive action", rendered)
        json_path, markdown_path = write_plan(inventory_path, candidates_path, self.output)
        self.assertTrue(json_path.exists())
        self.assertTrue(markdown_path.exists())

    def test_live_jev_requires_matching_preview_and_uses_only_preview_payload(self) -> None:
        inventory_path = write_inventory(self.repo, self.output)
        candidates_path = write_candidates(self.repo, inventory_path, self.output)
        preview = build_preview(json.loads(candidates_path.read_text()))
        preview_path = self.output / "jev-preview.json"
        preview_path.write_text(json.dumps(preview), encoding="utf-8")
        received: list[dict] = []

        def fake_transport(payload: dict, token: str) -> dict:
            received.append(payload)
            self.assertEqual("test-key", token)
            return {"model": "jev-latest", "usage": {"input_tokens": 100, "output_tokens": 7},
                    "answers": {key: {"noul": 0.5} for key in payload["questions"]}}

        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}, clear=False):
            relations = execute_preview(
                preview_path,
                preview["payload_sha256"],
                max_requests=preview["request_count"],
                transport=fake_transport,
            )
        self.assertEqual(preview["request_count"], len(received))
        self.assertTrue(relations["network_performed"])
        for payload in received:
            text = json.dumps(payload)
            self.assertNotIn("private secret payload", text)
            self.assertNotIn(str(self.repo), text)
            self.assertNotIn("auth.txt", text)
        with self.assertRaises(JgError):
            execute_preview(preview_path, "0" * 64, transport=fake_transport)

    def test_live_jev_defaults_to_one_bounded_request(self) -> None:
        inventory_path = write_inventory(self.repo, self.output)
        candidates_path = write_candidates(self.repo, inventory_path, self.output)
        preview = build_preview(json.loads(candidates_path.read_text()))
        preview["requests"].append(preview["requests"][0])
        preview["request_count"] = len(preview["requests"])
        preview["payload_bytes"] = len(canonical_json(preview["requests"]))
        preview["payload_sha256"] = digest(preview["requests"])
        preview_path = self.output / "jev-preview.json"
        preview_path.write_text(json.dumps(preview), encoding="utf-8")
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}, clear=False):
            with self.assertRaisesRegex(JgError, "live default permits 1"):
                execute_preview(preview_path, preview["payload_sha256"], transport=lambda *_: {})


if __name__ == "__main__":
    unittest.main()
