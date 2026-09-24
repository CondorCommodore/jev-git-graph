"""One disposition per inventoried object, with concrete preservation tasks.

This is advisory output. Only the guarded cleanup executor can remove refs.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from html import escape
import json
from pathlib import Path
import re
from typing import Any

from .errors import JgError
from .groups import _validate as validate_contributions
from .preservation import _object_index
from .review import object_fingerprint
from .safety import digest, read_json, write_json, write_private_text


OUTCOME_REVIEW_VERSION = 2
OUTCOME_REVIEW_DISPOSITIONS = {"RETAIN", "INTEGRATE", "ARCHIVE_PROPOSED", "UNRESOLVED"}
OUTCOME_REVIEW_APPROVAL_KIND = "outcome-human-review-approval"
OUTCOME_LEDGER_RECEIPT_KIND = "branch-presence-outcome-ledger-receipt"


def _signed(document: dict, field: str) -> None:
    if document.get(field) != digest({k: v for k, v in document.items() if k != field}):
        raise JgError(f"invalid {field}")


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _review_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _review_provenance_valid(review: dict[str, Any]) -> bool:
    provenance = review.get("provenance")
    if (review.get("kind") != "outcome-review"
            or review.get("schema_version") != OUTCOME_REVIEW_VERSION
            or not isinstance(review.get("repository_id"), str)
            or not isinstance(provenance, dict)
            or provenance.get("repository_id") != review.get("repository_id")
            or set(provenance) != {"repository_id", "inventory_digest", "snapshot_digest",
                                   "contributions_digest", "presence_digest", "coverage_digest"}):
        return False
    return (all(_valid_digest(provenance.get(field)) for field in
                ("inventory_digest", "snapshot_digest", "contributions_digest"))
            and all(value is None or _valid_digest(value) for field, value in provenance.items()
                    if field in {"presence_digest", "coverage_digest"}))


def approve_outcome_review(review: dict[str, Any], approved_review_sha256: str) -> dict[str, Any]:
    """Create a local receipt only for an explicitly digest-approved v2 review."""
    if not _review_provenance_valid(review) or not isinstance(review.get("decisions"), list):
        raise JgError("only a valid outcome-review v2 document can be approved")
    review_sha256 = digest(review)
    if not _valid_digest(approved_review_sha256) or approved_review_sha256 != review_sha256:
        raise JgError("approved review digest does not match the exact review document")
    from .presence import _signed_record
    return _signed_record(OUTCOME_REVIEW_APPROVAL_KIND, {
        "review_sha256": review_sha256,
        "repository_id": review["repository_id"],
        "provenance": review["provenance"],
    }, create_key=True)


def _review_approval_matches(review: dict[str, Any], approval: dict[str, Any] | None) -> bool:
    if approval is None:
        return False
    from .presence import _verify_signed_record
    return (_verify_signed_record(approval, OUTCOME_REVIEW_APPROVAL_KIND)
            and approval.get("review_sha256") == digest(review)
            and approval.get("repository_id") == review.get("repository_id")
            and approval.get("provenance") == review.get("provenance"))


def build_outcomes(inventory: dict, snapshot: dict, contributions: dict,
                   presence: dict | None = None, coverage: dict | None = None,
                   review: dict | None = None,
                   review_approval: dict | None = None) -> dict:
    _signed(snapshot, "snapshot_digest")
    validate_contributions(contributions)
    repository_id = inventory.get("repository", {}).get("id")
    if (snapshot.get("inventory_digest") != digest(inventory)
            or snapshot.get("repository_id") != repository_id
            or contributions.get("repository_id") != repository_id
            or contributions.get("snapshot_digest") != snapshot["snapshot_digest"]):
        raise JgError("outcome inputs do not share the same inventory and snapshot")
    objects = _object_index(inventory)
    branches = {b["name"]: b for b in contributions["branches"]}
    pins = {b["name"]: b for b in snapshot["branches"]}
    if set(branches) != set(pins) or any(
        branches[n]["tip"] != pins[n]["tip"] or branches[n]["eligible"] != pins[n]["eligible"]
        for n in pins
    ):
        raise JgError("contribution branch pins differ from snapshot")
    units = {u["id"]: u for u in contributions["units"]}
    by_branch: dict[str, list] = defaultdict(list)
    for unit in units.values():
        by_branch[unit["source"]["branch"]].append(unit)
    paths: dict[str, list] = defaultdict(list)
    for path in contributions["paths"]:
        paths[path["branch"]].append(path)
    presence_observations = {}
    judgments = {}
    if presence is not None:
        # All validated observations remain visible; only the trusted Jev
        # subset may create integration recommendations.
        from .presence import validate_presence_observations
        presence_observations = validate_presence_observations(presence, contributions)
        judgments = {cid: row for cid, row in presence_observations.items()
                     if row.get("routing_scope") == "production_review_candidate"}
    cov = {}
    if coverage is not None:
        if (coverage.get("kind") != "branch-coverage"
                or coverage.get("inventory_digest") != digest(inventory)
                or coverage.get("repository_id") != repository_id
                or coverage.get("main", {}).get("tip") != snapshot["main"]["tip"]):
            raise JgError("coverage does not match the pinned inventory")
        cov = {b["name"]: b for b in coverage["branches"]}
        expected = {b["name"]: b["tip"] for b in inventory["branches"]}
        if len(cov) != len(coverage["branches"]) or {n: b["tip"] for n, b in cov.items()} != expected:
            raise JgError("coverage source pins differ from inventory")
    review_provenance = {
        "repository_id": repository_id,
        "inventory_digest": digest(inventory),
        "snapshot_digest": snapshot["snapshot_digest"],
        "contributions_digest": contributions["contributions_digest"],
        "presence_digest": digest(presence) if presence is not None else None,
        "coverage_digest": digest(coverage) if coverage is not None else None,
    }
    reviewed: dict[str, dict[str, Any]] = {}
    historical_reviews: list[dict[str, Any]] = []
    review_version = None
    if review is not None:
        review_version = review.get("schema_version")
        if (review.get("kind") != "outcome-review" or review_version not in {1, OUTCOME_REVIEW_VERSION}
                or not isinstance(review.get("decisions"), list)):
            raise JgError("invalid outcome review provenance")
        if review_version == OUTCOME_REVIEW_VERSION:
            prior_provenance = review.get("provenance")
            required_digests = {"inventory_digest", "snapshot_digest", "contributions_digest"}
            if (not isinstance(prior_provenance, dict)
                    or review.get("repository_id") != repository_id
                    or prior_provenance.get("repository_id") != repository_id
                    or set(prior_provenance) != set(review_provenance)
                    or any(not _valid_digest(prior_provenance.get(field))
                           for field in required_digests)
                    or any(value is not None and not _valid_digest(value)
                           for field, value in prior_provenance.items()
                           if field not in required_digests and field != "repository_id")):
                raise JgError("outcome review has malformed repository or pin provenance")
        elif not isinstance(review.get("snapshot_digest"), str):
            raise JgError("legacy outcome review lacks its snapshot pin")
        for decision in review["decisions"]:
            key = decision.get("object_id") if isinstance(decision, dict) else None
            if not isinstance(key, str) or key in reviewed:
                raise JgError("outcome review contains a missing or duplicate object id")
            if review_version == OUTCOME_REVIEW_VERSION:
                decision_kind = decision.get("kind")
                if (not _valid_digest(decision.get("source_fingerprint"))
                        or not _valid_digest(decision.get("evidence_fingerprint"))
                        or decision.get("disposition") not in OUTCOME_REVIEW_DISPOSITIONS
                        or decision_kind not in {"branch", "worktree", "stash"}
                        or not key.startswith(decision_kind + ":")
                        or not all(isinstance(decision.get(field), str) and decision[field].strip()
                                   for field in ("rationale", "reviewer_id"))
                        or not _review_timestamp(decision.get("reviewed_at"))
                        or (decision.get("proposed_destination") is not None
                            and not isinstance(decision.get("proposed_destination"), str))
                        or decision.get("preservation_proof") is not None):
                    raise JgError("outcome review decision is malformed or claims unsupported preservation proof")
            else:
                if (decision.get("disposition") not in OUTCOME_REVIEW_DISPOSITIONS
                        or not all(isinstance(decision.get(field), str) and decision[field].strip()
                                   for field in ("reason", "reviewer"))
                        or not isinstance(decision.get("source_fingerprint"), str)):
                    raise JgError("legacy outcome review decision is malformed")
            if key not in objects:
                historical_reviews.append({**decision, "review_status": "stale",
                                           "review_reasons": ["object_missing_from_current_inventory"]})
            else:
                reviewed[key] = decision
    if review_approval is not None:
        if (review is None or review_version != OUTCOME_REVIEW_VERSION
                or not _review_approval_matches(review, review_approval)):
            raise JgError("human review receipt does not bind the exact v2 review document")
    elif review_version == OUTCOME_REVIEW_VERSION:
        # A browser-exported JSON file is review input, not proof that an
        # operator explicitly approved that exact document for action.
        pass
    occupied = {w.get("branch") for w in inventory["worktrees"] if w.get("branch")}
    records, tasks = [], []
    for key, (kind, item) in objects.items():
        record: dict[str, Any] = {
            "object_id": key, "kind": kind, "source_fingerprint": object_fingerprint(kind, item),
            "name": item.get("name") or item.get("path_id") or item.get("reference"),
            "tip": item.get("tip") or item.get("head") or item.get("sha"),
            "disposition": "UNRESOLVED", "reasons": [], "next_action": "Review missing evidence",
            "contribution_ids": [], "task_ids": [], "cleanup_authorized": False,
            "human_decision": None, "review_status": "unreviewed",
        }
        if kind == "branch":
            name = item["name"]
            branch = branches.get(name)
            branch_units = by_branch[name]
            record["contribution_ids"] = [u["id"] for u in branch_units]
            contribution_reviews = []
            for unit in sorted(branch_units, key=lambda value: value["id"]):
                observation = presence_observations.get(unit["id"], {
                    "contribution_id": unit["id"], "disposition": "UNRESOLVED",
                    "presence": "UNKNOWN", "evidence_sufficient": None,
                    "usable_delta": None, "dependencies": [], "evidence_ids": [],
                    "reasons": ["answer_missing"], "routing_scope": "advisory_only",
                })
                contribution_reviews.append({
                    "contribution_id": unit["id"], "name": unit.get("name"),
                    "kind": unit.get("kind"), "path": unit.get("path"),
                    "source": {"tip": unit.get("source_tip"), "blob": unit.get("source_blob"),
                               "range": unit.get("range")},
                    "destination": {"tip": unit.get("main_tip"),
                                    "candidate_ids": list(unit.get("destination_ids", []))},
                    "disposition": observation.get("disposition", "UNRESOLVED"),
                    "presence": observation.get("presence", "UNKNOWN"),
                    "evidence_sufficient": observation.get("evidence_sufficient"),
                    "usable_delta": observation.get("usable_delta"),
                    "comparison_context_complete": observation.get("comparison_context_complete"),
                    "dependency_context_status": observation.get("dependency_context_status", "unknown"),
                    "dependency_context_sufficient": observation.get("dependency_context_sufficient"),
                    "dependencies": list(observation.get("dependencies", [])),
                    "evidence_ids": list(observation.get("evidence_ids", [])),
                    "reasons": list(observation.get("reasons", [])),
                    "routing_scope": observation.get("routing_scope", "advisory_only"),
                    "presence_origin": presence.get("origin") if presence else None,
                })
            record["contribution_reviews"] = contribution_reviews
            record["presence_origin"] = presence.get("origin") if presence else None
            unresolved_ids = sorted(
                unit["id"] for unit in branch_units
                if judgments.get(unit["id"], {}).get("disposition") in {None, "UNRESOLVED"}
            )
            record["unresolved_contribution_ids"] = unresolved_ids
            record["unresolved_contribution_count"] = len(unresolved_ids)
            record["path_count"] = len(paths[name])
            record["exact_path_count"] = sum(p["exact"] for p in paths[name])
            record["remaining_contribution_count"] = len(branch_units)
            if name == snapshot["main"]["name"]:
                record.update(disposition="RETAIN", reasons=["default_branch"], next_action="Retain main")
            elif branch is None or not branch["eligible"]:
                record.update(disposition="EXCLUDED", reasons=(branch or {}).get("exclusion_reasons", ["missing_snapshot_branch"]),
                              next_action="Reassess activity in a later inventory")
            elif branch["analysis_status"] != "complete":
                record["reasons"].append("contribution_analysis_unavailable")
            else:
                exact = all(p["exact"] for p in paths[name])
                record["snapshot_path_coverage"] = "EXACT" if exact else "DISTINCT"
                if exact:
                    record.update(disposition="EXACT_REVIEW", reasons=["all_net_changed_paths_present_in_pinned_main"],
                                  next_action=(f"Resolve {len(unresolved_ids)} unresolved contribution cases before cleanup planning"
                                               if unresolved_ids else "Run fresh strict coverage and cleanup planning"))
                if cov.get(name, {}).get("verdict") == "EXACT":
                    record["reasons"].append("strict_coverage_exact")
                useful = [u for u in branch_units if judgments.get(u["id"], {}).get("disposition") == "USABLE_WORK_REMAINS"]
                present = [u for u in branch_units if judgments.get(u["id"], {}).get("disposition") == "LIKELY_PRESERVED"]
                record["remaining_contribution_count"] = len(branch_units) - len(present)
                if useful and not exact:
                    record.update(disposition="USABLE_WORK_REMAINS", reasons=["reviewed_evidence_suggests_missing_behavior"],
                                  next_action=("Review proposed integration tasks and resolve unresolved cases: "
                                               + (", ".join(unresolved_ids) if unresolved_ids else "none")))
                    for unit in useful:
                        judgment = judgments[unit["id"]]
                        task_id = "preserve-" + digest({"snapshot": snapshot["snapshot_digest"], "unit": unit["id"]})[:24]
                        task = {
                            "id": task_id, "object_id": key, "contribution_id": unit["id"],
                            "source": {"branch": name, "tip": unit["source_tip"], "path": unit["path"],
                                       "blob": unit.get("source_blob"), "range": unit.get("range"), "name": unit.get("name")},
                            "destination": {"branch": snapshot["main"]["name"], "tip": snapshot["main"]["tip"],
                                            "candidate_ids": unit.get("destination_ids", [])},
                            "dependencies": judgment.get("dependencies", []),
                            "behavior_to_preserve": unit.get("name") or f"File change in {unit['path']}",
                            "verification": "Demonstrate the missing behavior with a reproducer, integrate it, and run the affected package outcome",
                            "status": "PROPOSED", "requires_owner_priority_review": True,
                        }
                        tasks.append(task)
                        record["task_ids"].append(task_id)
                elif present and not exact:
                    record.update(disposition="LIKELY_PRESERVED", reasons=["semantic_presence_requires_verification"],
                                  next_action="Verify destination behavior and review remaining contributions")
                elif not exact:
                    record["reasons"].append("distinct_content_is_not_proof_of_useful_unique_work")
                    record["next_action"] = "Review source and destination evidence for unresolved contributions"
            if name in occupied:
                record["reasons"].append("checked_out_in_worktree")
            record["action_eligibility"] = "LIVE_GATES_REQUIRED"
        elif kind == "worktree":
            reason = "worktree_status_unavailable" if item.get("status") is None else "dirty_worktree" if item["status"] else "worktree_ownership_review"
            record.update(disposition="RETAIN", reasons=[reason], next_action="Establish ownership and preserve any uncommitted work before separate removal approval")
        else:
            record.update(disposition="RETAIN", reasons=["stash_preservation_unverified"], next_action="Inspect locally and verify a preservation destination before separate drop approval")
        evidence_context = {"object_id": key, "source_fingerprint": record["source_fingerprint"]}
        if kind == "branch":
            evidence_context.update({
                "main_tip": snapshot["main"]["tip"],
                "contribution_reviews": record.get("contribution_reviews", []),
                "path_coverage": {"count": record.get("path_count", 0),
                                  "exact_count": record.get("exact_path_count", 0),
                                  "snapshot_verdict": record.get("snapshot_path_coverage")},
                "strict_coverage": cov.get(item["name"]),
            })
        record["review_evidence_fingerprint"] = digest(evidence_context)
        prior = reviewed.get(key)
        if prior is not None:
            if review_version == 1:
                current = False
                status = "historical-limited"
                human_decision = {
                    "disposition": prior["disposition"], "rationale": prior["reason"],
                    "reviewer_id": prior["reviewer"],
                    "proposed_destination": prior.get("destination") or None,
                    "source_fingerprint": prior["source_fingerprint"],
                }
            else:
                current = (review.get("provenance") == review_provenance
                           and prior["source_fingerprint"] == record["source_fingerprint"]
                           and prior["evidence_fingerprint"] == record["review_evidence_fingerprint"])
                status = "current" if current else "stale"
                human_decision = dict(prior)
            record["human_decision"] = human_decision
            record["review_status"] = status
            record["review_approval_status"] = (
                "verified" if review_approval is not None
                else "unverified" if review_version == OUTCOME_REVIEW_VERSION
                else "historical-limited" if review_version == 1
                else "none"
            )
            record["reviewed_disposition"] = prior["disposition"]
            if current:
                record["next_action"] = {
                    "RETAIN": "Retain for the recorded reason",
                    "INTEGRATE": "Review and deliver the specific integration task",
                    "ARCHIVE_PROPOSED": "Create and independently verify a recovery archive",
                    "UNRESOLVED": "Resolve the recorded evidence gap",
                }[prior["disposition"]]
            elif status == "stale":
                record["reasons"] = sorted(set(record["reasons"] + ["review_evidence_stale"]))
                record["next_action"] = "Re-review this object against current pinned evidence"
        records.append(record)
    result = {"kind": "object-outcomes", "schema_version": 1, "repository_id": repository_id,
              "snapshot_digest": snapshot["snapshot_digest"], "inventory_digest": digest(inventory),
              "contributions_digest": contributions["contributions_digest"],
              "presence_digest": digest(presence) if presence else None,
              "project_utility_assessment": {
                  "status": "UNKNOWN",
                  "reason": "project_requirements_not_provided",
                  "source": "code_only_presence_review",
              },
              "review_digest": digest(review) if review else None,
              "human_review_approval": review_approval,
              "human_review_approval_status": (
                  "verified" if review_approval is not None
                  else "unverified" if review_version == OUTCOME_REVIEW_VERSION
                  else "none"
              ),
              "review_document_provenance": (
                  review.get("provenance") if review_version == OUTCOME_REVIEW_VERSION else None
              ),
              "review_provenance": review_provenance,
              "orphaned_reviews": historical_reviews,
              "objects": records, "integration_tasks": tasks,
              "counts": dict(Counter(r["disposition"] for r in records)), "object_count": len(records),
              "cleanup_authorized": False, "live_action_state": "NOT_REVALIDATED"}
    trusted_presence_receipt_digest = None
    if presence is not None and presence.get("origin") == "jev":
        trusted_presence_receipt_digest = digest(presence["trusted_provenance"])
        result["trusted_presence_receipt_digest"] = trusted_presence_receipt_digest
    if trusted_presence_receipt_digest is not None or review_approval is not None:
        from .presence import _signed_record
        body_sha256 = digest(result)
        result["outcome_ledger_receipt"] = _signed_record(OUTCOME_LEDGER_RECEIPT_KIND, {
            "outcomes_body_sha256": body_sha256,
            "repository_id": repository_id,
            "inventory_digest": digest(inventory),
            "snapshot_digest": snapshot["snapshot_digest"],
            "contributions_digest": contributions["contributions_digest"],
            "presence_digest": review_provenance["presence_digest"],
            "trusted_presence_receipt_digest": trusted_presence_receipt_digest,
            "review_approval_digest": digest(review_approval) if review_approval is not None else None,
        })
    result["outcomes_digest"] = digest(result)
    return result


def render_outcomes(ledger: dict) -> str:
    """Self-contained local reviewer for per-object and per-contribution evidence."""
    rows = []
    for row in ledger["objects"]:
        key = escape(row["object_id"], quote=True)
        fingerprint = escape(row["source_fingerprint"], quote=True)
        evidence_fingerprint = escape(row["review_evidence_fingerprint"], quote=True)
        prior = row["human_decision"] or {}
        prior_reviewer = escape(str(prior.get("reviewer_id", prior.get("reviewer", ""))), quote=True)
        prior_reason = escape(str(prior.get("rationale", prior.get("reason", ""))), quote=True)
        prior_destination = escape(str(prior.get("proposed_destination", prior.get("destination", "")) or ""), quote=True)
        prior_disposition = prior.get("disposition")
        selected_options = "".join(
            f"<option{' selected' if value == prior_disposition else ''}>{value}</option>"
            for value in ("RETAIN", "INTEGRATE", "ARCHIVE_PROPOSED", "UNRESOLVED"))
        unresolved_ids = row.get("unresolved_contribution_ids", [])
        unresolved = (f"<details><summary>{len(unresolved_ids)} unresolved contributions</summary>"
                      f"{escape(', '.join(unresolved_ids))}</details>" if unresolved_ids else "")
        contributions = []
        for unit in row.get("contribution_reviews", []):
            safe_name = escape(str(unit.get("name") or unit.get("contribution_id") or "unknown"))
            safe_path = escape(str(unit.get("path") or "path unavailable"))
            dependencies = escape(", ".join(
                f"{item.get('neighbor_id')}={item.get('relevant')}"
                for item in unit.get("dependencies", []) if isinstance(item, dict)
            ) or "none recorded")
            evidence_ids = escape(", ".join(unit.get("evidence_ids", [])) or "none")
            reasons = escape(", ".join(unit.get("reasons", [])) or "none")
            contributions.append(
                "<li><strong>" + safe_name + "</strong> · " + safe_path
                + " · " + escape(str(unit.get("disposition", "UNRESOLVED")))
                + " · presence " + escape(str(unit.get("presence", "UNKNOWN")))
                + " · usable delta " + escape(str(unit.get("usable_delta")))
                + " · route " + escape(str(unit.get("routing_scope", "advisory_only")))
                + "<br>Dependencies: " + dependencies
                + "<br>Evidence IDs: " + evidence_ids
                + "<br>Limits / reasons: " + reasons + "</li>"
            )
        contribution_detail = (
            f"<details><summary>{len(contributions)} contribution judgments · "
            f"origin {escape(str(row.get('presence_origin') or 'none'))}</summary><ul>"
            + "".join(contributions) + "</ul></details>"
        ) if row["kind"] == "branch" else ""
        status = escape(str(row.get("review_status", "unreviewed")))
        approval_status = escape(str(row.get("review_approval_status", "none")))
        rows.append("<tr>" + "".join(f"<td>{escape(str(v))}</td>" for v in (
            row["kind"], row["name"], row["disposition"], ", ".join(row["reasons"]),
            f"{len(row['contribution_ids'])} total / {row.get('unresolved_contribution_count', 0)} unresolved")) +
            f"<td>{escape(str(row['next_action']))}{unresolved}{contribution_detail}</td>"
            f"<td><span>Prior review: {status}; approval: {approval_status}</span>"
            f"<div data-object='{key}' data-kind='{escape(row['kind'], quote=True)}' "
            f"data-fingerprint='{fingerprint}' data-evidence-fingerprint='{evidence_fingerprint}' "
            f"data-prior-reviewer='{prior_reviewer}'>"
            f"<select aria-label='Disposition'><option value=''>No new decision</option>{selected_options}</select>"
            f"<input class='reason' aria-label='Reason' placeholder='Reason' value='{prior_reason}'>"
            f"<input class='destination' aria-label='Proposed destination' placeholder='Proposed destination' value='{prior_destination}'></div></td></tr>")
    provenance_js = json.dumps(ledger["review_provenance"], separators=(",", ":")).replace("<", "\\u003c")
    repository_js = json.dumps(ledger["repository_id"]).replace("<", "\\u003c")
    return ("<!doctype html><html lang='en'><meta charset='utf-8'><title>Git work disposition</title>"
            "<style>body{font:15px system-ui;margin:2rem}table{border-collapse:collapse;width:100%}"
            "td,th{padding:.6rem;text-align:left;border-bottom:1px solid #ccc;vertical-align:top}"
            "input{padding:.6rem;width:90%}details{max-width:70rem}li{margin:.5rem 0}</style>"
            f"<h1>Git work disposition</h1><p>{ledger['object_count']} objects. Live action state: NOT REVALIDATED."
            " Jev results are advisory and never authorize deletion.</p>"
            "<p>Project utility: UNKNOWN because project requirements were not provided.</p>"
            "<p>Contribution detail contains typed decisions and evidence IDs only; source excerpts are not included.</p>"
            "<p>An exported review document requires a separate exact-digest local approval before it can route implementation work.</p>"
            "<p><input id='reviewer' aria-label='Reviewer' placeholder='Reviewer name'>"
            "<button id='export'>Export review decisions</button> <span id='message' role='status'></span></p>"
            "<input id='search' aria-label='Filter objects' placeholder='Filter objects'>"
            "<table><thead><tr><th>Kind</th><th>Object</th><th>Disposition</th><th>Evidence / holds</th>"
            "<th>Units</th><th>Next action</th><th>Operator review</th></tr></thead><tbody>" + "".join(rows) +
            "</tbody></table><script>document.getElementById('search').addEventListener('input',e=>{"
            "const q=e.target.value.toLowerCase();document.querySelectorAll('tbody tr').forEach(r=>{"
            "r.hidden=!r.textContent.toLowerCase().includes(q)})});"
            "document.getElementById('export').addEventListener('click',()=>{"
            "const reviewer=document.getElementById('reviewer').value.trim();const decisions=[];"
            "let invalid=false;document.querySelectorAll('[data-object]').forEach(cell=>{"
            "const disposition=cell.querySelector('select').value;if(!disposition)return;"
            "const reason=cell.querySelector('.reason').value.trim();const rowReviewer=reviewer||cell.dataset.priorReviewer;"
            "if(!reason||!rowReviewer)invalid=true;"
            "decisions.push({object_id:cell.dataset.object,kind:cell.dataset.kind,"
            "source_fingerprint:cell.dataset.fingerprint,evidence_fingerprint:cell.dataset.evidenceFingerprint,"
            "disposition,rationale:reason,reviewer_id:rowReviewer,reviewed_at:new Date().toISOString(),"
            "proposed_destination:cell.querySelector('.destination').value.trim()||null,preservation_proof:null});});"
            "if(invalid||!decisions.length){document.getElementById('message').textContent="
            "'Enter reviewer, disposition and reason before exporting.';return;}"
            f"const doc={{kind:'outcome-review',schema_version:{OUTCOME_REVIEW_VERSION},repository_id:{repository_js},"
            f"provenance:{provenance_js},decisions}};"
            "const url=URL.createObjectURL(new Blob([JSON.stringify(doc,null,2)],{type:'application/json'}));"
            "const a=document.createElement('a');a.href=url;a.download='outcome-review.json';a.click();"
            "setTimeout(()=>URL.revokeObjectURL(url),1000);document.getElementById('message').textContent="
            "'Review exported. Inspect the exact file, then run jg outcome-review-approve --review outcome-review.json to see its digest. A second call must supply that digest and --out to approve it.';"
            "});</script></html>")


def write_outcomes(inventory_path: str, snapshot_path: str, contributions_path: str,
                   out: str | Path, presence_path: str | None = None,
                   coverage_path: str | None = None, review_path: str | None = None,
                   review_approval_path: str | None = None) -> Path:
    from .snapshot import load_snapshot
    snapshot, _ = load_snapshot(snapshot_path)
    result = build_outcomes(read_json(inventory_path), snapshot, read_json(contributions_path),
                            read_json(presence_path) if presence_path else None,
                            read_json(coverage_path) if coverage_path else None,
                            read_json(review_path) if review_path else None,
                            read_json(review_approval_path) if review_approval_path else None)
    destination = Path(out)
    if destination.exists():
        raise JgError("outcomes directory already exists; preserve the previous review")
    destination.mkdir(mode=0o700, parents=True)
    write_json(destination / "outcomes.json", result)
    write_private_text(destination / "index.html", render_outcomes(result))
    return destination / "outcomes.json"
