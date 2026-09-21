from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import JgError
from .questions import LIKELY_FALSE, LIKELY_TRUE, QUESTION_IDS, QUESTION_VERSION
from .safety import digest, read_json, write_json


def _prediction(value: Any) -> bool | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if value >= LIKELY_TRUE:
        return True
    if value <= LIKELY_FALSE:
        return False
    return None


def build_calibration(labels_path: str | Path, relations_path: str | Path) -> dict[str, Any]:
    labels = read_json(labels_path)
    relations = read_json(relations_path)
    if labels.get("kind") != "relationship-labels" or labels.get("schema_version") != 1:
        raise JgError("labels must use relationship-labels schema version 1")
    if not isinstance(labels.get("labels"), list):
        raise JgError("labels must contain labels[]")
    expected_digest = labels.get("candidate_content_digest")
    actual_digest = relations.get("candidate_content_digest")
    if expected_digest and actual_digest and expected_digest != actual_digest:
        raise JgError("labels and relations refer to different candidate artifacts")

    latest: dict[str, dict[str, Any]] = {}
    for relation in relations.get("relations", []):
        if not isinstance(relation, dict) or relation.get("question_version") != QUESTION_VERSION:
            continue
        candidate_id = relation.get("candidate_id")
        if isinstance(candidate_id, str):
            latest[candidate_id] = relation

    metrics = {question_id: {"labeled": 0, "correct": 0, "incorrect": 0, "unknown": 0} for question_id in QUESTION_IDS}
    rows = []
    seen = set()
    for item in labels["labels"]:
        if not isinstance(item, dict) or not isinstance(item.get("candidate_id"), str) or not isinstance(item.get("answers"), dict):
            raise JgError("each label must contain candidate_id and answers")
        candidate_id = item["candidate_id"]
        if candidate_id in seen:
            raise JgError(f"duplicate calibration label: {candidate_id}")
        seen.add(candidate_id)
        relation = latest.get(candidate_id)
        response_answers = relation.get("response", {}).get("answers", {}) if relation else {}
        row = {"candidate_id": candidate_id, "questions": {}}
        for question_id, expected in item["answers"].items():
            if question_id not in metrics or expected not in (True, False, None):
                raise JgError(f"invalid calibration answer for {candidate_id}: {question_id}")
            if expected is None:
                continue
            metric = metrics[question_id]
            metric["labeled"] += 1
            answer = response_answers.get(question_id, {})
            predicted = _prediction(answer.get("noul") if isinstance(answer, dict) else None)
            outcome = "unknown" if predicted is None else "correct" if predicted is expected else "incorrect"
            metric[outcome] += 1
            row["questions"][question_id] = {"expected": expected, "predicted": predicted, "outcome": outcome}
        rows.append(row)

    totals = {key: sum(metric[key] for metric in metrics.values()) for key in ("labeled", "correct", "incorrect", "unknown")}
    return {
        "kind": "relationship-calibration",
        "schema_version": 1,
        "question_version": QUESTION_VERSION,
        "thresholds": {"likely_true": LIKELY_TRUE, "likely_false": LIKELY_FALSE},
        "candidate_content_digest": actual_digest or expected_digest,
        "labels_digest": digest(labels),
        "relations_digest": digest(relations),
        "metrics": metrics,
        "totals": totals,
        "rows": rows,
        "network_performed": False,
    }


def write_calibration(labels_path: str | Path, relations_path: str | Path, output: str | Path) -> Path:
    target = Path(output) / "calibration.json"
    write_json(target, build_calibration(labels_path, relations_path))
    return target
