from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import JgError
from .questions import LIKELY_FALSE, LIKELY_TRUE, QUESTION_IDS, QUESTION_VERSION
from .safety import digest, read_json, write_json


def _prediction(value: Any) -> bool | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return None
    if value >= LIKELY_TRUE:
        return True
    if value <= LIKELY_FALSE:
        return False
    return None


def _label(value: Any, label: str) -> bool | None:
    if value is None or type(value) is bool:
        return value
    raise JgError(f"{label} must be true, false, or null")


def _relation_timestamp(relation: dict[str, Any]) -> float | None:
    for field in ("completed_at_epoch_ms", "judged_at_epoch_ms", "timestamp_epoch_ms"):
        value = relation.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
    for field in ("completed_at", "judged_at", "timestamp", "created_at"):
        value = relation.get(field)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                else:
                    parsed = parsed.astimezone(UTC)
                return parsed.timestamp() * 1000
            except ValueError:
                continue
    return None


def _relation_identity(relation: dict[str, Any]) -> str | None:
    for field in ("request_sha256", "judgment_id"):
        value = relation.get(field)
        if isinstance(value, str) and value:
            return f"{field}:{value}"
    return None


def _select_latest(relations: list[Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Select one judgment per candidate without using list order as recency."""
    by_candidate: dict[str, dict[str, dict[str, Any]]] = {}
    limitations: list[str] = []
    for index, relation in enumerate(relations):
        if not isinstance(relation, dict) or relation.get("question_version") != QUESTION_VERSION:
            continue
        candidate_id = relation.get("candidate_id")
        if not isinstance(candidate_id, str):
            continue
        identity = _relation_identity(relation) or f"record:{digest(relation)}"
        current = by_candidate.setdefault(candidate_id, {}).get(identity)
        if current is None or (
            (_relation_timestamp(relation) is not None, _relation_timestamp(relation) or float("-inf"), digest(relation))
            > (_relation_timestamp(current) is not None, _relation_timestamp(current) or float("-inf"), digest(current))
        ):
            by_candidate[candidate_id][identity] = relation

    selected: dict[str, dict[str, Any]] = {}
    for candidate_id, identities in by_candidate.items():
        records = list(identities.values())
        if len(records) == 1:
            selected[candidate_id] = records[0]
            continue
        timed = [record for record in records if _relation_timestamp(record) is not None]
        if not timed:
            limitations.append(f"latest_judgment_unknown_without_timestamp:{candidate_id}")
            continue
        selected[candidate_id] = max(
            timed,
            key=lambda record: (_relation_timestamp(record), _relation_identity(record) or "", digest(record)),
        )
    return selected, limitations


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

    relation_records = relations.get("relations", [])
    if not isinstance(relation_records, list):
        raise JgError("relations must contain relations[]")
    latest, selection_limitations = _select_latest(relation_records)

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
        row = {"candidate_id": candidate_id, "questions": {}, "selection_status": "selected" if relation else "unknown"}
        response_evidence = response_answers.get("evidence_sufficient", {})
        row["evidence_sufficiency"] = _prediction(response_evidence.get("noul") if isinstance(response_evidence, dict) else None)
        for question_id, expected in item["answers"].items():
            expected = _label(expected, f"calibration answer for {candidate_id}: {question_id}")
            if question_id not in metrics:
                raise JgError(f"invalid calibration answer for {candidate_id}: {question_id}")
            if expected is None:
                metrics[question_id]["unknown"] += 1
                row["questions"][question_id] = {"expected": None, "predicted": None, "outcome": "unknown"}
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
        "limitations": selection_limitations,
    }


def write_calibration(labels_path: str | Path, relations_path: str | Path, output: str | Path) -> Path:
    target = Path(output) / "calibration.json"
    write_json(target, build_calibration(labels_path, relations_path))
    return target
