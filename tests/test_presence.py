from __future__ import annotations

import base64
import copy
import hashlib
import subprocess
import threading
from types import SimpleNamespace

import pytest

import jev_git_graph.presence as presence_module
from jev_git_graph.errors import JgError
from jev_git_graph.group_requests import (
    approved_presence_preview,
    build_group_requests,
    build_source_only_evidence,
    build_two_sided_evidence,
    revalidate_two_sided_evidence,
)
from jev_git_graph.groups import build_groups
from jev_git_graph.presence import (
    execute_presence_preview,
    import_synthetic_answers,
    reconcile_presence,
    validate_outcome_presence,
)
from jev_git_graph.presence_calibration import build_presence_calibration
from jev_git_graph.questions import presence_questions
from jev_git_graph.safety import canonical_json, digest, read_json, write_json
from jev_git_graph.outcomes import approve_outcome_review, build_outcomes
from jev_git_graph.preservation import build_preservation_plan, write_preservation_plan


def _artifact():
    contribution = {
        "kind": "contributions", "schema_version": 1, "repository_id": "fixture",
        "snapshot_digest": "a" * 64, "main": {"name": "main", "tip": "b" * 40},
        "branches": [{"name": "feature/task", "tip": "c" * 40, "eligible": True,
                       "unit_ids": ["cu-1"], "exclusion_reasons": []}],
        "units": [{"id": "cu-1", "source_tip": "c" * 40, "main_tip": "b" * 40,
                    "path": "src.py", "source_blob": "d" * 40, "kind": "python_definition",
                    "name": "work", "range": {"start_line": 1, "end_line": 2},
                    "source": {"branch": "feature/task", "path": "src.py", "blob": "d" * 40},
                    "destination_ids": ["du-1"], "limitations": []}],
        "destination_units": [{"id": "du-1", "path": "dst.py", "blob": "e" * 40,
                               "mode": "100644", "kind": "python_definition",
                               "name": "work", "range": {"start_line": 1, "end_line": 2}}],
        "edges": [], "paths": [], "limitations": [],
    }
    contribution["contributions_digest"] = digest(contribution)
    groups = build_groups(contribution)
    return contribution, groups


def _response(request, presence="PRESENT", sufficient=0.95, delta=0.05,
              dependencies_sufficient=0.95, project_relevance=0.95):
    answers = {}
    for qid, question in request["questions"].items():
        if question["type"] == "noul":
            if qid.endswith(":evidence_sufficient"):
                value = sufficient
            elif qid.endswith(":dependency_context_sufficient"):
                value = dependencies_sufficient
            elif qid.endswith(":project_relevance"):
                value = project_relevance
            else:
                value = delta
            answers[qid] = {"noul": value}
        else:
            answers[qid] = {
                "choice": presence, "confidence": 0.9,
                "probabilities": {choice: 0.9 if choice == presence else 0.1 / 3
                                  for choice in ("PRESENT", "PARTIAL", "ABSENT", "UNKNOWN")},
            }
    return {"model": "offline-fixture", "usage": {"input_tokens": 11, "output_tokens": 5},
            "answers": answers}


def _synthetic_result(contributions, groups, plan, choices):
    preview = approved_presence_preview(plan)
    records = [{"request_sha256": digest(request), "response": _response(request, **choice)}
               for request, choice in zip(preview["requests"], choices)]
    artifact = import_synthetic_answers(preview, records)
    return preview, reconcile_presence(contributions, groups, artifact)


def _add_context_contract(plan, *, comparison_complete=True, dependency_status="complete"):
    """Exercise new per-unit context metadata without depending on group builder changes."""
    for request in plan["requests"]:
        state = request["state"]
        rebuilt = {}
        for item in state["contributions"]:
            item["comparison_context_complete"] = comparison_complete
            item["comparison_context_limitations"] = [] if comparison_complete else ["range_missing"]
            item["dependency_context_status"] = dependency_status
            item["dependency_context_limitations"] = [] if dependency_status == "complete" else ["references_not_extracted"]
            cid = item["contribution_id"]
            for question_id, question in presence_questions(
                cid, item.get("dependency_edges", []), dependency_context_status=dependency_status,
            ).items():
                rebuilt[f"{cid}:{question_id}"] = question
        request["questions"] = rebuilt


def _expand_approved_preview(preview, count):
    """Create a multi-request approved fixture without changing request semantics."""
    requests = []
    for index in range(count):
        request = copy.deepcopy(preview["requests"][0])
        request["state"]["group_id"] = f"fixture-group-{index}"
        requests.append(request)
    preview = copy.deepcopy(preview)
    preview["requests"] = requests
    preview["request_count"] = count
    budgets = dict(preview["request_budgets"])
    budgets.update({"max_requests": max(count, budgets["max_requests"]),
                    "max_groups": max(count, budgets["max_groups"]),
                    "estimated_input_tokens": max(count * 100, budgets["estimated_input_tokens"]),
                    "max_provider_tokens": max(count * 100, budgets["max_provider_tokens"])})
    preview["request_budgets"] = budgets
    payload = canonical_json(requests)
    preview["payload_sha256"] = digest(requests)
    preview["payload_bytes"] = len(payload)
    preview["request_bytes_base64"] = base64.b64encode(payload).decode("ascii")
    preview["request_bytes_by_chunk"] = [len(canonical_json(request)) for request in requests]
    preview["approval_sha256"] = digest({"payload_sha256": preview["payload_sha256"],
                                         "plan_digest": preview["plan_digest"],
                                         "request_count": count,
                                         "request_budgets": budgets})
    return preview


def test_synthetic_answers_are_advisory_and_origin_cannot_be_overridden():
    contributions, groups = _artifact()
    plan = build_group_requests(contributions, groups)
    preview, result = _synthetic_result(contributions, groups, plan, [{}])

    assert result["contributions"][0]["disposition"] == "UNRESOLVED"
    assert result["contributions"][0]["comparison_context_complete"] is False
    assert "comparison_context_incomplete" in result["contributions"][0]["reasons"]
    assert result["contributions"][0]["routing_scope"] == "advisory_only"
    assert validate_outcome_presence(result, contributions) == {}
    with pytest.raises(JgError, match="origin override"):
        reconcile_presence(contributions, groups,
                           import_synthetic_answers(preview, [{"request_sha256": digest(preview["requests"][0]),
                                                              "response": _response(preview["requests"][0])}]),
                           origin="jev")


def test_project_purpose_is_pinned_scanned_and_separate_from_presence():
    contributions, groups = _artifact()
    without_goals = build_group_requests(contributions, groups)
    empty_goals = build_group_requests(contributions, groups, project_goals="")
    assert without_goals["requests"] == empty_goals["requests"]

    goals = "Keep interactive examples safe and accessible."
    plan = build_group_requests(contributions, groups, project_goals=goals)
    request = plan["requests"][0]
    purpose = request["state"]["project_purpose"]
    assert purpose == {
        "text": goals, "sha256": hashlib.sha256(goals.encode("utf-8")).hexdigest(),
        "version": "project-purpose-v1",
    }
    assert "cu-1:project_relevance" in request["questions"]
    assert request["questions"]["cu-1:usable_delta"] == without_goals["requests"][0]["questions"]["cu-1:usable_delta"]
    assert request["questions"]["cu-1:project_relevance"]["scope_limits"]["permitted_judgment"] == \
        "advisory_project_relevance_to_supplied_purpose"

    with pytest.raises(JgError, match="sensitive material"):
        build_group_requests(contributions, groups, project_goals="password='fake-secret-value-123'")
    with pytest.raises(JgError, match="UTF-8 limit"):
        build_group_requests(contributions, groups, project_goals="é" * 2_001)

    preview = approved_presence_preview(plan)
    imported = import_synthetic_answers(preview, [{
        "request_sha256": digest(preview["requests"][0]),
        "response": _response(preview["requests"][0], project_relevance=0.95),
    }])
    result = reconcile_presence(contributions, groups, imported)
    row = result["contributions"][0]
    assert row["project_relevance"] is True
    assert row["project_goal_digest"] == purpose["sha256"]
    assert row["project_goal_version"] == "project-purpose-v1"
    assert row["disposition"] == "UNRESOLVED"
    assert row["usable_delta"] is None
    assert row["routing_scope"] == "advisory_only"
    assert result["project_utility_assessment"]["status"] == "UNKNOWN"
    assert result["project_utility_assessment"]["reason"] == "comparison_or_source_evidence_insufficient"

    from jev_git_graph import cli
    study = {"kind": "presence-study", "schema_version": 2,
             "snapshot_digest": contributions["snapshot_digest"],
             "contributions_digest": contributions["contributions_digest"],
             "groups_digest": groups["groups_digest"], "project_goals": goals,
             "selection_policy": "fixture", "cases": [{"contribution_id": "cu-1"}]}
    study["study_digest"] = digest(study)
    snapshot = {"snapshot_digest": contributions["snapshot_digest"]}
    inputs = {"contributions.json": contributions, "groups.json": groups, "study.json": study}
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(cli, "load_snapshot", lambda _path: (snapshot, None))
        monkeypatch.setattr(cli, "read_json", lambda path: inputs[str(path)])
        _repo, _ranges, replay, cli_preview = cli._build_group_presence_preview(SimpleNamespace(
            snapshot="snapshot.json", contributions="contributions.json", groups="groups.json",
            study="study.json", evidence_ranges=None, model_settings=None, max_groups=64,
            max_requests=64, max_request_bytes=64_000, auto_estimate_input_tokens=False,
            estimated_input_tokens=None, max_provider_tokens=None))
    finally:
        monkeypatch.undo()
    cli_purpose = cli_preview["requests"][0]["state"]["project_purpose"]
    assert cli_purpose["sha256"] == purpose["sha256"]
    assert replay["project_goal_sha256"] == purpose["sha256"]
    assert replay["project_goal_version"] == "project-purpose-v1"
    assert goals not in canonical_json(replay).decode("utf-8")


def test_project_relevance_summary_rejects_mixed_goal_contexts():
    assessment = presence_module._project_utility_assessment([
        {"project_goal_digest": "a" * 64, "project_goal_version": "project-purpose-v1",
         "project_relevance": True, "project_goal_context_conflict": False,
         "evidence_sufficient": True, "comparison_context_complete": True},
        {"project_goal_digest": "b" * 64, "project_goal_version": "project-purpose-v1",
         "project_relevance": False, "project_goal_context_conflict": False,
         "evidence_sufficient": True, "comparison_context_complete": True},
    ])
    assert assessment == {
        "status": "UNKNOWN", "reason": "project_goal_context_conflict",
        "source": "typed_project_relevance_review",
    }


def test_injected_executor_is_synthetic_and_public_origin_spoof_fails_closed(tmp_path, monkeypatch):
    contributions, groups = _artifact()
    plan = build_group_requests(contributions, groups, estimated_input_tokens=100, max_provider_tokens=1000)
    _add_context_contract(plan)
    preview = approved_presence_preview(plan)
    monkeypatch.setattr(presence_module, "_presence_key_path", lambda: tmp_path / "private" / "key")

    def injected(payload, token):
        response = _response(payload)
        response["model"] = payload["model"]
        return response

    executed = execute_presence_preview(
        preview, preview["payload_sha256"],
        approved_approval_sha256=preview["approval_sha256"],
        transport=injected, token="fixture-only", checkpoint=tmp_path / "checkpoint.json",
    )
    assert executed["origin"] == "synthetic"
    advisory = reconcile_presence(contributions, groups, executed)
    assert advisory["project_utility_assessment"] == {
        "status": "UNKNOWN", "reason": "project_requirements_not_provided",
        "source": "code_only_presence_review",
    }
    assert advisory["contributions"][0]["routing_scope"] == "advisory_only"
    assert validate_outcome_presence(advisory, contributions) == {}

    spoofed = {**executed, "origin": "jev", "executor": "pooled-jev-sdk-v1"}
    spoofed["answers_digest"] = digest({key: value for key, value in spoofed.items()
                                        if key not in {"answers_digest", "execution_receipt"}})
    with pytest.raises(JgError, match="separate trusted executor receipt"):
        reconcile_presence(contributions, groups, spoofed)


def test_trusted_sdk_receipt_binds_answers_and_reconciled_outcome(tmp_path, monkeypatch):
    contributions, groups = _artifact()
    plan = build_group_requests(contributions, groups, estimated_input_tokens=100, max_provider_tokens=1000)
    _add_context_contract(plan)
    preview = approved_presence_preview(plan)
    monkeypatch.setattr(presence_module, "_presence_key_path", lambda: tmp_path / "private" / "key")

    def fixture_sdk(payload, token):
        response = _response(payload)
        response["model"] = payload["model"]
        return response

    monkeypatch.setattr(presence_module, "_default_transport", fixture_sdk)
    receipt_path = tmp_path / "receipt.json"
    executed = execute_presence_preview(
        preview, preview["payload_sha256"],
        approved_approval_sha256=preview["approval_sha256"],
        token="fixture-only", checkpoint=tmp_path / "checkpoint.json",
        execution_receipt_path=receipt_path,
    )
    assert executed["origin"] == "jev"
    receipt = presence_module.read_json(receipt_path)
    result = reconcile_presence(contributions, groups, executed, execution_receipt=receipt)
    assert result["contributions"][0]["routing_scope"] == "production_review_candidate"
    assert validate_outcome_presence(result, contributions)

    with pytest.raises(JgError, match="separate trusted executor receipt"):
        reconcile_presence(contributions, groups, executed)

    changed = {**executed, "answers": [dict(executed["answers"][0])]}
    changed["answers"][0]["group_id"] = "changed"
    changed["answers_digest"] = digest({key: value for key, value in changed.items()
                                        if key not in {"answers_digest", "execution_receipt"}})
    with pytest.raises(JgError, match="receipt does not match"):
        reconcile_presence(contributions, groups, changed, execution_receipt=receipt)

    tampered_result = {**result, "contributions": [dict(result["contributions"][0])]}
    tampered_result["contributions"][0]["routing_scope"] = "advisory_only"
    tampered_result.pop("presence_digest")
    tampered_result["presence_digest"] = digest(tampered_result)
    with pytest.raises(JgError, match="trusted reconciliation receipt"):
        validate_outcome_presence(tampered_result, contributions)


def test_unsigned_success_checkpoint_cannot_mint_receipt_without_dispatch(tmp_path, monkeypatch):
    contributions, groups = _artifact()
    plan = build_group_requests(contributions, groups, estimated_input_tokens=100, max_provider_tokens=1000)
    _add_context_contract(plan)
    preview = approved_presence_preview(plan)
    monkeypatch.setattr(presence_module, "_presence_key_path", lambda: tmp_path / "private" / "key")

    def fixture_sdk(payload, token):
        response = _response(payload)
        response["model"] = payload["model"]
        return response

    monkeypatch.setattr(presence_module, "_default_transport", fixture_sdk)
    checkpoint = tmp_path / "checkpoint.json"
    execute_presence_preview(
        preview, preview["payload_sha256"],
        approved_approval_sha256=preview["approval_sha256"],
        token="fixture-only", checkpoint=checkpoint,
    )
    forged = presence_module.read_json(checkpoint)
    forged.pop("checkpoint_signature")
    presence_module.write_json(checkpoint, forged)

    def must_not_dispatch(payload, token):
        raise AssertionError("forged checkpoint must be rejected before dispatch")

    monkeypatch.setattr(presence_module, "_default_transport", must_not_dispatch)
    with pytest.raises(JgError, match="trusted progress authentication"):
        execute_presence_preview(
            preview, preview["payload_sha256"],
            approved_approval_sha256=preview["approval_sha256"],
            token="fixture-only", checkpoint=checkpoint,
        )


def test_pooled_executor_stops_after_first_failure_and_legacy_checkpoint_fails_closed(tmp_path, monkeypatch):
    contributions, groups = _artifact()
    plan = build_group_requests(contributions, groups, estimated_input_tokens=100, max_provider_tokens=1000)
    _add_context_contract(plan)
    preview = _expand_approved_preview(approved_presence_preview(plan), 4)
    monkeypatch.setattr(presence_module, "_presence_key_path", lambda: tmp_path / "private" / "key")
    checkpoint = tmp_path / "checkpoint.json"
    calls = []
    lock = threading.Lock()

    def fail_once(payload, token):
        with lock:
            first = not calls
            calls.append(digest(payload))
        if first:
            raise RuntimeError("fixture-secret must never enter checkpoint")
        response = _response(payload)
        response["model"] = payload["model"]
        return response

    first_run = execute_presence_preview(
        preview, preview["payload_sha256"], approved_approval_sha256=preview["approval_sha256"],
        transport=fail_once, token="fixture-only", max_workers=2, checkpoint=checkpoint,
    )
    first_checkpoint = read_json(checkpoint)
    assert len(calls) == 2
    assert len(first_checkpoint["attempts"]) == 2
    assert len(first_checkpoint["unattempted_request_sha256s"]) == 2
    assert first_run["actual_budgets"]["input_tokens"] is None
    assert first_run["actual_budgets"]["output_tokens"] is None
    failed = [item for item in first_checkpoint["attempts"] if item["status"] == "uncertain"]
    assert len(failed) == 1
    assert failed[0]["failure_stage"] == "sdk_transport"
    assert failed[0]["error_class"] == "transport_error"
    serialized = canonical_json(first_checkpoint).decode("utf-8")
    assert "fixture-secret" not in serialized
    assert '"body"' not in serialized

    remaining_before_resume = sorted(
        {digest(request) for request in preview["requests"]}
        - {item["request_sha256"] for item in first_checkpoint["attempts"]}
    )
    failed[0]["error_class"] = "transport_or_validation_error"
    first_checkpoint.pop("unattempted_request_sha256s")
    presence_module._seal_checkpoint(first_checkpoint)
    presence_module._write_checkpoint(checkpoint, first_checkpoint)
    resume_calls = []

    def must_not_dispatch(payload, token):
        resume_calls.append(digest(payload))
        raise AssertionError("uncertain checkpoint must fail closed before transport")

    with pytest.raises(JgError, match="uncertain requests; reconcile them before resuming"):
        execute_presence_preview(
            preview, preview["payload_sha256"], approved_approval_sha256=preview["approval_sha256"],
            transport=must_not_dispatch, token="fixture-only", max_workers=2, checkpoint=checkpoint,
        )
    final_checkpoint = read_json(checkpoint)
    assert resume_calls == []
    assert final_checkpoint["unattempted_request_sha256s"] == remaining_before_resume
    assert final_checkpoint["actual_budgets"]["input_tokens"] is None
    assert final_checkpoint["actual_budgets"]["output_tokens"] is None
    assert next(item for item in final_checkpoint["attempts"] if item["status"] == "uncertain")["error_class"] == \
        "transport_or_validation_error"


def test_null_sdk_usage_stays_uncertain_and_budget_usage_unknown(tmp_path, monkeypatch):
    contributions, groups = _artifact()
    plan = build_group_requests(contributions, groups, estimated_input_tokens=100, max_provider_tokens=1000)
    _add_context_contract(plan)
    preview = _expand_approved_preview(approved_presence_preview(plan), 4)
    monkeypatch.setattr(presence_module, "_presence_key_path", lambda: tmp_path / "private" / "key")
    first_calls = []

    def missing_usage(payload, token):
        first_calls.append(digest(payload))
        response = _response(payload)
        response["model"] = payload["model"]
        response["usage"]["input_tokens"] = None
        return response

    executed = execute_presence_preview(
        preview, preview["payload_sha256"], approved_approval_sha256=preview["approval_sha256"],
        transport=missing_usage, token="fixture-only", max_workers=2, checkpoint=tmp_path / "checkpoint.json",
    )
    assert len(first_calls) == 2
    assert all(attempt["status"] == "uncertain" for attempt in executed["attempts"])
    assert all(attempt["failure_stage"] == "response_validation" for attempt in executed["attempts"])
    assert all(attempt["error_class"] == "usage_unavailable" for attempt in executed["attempts"])
    assert len(executed["unattempted_request_sha256s"]) == 2
    assert executed["answers"] == []
    assert executed["actual_budgets"]["input_tokens"] is None
    assert executed["actual_budgets"]["output_tokens"] is None

    resume_calls = []

    def must_not_dispatch(payload, token):
        resume_calls.append(digest(payload))
        raise AssertionError("unknown usage must fail closed before transport")

    with pytest.raises(JgError, match="uncertain requests; reconcile them before resuming"):
        execute_presence_preview(
            preview, preview["payload_sha256"], approved_approval_sha256=preview["approval_sha256"],
            transport=must_not_dispatch, token="fixture-only", max_workers=2,
            checkpoint=tmp_path / "checkpoint.json",
        )
    assert resume_calls == []
    checkpoint = read_json(tmp_path / "checkpoint.json")
    assert len(checkpoint["unattempted_request_sha256s"]) == 2


def test_sdk_http_failure_persists_only_allowlisted_status(tmp_path, monkeypatch):
    contributions, groups = _artifact()
    plan = build_group_requests(contributions, groups, estimated_input_tokens=100, max_provider_tokens=1000)
    _add_context_contract(plan)
    preview = approved_presence_preview(plan)
    monkeypatch.setattr(presence_module, "_presence_key_path", lambda: tmp_path / "private" / "key")

    class TypeSafeAPIError(Exception):
        def __init__(self):
            self.status = 429
            self.body = "fixture-secret response body"
            super().__init__("fixture-secret exception text")

    def http_failure(payload, token):
        raise TypeSafeAPIError()

    executed = execute_presence_preview(
        preview, preview["payload_sha256"], approved_approval_sha256=preview["approval_sha256"],
        transport=http_failure, token="fixture-only", checkpoint=tmp_path / "checkpoint.json",
    )
    attempt = executed["attempts"][0]
    assert attempt["error_class"] == "sdk_http_error"
    assert attempt["failure_stage"] == "sdk_transport"
    assert attempt["http_status"] == 429
    serialized = canonical_json(read_json(tmp_path / "checkpoint.json")).decode("utf-8")
    assert "fixture-secret" not in serialized
    assert '"body"' not in serialized


def test_overlapping_contradiction_and_incomplete_group_remain_unresolved():
    contributions, groups = _artifact()
    duplicate = dict(groups["groups"][0])
    duplicate["id"] = "grp-second"
    groups["groups"] = [*groups["groups"], duplicate]
    groups.pop("groups_digest")
    groups["groups_digest"] = digest(groups)
    plan = build_group_requests(contributions, groups)
    _, contradicted = _synthetic_result(contributions, groups, plan,
        [{}, {"presence": "ABSENT", "delta": 0.95}])
    row = contradicted["contributions"][0]
    assert row["disposition"] == "UNRESOLVED"
    assert "overlapping_answers_contradict" in row["reasons"]

    contributions, groups = _artifact()
    groups["groups"][0]["context_complete"] = False
    groups["groups_digest"] = digest({key: value for key, value in groups.items() if key != "groups_digest"})
    plan = build_group_requests(contributions, groups)
    _, incomplete = _synthetic_result(contributions, groups, plan, [{}])
    assert incomplete["contributions"][0]["disposition"] == "UNRESOLVED"
    assert incomplete["contributions"][0]["group_context_complete"] is False
    assert incomplete["contributions"][0]["comparison_context_complete"] is False


def test_empty_dependency_edges_do_not_claim_complete_dependency_context():
    contributions, groups = _artifact()
    plan = build_group_requests(contributions, groups)
    _add_context_contract(plan, dependency_status="unknown")
    preview, result = _synthetic_result(contributions, groups, plan, [{"dependencies_sufficient": 0.99}])
    row = result["contributions"][0]
    assert row["presence"] == "PRESENT"
    assert row["model_usable_delta"] is False
    assert row["usable_delta"] is None
    assert row["dependency_context_status"] == "unknown"
    assert row["disposition"] == "UNRESOLVED"
    assert "dependency_context_unknown" in row["reasons"]
    assert row["routing_scope"] == "advisory_only"


def test_study_selection_binds_only_targets_and_keeps_original_group_context():
    contributions, groups = _artifact()
    second = {**contributions["units"][0], "id": "cu-2", "name": "neighbor"}
    contributions["units"].append(second)
    contributions["branches"][0]["unit_ids"].append("cu-2")
    contributions.pop("contributions_digest")
    contributions["contributions_digest"] = digest(contributions)
    groups["contributions_digest"] = contributions["contributions_digest"]
    groups["groups"][0]["unit_ids"].append("cu-2")
    boundary_edges = [
        {"id": "rel-1", "source_id": "cu-1", "destination_id": "cu-2", "kind": "dependency"},
        {"id": "rel-2", "source_id": "outside-a", "destination_id": "outside-b", "kind": "similarity"},
    ]
    groups["groups"][0]["boundary_edges"] = boundary_edges
    groups["groups"][0]["edges"] = boundary_edges
    groups.pop("groups_digest")
    groups["groups_digest"] = digest(groups)

    selection = "f" * 64
    plan = build_group_requests(contributions, groups, selected_contribution_ids=["cu-1"],
                                selection_digest=selection)
    request = plan["requests"][0]
    assert plan["groups_digest"] == groups["groups_digest"]
    assert plan["selected_contribution_ids"] == ["cu-1"]
    assert plan["selection_digest"] == selection
    assert [item["contribution_id"] for item in request["state"]["contributions"]] == ["cu-1"]
    assert request["questions"] and all(key.startswith("cu-1:") for key in request["questions"])
    assert request["state"]["context_contribution_ids"] == []
    assert request["state"]["context_contribution_ids_summary"]["count"] == 1
    assert request["state"]["context_contribution_ids_summary"]["non_exhaustive"] is True
    assert request["state"]["context_units"] == []
    assert request["state"]["limitations"]
    summary = request["state"]["boundary_summary"]
    assert request["state"]["boundary_edges"] == []
    assert summary["original_count"] == 2
    assert summary["original_type_counts"] == {"dependency": 1, "similarity": 1}
    assert summary["relevant_type_counts"] == {"dependency": 1}
    assert summary["omitted_type_counts"] == {"similarity": 1}
    assert summary["non_exhaustive"] is True


def test_study_selection_rejects_unassigned_contributions():
    contributions, groups = _artifact()
    with pytest.raises(JgError, match="absent from the pinned artifact"):
        build_group_requests(contributions, groups, selected_contribution_ids=["cu-1", "cu-missing"],
                             selection_digest="f" * 64)


def test_selected_study_chunks_keep_full_cohort_and_dependency_map():
    contributions, groups = _artifact()
    original = contributions["units"][0]
    ids = ["cu-1"]
    for index in range(2, 6):
        cid = f"cu-{index}"
        ids.append(cid)
        contributions["units"].append({**original, "id": cid, "name": f"work-{index}"})
        contributions["branches"][0]["unit_ids"].append(cid)
        groups["groups"][0]["unit_ids"].append(cid)
        contributions["edges"].append({"id": f"edge-{index}", "source_id": cid,
                                        "destination_id": "cu-1", "kind": "dependency"})
    contributions["contributions_digest"] = digest({key: value for key, value in contributions.items()
                                                       if key != "contributions_digest"})
    groups["contributions_digest"] = contributions["contributions_digest"]
    groups["groups_digest"] = digest({key: value for key, value in groups.items() if key != "groups_digest"})
    plan = build_group_requests(contributions, groups, selected_contribution_ids=ids,
                                selection_digest="f" * 64)
    assert len(plan["requests"]) == 2
    seen = []
    for request in plan["requests"]:
        state = request["state"]
        seen.extend(item["contribution_id"] for item in state["contributions"])
        assert state["cohort_contribution_ids"] == sorted(ids)
        assert state["cohort_dependency_edge_count"] == 4
        assert state["cohort_relationships_non_exhaustive"] is True
        assert state["study_scope"]["selected_case_count"] == 5
        assert state["study_scope"]["selected_groups_with_full_context_omitted"] == 1
        assert state["study_scope"]["relationship_context_non_exhaustive"] is True
        assert "Size and excerpt caps" in state["study_scope"]["relationship_context_compaction_reason"]
        assert all("scope_limits" in question for question in request["questions"].values())
    assert sorted(seen) == sorted(ids)


def test_token_estimator_label_is_bound_into_request_budget():
    contributions, groups = _artifact()
    sizing = build_group_requests(contributions, groups)
    estimate = sizing["payload_bytes"] + 256 * sizing["request_count"]
    plan = build_group_requests(
        contributions, groups, estimated_input_tokens=estimate,
        max_provider_tokens=estimate + 1000,
        token_estimator="serialized_utf8_bytes_plus_256_per_request_v1")
    assert plan["request_budgets"]["estimated_input_tokens"] == estimate
    assert plan["request_budgets"]["token_estimator"] == "serialized_utf8_bytes_plus_256_per_request_v1"
    assert plan["plan_digest"] != sizing["plan_digest"]


def test_resolved_per_unit_context_can_route_without_global_group_completeness():
    contributions, groups = _artifact()
    groups["groups"][0]["context_complete"] = False
    groups["groups"][0]["limitations"] = ["partition_has_known_cross_group_edges"]
    groups["groups_digest"] = digest({key: value for key, value in groups.items() if key != "groups_digest"})
    plan = build_group_requests(contributions, groups)
    _add_context_contract(plan, comparison_complete=True, dependency_status="complete")
    _, result = _synthetic_result(contributions, groups, plan, [{}])
    row = result["contributions"][0]
    assert row["group_context_complete"] is False
    assert row["comparison_context_complete"] is True
    assert row["dependency_context_status"] == "complete"
    assert "group_context_incomplete" not in row["reasons"]
    assert row["disposition"] == "LIKELY_PRESERVED"


def test_missing_comparison_ranges_keep_presence_advisory():
    contributions, groups = _artifact()
    plan = build_group_requests(contributions, groups)
    _add_context_contract(plan, comparison_complete=False, dependency_status="complete")
    _, result = _synthetic_result(contributions, groups, plan, [{}])
    row = result["contributions"][0]
    assert row["presence"] == "PRESENT"
    assert row["disposition"] == "UNRESOLVED"
    assert "comparison_context_incomplete" in row["reasons"]


def test_control_presence_flows_to_outcomes_review_and_stales_on_evidence_change():
    contributions, groups = _artifact()
    contributions["branches"][0]["analysis_status"] = "complete"
    contributions["branches"].append({"name": "main", "tip": "b" * 40,
                                      "eligible": True, "unit_ids": [],
                                      "exclusion_reasons": [], "analysis_status": "complete"})
    contributions["contributions_digest"] = digest({
        key: value for key, value in contributions.items() if key != "contributions_digest"
    })
    groups = build_groups(contributions)
    inventory = {
        "repository": {"id": "fixture", "default_branch": "main"},
        "branches": [{"name": "main", "tip": "b" * 40},
                     {"name": "feature/task", "tip": "c" * 40}],
        "worktrees": [], "stashes": [], "collection": {"complete": True},
    }
    snapshot = {
        "repository_id": "fixture", "inventory_digest": digest(inventory),
        "main": {"name": "main", "tip": "b" * 40},
        "branches": [{"name": "main", "tip": "b" * 40, "eligible": True},
                     {"name": "feature/task", "tip": "c" * 40, "eligible": True}],
    }
    snapshot["snapshot_digest"] = digest(snapshot)
    contributions["snapshot_digest"] = snapshot["snapshot_digest"]
    contributions["contributions_digest"] = digest({
        key: value for key, value in contributions.items() if key != "contributions_digest"
    })
    groups = build_groups(contributions)
    plan = build_group_requests(contributions, groups)
    _, presence = _synthetic_result(contributions, groups, plan, [{}])
    presence["contributions"][0]["reasons"].append("overlapping_answers_contradict")
    presence["presence_digest"] = digest({
        key: value for key, value in presence.items() if key != "presence_digest"
    })

    initial = build_outcomes(inventory, snapshot, contributions, presence=presence)
    branch = next(row for row in initial["objects"] if row["object_id"] == "branch:feature/task")
    unit = branch["contribution_reviews"][0]
    assert unit["routing_scope"] == "advisory_only"
    assert "overlapping_answers_contradict" in unit["reasons"]
    assert initial["integration_tasks"] == []
    assert initial["cleanup_authorized"] is False
    assert initial["project_utility_assessment"]["status"] == "UNKNOWN"

    goals = "Support safe and accessible examples."
    goal_plan = build_group_requests(contributions, groups, project_goals=goals)
    goal_preview = approved_presence_preview(goal_plan)
    goal_import = import_synthetic_answers(goal_preview, [{
        "request_sha256": digest(goal_preview["requests"][0]),
        "response": _response(goal_preview["requests"][0], project_relevance=0.95),
    }])
    goal_presence = reconcile_presence(contributions, groups, goal_import)
    goal_outcomes = build_outcomes(inventory, snapshot, contributions, presence=goal_presence)
    goal_branch = next(row for row in goal_outcomes["objects"]
                       if row["object_id"] == "branch:feature/task")
    goal_review = goal_branch["contribution_reviews"][0]
    assert goal_review["project_relevance"] is True
    assert goal_review["project_goal_digest"] == goal_presence["contributions"][0]["project_goal_digest"]
    assert goal_review["project_goal_version"] == "project-purpose-v1"
    assert goal_review["routing_scope"] == "advisory_only"
    assert goal_outcomes["cleanup_authorized"] is False
    assert goal_outcomes["project_utility_assessment"]["reason"] == \
        "comparison_or_source_evidence_insufficient"
    assert goal_outcomes["project_utility_assessment"]["reason"] != \
        "project_requirements_not_provided"

    queue = build_preservation_plan(inventory, outcomes=initial)
    queue_branch = next(row for row in queue["objects"] if row["object_id"] == branch["object_id"])
    assert queue_branch["outcome_review"]["contribution_reviews"][0]["routing_scope"] == "advisory_only"
    assert queue_branch["integration_actions"] == []
    assert any(item["queue"] == "PRESENCE_EVIDENCE_HOLD" for item in queue_branch["suggestions"])

    review = {
        "kind": "outcome-review", "schema_version": 2, "repository_id": "fixture",
        "provenance": initial["review_provenance"],
        "decisions": [{
            "object_id": branch["object_id"], "kind": "branch",
            "source_fingerprint": branch["source_fingerprint"],
            "evidence_fingerprint": branch["review_evidence_fingerprint"],
            "disposition": "UNRESOLVED", "rationale": "Contradictory pilot evidence",
            "reviewer_id": "operator", "reviewed_at": "2026-09-24T12:00:00Z",
            "proposed_destination": None, "preservation_proof": None,
        }],
    }
    current = build_outcomes(inventory, snapshot, contributions, presence=presence, review=review)
    current_branch = next(row for row in current["objects"] if row["object_id"] == branch["object_id"])
    assert current_branch["review_status"] == "current"
    assert current["review_provenance"]["presence_digest"] == digest(presence)
    malformed_review = {**review, "decisions": [{**review["decisions"][0], "kind": "stash"}]}
    with pytest.raises(JgError, match="malformed or claims unsupported"):
        build_outcomes(inventory, snapshot, contributions, presence=presence, review=malformed_review)

    presence["contributions"][0]["reasons"].append("new_evidence_limit")
    presence["presence_digest"] = digest({
        key: value for key, value in presence.items() if key != "presence_digest"
    })
    stale = build_outcomes(inventory, snapshot, contributions, presence=presence, review=review)
    stale_branch = next(row for row in stale["objects"] if row["object_id"] == branch["object_id"])
    assert stale_branch["review_status"] == "stale"
    assert "review_evidence_stale" in stale_branch["reasons"]


def test_preservation_queue_requires_current_review_and_trusted_routeable_unit(tmp_path, monkeypatch):
    contributions, groups = _artifact()
    contributions["branches"][0]["analysis_status"] = "complete"
    contributions["branches"].append({"name": "main", "tip": "b" * 40,
                                      "eligible": True, "unit_ids": [],
                                      "exclusion_reasons": [], "analysis_status": "complete"})
    contributions["paths"] = [{"branch": "feature/task", "path": "src.py", "exact": False}]
    inventory = {
        "repository": {"id": "fixture", "default_branch": "main"},
        "branches": [{"name": "main", "tip": "b" * 40},
                     {"name": "feature/task", "tip": "c" * 40}],
        "worktrees": [], "stashes": [], "collection": {"complete": True},
    }
    snapshot = {
        "repository_id": "fixture", "inventory_digest": digest(inventory),
        "main": {"name": "main", "tip": "b" * 40},
        "branches": [{"name": "main", "tip": "b" * 40, "eligible": True},
                     {"name": "feature/task", "tip": "c" * 40, "eligible": True}],
    }
    snapshot["snapshot_digest"] = digest(snapshot)
    contributions["snapshot_digest"] = snapshot["snapshot_digest"]
    contributions["contributions_digest"] = digest({
        key: value for key, value in contributions.items() if key != "contributions_digest"
    })
    groups = build_groups(contributions)
    monkeypatch.setattr(presence_module, "_presence_key_path", lambda: tmp_path / "private" / "key")
    body = {
        "kind": "branch-presence-result", "schema": presence_module.RESULT_SCHEMA,
        "schema_version": 1, "question_version": presence_module.PRESENCE_QUESTION_VERSION,
        "origin": "jev", "snapshot_digest": snapshot["snapshot_digest"],
        "groups_digest": groups["groups_digest"],
        "contributions_digest": contributions["contributions_digest"],
        "contributions": [{
            "contribution_id": "cu-1", "disposition": "USABLE_WORK_REMAINS",
            "presence": "ABSENT", "evidence_sufficient": True, "usable_delta": True,
            "comparison_context_complete": True, "comparison_context_limitations": [],
            "dependency_context_status": "complete", "dependency_context_sufficient": True,
            "dependency_context_limitations": [], "group_context_complete": True,
            "group_context_limitations": [], "dependencies": [], "evidence_ids": ["range-1"],
            "answer_request_ids": ["a" * 64], "routing_scope": "production_review_candidate",
            "reasons": [],
        }],
        "project_utility_assessment": {"status": "UNKNOWN",
                                       "reason": "project_requirements_not_provided",
                                       "source": "code_only_presence_review"},
        "network_performed": False,
    }
    body["trusted_provenance"] = presence_module._signed_record(
        "branch-presence-reconciliation-receipt",
        {"executor_receipt_sha256": "b" * 64,
         "result_body_sha256": digest(body)}, create_key=True,
    )
    body["presence_digest"] = digest(body)
    unreviewed = build_outcomes(inventory, snapshot, contributions, presence=body)
    assert len(unreviewed["integration_tasks"]) == 1
    queue = build_preservation_plan(inventory, outcomes=unreviewed)
    branch = next(row for row in queue["objects"] if row["object_id"] == "branch:feature/task")
    task = branch["integration_actions"][0]
    assert task["action_state"] == "BLOCKED"
    assert task["blocked_reason"] == "current_human_integration_review_required"
    reviewed_branch = next(row for row in unreviewed["objects"]
                           if row["object_id"] == "branch:feature/task")
    review = {
        "kind": "outcome-review", "schema_version": 2, "repository_id": "fixture",
        "provenance": unreviewed["review_provenance"],
        "decisions": [{
            "object_id": reviewed_branch["object_id"], "kind": "branch",
            "source_fingerprint": reviewed_branch["source_fingerprint"],
            "evidence_fingerprint": reviewed_branch["review_evidence_fingerprint"],
            "disposition": "INTEGRATE", "rationale": "Review the proposed package behavior",
            "reviewer_id": "operator", "reviewed_at": "2026-09-24T12:00:00Z",
            "proposed_destination": "main", "preservation_proof": None,
        }],
    }
    proposed = build_outcomes(inventory, snapshot, contributions, presence=body, review=review)
    proposed_queue = build_preservation_plan(inventory, outcomes=proposed)
    proposed_branch = next(row for row in proposed_queue["objects"]
                           if row["object_id"] == "branch:feature/task")
    assert proposed_branch["integration_actions"][0]["action_state"] == "PROPOSED_AWAITING_HUMAN_VERIFICATION"
    assert proposed_branch["integration_actions"][0]["blocked_reason"] == "explicit_outcome_review_approval_receipt_required"

    review_digest = digest(review)
    review_approval = approve_outcome_review(review, review_digest)
    forged_review = {**review, "decisions": [{
        **review["decisions"][0], "proposed_destination": "unverified-branch",
    }]}
    with pytest.raises(JgError, match="does not bind the exact v2 review"):
        build_outcomes(inventory, snapshot, contributions, presence=body,
                       review=forged_review, review_approval=review_approval)

    current = build_outcomes(inventory, snapshot, contributions, presence=body,
                             review=review, review_approval=review_approval)
    queue = build_preservation_plan(inventory, outcomes=current)
    branch = next(row for row in queue["objects"] if row["object_id"] == "branch:feature/task")
    assert branch["integration_actions"][0]["action_state"] == "READY_FOR_IMPLEMENTATION"
    assert branch["cleanup_authority"] is False

    forged_outcomes = {**current, "objects": [dict(row) for row in current["objects"]]}
    forged_branch = next(row for row in forged_outcomes["objects"]
                         if row["object_id"] == "branch:feature/task")
    forged_branch["human_decision"] = {
        **forged_branch["human_decision"], "rationale": "caller-forged current INTEGRATE review",
    }
    forged_outcomes.pop("outcomes_digest")
    forged_outcomes["outcomes_digest"] = digest(forged_outcomes)
    with pytest.raises(JgError, match="matching signed outcome receipt"):
        build_preservation_plan(inventory, outcomes=forged_outcomes)

    wrong_destination_review = {**review, "decisions": [{
        **review["decisions"][0], "proposed_destination": "unverified-branch",
    }]}
    wrong_destination = build_outcomes(inventory, snapshot, contributions,
                                       presence=body, review=wrong_destination_review,
                                       review_approval=approve_outcome_review(
                                           wrong_destination_review, digest(wrong_destination_review)))
    blocked_queue = build_preservation_plan(inventory, outcomes=wrong_destination)
    blocked_branch = next(row for row in blocked_queue["objects"]
                          if row["object_id"] == "branch:feature/task")
    assert blocked_branch["integration_actions"][0]["action_state"] == "BLOCKED"
    assert blocked_branch["integration_actions"][0]["blocked_reason"] == "integration_destination_not_pinned_default"
    inventory_path = tmp_path / "inventory.json"
    outcomes_path = tmp_path / "outcomes.json"
    write_json(inventory_path, inventory)
    write_json(outcomes_path, current)
    queue_file = write_preservation_plan(inventory_path, tmp_path / "queue",
                                        outcomes_path=outcomes_path)
    saved_queue = read_json(queue_file)
    assert saved_queue["integration_action_count"] == 1
    assert "READY_FOR_IMPLEMENTATION" in (tmp_path / "queue" / "index.html").read_text()

    changed_presence = {**body, "contributions": [dict(body["contributions"][0])],
                        "presence_digest": None}
    changed_presence["contributions"][0]["evidence_ids"] = ["different-range"]
    changed_presence.pop("trusted_provenance")
    changed_presence["trusted_provenance"] = presence_module._signed_record(
        "branch-presence-reconciliation-receipt",
        {"executor_receipt_sha256": "b" * 64,
         "result_body_sha256": digest({key: value for key, value in changed_presence.items()
                                      if key not in {"trusted_provenance", "presence_digest"}})},
    )
    changed_presence["presence_digest"] = digest({
        key: value for key, value in changed_presence.items() if key != "presence_digest"
    })
    stale = build_outcomes(inventory, snapshot, contributions, presence=changed_presence,
                           review=review, review_approval=review_approval)
    stale_queue = build_preservation_plan(inventory, outcomes=stale)
    branch = next(row for row in stale_queue["objects"] if row["object_id"] == "branch:feature/task")
    assert branch["outcome_review"]["status"] == "stale"
    assert branch["integration_actions"][0]["action_state"] == "BLOCKED"
    assert branch["integration_actions"][0]["blocked_reason"] == "outcome_review_stale"


def test_calibration_excludes_unreviewed_labels_and_uses_matched_cases(tmp_path, monkeypatch):
    contributions, groups = _artifact()
    plan = build_group_requests(contributions, groups)
    _, result = _synthetic_result(contributions, groups, plan, [{}])
    # Calibration consumes matched outputs from both arms; synthetic fixture
    # answers remain labelled as offline prediction data, never owner truth.
    labels = {"kind": "branch-presence-owner-labels", "schema_version": 1,
              "snapshot_digest": contributions["snapshot_digest"],
              "contributions_digest": contributions["contributions_digest"],
              "groups_digest": groups["groups_digest"], "accepted_by": "owner",
              "label_source": "owner_review", "blinded": True,
              "labels": [{"contribution_id": "cu-1", "family_id": "family-1",
                          "reviewed": True, "disposition": "LIKELY_PRESERVED"},
                         {"contribution_id": "cu-unreviewed", "family_id": "family-2",
                          "reviewed": False, "disposition": None}]}
    monkeypatch.setattr(presence_module, "_presence_key_path", lambda: tmp_path / "private" / "key")
    jev_body = {**result, "origin": "jev"}
    jev_body.pop("presence_digest")
    control_result = {**result, "origin": "control"}
    control_result.pop("presence_digest")
    control_result["presence_digest"] = digest(control_result)
    spoofed_jev = {**jev_body, "presence_digest": digest(jev_body)}
    with pytest.raises(JgError, match="trusted executor provenance"):
        build_presence_calibration(labels, spoofed_jev, control_result)
    jev_body["trusted_provenance"] = presence_module._signed_record(
        "branch-presence-reconciliation-receipt",
        {"executor_receipt_sha256": "a" * 64,
         "result_body_sha256": digest(jev_body)}, create_key=True)
    jev_result = {**jev_body, "presence_digest": digest(jev_body)}
    calibration = build_presence_calibration(labels, jev_result, control_result)
    assert calibration["labels"]["unreviewed_excluded"] == 1
    assert calibration["matched_contribution_ids"] == ["cu-1"]
    assert calibration["arms"]["jev"]["incorrect_preservation_claims"] == 0


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()


def test_two_sided_evidence_is_pinned_to_distinct_source_and_destination_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "destination.py").write_text("def work():\n    return 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "destination")
    destination_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "feature")
    (repo / "source.py").write_text("def work():\n    return 3\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "source")
    source_tip = _git(repo, "rev-parse", "HEAD")

    evidence = build_two_sided_evidence(repo, source_tip, destination_tip, [{
        "evidence_id": "ev-1", "source_path": "source.py",
        "source_range": {"start_line": 1, "end_line": 2},
        "destination_path": "destination.py",
        "destination_range": {"start_line": 1, "end_line": 2},
    }])
    assert evidence["records"][0]["source"]["path"] == "source.py"
    assert evidence["records"][0]["destination"]["path"] == "destination.py"
    assert revalidate_two_sided_evidence(repo, evidence) == evidence


def test_source_only_evidence_keeps_destination_unknown_and_transient(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "src.py").write_text("def work():\n    return 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "destination")
    destination_tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "feature")
    (repo / "src.py").write_text("def work():\n    return 3\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "source")
    source_tip = _git(repo, "rev-parse", "HEAD")

    evidence = build_source_only_evidence(repo, source_tip, destination_tip, [{
        "evidence_id": "source-only-1", "source_path": "src.py",
        "source_range": {"start_line": 1, "end_line": 2},
    }])
    assert evidence["kind"] == "branch-presence-source-only-evidence"
    assert evidence["records"][0]["source"]["path"] == "src.py"
    assert "destination" not in evidence["records"][0]

    contributions, _ = _artifact()
    unit = contributions["units"][0]
    unit.update({"source_tip": source_tip, "main_tip": destination_tip, "path": "src.py",
                 "source_blob": _git(repo, "rev-parse", f"{source_tip}:src.py"),
                 "range": {"start_line": 1, "end_line": 2}, "destination_ids": [],
                 "dependency_context_status": "unknown"})
    unit["source"].update({"path": "src.py", "blob": unit["source_blob"]})
    contributions["branches"][0]["tip"] = source_tip
    contributions["edges"] = [
        {"id": "edge-source-dep-1", "source_id": "cu-1", "destination_id": "du-1",
         "kind": "dependency"},
        {"id": "edge-source-dep-2", "source_id": "cu-1", "destination_id": "du-1",
         "kind": "dependency"},
    ]
    contributions["contributions_digest"] = digest({key: value for key, value in contributions.items()
                                                      if key != "contributions_digest"})
    groups = build_groups(contributions)
    plan = build_group_requests(contributions, groups, {"cu-1": evidence}, selected_contribution_ids=["cu-1"],
                                selection_digest="a" * 64)
    binding = plan["requests"][0]["state"]["contributions"][0]
    assert binding["dependency_context_status"] == "unknown"
    assert binding["destination_ids"] == []
    assert binding["comparison_context_complete"] is False
    assert "destination_candidate_missing" in binding["comparison_context_limitations"]
    assert "source_only_unknown_selection" in binding["comparison_context_limitations"]
    presence_question = plan["requests"][0]["questions"]["cu-1:presence"]
    assert "UNKNOWN" in presence_question["criteria"]
    assert "return UNKNOWN" in presence_question["instructions"]
    usable_delta_question = plan["requests"][0]["questions"]["cu-1:usable_delta"]
    assert "bounded source-unit utility only" in usable_delta_question["instructions"]
    assert "Do not infer destination presence or absence, integration readiness, deduplication, preservation, or deletion" in usable_delta_question["instructions"]
    assert "source-only usable_delta only" in usable_delta_question["scope_limits"]
    assert "preservation" in usable_delta_question["scope_limits"]
    assert "missing from destination" not in usable_delta_question["instructions"]
    assert "cu-1:dependency_context_sufficient" in plan["requests"][0]["questions"]
    assert "integration readiness remains unassessed" in plan["requests"][0]["questions"]["cu-1:dependency_context_sufficient"]["criteria"]["false"]
    assert len(binding["dependency_edges"]) == 2
    assert not any(key.startswith("cu-1:dependency:") for key in plan["requests"][0]["questions"])

    preview = approved_presence_preview(plan)
    request = preview["requests"][0]
    imported = import_synthetic_answers(preview, [{
        "request_sha256": digest(request),
        "response": _response(request, presence="UNKNOWN", dependencies_sufficient=0.05, delta=0.95),
    }])
    result = reconcile_presence(contributions, groups, imported)
    assert result["contributions"][0]["presence"] == "UNKNOWN"
    assert result["contributions"][0]["usable_delta"] is None
    assert result["contributions"][0]["disposition"] == "UNRESOLVED"
    assert result["contributions"][0]["routing_scope"] == "advisory_only"
    assert result["contributions"][0]["dependencies"] == [
        {"edge_id": "edge-source-dep-1", "relevant": None},
        {"edge_id": "edge-source-dep-2", "relevant": None},
    ]

    full_contributions, _ = _artifact()
    full_unit = full_contributions["units"][0]
    source_blob = _git(repo, "rev-parse", f"{source_tip}:src.py")
    destination_blob = _git(repo, "rev-parse", f"{destination_tip}:src.py")
    full_unit.update({"source_tip": source_tip, "main_tip": destination_tip, "path": "src.py",
                      "source_blob": source_blob, "range": {"start_line": 1, "end_line": 2},
                      "destination_ids": ["du-1"], "dependency_context_status": "complete"})
    full_unit["source"].update({"path": "src.py", "blob": source_blob})
    full_contributions["branches"][0]["tip"] = source_tip
    full_contributions["destination_units"][0].update({
        "path": "src.py", "blob": destination_blob, "range": {"start_line": 1, "end_line": 2},
    })
    full_contributions["edges"] = [
        {"id": "edge-source-dep-1", "source_id": "cu-1", "destination_id": "du-1",
         "kind": "dependency"},
        {"id": "edge-source-dep-2", "source_id": "cu-1", "destination_id": "du-1",
         "kind": "dependency"},
    ]
    full_contributions["contributions_digest"] = digest({key: value for key, value in full_contributions.items()
                                                           if key != "contributions_digest"})
    full_groups = build_groups(full_contributions)
    full_evidence = build_two_sided_evidence(repo, source_tip, destination_tip, [{
        "evidence_id": "two-sided-1", "source_path": "src.py",
        "source_range": {"start_line": 1, "end_line": 2},
        "destination_path": "src.py", "destination_range": {"start_line": 1, "end_line": 2},
    }])
    full_plan = build_group_requests(full_contributions, full_groups, {"cu-1": full_evidence})
    full_preview = approved_presence_preview(full_plan)
    full_request = full_preview["requests"][0]
    incomplete_full = import_synthetic_answers(full_preview, [{
        "request_sha256": digest(full_request), "response": _response(full_request),
    }])
    incomplete_full["answers"][0]["response"]["answers"].pop(
        "cu-1:dependency:edge-source-dep-1")
    incomplete_full["answers_digest"] = presence_module._answers_digest(incomplete_full)
    with pytest.raises(JgError, match="presence answer IDs do not match contribution bindings"):
        reconcile_presence(full_contributions, full_groups, incomplete_full)

    mismatched, _ = _artifact()
    mismatched_unit = mismatched["units"][0]
    mismatched_unit.update({"source_tip": source_tip, "main_tip": destination_tip, "path": "src.py",
                            "source_blob": _git(repo, "rev-parse", f"{source_tip}:src.py"),
                            "range": {"start_line": 1, "end_line": 2},
                            "destination_ids": ["du-1"], "dependency_context_status": "complete"})
    mismatched_unit["source"].update({"path": "src.py", "blob": mismatched_unit["source_blob"]})
    mismatched["branches"][0]["tip"] = source_tip
    mismatched["contributions_digest"] = digest({key: value for key, value in mismatched.items()
                                                  if key != "contributions_digest"})
    mismatched_groups = build_groups(mismatched)
    with pytest.raises(JgError, match="source-only evidence cannot accompany destination candidates"):
        build_group_requests(mismatched, mismatched_groups, {"cu-1": evidence},
                             selected_contribution_ids=["cu-1"], selection_digest="b" * 64)


def test_source_only_evidence_rejects_destination_claim(tmp_path):
    with pytest.raises(JgError, match="cannot contain a destination"):
        build_source_only_evidence("/tmp", "a" * 40, "b" * 40, [{
            "source_path": "source.py", "source_range": {"start_line": 1, "end_line": 1},
            "destination_path": "main.py", "destination_range": {"start_line": 1, "end_line": 1},
        }])
