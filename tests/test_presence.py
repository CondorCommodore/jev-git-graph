from __future__ import annotations

import subprocess

import pytest

import jev_git_graph.presence as presence_module
from jev_git_graph.errors import JgError
from jev_git_graph.group_requests import (
    approved_presence_preview,
    build_group_requests,
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
from jev_git_graph.safety import digest


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
              dependencies_sufficient=0.95):
    answers = {}
    for qid, question in request["questions"].items():
        if question["type"] == "noul":
            if qid.endswith(":evidence_sufficient"):
                value = sufficient
            elif qid.endswith(":dependency_context_sufficient"):
                value = dependencies_sufficient
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


def test_injected_executor_is_synthetic_and_public_origin_spoof_fails_closed():
    contributions, groups = _artifact()
    plan = build_group_requests(contributions, groups, estimated_input_tokens=100, max_provider_tokens=1000)
    _add_context_contract(plan)
    preview = approved_presence_preview(plan)

    def injected(payload, token):
        response = _response(payload)
        response["model"] = payload["model"]
        return response

    executed = execute_presence_preview(
        preview, preview["payload_sha256"],
        approved_approval_sha256=preview["approval_sha256"],
        transport=injected, token="fixture-only",
    )
    assert executed["origin"] == "synthetic"
    advisory = reconcile_presence(contributions, groups, executed)
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
        token="fixture-only", execution_receipt_path=receipt_path,
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
    assert request["state"]["context_contribution_ids"] == ["cu-2"]


def test_study_selection_rejects_unassigned_contributions():
    contributions, groups = _artifact()
    with pytest.raises(JgError, match="absent from the pinned artifact"):
        build_group_requests(contributions, groups, selected_contribution_ids=["cu-1", "cu-missing"],
                             selection_digest="f" * 64)


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
