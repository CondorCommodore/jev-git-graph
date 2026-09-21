import tempfile
import unittest
from pathlib import Path

from jev_git_graph.calibration import build_calibration, write_calibration
from jev_git_graph.errors import JgError
from jev_git_graph.questions import QUESTION_IDS, QUESTION_VERSION
from jev_git_graph.safety import read_json, write_json


class CalibrationTests(unittest.TestCase):
    def test_labels_require_strict_bool_or_null_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "labels.json", {
                "kind": "relationship-labels", "schema_version": 1,
                "candidate_content_digest": "abc",
                "labels": [{"candidate_id": "pair-1", "answers": {"same_intent": 1}}],
            })
            write_json(root / "relations.json", {"candidate_content_digest": "abc", "relations": []})
            with self.assertRaisesRegex(JgError, "true, false, or null"):
                build_calibration(root / "labels.json", root / "relations.json")

    def test_latest_judgment_uses_timestamp_and_identity_not_list_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "labels.json", {
                "kind": "relationship-labels", "schema_version": 1,
                "candidate_content_digest": "abc",
                "labels": [{"candidate_id": "pair-1", "answers": {
                    "same_intent": False, "evidence_sufficient": True, "partial_overlap": None,
                }}],
            })
            answers_old = {"same_intent": {"noul": 0.9}, "evidence_sufficient": {"noul": 0.9}}
            answers_new = {"same_intent": {"noul": 0.1}, "evidence_sufficient": {"noul": 0.9}}
            write_json(root / "relations.json", {"candidate_content_digest": "abc", "relations": [
                {"candidate_id": "pair-1", "request_sha256": "b" * 64, "judgment_id": "new",
                 "completed_at_epoch_ms": 200, "question_version": QUESTION_VERSION,
                 "response": {"answers": answers_new}},
                {"candidate_id": "pair-1", "request_sha256": "b" * 64, "judgment_id": "old",
                 "completed_at_epoch_ms": 100, "question_version": QUESTION_VERSION,
                 "response": {"answers": answers_old}},
            ]})
            result = build_calibration(root / "labels.json", root / "relations.json")
            self.assertEqual("correct", result["rows"][0]["questions"]["same_intent"]["outcome"])
            self.assertEqual(True, result["rows"][0]["evidence_sufficiency"])
            self.assertEqual("unknown", result["rows"][0]["questions"]["partial_overlap"]["outcome"])
            self.assertEqual(1, result["metrics"]["evidence_sufficient"]["correct"])

    def test_scores_v3_answers_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            answers = {key: {"noul": 0.5} for key in QUESTION_IDS}
            answers["same_intent"] = {"noul": 0.9}
            answers["partial_overlap"] = {"noul": 0.1}
            write_json(root / "labels.json", {
                "kind": "relationship-labels", "schema_version": 1,
                "candidate_content_digest": "abc",
                "labels": [{"candidate_id": "pair-1", "answers": {"same_intent": True, "partial_overlap": True}}],
            })
            write_json(root / "relations.json", {
                "candidate_content_digest": "abc",
                "relations": [{"candidate_id": "pair-1", "question_version": QUESTION_VERSION,
                               "response": {"answers": answers}}],
            })
            result = build_calibration(root / "labels.json", root / "relations.json")
            self.assertEqual({"labeled": 2, "correct": 1, "incorrect": 1, "unknown": 0}, result["totals"])
            self.assertFalse(result["network_performed"])
            target = write_calibration(root / "labels.json", root / "relations.json", root / "out")
            self.assertEqual("relationship-calibration", read_json(target)["kind"])

    def test_rejects_mismatched_candidate_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "labels.json", {"kind": "relationship-labels", "schema_version": 1,
                                               "candidate_content_digest": "one", "labels": []})
            write_json(root / "relations.json", {"candidate_content_digest": "two", "relations": []})
            with self.assertRaisesRegex(JgError, "different candidate"):
                build_calibration(root / "labels.json", root / "relations.json")
