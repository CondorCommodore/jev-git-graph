"""Offline owner-labeled calibration for branch-presence judgments."""

from __future__ import annotations

from typing import Any, Mapping

from .errors import JgError
from .presence import verify_trusted_presence_result
from .safety import digest

CLASSES = ("LIKELY_PRESERVED", "USABLE_WORK_REMAINS", "UNRESOLVED")


def _check_labels(labels: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if labels.get("kind") != "branch-presence-owner-labels" or labels.get("schema_version") != 1:
        raise JgError("labels must use branch-presence-owner-labels schema version 1")
    if labels.get("label_source") != "owner_review" or labels.get("blinded") is not True:
        raise JgError("presence labels require blinded owner review provenance")
    items = labels.get("labels")
    if not isinstance(items, list):
        raise JgError("presence labels must contain labels[]")
    result = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise JgError("each presence label must be an object")
        cid, label = item.get("contribution_id"), item.get("disposition")
        if not isinstance(cid, str) or not cid or cid in result:
            raise JgError("presence labels contain a missing or duplicate contribution_id")
        reviewed = item.get("reviewed")
        if not isinstance(reviewed, bool):
            raise JgError(f"owner label {cid} reviewed must be boolean")
        if reviewed and label not in CLASSES:
            raise JgError(f"invalid owner disposition label for {cid}")
        if not reviewed and label is not None:
            raise JgError(f"unreviewed owner label {cid} must not contain a disposition")
        family = item.get("family_id")
        if not isinstance(family, str) or not family:
            raise JgError(f"owner label {cid} lacks family_id")
        result[cid] = {"disposition": label, "family_id": family, "reviewed": reviewed}
    if any(item["reviewed"] for item in result.values()):
        if not isinstance(labels.get("accepted_by"), str) or not labels["accepted_by"].strip():
            raise JgError("accepted presence labels require accepted_by")
    return result


def _validate_result(result: Mapping[str, Any], labels: Mapping[str, Any], arm: str) -> dict[str, dict[str, Any]]:
    if result.get("kind") != "branch-presence-result" or result.get("schema") != "branch-presence-result-v1":
        raise JgError(f"{arm} result is not a normalized branch-presence result")
    if result.get("presence_digest") != digest({key: value for key, value in result.items() if key != "presence_digest"}):
        raise JgError(f"{arm} result digest is invalid")
    expected_origin = "jev" if arm == "JeV" else "control"
    if result.get("origin") != expected_origin:
        raise JgError(f"{arm} result has the wrong origin")
    if arm == "JeV" and not verify_trusted_presence_result(result):
        raise JgError("JeV calibration result lacks trusted executor provenance")
    for field in ("snapshot_digest", "contributions_digest", "groups_digest"):
        if result.get(field) != labels.get(field):
            raise JgError(f"{arm} result and owner labels differ at {field}")
    rows = result.get("contributions")
    if not isinstance(rows, list):
        raise JgError(f"{arm} result lacks contributions")
    indexed = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise JgError(f"{arm} result contains a malformed contribution")
        cid = row.get("contribution_id")
        if not isinstance(cid, str) or cid in indexed:
            raise JgError(f"{arm} result has duplicate or missing contribution IDs")
        if row.get("disposition") not in CLASSES:
            raise JgError(f"{arm} result has an invalid disposition")
        indexed[cid] = dict(row)
    return indexed


def _operational_stats(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    metadata = metadata or {}
    def nonnegative(name: str) -> float | None:
        value = metadata.get(name)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None
    return {"calls": nonnegative("calls"), "input_tokens": nonnegative("input_tokens"),
            "output_tokens": nonnegative("output_tokens"), "cost_usd": nonnegative("cost_usd"),
            "wall_time_seconds": nonnegative("wall_time_seconds"),
            "review_time_seconds": nonnegative("review_time_seconds")}


def _score(labels: Mapping[str, dict[str, Any]], predictions: Mapping[str, dict[str, Any]],
           metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    confusion = {actual: {predicted: 0 for predicted in CLASSES} for actual in CLASSES}
    rows, abstentions, incorrect_preservation = [], 0, 0
    selected = sorted(set(labels) & set(predictions))
    for cid in selected:
        actual = labels[cid]["disposition"]
        predicted = predictions[cid]["disposition"]
        abstain = predicted == "UNRESOLVED"
        abstentions += int(abstain)
        confusion[actual][predicted] += 1
        wrong_preservation = predicted == "LIKELY_PRESERVED" and actual != "LIKELY_PRESERVED"
        incorrect_preservation += int(wrong_preservation)
        rows.append({"contribution_id": cid, "family_id": labels[cid]["family_id"],
                     "expected": actual, "predicted": predicted,
                     "abstained": abstain, "incorrect_preservation_claim": wrong_preservation})
    per_class = {}
    for label in CLASSES:
        tp = confusion[label][label]
        fp = sum(confusion[actual][label] for actual in CLASSES if actual != label)
        fn = sum(confusion[label][predicted] for predicted in CLASSES if predicted != label)
        per_class[label] = {"support": sum(confusion[label].values()),
                            "predicted": sum(confusion[actual][label] for actual in CLASSES),
                            "precision": tp / (tp + fp) if tp + fp else None,
                            "recall": tp / (tp + fn) if tp + fn else None}
    correct = sum(confusion[label][label] for label in CLASSES)
    return {"matched_labeled": len(selected), "correct": correct,
            "accuracy": correct / len(selected) if selected else None,
            "abstentions": abstentions,
            "abstention_rate": abstentions / len(selected) if selected else None,
            "incorrect_preservation_claims": incorrect_preservation,
            "confusion": confusion, "per_class": per_class,
            "operational": _operational_stats(metadata), "rows": rows}


def build_presence_calibration(
    labels: Mapping[str, Any],
    jev_result: Mapping[str, Any],
    control_result: Mapping[str, Any],
    *,
    jev_metadata: Mapping[str, Any] | None = None,
    control_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score Jev and blind control on the identical pinned owner-labeled IDs."""
    all_labels = _check_labels(labels)
    label_index = {cid: item for cid, item in all_labels.items() if item["reviewed"]}
    if not label_index:
        raise JgError("presence calibration requires at least one accepted owner label")
    jev_predictions = _validate_result(jev_result, labels, "JeV")
    control_predictions = _validate_result(control_result, labels, "control")
    shared = sorted(set(label_index) & set(jev_predictions) & set(control_predictions))
    subset_labels = {cid: label_index[cid] for cid in shared}
    jev_shared = {cid: jev_predictions[cid] for cid in shared}
    control_shared = {cid: control_predictions[cid] for cid in shared}
    result = {"kind": "branch-presence-calibration", "schema_version": 1,
              "question_version": "branch-presence-v1",
              "labels_digest": digest(labels), "snapshot_digest": labels.get("snapshot_digest"),
              "contributions_digest": labels.get("contributions_digest"),
              "groups_digest": labels.get("groups_digest"),
              "matched_contribution_ids": shared,
              "labels": {"count": len(shared), "unreviewed_excluded": len(all_labels) - len(label_index),
                         "accepted_by": labels.get("accepted_by"),
                         "label_source": "owner_review", "blinded": True},
              "arms": {"jev": _score(subset_labels, jev_shared, jev_metadata),
                       "control": _score(subset_labels, control_shared, control_metadata)},
              "network_performed": False,
              "limitations": ["small samples cannot establish rare-error safety",
                               "owner labels are reference judgments, not absolute ground truth"]}
    result["calibration_digest"] = digest(result)
    return result
