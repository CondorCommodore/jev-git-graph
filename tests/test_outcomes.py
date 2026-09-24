from jev_git_graph.outcomes import render_outcomes


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
