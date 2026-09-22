"""Resume an approved batch plan without retrying uncertain Jev attempts."""

from __future__ import annotations

from pathlib import Path

from .errors import JgError
from .jev import JEV_ENDPOINT, checkpoint_lock, execute_preview
from .safety import canonical_json, digest, read_json


def resume_batches(plan_path: str | Path, approved_plan_sha256: str, *, max_requests: int, max_payload_bytes: int,
                   max_total_requests: int) -> dict[str, int]:
    plan_path = Path(plan_path).resolve()
    plan = read_json(plan_path)
    if plan.get("kind") != "jev-batch-plan" or digest(plan) != approved_plan_sha256:
        raise JgError("batch plan digest does not match the approved plan")
    batches = plan.get("batches")
    if not isinstance(batches, list) or max_requests < 1 or max_payload_bytes < 1 or max_total_requests < 1:
        raise JgError("invalid batch plan or request budget")
    summary = {"batches": len(batches), "new_attempts": 0, "succeeded": 0, "uncertain": 0}
    checked = []
    outstanding = 0
    for batch in batches:
        name = batch.get("directory")
        if not isinstance(name, str):
            raise JgError("batch plan has an invalid directory")
        directory = (plan_path.parent / name).resolve()
        if directory.parent != plan_path.parent:
            raise JgError("batch directory escapes the plan directory")
        preview_path = directory / "jev-preview.json"
        preview = read_json(preview_path)
        if (
            preview.get("kind") != "jev-preview"
            or preview.get("endpoint") != JEV_ENDPOINT
            or preview.get("network_performed") is not False
            or preview.get("payload_sha256") != batch.get("payload_sha256")
            or preview.get("request_count") != batch.get("request_count")
            or preview.get("payload_bytes") != batch.get("payload_bytes")
            or not isinstance(preview.get("requests"), list)
            or digest(preview["requests"]) != batch.get("payload_sha256")
            or len(canonical_json(preview["requests"])) != batch.get("payload_bytes")
        ):
            raise JgError("batch preview does not match the approved plan")
        if preview["request_count"] > max_requests or preview["payload_bytes"] > max_payload_bytes:
            raise JgError("batch exceeds approved request or byte budget")
        checkpoint = directory / "relations.json"
        previous = read_json(checkpoint) if checkpoint.exists() else {"attempts": []}
        attempted = {item.get("request_sha256") for item in previous.get("attempts", [])}
        outstanding += sum(digest(request) not in attempted for request in preview["requests"])
        checked.append((batch, preview_path, checkpoint))
    if outstanding > max_total_requests:
        raise JgError("remaining requests exceed approved total request budget")
    for batch, preview_path, checkpoint in checked:
        with checkpoint_lock(checkpoint):
            previous = read_json(checkpoint) if checkpoint.exists() else {"attempts": []}
            prior_count = len(previous.get("attempts", []))
            result = execute_preview(
                preview_path, batch["payload_sha256"],
                max_requests=max_requests, max_payload_bytes=max_payload_bytes,
                checkpoint=checkpoint,
            )
        summary["new_attempts"] += len(result["attempts"]) - prior_count
        summary["succeeded"] += sum(item.get("status") == "succeeded" for item in result["attempts"])
        summary["uncertain"] += sum(item.get("status") == "uncertain" for item in result["attempts"])
    return summary
