from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import unittest
import urllib.request
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


def find_headless_browser() -> Path | None:
    candidates = [
        shutil.which(name)
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
    ]
    candidates.extend(
        (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        )
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return Path(candidate)
    return None


class CdpWebSocket:
    def __init__(self, url: str) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.port is None:
            raise AssertionError(f"browser probe received a non-loopback DevTools URL: {url}")
        self.socket = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
        key = __import__("base64").b64encode(os.urandom(16)).decode("ascii")
        self.socket.sendall(
            (
                f"GET {parsed.path} HTTP/1.1\r\n"
                f"Host: {parsed.hostname}:{parsed.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.socket.recv(4096)
        if not response.startswith(b"HTTP/1.1 101"):
            raise AssertionError(f"browser DevTools WebSocket handshake failed: {response[:200]!r}")
        self.next_id = 1

    def _read_exact(self, size: int) -> bytes:
        value = b""
        while len(value) < size:
            chunk = self.socket.recv(size - len(value))
            if not chunk:
                raise AssertionError("browser DevTools WebSocket closed unexpectedly")
            value += chunk
        return value

    def _send(self, payload: dict) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(raw))
        length = len(masked)
        if length < 126:
            header = bytes((0x81, 0x80 | length))
        elif length < 65536:
            header = bytes((0x81, 0xFE)) + struct.pack(">H", length)
        else:
            header = bytes((0x81, 0xFF)) + struct.pack(">Q", length)
        self.socket.sendall(header + mask + masked)

    def _receive(self) -> tuple[int, bytes]:
        first, second = self._read_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        if second & 0x80:
            mask = self._read_exact(4)
            value = self._read_exact(length)
            value = bytes(byte ^ mask[index % 4] for index, byte in enumerate(value))
        else:
            value = self._read_exact(length)
        return opcode, value

    def command(self, method: str, params: dict | None = None) -> dict:
        command_id = self.next_id
        self.next_id += 1
        self._send({"id": command_id, "method": method, "params": params or {}})
        while True:
            opcode, raw = self._receive()
            if opcode == 9:
                self._send({"id": 0, "method": "", "params": {}})
                continue
            if opcode == 8:
                raise AssertionError("browser DevTools WebSocket closed while awaiting a command")
            if opcode != 1:
                continue
            message = json.loads(raw)
            if message.get("id") == command_id:
                if "error" in message:
                    raise AssertionError(f"browser DevTools command failed: {message['error']}")
                return message

    def close(self) -> None:
        self.socket.close()


def local_json(url: str) -> dict | list:
    if not url.startswith("http://127.0.0.1:"):
        raise AssertionError(f"browser probe attempted a non-loopback HTTP request: {url}")
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def run_browser_probe(browser: Path, inventory: dict, candidates: dict, relations: dict, output: Path) -> str:
    """Run a real local Chrome DOM probe over the same 3,000-record artifacts."""

    browser_dir = output / "browser"
    browser_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    html_path = browser_dir / "l6-browser-fixture.html"
    payload = json.dumps(
        {"inventory": inventory, "candidates": candidates, "relations": relations},
        ensure_ascii=True,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    viewer_script = (Path(__file__).parents[1] / "docs" / "viewer-data.js").resolve().as_uri()
    html_path.write_text(
        f"""<!doctype html>
<html><head><meta charset="utf-8"><script src={json.dumps(viewer_script)}></script></head>
<body>
<div id="candidate-count"></div>
<div id="page-status"></div>
<div id="page-one"></div>
<div id="page-thirty"></div>
<div id="component-status"></div>
<script id="artifacts" type="application/json">{payload}</script>
<script>
(function () {{
  const root = document.documentElement;
  try {{
    const artifacts = JSON.parse(document.getElementById("artifacts").textContent);
    const model = globalThis.JevGraphData.normalize(artifacts);
    const pageSize = 100;
    const pageCount = Math.ceil(model.candidates.length / pageSize);
    const page = (number) => model.candidates.slice((number - 1) * pageSize, number * pageSize);
    const first = page(1);
    const last = page(pageCount);
    const connectedGroups = model.groups.filter((group) => group.count > 1);
    const pageOneText = `Page 1 of ${{pageCount}}: ${{first[0].candidateId}} → ${{first.at(-1).candidateId}}`;
    const pageThirtyText = `Page ${{pageCount}} of ${{pageCount}}: ${{last[0].candidateId}} → ${{last.at(-1).candidateId}}`;
    document.getElementById("candidate-count").textContent = `Candidates: ${{model.counts.candidates}}`;
    document.getElementById("page-status").textContent = `${{pageOneText}}; ${{pageThirtyText}}`;
    document.getElementById("page-one").innerHTML = `<ol>${{first.map((item) => `<li>${{item.candidateId}}</li>`).join("")}}</ol>`;
    document.getElementById("page-thirty").innerHTML = `<ol>${{last.map((item) => `<li>${{item.candidateId}}</li>`).join("")}}</ol>`;
    document.getElementById("component-status").textContent = `Disconnected groups: ${{model.groups.length}}; connected groups: ${{connectedGroups.length}}`;
    root.dataset.l6Status = model.errors.length === 0 && model.counts.candidates === 3000 && pageCount === 30 && first[0].candidateId === "fact-0000" && last.at(-1).candidateId === "unresolved-0999" && connectedGroups.length >= 2 ? "PASS" : "FAIL";
    root.dataset.candidateCount = String(model.counts.candidates);
    root.dataset.pageOne = first[0].candidateId;
    root.dataset.pageThirty = last.at(-1).candidateId;
    root.dataset.connectedGroups = String(connectedGroups.length);
  }} catch (error) {{
    root.dataset.l6Status = "FAIL";
    root.dataset.l6Error = String(error);
  }}
}})();
</script>
</body></html>
""",
        encoding="utf-8",
    )
    port_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    port_socket.bind(("127.0.0.1", 0))
    port = port_socket.getsockname()[1]
    port_socket.close()
    profile = browser_dir / "profile"
    command = (
        "open",
        "-na",
        "Google Chrome" if "Google Chrome" in str(browser) else "Chromium",
        "--args",
        "--headless",
        "--no-sandbox",
        "--no-startup-window",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--allow-file-access-from-files",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--disable-extensions",
        "--disable-breakpad",
        "--disable-crash-reporter",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-proxy-server",
    )
    launcher = None
    browser_socket = None
    page_socket = None
    try:
        launcher = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        version_url = f"http://127.0.0.1:{port}/json/version"
        deadline = time.monotonic() + 20
        version = None
        while time.monotonic() < deadline:
            try:
                version = local_json(version_url)
                break
            except (OSError, ValueError):
                time.sleep(0.1)
        if not isinstance(version, dict) or not isinstance(version.get("webSocketDebuggerUrl"), str):
            stderr = launcher.stderr.read() if launcher.stderr else ""
            raise AssertionError(f"installed Chrome/Chromium browser probe did not start on loopback: {browser}\n{stderr[-4000:]}")
        browser_socket = CdpWebSocket(version["webSocketDebuggerUrl"])
        created = browser_socket.command("Target.createTarget", {"url": "about:blank"})
        target_id = created["result"]["targetId"]
        target = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            target_list = local_json(f"http://127.0.0.1:{port}/json/list")
            target = next((item for item in target_list if item.get("id") == target_id), None)
            if target and isinstance(target.get("webSocketDebuggerUrl"), str):
                break
            time.sleep(0.1)
        if not target or not isinstance(target.get("webSocketDebuggerUrl"), str):
            raise AssertionError("installed Chrome/Chromium browser probe did not expose its local page target")
        page_socket = CdpWebSocket(target["webSocketDebuggerUrl"])
        page_socket.command("Page.navigate", {"url": html_path.resolve().as_uri()})
        dom = ""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            evaluated = page_socket.command(
                "Runtime.evaluate",
                {"expression": "document.documentElement.outerHTML", "returnByValue": True},
            )
            dom = evaluated["result"]["result"].get("value", "")
            if 'data-l6-status="PASS"' in dom or 'data-l6-status="FAIL"' in dom:
                break
            time.sleep(0.1)
    except (OSError, KeyError, TypeError, ValueError, AssertionError) as exc:
        raise AssertionError(f"installed Chrome/Chromium browser probe failed: {browser}: {exc}\nDOM tail:\n{dom[-4000:]}") from exc
    finally:
        if page_socket is not None:
            page_socket.close()
        if browser_socket is not None:
            try:
                browser_socket.command("Browser.close")
            except Exception:
                pass
            browser_socket.close()
        if launcher is not None:
            try:
                launcher.wait(timeout=5)
            except subprocess.TimeoutExpired:
                launcher.terminate()
                launcher.wait(timeout=5)
            finally:
                if launcher.stdout is not None:
                    launcher.stdout.close()
                if launcher.stderr is not None:
                    launcher.stderr.close()
    if 'data-l6-status="PASS"' not in dom:
        raise AssertionError(
            f"installed Chrome/Chromium browser probe reported FAIL: {browser}\nDOM tail:\n{dom[-4000:]}"
        )
    attributes = {}
    for name in ("data-candidate-count", "data-page-one", "data-page-thirty", "data-connected-groups"):
        match = re.search(rf'{name}="([^"]+)"', dom)
        if match is None:
            raise AssertionError(f"installed Chrome/Chromium browser probe omitted DOM attribute: {name}")
        attributes[name] = match.group(1)
    if attributes != {
        "data-candidate-count": "3000",
        "data-page-one": "fact-0000",
        "data-page-thirty": "unresolved-0999",
        "data-connected-groups": attributes["data-connected-groups"],
    } or int(attributes["data-connected-groups"]) < 2:
        raise AssertionError(f"installed Chrome/Chromium browser probe returned unexpected DOM attributes: {attributes}")
    if "Page 1 of 30" not in dom or "Page 30 of 30" not in dom or "Disconnected groups:" not in dom:
        raise AssertionError("installed Chrome/Chromium browser probe did not render required DOM evidence")
    return f"PASS ({browser})"


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

            browser = find_headless_browser()
            if browser is None:
                browser_evidence = "NOT RUN (no Chrome/Chromium binary installed)"
            else:
                browser_evidence = run_browser_probe(browser, inventory, candidates, relations, output)
            print(f"L6 browser probe: {browser_evidence}")

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
