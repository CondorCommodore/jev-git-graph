from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev_git_graph import git as git_module
from jev_git_graph.artifacts import candidate_content_digest, validate_artifacts
from jev_git_graph.candidates import build_candidates, write_candidates
from jev_git_graph.inventory import write_inventory
from jev_git_graph.preservation import build_preservation_plan
from jev_git_graph.review import object_fingerprint, reconcile_reviews, validate_review_document
from jev_git_graph.safety import digest, read_json, write_json
from jev_git_graph.errors import JgError


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout


def write_commit(repo: Path, relative: str, content: str, message: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    run_git(repo, "add", relative)
    run_git(repo, "commit", "-m", message)


def fixture_tree_bytes(paths: list[Path]) -> str:
    hasher = hashlib.sha256()
    for root in sorted(paths):
        for directory, _names, filenames in os.walk(root, followlinks=False):
            for name in sorted(filenames):
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                hasher.update(str(root).encode("utf-8"))
                hasher.update(b"\0")
                hasher.update(relative.encode("utf-8"))
                hasher.update(b"\0")
                hasher.update(path.read_bytes())
    return hasher.hexdigest()


def fixture_refs(repo: Path) -> str:
    return run_git(repo, "show-ref")


def make_fixture(parent: Path) -> tuple[Path, Path, Path]:
    repo = parent / "fixture"
    dirty_worktree = parent / "dirty-worktree"
    inaccessible_worktree = parent / "inaccessible-worktree"
    run_git(parent, "init", "-b", "main", str(repo))
    run_git(repo, "config", "user.name", "Offline Acceptance Maintainer")
    run_git(repo, "config", "user.email", "offline@example.test")
    run_git(repo, "remote", "add", "origin", "https://private.invalid/review-token")

    write_commit(repo, "README.md", "base\n", "base")
    write_commit(repo, "shared.txt", "shared base\n", "shared base")

    run_git(repo, "switch", "-c", "merged-topic")
    write_commit(repo, "merged.txt", "merged work\n", "merged work")
    run_git(repo, "switch", "main")
    run_git(repo, "merge", "--no-ff", "merged-topic", "-m", "merge merged topic")

    run_git(repo, "switch", "-c", "patch-source")
    write_commit(repo, "patch.txt", "same patch payload\n", "PATCH-1 add patch payload")
    run_git(repo, "switch", "main")
    run_git(repo, "switch", "-c", "patch-equivalent")
    run_git(repo, "cherry-pick", "patch-source")

    run_git(repo, "switch", "main")
    run_git(repo, "switch", "-c", "dependency-base")
    write_commit(repo, "dependency.txt", "base dependency\n", "dependency base")
    run_git(repo, "switch", "-c", "dependency-child")
    write_commit(repo, "child.txt", "dependent work\n", "dependency child")

    run_git(repo, "switch", "main")
    run_git(repo, "switch", "-c", "same-file-a")
    write_commit(repo, "shared.txt", "independent A\n", "independent same-file A")
    run_git(repo, "switch", "main")
    run_git(repo, "switch", "-c", "same-file-b")
    write_commit(repo, "shared.txt", "independent B\n", "independent same-file B")

    run_git(repo, "switch", "main")
    run_git(repo, "switch", "-c", "isolated-a")
    write_commit(repo, "isolated-a.txt", "isolated A\n", "isolated A")
    run_git(repo, "switch", "main")
    run_git(repo, "switch", "-c", "isolated-b")
    write_commit(repo, "isolated-b.txt", "isolated B\n", "isolated B")

    run_git(repo, "switch", "main")
    run_git(repo, "switch", "-c", "dirty-worktree")
    write_commit(repo, "dirty-base.txt", "dirty base\n", "dirty worktree base")
    run_git(repo, "switch", "main")
    run_git(repo, "switch", "-c", "inaccessible-worktree")
    write_commit(repo, "inaccessible-base.txt", "inaccessible base\n", "inaccessible worktree base")
    run_git(repo, "switch", "main")
    run_git(repo, "worktree", "add", str(dirty_worktree), "dirty-worktree")
    run_git(repo, "worktree", "add", str(inaccessible_worktree), "inaccessible-worktree")
    (dirty_worktree / "untracked-private-note.txt").write_text("UNTRACKED_FIXTURE_BYTES\n", encoding="utf-8")
    (dirty_worktree / "README.md").write_text("dirty working tree\n", encoding="utf-8")

    (repo / "stash-private-note.txt").write_text("STASH_FIXTURE_BYTES\n", encoding="utf-8")
    run_git(repo, "stash", "push", "-u", "-m", "offline acceptance stash")
    return repo, dirty_worktree, inaccessible_worktree


def branch_pair(candidate: dict, names: set[str]) -> bool:
    return {endpoint["branch"] for endpoint in candidate["endpoints"].values()} == names


def make_large_artifacts(inventory: dict) -> tuple[dict, dict]:
    branches = {branch["name"]: branch for branch in inventory["branches"]}
    required = {"dependency-base", "dependency-child", "patch-source", "patch-equivalent", "same-file-a", "same-file-b"}
    missing = required - branches.keys()
    if missing:
        raise AssertionError(f"fixture did not produce required branches: {sorted(missing)}")

    def candidate(index: int, kind: str, first: str, second: str, factual: bool = False) -> dict:
        evidence = {"shared_paths": [], "shared_subject_tokens": [], "shared_patch_ids": []}
        if factual:
            evidence.update({"a_ancestor_of_b": True, "b_ancestor_of_a": False})
            reasons = ["ANCESTRY"]
        elif kind == "jev":
            evidence["shared_patch_ids"] = [f"offline-patch-{index:04d}"]
            reasons = ["PATCH_EQUIVALENCE"]
        else:
            evidence["shared_paths"] = ["same-file.txt"]
            reasons = ["CHANGED_PATH_OVERLAP"]
        return {
            "id": f"{kind}-{index:04d}",
            "endpoints": {
                "a": {"branch": first, "tip": branches[first]["tip"]},
                "b": {"branch": second, "tip": branches[second]["tip"]},
            },
            "reasons": reasons,
            "evidence": evidence,
        }

    records = []
    for index in range(1000):
        records.append(candidate(index, "fact", "dependency-base", "dependency-child", factual=True))
    for index in range(1000):
        records.append(candidate(index, "jev", "patch-source", "patch-equivalent"))
    for index in range(1000):
        records.append(candidate(index, "unresolved", "same-file-a", "same-file-b"))
    candidates = {
        "kind": "candidates",
        "schema_version": 1,
        "repository_id": inventory["repository"]["id"],
        "inventory_digest": digest(inventory),
        "candidate_count_before_limit": len(records),
        "candidate_count": len(records),
        "coverage": {"strategy": "L6 synthetic metadata-only fixture", "truncated": False},
        "candidates": records,
    }
    candidates["content_digest"] = candidate_content_digest(candidates)

    response = {
        "model": "offline-fixture",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "answers": {
            "same_intent": {"noul": 0.95},
            "relationship": {
                "choice": "PARTIAL_OVERLAP",
                "confidence": 0.95,
                "probabilities": {"PARTIAL_OVERLAP": 0.95, "UNKNOWN": 0.05},
            },
        },
    }
    relations = {
        "kind": "relations",
        "question_version": "branch-relationship-v2",
        "repository_id": inventory["repository"]["id"],
        "inventory_digest": digest(inventory),
        "candidate_digest": digest(candidates),
        "candidate_content_digest": candidates["content_digest"],
        "network_performed": False,
        "relations": [
            {
                "candidate_id": f"jev-{index:04d}",
                "judgment_id": f"offline-judgment-{index:04d}",
                "question_version": "branch-relationship-v2",
                "response": copy.deepcopy(response),
            }
            for index in range(1000)
        ],
    }
    return candidates, relations


class L6OfflineAcceptanceTests(unittest.TestCase):
    def test_offline_integrated_acceptance_harness(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.fail("Node is required for the integrated viewer page/component acceptance probe")

        with tempfile.TemporaryDirectory(prefix="jev-l6-") as temporary:
            parent = Path(temporary)
            repo, dirty_worktree, inaccessible_worktree = make_fixture(parent)
            fixture_paths = [repo, dirty_worktree, inaccessible_worktree]
            before_bytes = fixture_tree_bytes(fixture_paths)
            before_refs = fixture_refs(repo)
            output = parent / "artifacts"

            blocked_network_calls: list[str] = []

            def blocked(label: str):
                def fail(*_args, **_kwargs):
                    blocked_network_calls.append(label)
                    raise AssertionError(f"unexpected network client: {label}")

                return fail

            with patch("urllib.request.urlopen", side_effect=blocked("urllib")), patch(
                "http.client.HTTPConnection.connect", side_effect=blocked("http")
            ), patch("socket.create_connection", side_effect=blocked("socket")):
                inventory_path = write_inventory(repo, output)
                inventory = read_json(inventory_path)

            self.assertEqual([], blocked_network_calls)
            self.assertTrue(inventory["collection"]["complete"])
            self.assertEqual(1, len(inventory["stashes"]))
            self.assertGreaterEqual(len(inventory["worktrees"]), 3)
            self.assertTrue(any(item["status"] for item in inventory["worktrees"] if item["path_id"] != inventory["worktrees"][0]["path_id"]))
            self.assertTrue(any(branch["name"] == "merged-topic" and branch["merged_into_default"] and not branch["unique_commits"] for branch in inventory["branches"]))

            generated_candidates_path = write_candidates(repo, inventory_path, output / "generated")
            generated_candidates = read_json(generated_candidates_path)
            generated_by_pair = generated_candidates["candidates"]
            self.assertTrue(any(branch_pair(item, {"patch-source", "patch-equivalent"}) and item["evidence"]["shared_patch_ids"] for item in generated_by_pair))
            self.assertTrue(any(branch_pair(item, {"dependency-base", "dependency-child"}) and item["evidence"]["a_ancestor_of_b"] for item in generated_by_pair))
            self.assertTrue(any(branch_pair(item, {"same-file-a", "same-file-b"}) and item["evidence"]["shared_paths"] for item in generated_by_pair))

            candidates, relations = make_large_artifacts(inventory)
            validate_artifacts(inventory, candidates, relations)
            large_dir = output / "large"
            large_dir.mkdir(mode=0o700)
            candidates_path = large_dir / "candidates.json"
            relations_path = large_dir / "relations.json"
            write_json(candidates_path, candidates)
            write_json(relations_path, relations)

            viewer_dir = output / "viewer"
            viewer_dir.mkdir(mode=0o700)
            write_json(viewer_dir / "inventory.json", inventory)
            write_json(viewer_dir / "candidates.json", candidates)
            write_json(viewer_dir / "relations.json", relations)
            probe = subprocess.run(
                (node, str(Path(__file__).with_name("l6_viewer_acceptance.mjs")), str(viewer_dir)),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertIn("candidates=3000", probe.stdout)
            self.assertIn("first=fact-0000", probe.stdout)
            self.assertIn("last=unresolved-0999", probe.stdout)

            repository_id = inventory["repository"]["id"]
            target_branch = next(item for item in inventory["branches"] if item["name"] == "patch-source")
            source_provenance = {
                "repository_id": repository_id,
                "inventory_digest": digest(inventory),
                "candidate_digest": digest(candidates),
                "candidate_content_digest": candidates["content_digest"],
                "relations_digest": digest(relations),
            }
            destination = {"kind": "branch", "name": "patch-source"}
            fingerprint = object_fingerprint("branch", target_branch)
            decision = {
                "object_id": "branch:patch-source",
                "kind": "branch",
                "fingerprint": fingerprint,
                "source_fingerprint": fingerprint,
                "source_provenance": copy.deepcopy(source_provenance),
                "reviewer_id": "l6@example.test",
                "reviewer": "l6@example.test",
                "rationale": "Keep the source branch as the reviewed preservation destination.",
                "reviewed_at": "2026-09-21T12:00:00Z",
                "disposition": "PRESERVE_IN_BRANCH",
                "preservation_destination": destination,
                "preservation_proof": {
                    "verified": True,
                    "source_fingerprint": fingerprint,
                    "destination_fingerprint": digest(destination),
                    "destination": destination,
                },
                "evidence": {"fixture": "offline", "candidate_family": "jev-like"},
            }
            decision["evidence_fingerprint"] = digest(decision["evidence"])
            review = {
                "kind": "relationship-review",
                "schema_version": 2,
                "repository_id": repository_id,
                **source_provenance,
                "provenance": copy.deepcopy(source_provenance),
                "reviewer_identity": "l6@example.test",
                "decisions": [decision],
                "limitations": [],
                "cleanup_readiness": "not_verified",
            }
            validate_review_document(review, repository_id)
            review_path = output / "review.json"
            write_json(review_path, review)
            imported_review = read_json(review_path)
            self.assertEqual(review, imported_review)
            self.assertEqual("PRESERVE_IN_BRANCH", imported_review["decisions"][0]["disposition"])

            unchanged = reconcile_reviews(imported_review, inventory, candidates, relations)
            carried = next(item for item in unchanged["decisions"] if item["object_id"] == "branch:patch-source")
            self.assertEqual("current", carried["reconciliation"]["status"])

            changed_inventory = copy.deepcopy(inventory)
            changed_branch = next(item for item in changed_inventory["branches"] if item["name"] == "patch-source")
            changed_branch["tip"] = "f" * 40
            changed_candidates = copy.deepcopy(candidates)
            for record in changed_candidates["candidates"]:
                for endpoint in record["endpoints"].values():
                    if endpoint["branch"] == "patch-source":
                        endpoint["tip"] = changed_branch["tip"]
            changed_candidates["inventory_digest"] = digest(changed_inventory)
            changed_candidates["content_digest"] = candidate_content_digest(changed_candidates)
            changed_relations = copy.deepcopy(relations)
            changed_relations["inventory_digest"] = digest(changed_inventory)
            changed_relations["candidate_digest"] = digest(changed_candidates)
            changed_relations["candidate_content_digest"] = changed_candidates["content_digest"]
            changed = reconcile_reviews(imported_review, changed_inventory, changed_candidates, changed_relations)
            changed_carried = next(item for item in changed["decisions"] if item["object_id"] == "branch:patch-source")
            self.assertEqual("stale", changed_carried["reconciliation"]["status"])
            self.assertIn("source_fingerprint_changed", changed_carried["reconciliation"]["reasons"])

            preservation = build_preservation_plan(inventory, candidates, relations, imported_review)
            preserved = next(item for item in preservation["objects"] if item["object_id"] == "branch:patch-source")
            self.assertEqual(imported_review["decisions"][0], preserved["human_decision"])
            self.assertEqual("PRESERVE_IN_BRANCH", preserved["human_decision"]["disposition"])
            preservation_path = output / "preservation-plan.json"
            write_json(preservation_path, preservation)

            cli_plan_dir = output / "cli-plan"
            cli = subprocess.run(
                (
                    os.environ.get("PYTHON", "python3"),
                    "-m",
                    "jev_git_graph",
                    "plan",
                    "--repo",
                    str(repo),
                    "--inventory",
                    str(viewer_dir / "inventory.json"),
                    "--candidates",
                    str(candidates_path),
                    "--relations",
                    str(relations_path),
                    "--review",
                    str(review_path),
                    "--out",
                    str(cli_plan_dir),
                ),
                check=True,
                cwd=Path(__file__).parents[1],
                env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertTrue(cli.stdout.strip().endswith("plan.md"))
            plan = read_json(cli_plan_dir / "plan.json")
            cli_decision = next(item for item in plan["dispositions"] if item.get("name") == "patch-source")
            self.assertEqual("PRESERVE_IN_BRANCH", cli_decision["disposition"])
            self.assertEqual("current", cli_decision["review_status"])
            self.assertIn("patch-source", (cli_plan_dir / "plan.md").read_text(encoding="utf-8"))

            inaccessible_output = output / "inaccessible"
            inaccessible_resolved = inaccessible_worktree.resolve()
            original_status_for = git_module.status_for

            def unavailable(path: Path) -> list[str]:
                if path.resolve() == inaccessible_resolved:
                    raise JgError("simulated inaccessible worktree")
                return original_status_for(path)

            with patch.object(git_module, "status_for", side_effect=unavailable):
                with self.assertRaisesRegex(Exception, "inventory incomplete"):
                    write_inventory(repo, inaccessible_output)
            incomplete = read_json(inaccessible_output / "inventory.json")
            self.assertFalse(incomplete["collection"]["complete"])
            self.assertTrue(any(error["kind"] == "worktree_status_unavailable" for error in incomplete["collection"]["errors"]))

            self.assertEqual(before_bytes, fixture_tree_bytes(fixture_paths))
            self.assertEqual(before_refs, fixture_refs(repo))
            command_text = "\n".join(" ".join(command) for command in inventory["collection"]["commands"])
            for forbidden in (" fetch", " push", " pull", " remote update"):
                self.assertNotIn(forbidden, command_text)
            self.assertFalse(inventory["collection"]["network"])
            self.assertFalse(inventory["collection"]["remote_operations"])
            artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in output.rglob("*.json"))
            self.assertNotIn("private.invalid", artifact_text)
            self.assertNotIn("STASH_FIXTURE_BYTES", artifact_text)
            self.assertNotIn("UNTRACKED_FIXTURE_BYTES", artifact_text)


if __name__ == "__main__":
    unittest.main()
