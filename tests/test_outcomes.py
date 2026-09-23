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
            "source_fingerprint": "fingerprint",
            "kind": "branch",
            "name": "feature",
            "disposition": "USABLE_WORK_REMAINS",
            "reasons": ["reviewed_evidence_suggests_missing_behavior"],
            "contribution_ids": ["unit-1", "unit-2"],
            "unresolved_contribution_ids": ["unit-2"],
            "unresolved_contribution_count": 1,
            "next_action": "Review integration and unresolved cases",
        }],
    }
    page = render_outcomes(ledger)
    assert "<option selected>INTEGRATE</option>" in page
    assert "value='Preserve the new behavior'" in page
    assert "value='main'" in page
    assert "data-prior-reviewer='owner@example.test'" in page
    assert "1 unresolved contributions" in page
    assert "unit-2" in page
    assert "reviewer:rowReviewer" in page
