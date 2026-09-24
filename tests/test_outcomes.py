from jev_git_graph.outcomes import render_outcomes
from jev_git_graph.outcomes import build_outcomes
from jev_git_graph.errors import JgError
from jev_git_graph.safety import digest
from jev_git_graph.preservation import build_preservation_plan, render_preservation_plan


def _delivery_fixture():
    inventory = {
        "repository": {"id": "fixture", "default_branch": "main"},
        "branches": [{"name": "main", "tip": "b" * 40},
                     {"name": "feature", "tip": "c" * 40}],
        "worktrees": [], "stashes": [], "collection": {"complete": True},
    }
    snapshot = {
        "repository_id": "fixture", "inventory_digest": digest(inventory),
        "main": {"name": "main", "tip": "b" * 40},
        "branches": [{"name": "main", "tip": "b" * 40, "eligible": True},
                     {"name": "feature", "tip": "c" * 40, "eligible": True}],
    }
    snapshot["snapshot_digest"] = digest(snapshot)
    contributions = {
        "kind": "contributions", "schema_version": 1, "repository_id": "fixture",
        "snapshot_digest": snapshot["snapshot_digest"],
        "main": {"name": "main", "tip": "b" * 40},
        "branches": [
            {"name": "main", "tip": "b" * 40, "eligible": True,
             "analysis_status": "complete", "unit_ids": [], "exclusion_reasons": []},
            {"name": "feature", "tip": "c" * 40, "eligible": True,
             "analysis_status": "complete", "unit_ids": ["cu-1"], "exclusion_reasons": []},
        ],
        "units": [{"id": "cu-1", "source_tip": "c" * 40, "main_tip": "b" * 40,
                    "path": "src.py", "source_blob": "d" * 40, "kind": "python_definition",
                    "name": "work", "range": {"start_line": 1, "end_line": 2},
                    "source": {"branch": "feature", "path": "src.py", "blob": "d" * 40},
                    "destination_ids": [], "limitations": []}],
        "destination_units": [], "edges": [], "paths": [], "limitations": [],
    }
    contributions["contributions_digest"] = digest(contributions)
    sidecar = {
        "kind": "delivery-observations", "schema_version": 1,
        "repository_id": "fixture", "snapshot_digest": snapshot["snapshot_digest"],
        "contributions_digest": contributions["contributions_digest"],
        "observations": [{
            "contribution_id": "cu-1",
            "source": {"branch": "feature", "tip": "c" * 40,
                       "path": "src.py", "blob": "d" * 40},
            "destination": {"branch": "main", "tip": "b" * 40},
            "pull_request": {"url": "https://github.com/acme/repo/pull/12",
                             "repository": "acme/repo", "number": 12,
                             "head_sha": "e" * 40, "base_sha": "f" * 40,
                             "status": "OPEN", "merge_sha": None},
            "observed_at": "2026-09-24T23:00:00Z",
            "observer": {"identity": "fixture-agent", "kind": "agent_readback"},
            "evidence": {"reference": "pr-readback.json", "sha256": "a" * 64},
        }],
    }
    return inventory, snapshot, contributions, sidecar


def test_review_export_prefills_and_retains_existing_decisions():
    ledger = {
        "snapshot_digest": "snapshot",
        "object_count": 1,
        "objects": [{
            "human_decision": {
                "disposition": "INTEGRATE",
                "reason": "Preserve the new behavior",
                "reviewer": "owner@example.test",
                "destination": "main",
            },
            "object_id": "branch:feature",
            "review_evidence_fingerprint": "e" * 64,
            "source_fingerprint": "fingerprint",
            "kind": "branch",
            "name": "feature",
            "disposition": "USABLE_WORK_REMAINS",
            "reasons": ["reviewed_evidence_suggests_missing_behavior"],
            "contribution_ids": ["unit-1", "unit-2"],
            "contribution_reviews": [{
                "contribution_id": "unit-1", "name": "validate", "path": "src/<script>.py",
                "disposition": "UNRESOLVED", "presence": "UNKNOWN", "usable_delta": None,
                "routing_scope": "advisory_only", "dependencies": [], "evidence_ids": ["range-1"],
                "reasons": ["overlapping_answers_contradict"], "presence_origin": "control",
            }],
            "presence_origin": "control",
            "unresolved_contribution_ids": ["unit-2"],
            "unresolved_contribution_count": 1,
            "next_action": "Review integration and unresolved cases",
        }],
        "repository_id": "repo-fixture",
        "review_provenance": {
            "repository_id": "repo-fixture", "inventory_digest": "a" * 64,
            "snapshot_digest": "b" * 64, "contributions_digest": "c" * 64,
            "presence_digest": "d" * 64, "coverage_digest": None,
        },
    }
    page = render_outcomes(ledger)
    assert "<option selected>INTEGRATE</option>" in page
    assert "value='Preserve the new behavior'" in page
    assert "value='main'" in page
    assert "data-prior-reviewer='owner@example.test'" in page
    assert "1 unresolved contributions" in page
    assert "unit-2" in page
    assert "reviewer_id:rowReviewer" in page
    assert "overlapping_answers_contradict" in page
    assert "route advisory_only" in page
    assert "&lt;script&gt;" in page
    assert "source excerpts are not included" in page


def test_outcome_review_exports_all_provenance_and_marks_prior_review_status():
    from jev_git_graph.outcomes import OUTCOME_REVIEW_VERSION

    ledger = {
        "repository_id": "repo-fixture", "object_count": 1, "snapshot_digest": "b" * 64,
        "review_provenance": {
            "repository_id": "repo-fixture", "inventory_digest": "a" * 64,
            "snapshot_digest": "b" * 64, "contributions_digest": "c" * 64,
            "presence_digest": None, "coverage_digest": None,
        },
        "objects": [{
            "human_decision": None, "review_status": "unreviewed",
            "object_id": "branch:feature", "source_fingerprint": "f" * 64,
            "review_evidence_fingerprint": "e" * 64, "kind": "branch", "name": "feature",
            "disposition": "UNRESOLVED", "reasons": ["answer_missing"],
            "contribution_ids": [], "contribution_reviews": [], "presence_origin": None,
            "unresolved_contribution_ids": [], "unresolved_contribution_count": 0,
            "next_action": "Review missing evidence",
        }],
    }

    page = render_outcomes(ledger)
    assert f"schema_version:{OUTCOME_REVIEW_VERSION}" in page
    assert "inventory_digest" in page and "contributions_digest" in page
    assert "reviewer_id:rowReviewer" in page
    assert "evidence_fingerprint:cell.dataset.evidenceFingerprint" in page
    assert "preservation_proof:null" in page


def test_delivery_observation_is_pinned_display_only_and_does_not_change_actionability():
    inventory, snapshot, contributions, sidecar = _delivery_fixture()
    baseline = build_outcomes(inventory, snapshot, contributions)
    observed = build_outcomes(inventory, snapshot, contributions,
                              delivery_observations=sidecar)
    baseline_row = next(row for row in baseline["objects"] if row["object_id"] == "branch:feature")
    observed_row = next(row for row in observed["objects"] if row["object_id"] == "branch:feature")
    assert observed["integration_tasks"] == baseline["integration_tasks"] == []
    assert observed_row["disposition"] == baseline_row["disposition"]
    assert observed_row["review_evidence_fingerprint"] == baseline_row["review_evidence_fingerprint"]
    assert observed["delivery_observations"]["observations"][0]["pull_request"]["status"] == "OPEN"
    assert observed_row["contribution_reviews"][0]["delivery_observation"]["interpretation"].startswith(
        "reported_observation_only")
    assert observed_row.get("preservation_proof") is None
    page = render_outcomes(observed)
    assert "Delivery observation (reported; not provider-verified or preservation proof)" in page
    assert "https://github.com/acme/repo/pull/12" in page
    assert "PR base ffffffffffffffffffffffffffffffffffffffff" in page
    assert "analysis destination main@bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" in page
    queue = build_preservation_plan(inventory, outcomes=observed)
    feature = next(row for row in queue["objects"] if row["object_id"] == "branch:feature")
    assert feature["preservation_proof"] is None
    assert feature["integration_actions"] == []
    queue_page = render_preservation_plan(queue)
    assert "Delivery observation (reported only)" in queue_page
    assert "analysis destination remains pinned separately" in queue_page

    sha64 = {**sidecar, "observations": [dict(sidecar["observations"][0])]}
    sha64["observations"][0]["pull_request"] = {
        **sidecar["observations"][0]["pull_request"], "head_sha": "e" * 64,
    }
    assert build_outcomes(inventory, snapshot, contributions,
                          delivery_observations=sha64)["delivery_observations"]["observations"][0]["pull_request"]["head_sha"] == "e" * 64

    sha41 = {**sidecar, "observations": [dict(sidecar["observations"][0])]}
    sha41["observations"][0]["pull_request"] = {
        **sidecar["observations"][0]["pull_request"], "head_sha": "e" * 41,
    }
    try:
        build_outcomes(inventory, snapshot, contributions, delivery_observations=sha41)
    except JgError as exc:
        assert "pull request metadata" in str(exc)
    else:
        raise AssertionError("intermediate-length SHA was accepted")

    wrong_pins = {**sidecar, "observations": [dict(sidecar["observations"][0])]}
    wrong_pins["observations"][0]["destination"] = {"branch": "main", "tip": "9" * 40}
    try:
        build_outcomes(inventory, snapshot, contributions, delivery_observations=wrong_pins)
    except JgError as exc:
        assert "destination pin" in str(exc)
    else:
        raise AssertionError("mismatched destination pin was accepted")

    wrong_status = {**sidecar, "observations": [dict(sidecar["observations"][0])]}
    wrong_status["observations"][0]["pull_request"] = {
        **sidecar["observations"][0]["pull_request"], "status": "UNKNOWN",
    }
    try:
        build_outcomes(inventory, snapshot, contributions, delivery_observations=wrong_status)
    except JgError as exc:
        assert "pull request metadata" in str(exc)
    else:
        raise AssertionError("unknown PR status was accepted")
