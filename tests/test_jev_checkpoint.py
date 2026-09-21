import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev_git_graph.errors import JgError
from jev_git_graph.jev import JEV_ENDPOINT, checkpoint_lock, execute_preview
from jev_git_graph.safety import canonical_json, digest, read_json, write_json


class CheckpointTests(unittest.TestCase):
    def test_resume_preserves_success_and_never_repeats_uncertain_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests = [{"state": {"candidate_id": str(i)}, "model": "jev-latest", "questions": {"test": {"type": "noul", "instructions": "synthetic"}}} for i in range(3)]
            sha = digest(requests)
            preview = root / "preview.json"
            checkpoint = root / "relations.json"
            write_json(preview, {
                "kind": "jev-preview", "endpoint": JEV_ENDPOINT,
                "network_performed": False, "requests": requests,
                "payload_sha256": sha, "request_count": 3,
                "payload_bytes": len(canonical_json(requests)),
            })
            called = []

            def transport(request, token):
                candidate = request["state"]["candidate_id"]
                called.append(candidate)
                self.assertEqual(read_json(checkpoint)["attempts"][-1]["status"], "uncertain")
                if candidate == "1":
                    raise RuntimeError("sensitive error must not be saved")
                return {"model": "jev-latest", "usage": {"input_tokens": 10, "output_tokens": 1}, "answers": {"test": {"noul": .5}}}

            with patch.dict(os.environ, {"TYPESAFE_API_KEY": "synthetic-test-only"}):
                with checkpoint_lock(checkpoint):
                    with self.assertRaises(JgError):
                        execute_preview(preview, sha, max_requests=3, transport=transport, checkpoint=checkpoint)
                self.assertEqual(len(read_json(checkpoint)["relations"]), 1)
                with checkpoint_lock(checkpoint):
                    result = execute_preview(preview, sha, max_requests=3, transport=transport, checkpoint=checkpoint)
            self.assertEqual(called, ["0", "1", "2"])
            self.assertEqual(len(result["relations"]), 2)
            self.assertEqual(result["attempts"][1]["status"], "uncertain")
            self.assertEqual(result["counts"], {"attempted": 3, "succeeded": 2, "uncertain": 1})
            self.assertEqual(result["statistics"]["input_tokens"], 20)
            self.assertIsInstance(result["attempts"][0]["latency_ms"], int)
            self.assertEqual(result["attempts"][1]["error_class"], "transport_error")
            self.assertNotIn("sensitive error", checkpoint.read_text())
            self.assertEqual(checkpoint.stat().st_mode & 0o777, 0o600)

    def test_concurrent_checkpoint_writer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "relations.json"
            with checkpoint_lock(checkpoint):
                with self.assertRaises(JgError):
                    with checkpoint_lock(checkpoint):
                        self.fail("second writer acquired lock")

    def test_invalid_provider_response_is_uncertain_and_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests = [{"state": {"candidate_id": "pair"}, "model": "jev-latest", "questions": {"test": {"type": "noul", "instructions": "synthetic"}}}]
            sha = digest(requests)
            preview = root / "preview.json"
            checkpoint = root / "relations.json"
            write_json(preview, {"kind": "jev-preview", "endpoint": JEV_ENDPOINT, "network_performed": False,
                       "requests": requests, "payload_sha256": sha, "request_count": 1,
                       "payload_bytes": len(canonical_json(requests))})
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": "synthetic-test-only"}):
                with self.assertRaises(JgError):
                    execute_preview(preview, sha, transport=lambda *_: {"secret": "must-not-persist"}, checkpoint=checkpoint)
            result = read_json(checkpoint)
            self.assertEqual("response_validation_error", result["attempts"][0]["error_class"])
            self.assertNotIn("must-not-persist", checkpoint.read_text())
