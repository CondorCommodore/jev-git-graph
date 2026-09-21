import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jev_git_graph.errors import JgError
from jev_git_graph.jev import (
    JEV_ENDPOINT,
    checkpoint_lock,
    estimate_cost,
    execute_preview,
    parse_pricing,
    update_ledger_statistics,
)
from jev_git_graph.safety import canonical_json, digest, read_json, write_json


class CheckpointTests(unittest.TestCase):
    def test_nonfinite_provider_probability_is_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, invalid in enumerate((float("nan"), float("inf"), float("-inf"))):
                with self.subTest(invalid=repr(invalid)):
                    request = {"state": {"candidate_id": "pair"}, "model": "jev-latest",
                               "questions": {"test": {"type": "noul", "instructions": "synthetic"}}}
                    sha = digest([request])
                    preview = root / f"preview-{index}.json"
                    checkpoint = root / f"relations-{index}.json"
                    write_json(preview, {"kind": "jev-preview", "endpoint": JEV_ENDPOINT, "network_performed": False,
                               "requests": [request], "payload_sha256": sha, "request_count": 1,
                               "payload_bytes": len(canonical_json([request]))})
                    with patch.dict(os.environ, {"TYPESAFE_API_KEY": "synthetic-test-only"}):
                        with self.assertRaises(JgError):
                            execute_preview(preview, sha, transport=lambda *_: {
                                "model": "jev-latest", "usage": {"input_tokens": 1, "output_tokens": 1},
                                "answers": {"test": {"noul": invalid}},
                            }, checkpoint=checkpoint)
                    self.assertEqual("response_validation_error", read_json(checkpoint)["attempts"][0]["error_class"])

    def test_historical_missing_timing_is_null_and_pricing_is_provenanced(self):
        ledger = {"attempts": [{"request_sha256": "a" * 64, "status": "succeeded"}], "relations": []}
        update_ledger_statistics(ledger)
        self.assertIsNone(ledger["statistics"]["api_time_ms"])
        self.assertIsNone(ledger["statistics"]["wall_time_ms"])
        pricing = parse_pricing({
            "model": "jev-latest", "source": "local-test-pricing",
            "input_usd_per_million_tokens": 1.0, "output_usd_per_million_tokens": 2.0,
        })
        quote = estimate_cost(ledger, pricing)
        self.assertTrue(quote["unknown"])
        self.assertIsNone(quote["estimated_cost_usd"])
        self.assertEqual("local-test-pricing", quote["pricing_provenance"]["source"])

    def test_pricing_estimate_is_local_and_deterministic(self):
        ledger = {"attempts": [], "relations": [{"response": {"usage": {"input_tokens": 1000, "output_tokens": 500}}}]}
        update_ledger_statistics(ledger)
        quote = estimate_cost(ledger, {
            "model": "jev-latest", "source": "fixture",
            "rates": {"input_usd_per_1m": 2.0, "output_usd_per_1m": 4.0},
        })
        self.assertEqual(0.004, quote["estimated_cost_usd"])
        self.assertEqual("fixture", quote["pricing_provenance"]["source"])
        with self.assertRaisesRegex(JgError, "finite"):
            parse_pricing({"source": "fixture", "input_usd_per_1m": float("inf"), "output_usd_per_1m": 1.0})

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
