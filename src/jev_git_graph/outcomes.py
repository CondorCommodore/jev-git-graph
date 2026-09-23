"""One disposition per inventoried object, with concrete preservation tasks.

This is advisory output. Only the guarded cleanup executor can remove refs.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from html import escape
import json
from pathlib import Path
from typing import Any

from .errors import JgError
from .groups import _validate as validate_contributions
from .preservation import _object_index
from .review import object_fingerprint
from .safety import digest, read_json, write_json, write_private_text


def _signed(document: dict, field: str) -> None:
    if document.get(field) != digest({k: v for k, v in document.items() if k != field}):
        raise JgError(f"invalid {field}")


def build_outcomes(inventory: dict, snapshot: dict, contributions: dict,
                   presence: dict | None = None, coverage: dict | None = None,
                   review: dict | None = None) -> dict:
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
        by_branch[unit["branch"]].append(unit)
    paths: dict[str, list] = defaultdict(list)
    for path in contributions["paths"]:
        paths[path["branch"]].append(path)
    judgments = {}
    if presence is not None:
        # The presence module owns version dispatch and reconciliation. Never
        # infer production recommendations from imported test/control answers.
        from .presence import validate_outcome_presence
        judgments = validate_outcome_presence(presence, contributions)
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
    reviewed = {}
    if review is not None:
        if (review.get("kind") != "outcome-review" or review.get("schema_version") != 1
                or review.get("snapshot_digest") != snapshot["snapshot_digest"]
                or not isinstance(review.get("decisions"), list)):
            raise JgError("invalid outcome review provenance")
        for decision in review["decisions"]:
            key = decision.get("object_id")
            if key not in objects or key in reviewed:
                raise JgError("unknown or duplicate reviewed object")
            kind, item = objects[key]
            if decision.get("source_fingerprint") != object_fingerprint(kind, item):
                raise JgError("review source fingerprint changed")
            if (decision.get("disposition") not in {"RETAIN", "INTEGRATE", "ARCHIVE_PROPOSED", "UNRESOLVED"}
                    or not all(isinstance(decision.get(f), str) and decision[f].strip()
                               for f in ("reason", "reviewer"))):
                raise JgError("review requires a supported disposition, reason and reviewer")
            reviewed[key] = decision
    occupied = {w.get("branch") for w in inventory["worktrees"] if w.get("branch")}
    records, tasks = [], []
    for key, (kind, item) in objects.items():
        record: dict[str, Any] = {
            "object_id": key, "kind": kind, "source_fingerprint": object_fingerprint(kind, item),
            "name": item.get("name") or item.get("path_id") or item.get("reference"),
            "tip": item.get("tip") or item.get("head") or item.get("sha"),
            "disposition": "UNRESOLVED", "reasons": [], "next_action": "Review missing evidence",
            "contribution_ids": [], "task_ids": [], "cleanup_authorized": False,
            "human_decision": reviewed.get(key),
        }
        if kind == "branch":
            name = item["name"]
            branch = branches.get(name)
            branch_units = by_branch[name]
            record["contribution_ids"] = [u["id"] for u in branch_units]
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
                                  next_action="Run fresh strict coverage and cleanup planning")
                if cov.get(name, {}).get("verdict") == "EXACT":
                    record["reasons"].append("strict_coverage_exact")
                useful = [u for u in branch_units if judgments.get(u["id"], {}).get("disposition") == "USABLE_WORK_REMAINS"]
                present = [u for u in branch_units if judgments.get(u["id"], {}).get("disposition") == "LIKELY_PRESERVED"]
                record["remaining_contribution_count"] = len(branch_units) - len(present)
                if useful and not exact:
                    record.update(disposition="USABLE_WORK_REMAINS", reasons=["reviewed_evidence_suggests_missing_behavior"],
                                  next_action="Review proposed integration tasks and required behavior")
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
        if key in reviewed:
            record["reviewed_disposition"] = reviewed[key]["disposition"]
            record["next_action"] = {"RETAIN": "Retain for the recorded reason", "INTEGRATE": "Review and deliver the specific integration task",
                                     "ARCHIVE_PROPOSED": "Create and independently verify a recovery archive", "UNRESOLVED": "Resolve the recorded evidence gap"}[reviewed[key]["disposition"]]
        records.append(record)
    result = {"kind": "object-outcomes", "schema_version": 1, "repository_id": repository_id,
              "snapshot_digest": snapshot["snapshot_digest"], "inventory_digest": digest(inventory),
              "contributions_digest": contributions["contributions_digest"],
              "presence_digest": digest(presence) if presence else None,
              "review_digest": digest(review) if review else None,
              "objects": records, "integration_tasks": tasks,
              "counts": dict(Counter(r["disposition"] for r in records)), "object_count": len(records),
              "cleanup_authorized": False, "live_action_state": "NOT_REVALIDATED"}
    result["outcomes_digest"] = digest(result)
    return result


def render_outcomes(ledger: dict) -> str:
    """Self-contained review page; no external assets or source code."""
    rows = []
    for row in ledger["objects"]:
        detail = escape(str(row["human_decision"] or ""))
        key = escape(row["object_id"], quote=True)
        fingerprint = escape(row["source_fingerprint"], quote=True)
        rows.append("<tr>" + "".join(f"<td>{escape(str(v))}</td>" for v in (
            row["kind"], row["name"], row["disposition"], ", ".join(row["reasons"]),
            len(row["contribution_ids"]), row["next_action"])) +
            f"<td data-object='{key}' data-fingerprint='{fingerprint}'>{detail}"
            "<select aria-label='Disposition'><option value=''>No new decision</option>"
            "<option>RETAIN</option><option>INTEGRATE</option><option>ARCHIVE_PROPOSED</option>"
            "<option>UNRESOLVED</option></select><input class='reason' aria-label='Reason' placeholder='Reason'>"
            "<input class='destination' aria-label='Destination' placeholder='Proposed destination'></td></tr>")
    snapshot_js = json.dumps(ledger["snapshot_digest"]).replace("<", "\\u003c")
    return ("<!doctype html><html lang='en'><meta charset='utf-8'><title>Git work disposition</title>"
            "<style>body{font:15px system-ui;margin:2rem}table{border-collapse:collapse;width:100%}"
            "td,th{padding:.6rem;text-align:left;border-bottom:1px solid #ccc}input{padding:.6rem;width:90%}</style>"
            f"<h1>Git work disposition</h1><p>{ledger['object_count']} objects. Live action state: NOT REVALIDATED.</p>"
            "<p>Search objects, reasons and next actions. Integration task details are in outcomes.json.</p>"
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
            "let invalid=!reviewer;document.querySelectorAll('[data-object]').forEach(cell=>{"
            "const disposition=cell.querySelector('select').value;if(!disposition)return;"
            "const reason=cell.querySelector('.reason').value.trim();if(!reason)invalid=true;"
            "decisions.push({object_id:cell.dataset.object,source_fingerprint:cell.dataset.fingerprint,"
            "disposition,reason,reviewer,destination:cell.querySelector('.destination').value.trim()});});"
            "if(invalid||!decisions.length){document.getElementById('message').textContent="
            "'Enter reviewer, disposition and reason before exporting.';return;}"
            f"const doc={{kind:'outcome-review',schema_version:1,snapshot_digest:{snapshot_js},decisions}};"
            "const url=URL.createObjectURL(new Blob([JSON.stringify(doc,null,2)],{type:'application/json'}));"
            "const a=document.createElement('a');a.href=url;a.download='outcome-review.json';a.click();"
            "setTimeout(()=>URL.revokeObjectURL(url),1000);document.getElementById('message').textContent="
            "'Review exported. Import it with jg outcomes --review; no Git action was performed.';"
            "});</script></html>")


def write_outcomes(inventory_path: str, snapshot_path: str, contributions_path: str,
                   out: str | Path, presence_path: str | None = None,
                   coverage_path: str | None = None, review_path: str | None = None) -> Path:
    from .snapshot import load_snapshot
    snapshot, _ = load_snapshot(snapshot_path)
    result = build_outcomes(read_json(inventory_path), snapshot, read_json(contributions_path),
                            read_json(presence_path) if presence_path else None,
                            read_json(coverage_path) if coverage_path else None,
                            read_json(review_path) if review_path else None)
    destination = Path(out)
    if destination.exists():
        raise JgError("outcomes directory already exists; preserve the previous review")
    destination.mkdir(mode=0o700, parents=True)
    write_json(destination / "outcomes.json", result)
    write_private_text(destination / "index.html", render_outcomes(result))
    return destination / "outcomes.json"
