import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev_git_graph.code_evidence import (
    approve_code_batch,
    build_code_evidence,
    build_transient_preview,
    code_evidence_digest,
    revalidate_code_evidence,
    sanitize_provider_response,
    scan_sensitive_content,
)
from jev_git_graph.errors import JgError
from jev_git_graph.jev import JEV_ENDPOINT, execute_transient_preview, payload_for_candidate, write_preview
from jev_git_graph.safety import digest


def git(root: Path, *args: str) -> str:
    result = subprocess.run(("git", "-C", str(root), *args), check=True, text=True, capture_output=True)
    return result.stdout.strip()


class CodeEvidenceTests(unittest.TestCase):
    def fixture(self) -> tuple[Path, str, str]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "test@example.invalid")
        git(root, "config", "user.name", "Test")
        (root / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
        git(root, "add", "app.py")
        git(root, "commit", "-qm", "main")
        main_tip = git(root, "rev-parse", "HEAD")
        git(root, "switch", "-qc", "source")
        (root / "app.py").write_text("def answer():\n    value = 42\n    return value\n", encoding="utf-8")
        git(root, "commit", "-qam", "source")
        source_tip = git(root, "rev-parse", "HEAD")
        return root, source_tip, main_tip

    def test_builds_bounded_pinned_excerpt_and_revalidates(self):
        root, source_tip, main_tip = self.fixture()
        record = build_code_evidence(
            root, source_tip, main_tip,
            [{"path": "app.py", "start_line": 1, "end_line": 2}],
            source_ref="refs/heads/source", main_ref="refs/heads/main",
        )
        excerpt = record["excerpts"][0]
        self.assertEqual(source_tip, excerpt["source_tip"])
        self.assertEqual(main_tip, excerpt["main_tip"])
        self.assertEqual(1, excerpt["range"]["start_line"])
        self.assertEqual(2, excerpt["range"]["end_line"])
        self.assertRegex(excerpt["source_blob"], r"^[0-9a-f]{40}$")
        self.assertEqual(code_evidence_digest(record), record["evidence_sha256"])
        self.assertEqual(record, revalidate_code_evidence(root, record, record["evidence_sha256"]))
        pinned_record = build_code_evidence(
            root, source_tip, main_tip,
            [{"path": "app.py", "start_line": 1, "end_line": 2}],
        )

        git(root, "switch", "-q", "main")
        git(root, "branch", "-f", "source", main_tip)
        with self.assertRaisesRegex(JgError, "ref moved"):
            revalidate_code_evidence(root, record)
        self.assertEqual(
            pinned_record,
            revalidate_code_evidence(root, pinned_record, pinned_record["evidence_sha256"]),
        )

        changed = json.loads(json.dumps(record))
        changed["excerpts"][0]["text"] = "def answer():\n    return 99\n"
        with self.assertRaisesRegex(JgError, "hash"):
            revalidate_code_evidence(root, changed)

    def test_bounds_and_sensitive_scan_fail_closed_without_network(self):
        root, source_tip, main_tip = self.fixture()
        with self.assertRaisesRegex(JgError, "sensitive directory"):
            build_code_evidence(root, source_tip, main_tip, [{"path": "config.py", "start_line": 1, "end_line": 1}])
        with self.assertRaisesRegex(JgError, "line bound"):
            build_code_evidence(root, source_tip, main_tip, [{"path": "app.py", "start_line": 1, "end_line": 81}])
        (root / "app_secret.py").write_text('password = "do-not-send"\n', encoding="utf-8")
        git(root, "add", "app_secret.py")
        git(root, "commit", "-qm", "secret")
        secret_tip = git(root, "rev-parse", "HEAD")
        self.assertTrue(scan_sensitive_content('token = "sk-1234567890123456"'))
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with self.assertRaisesRegex(JgError, "sensitive"):
                build_code_evidence(root, secret_tip, main_tip, [{"path": "app_secret.py", "start_line": 1, "end_line": 1}])

    def test_transient_preview_requires_batch_digest_and_has_no_store_marker(self):
        root, source_tip, main_tip = self.fixture()
        record = build_code_evidence(root, source_tip, main_tip, [{"path": "app.py", "start_line": 1, "end_line": 1}])
        candidate = {
            "id": "pair",
            "endpoints": {"a": {"tip": source_tip}, "b": {"tip": main_tip}},
            "reasons": [],
            "evidence": {"shared_patch_ids": [], "shared_paths": [], "shared_subject_tokens": [],
                         "a_unique_commit_count": 1, "b_unique_commit_count": 1,
                         "a_merge_base": main_tip, "b_merge_base": main_tip},
        }
        request = payload_for_candidate(candidate, "code", record)
        approval = approve_code_batch(record, [request])
        preview = build_transient_preview({"candidates": [candidate]}, {"pair": record}, approved_batch=approval)
        self.assertTrue(preview["no_store"])
        self.assertEqual("transient", preview["storage"])
        self.assertEqual(approval["payload_sha256"], preview["payload_sha256"])
        with self.assertRaisesRegex(JgError, "transient"):
            write_preview(tempfile.NamedTemporaryFile().name, tempfile.mkdtemp(), "code")

    def test_provider_record_is_strictly_sanitized(self):
        request = {"questions": {
            "one": {"type": "noul"},
            "two": {"type": "choice", "criteria": {"a": "A", "b": "B"}},
        }}
        response = {
            "model": "jev-latest",
            "usage": {"input_tokens": 3, "output_tokens": 2},
            "answers": {"one": {"noul": 0.5}, "two": {
                "choice": "a", "confidence": 0.8, "probabilities": {"a": 0.8, "b": 0.2},
            }},
            "provider_trace": "must-not-persist",
            "raw_text": "secret",
        }
        sanitized = sanitize_provider_response(request, response)
        self.assertNotIn("provider_trace", sanitized)
        self.assertNotIn("raw_text", sanitized)
        self.assertEqual({"model", "usage", "answers"}, set(sanitized))

    def test_in_memory_execution_revalidates_each_request_and_stores_no_raw_response(self):
        root, source_tip, main_tip = self.fixture()
        record = build_code_evidence(root, source_tip, main_tip, [{"path": "app.py", "start_line": 1, "end_line": 1}])
        candidate = {
            "id": "pair", "endpoints": {"a": {"tip": source_tip}, "b": {"tip": main_tip}},
            "reasons": [], "evidence": {"shared_patch_ids": [], "shared_paths": [],
            "shared_subject_tokens": [], "a_unique_commit_count": 1, "b_unique_commit_count": 1,
            "a_merge_base": main_tip, "b_merge_base": main_tip},
        }
        request = payload_for_candidate(candidate, "code", record)
        approval = approve_code_batch(record, [request])
        preview = build_transient_preview({"candidates": [candidate]}, {"pair": record}, approved_batch=approval)

        def transport(current, _token):
            answers = {}
            for question_id, question in current["questions"].items():
                if question["type"] == "noul":
                    answers[question_id] = {"noul": 0.5}
                else:
                    choices = question["criteria"]
                    names = list(choices)
                    answers[question_id] = {
                        "choice": names[0], "confidence": 0.5,
                        "probabilities": {name: (0.5 if i == 0 else 0.0) for i, name in enumerate(names)},
                    }
            return {"model": "jev-latest", "usage": {"input_tokens": 1, "output_tokens": 1},
                    "answers": answers, "raw": "drop me"}

        with patch.dict("os.environ", {"TYPESAFE_API_KEY": "synthetic-test-only"}), patch(
            "jev_git_graph.jev.revalidate_code_evidence", wraps=revalidate_code_evidence
        ) as revalidate:
            result = execute_transient_preview(
                preview,
                preview["payload_sha256"],
                code_evidence_repo=root,
                approved_code_batch_sha256=approval["approval_sha256"],
                transport=transport,
            )
        self.assertEqual(1, revalidate.call_count)
        self.assertNotIn("raw", result["relations"][0]["response"])


if __name__ == "__main__":
    unittest.main()
