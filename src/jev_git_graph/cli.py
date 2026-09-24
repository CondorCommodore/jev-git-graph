from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from . import git
from .candidates import write_candidates
from .batches import prepare_batches, collect_batches
from .calibration import write_calibration
from .coverage import write_coverage
from .cleanup import approve_cleanup_plan, execute_cleanup, write_cleanup_plan
from .coordinator import (DISPOSABLE_FIXTURE_ROOT, CleanupActionJournal, CooperativeBranchLeaseAdapter,
                          CreatorLeaseCapability, DisposableFixtureLeaseCapability,
                          _common_dir, default_cleanup_journal_path,
                          reconcile_interrupted_cleanup)
from .code_evidence import approve_code_batch, build_code_evidence, build_transient_preview
from .credential_cache import KeychainLease, credential_status, resolve_provider_token, serve_cache_form
from .decisions import write_decisions
from .equivalence import write_equivalence
from .errors import JgError
from .inventory import protected_worktree_paths, write_inventory
from .jev import DEFAULT_MAX_JEV_PAYLOAD_BYTES, DEFAULT_MAX_JEV_REQUESTS, EVIDENCE_PROFILES, checkpoint_lock, execute_preview, execute_transient_preview, write_preview
from .plan import write_plan
from .residual import analyze_residual
from .snapshot import load_snapshot, write_snapshot
from .contributions import write_contributions
from .groups import write_groups
from .outcomes import approve_outcome_review, write_outcomes
from .preservation import write_preservation_plan
from .study import (build_selected_study, validate_selected_range_manifest,
                    write_selected_study_artifacts, write_study)
from .snapshot import load_snapshot
from .transient_preview import serve_presence_preview


def _build_group_presence_preview(args):
    """Rebuild an approved presence payload without persisting request text."""
    snapshot, object_repo = load_snapshot(args.snapshot)
    contributions = read_json(args.contributions)
    groups = read_json(args.groups)
    if (snapshot.get("snapshot_digest") != contributions.get("snapshot_digest") or
            contributions.get("contributions_digest") != groups.get("contributions_digest") or
            snapshot.get("snapshot_digest") != groups.get("snapshot_digest")):
        raise JgError("snapshot, contributions, and groups pins do not match")
    if args.evidence_ranges:
        range_manifest = read_json(args.evidence_ranges)
        expected = {
            "snapshot_digest": snapshot["snapshot_digest"],
            "contributions_digest": contributions["contributions_digest"],
            "groups_digest": groups["groups_digest"],
        }
        if (range_manifest.get("kind") != "branch-presence-range-manifest" or
                range_manifest.get("schema_version") != 1 or
                any(range_manifest.get(key) != value for key, value in expected.items()) or
                not isinstance(range_manifest.get("ranges"), list)):
            raise JgError("evidence range manifest is invalid or belongs to different pinned artifacts")
    else:
        range_manifest = {
            "kind": "branch-presence-range-manifest", "schema_version": 1,
            "snapshot_digest": snapshot["snapshot_digest"],
            "contributions_digest": contributions["contributions_digest"],
            "groups_digest": groups["groups_digest"], "ranges": [],
        }
    study = read_json(args.study) if args.study else None
    selected_ids = None
    selection_digest = None
    project_goals = ""
    if study is not None:
        if (study.get("kind") != "presence-study" or study.get("schema_version") not in {2, 3}
                or study.get("study_digest") != digest({k: v for k, v in study.items() if k != "study_digest"})
                or any(study.get(key) != expected for key, expected in (
                    ("snapshot_digest", snapshot["snapshot_digest"]),
                    ("contributions_digest", contributions["contributions_digest"]),
                    ("groups_digest", groups["groups_digest"])) )):
            raise JgError("study is invalid or belongs to different pinned artifacts")
        cases = study.get("cases")
        if not isinstance(cases, list) or not cases:
            raise JgError("study must contain at least one selected case")
        selected_ids = [case.get("contribution_id") for case in cases if isinstance(case, dict)]
        if len(selected_ids) != len(cases) or len(selected_ids) != len(set(selected_ids)):
            raise JgError("study cases must have unique contribution IDs")
        selection_digest = study["study_digest"]
        project_goals = study.get("project_goals", "")
        if study.get("selection_policy") == "explicit-validated-case-list-v1":
            validate_selected_range_manifest(study, range_manifest)
    model_settings = read_json(args.model_settings) if args.model_settings else None
    units = {unit["id"]: unit for unit in contributions.get("units", [])}
    evidence_by_contribution = {}
    source_only_excerpt_count = 0
    source_only_excerpt_lines = 0
    source_only_excerpt_bytes = 0
    for item in range_manifest["ranges"]:
        if not isinstance(item, dict) or item.get("contribution_id") not in units:
            raise JgError("evidence range manifest refers to an unknown contribution")
        unit = units[item["contribution_id"]]
        if item.get("source_tip") != unit.get("source_tip") or item.get("destination_tip") != unit.get("main_tip"):
            raise JgError("evidence ranges do not match pinned contribution endpoints")
        approved_ranges = item.get("ranges")
        if not isinstance(approved_ranges, list):
            raise JgError("evidence ranges must be a list")
        if item.get("arm") == "source_only_unknown":
            if unit.get("destination_ids") or unit.get("dependency_context_status") != "unknown":
                raise JgError("source-only evidence requires an unknown unit with no destination candidates")
            if not approved_ranges or source_only_excerpt_count >= 8 or source_only_excerpt_lines >= 240:
                raise JgError("source-only cohort lacks room for its bounded approved source excerpts")
            first = approved_ranges[0]
            source_range = first.get("source_range", {})
            start_line, end_line = source_range.get("start_line"), source_range.get("end_line")
            if (not isinstance(start_line, int) or not isinstance(end_line, int)
                    or start_line < 1 or end_line < start_line):
                raise JgError("source-only range manifest has invalid line bounds")
            emitted_lines = min(30, 240 - source_only_excerpt_lines, end_line - start_line + 1)
            emitted = {"evidence_id": first.get("evidence_id"),
                       "source_path": first.get("source_path"),
                       "source_range": {"start_line": start_line,
                                        "end_line": start_line + emitted_lines - 1}}
            evidence = build_source_only_evidence(
                object_repo, item["source_tip"], item["destination_tip"], [emitted],
                max_total_bytes=24_000 - source_only_excerpt_bytes,
                max_excerpt_count=8 - source_only_excerpt_count,
                max_total_lines=240 - source_only_excerpt_lines)
            omitted_ranges = []
            if emitted["source_range"]["end_line"] < end_line:
                omitted_ranges.append({"source_path": first.get("source_path"),
                                       "source_range": {"start_line": emitted["source_range"]["end_line"] + 1,
                                                        "end_line": end_line}})
            omitted_ranges.extend({"source_path": spec.get("source_path"),
                                   "source_range": spec.get("source_range")}
                                  for spec in approved_ranges[1:])
            evidence["excerpt_scope"] = "bounded approved source excerpt only; destination presence and integration remain unknown"
            evidence["source_context_non_exhaustive"] = True
            evidence["omitted_source_range_count"] = len(omitted_ranges)
            evidence["omitted_source_ranges_digest"] = digest(omitted_ranges)
            evidence["evidence_digest"] = digest({key: value for key, value in evidence.items()
                                                    if key != "evidence_digest"})
            evidence_by_contribution[item["contribution_id"]] = evidence
            source_only_excerpt_count += len(evidence["records"])
            source_only_excerpt_lines += emitted_lines
            source_only_excerpt_bytes += evidence["total_bytes"]
        elif approved_ranges:
            evidence_by_contribution[item["contribution_id"]] = build_two_sided_evidence(
                object_repo, item["source_tip"], item["destination_tip"], approved_ranges)
        # An explicitly selected case with no approved pair stays in the request
        # plan. The builder records missing comparison evidence and reconciliation
        # fails closed for that contribution.
    plan_kwargs = {
        "max_groups": args.max_groups,
        "max_requests": args.max_requests,
        "max_request_bytes": args.max_request_bytes,
        "model_settings": model_settings,
        "selected_contribution_ids": selected_ids,
        "selection_digest": selection_digest,
        "project_goals": project_goals,
    }
    if args.auto_estimate_input_tokens:
        if args.estimated_input_tokens is not None:
            raise JgError("choose automatic or caller-supplied input token estimate")
        if args.max_provider_tokens is None:
            raise JgError("automatic token estimation requires --max-provider-tokens")
        sizing = build_group_requests(contributions, groups, evidence_by_contribution, **plan_kwargs)
        # This is a labeled conservative planning heuristic, not a tokenizer
        # measurement: one token per serialized byte plus provider framing headroom.
        estimate = sizing["payload_bytes"] + 256 * sizing["request_count"]
        if estimate > args.max_provider_tokens:
            sizes = [len(canonical_json(request)) for request in sizing["requests"]]
            maps = [len(canonical_json({
                "case_ids": request.get("state", {}).get("cohort_contribution_ids", []),
                "edges": request.get("state", {}).get("cohort_relationship_edges", []),
            })) for request in sizing["requests"]]
            raise JgError(
                "serialized_utf8_bytes_plus_256_per_request_v1 estimate "
                f"{estimate} exceeds max_provider_tokens {args.max_provider_tokens}; "
                f"serialized_bytes={sizing['payload_bytes']}; requests={sizing['request_count']}; "
                f"max_request_bytes={max(sizes, default=0)}; "
                f"mean_request_bytes={(sum(sizes) / len(sizes)) if sizes else 0:.1f}; "
                f"cohort_map_bytes_max={max(maps, default=0)}"
            )
        plan = build_group_requests(
            contributions, groups, evidence_by_contribution, **plan_kwargs,
            estimated_input_tokens=estimate, max_provider_tokens=args.max_provider_tokens,
            token_estimator="serialized_utf8_bytes_plus_256_per_request_v1")
    else:
        plan = build_group_requests(
            contributions, groups, evidence_by_contribution, **plan_kwargs,
            estimated_input_tokens=args.estimated_input_tokens,
            max_provider_tokens=args.max_provider_tokens)
    preview = approved_presence_preview(plan)
    excerpt_stats = []
    for request in preview["requests"]:
        cases_with_excerpt, excerpt_count, excerpt_bytes, excerpt_lines = [], 0, 0, 0
        for item in request.get("state", {}).get("contributions", []):
            evidence = item.get("evidence") or {}
            records = evidence.get("records", [])
            if records:
                cases_with_excerpt.append(item.get("contribution_id"))
                excerpt_count += len(records)
                excerpt_bytes += int(evidence.get("total_bytes", 0))
                excerpt_lines += sum(len(side.get("text", "").splitlines()) for record in records
                                     for side in record.values() if isinstance(side, dict) and "text" in side)
        excerpt_stats.append({"case_ids_with_excerpt": cases_with_excerpt, "excerpt_count": excerpt_count,
                              "excerpt_bytes": excerpt_bytes, "excerpt_lines": excerpt_lines})
    replay = {
        "kind": "branch-presence-approved-manifest", "schema_version": 1,
        "snapshot_digest": snapshot["snapshot_digest"],
        "contributions_digest": contributions["contributions_digest"],
        "groups_digest": groups["groups_digest"],
        "selected_contribution_ids": selected_ids,
        "selection_digest": selection_digest,
        "range_manifest_digest": digest(range_manifest),
        "model_settings": model_settings,
        "model_settings_digest": preview["model_settings_digest"],
        "request_budgets": preview["request_budgets"],
        "request_count": preview["request_count"], "payload_bytes": preview["payload_bytes"],
        "request_bytes_by_chunk": preview["request_bytes_by_chunk"],
        "request_case_ids": [[item.get("contribution_id") for item in request.get("state", {}).get("contributions", [])]
                             for request in preview["requests"]],
        "request_question_case_ids": [sorted({key.split(":", 1)[0] for key in request.get("questions", {})})
                                       for request in preview["requests"]],
        "request_excerpt_stats": excerpt_stats,
        "source_only_unknown_ids": sorted(case.get("contribution_id") for case in (study or {}).get("cases", [])
                                           if case.get("selection_arm") == "source_only_unknown"),
        "two_sided_control_ids": sorted(case.get("contribution_id") for case in (study or {}).get("cases", [])
                                         if case.get("selection_arm") == "two_sided_control"),
        "cohort_case_count": len(selected_ids or []),
        "cohort_relationship_edge_count": max(
            (request.get("state", {}).get("cohort_relationship_edge_count", 0) for request in preview["requests"]),
            default=0),
        "cohort_relationship_edge_digest": next((request.get("state", {}).get("cohort_relationship_edge_digest")
                                                  for request in preview["requests"]), None),
        "cohort_relationships_non_exhaustive": bool(preview["requests"] and
            preview["requests"][0].get("state", {}).get("cohort_relationships_non_exhaustive")),
        "payload_sha256": preview["payload_sha256"], "plan_digest": preview["plan_digest"],
        "approval_sha256": preview["approval_sha256"],
    }
    purpose = next((request.get("state", {}).get("project_purpose")
                    for request in preview["requests"]
                    if request.get("state", {}).get("project_purpose")), None)
    if purpose is not None:
        replay["project_goal_sha256"] = purpose["sha256"]
        replay["project_goal_version"] = purpose["version"]
    replay["manifest_digest"] = digest(replay)
    return object_repo, range_manifest, replay, preview


def _cleanup_executor_context(repo: str, plan: dict, journal_arg: str | None,
                              fixture_inventory_arg: str | None):
    root, _common, _runner = git.open_repository(repo)
    common_dir = _common_dir(root)
    journal_path = Path(journal_arg).expanduser() if journal_arg else default_cleanup_journal_path(common_dir)
    journal_path = validate_output_path(
        journal_path, [*protected_worktree_paths(root), common_dir])
    branch_names = sorted(str(item.get("name")) for item in plan.get("candidates", []))
    if fixture_inventory_arg:
        fixture_path = Path(fixture_inventory_arg).expanduser()
        if fixture_path.is_symlink():
            raise JgError("disposable fixture inventory must not be a symlink")
        fixture_path = fixture_path.resolve(strict=True)
        fixture_root = DISPOSABLE_FIXTURE_ROOT.resolve(strict=False)
        try:
            fixture_path.relative_to(fixture_root)
        except ValueError as exc:
            raise JgError("disposable fixture inventory is outside the controlled fixture root") from exc
        fixture_inventory = read_json(fixture_path)
        lease = CooperativeBranchLeaseAdapter.for_disposable_fixture(
            root, fixture_inventory, branch_names, fixture_root=fixture_root)
        return lease, journal_path, None
    try:
        lease = CooperativeBranchLeaseAdapter.for_production_repository(root)
        return lease, journal_path, None
    except JgError as exc:
        return None, journal_path, str(exc)
from .group_requests import (
    approved_presence_preview,
    build_group_requests,
    build_source_only_evidence,
    build_two_sided_evidence,
)
from .presence import (
    execute_presence_preview,
    import_control_answers,
    import_synthetic_answers,
    reconcile_presence,
)
from .presence_calibration import build_presence_calibration
from .resume import resume_batches
from .safety import canonical_json, digest, opaque_path_id, read_json, validate_output_path, write_json
from .viewer import serve as serve_viewer


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="jg", description="Local-first Git relationship review")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)

    credential = commands.add_parser("credential", help="manage a 48-hour Jev Keychain credential lease")
    credential_actions = credential.add_subparsers(dest="credential_action", required=True)
    credential_actions.add_parser("cache", help="open a one-time local form for Jev_Key")
    credential_actions.add_parser("status", help="show cache expiry without reading the key aloud")
    credential_actions.add_parser("clear", help="remove the cached Jev key")

    inventory = commands.add_parser("inventory", help="collect a read-only local Git snapshot")
    inventory.add_argument("--repo", required=True)
    inventory.add_argument("--out", required=True)

    snapshot = commands.add_parser("snapshot", help="preserve immutable analysis commits in an independent local object store")
    snapshot.add_argument("--repo", required=True)
    snapshot.add_argument("--inventory", required=True)
    snapshot.add_argument("--out", required=True)
    snapshot.add_argument("--recent-hours", type=float, default=24)

    contributions = commands.add_parser("contributions", help="account for changed units in a pinned snapshot without model calls")
    contributions.add_argument("--repo", required=True)
    contributions.add_argument("--snapshot", required=True)
    contributions.add_argument("--out", required=True)
    contributions.add_argument("--workers", type=int, help="CPU worker processes for independent branch analysis")
    contributions.add_argument("--checkpoint", help="private resumable branch-analysis checkpoint file")
    contributions.add_argument("--max-destination-blobs", type=int)

    groups = commands.add_parser("groups", help="build bounded local contribution context groups")
    groups.add_argument("--repo", required=True)
    groups.add_argument("--contributions", required=True)
    groups.add_argument("--out", required=True)
    groups.add_argument("--max-units", type=int, default=24)
    groups.add_argument("--max-edges", type=int, default=1000)

    group_relate = commands.add_parser("group-relate", help="plan or explicitly execute bounded branch-presence requests")
    group_relate.add_argument("--snapshot", required=True)
    group_relate.add_argument("--contributions", required=True)
    group_relate.add_argument("--groups", required=True)
    group_relate.add_argument("--study", help="digest-validated study whose cases bound requested contributions")
    group_relate.add_argument("--evidence-ranges")
    group_relate.add_argument("--model-settings")
    group_relate.add_argument("--out", required=True)
    group_relate.add_argument("--show-preview", action="store_true",
                              help="serve exact request content transiently from a loopback no-store page")
    group_relate.add_argument("--max-groups", type=int, default=64,
                              help="maximum distinct source groups in the request batch")
    group_relate.add_argument("--max-requests", type=int, default=64,
                              help="maximum chunk requests in the bounded batch")
    group_relate.add_argument("--max-request-bytes", type=int, default=64000)
    group_relate.add_argument("--estimated-input-tokens", type=int)
    group_relate.add_argument("--auto-estimate-input-tokens", action="store_true",
                              help="derive a labeled planning estimate from exact serialized request bytes")
    group_relate.add_argument("--max-provider-tokens", type=int)
    group_relate.add_argument("--execute", action="store_true")
    group_relate.add_argument("--approved-payload-sha256")
    group_relate.add_argument("--approved-approval-sha256")
    group_relate.add_argument("--max-workers", type=int, default=2)
    group_relate.add_argument("--checkpoint")
    group_relate.add_argument("--answers")
    group_relate.add_argument("--answer-origin", choices=("synthetic", "control"))

    presence_reconcile = commands.add_parser("presence-reconcile", help="reconcile presence answers against pinned groups")
    presence_reconcile.add_argument("--contributions", required=True)
    presence_reconcile.add_argument("--groups", required=True)
    presence_reconcile.add_argument("--answers", required=True)
    presence_reconcile.add_argument("--execution-receipt", help="local signed receipt required for Jev-origin answers")
    presence_reconcile.add_argument("--out", required=True)

    presence_calibrate = commands.add_parser("presence-calibrate", help="compare reviewed presence labels with Jev and control results")
    presence_calibrate.add_argument("--labels", required=True)
    presence_calibrate.add_argument("--jev", required=True)
    presence_calibrate.add_argument("--control", required=True)
    presence_calibrate.add_argument("--jev-metadata")
    presence_calibrate.add_argument("--control-metadata")
    presence_calibrate.add_argument("--out", required=True)

    study = commands.add_parser("study", help="select a bounded, family-stratified review study")
    study.add_argument("--repo", required=True)
    study.add_argument("--contributions", required=True)
    study.add_argument("--groups", required=True)
    study.add_argument("--out", required=True)
    study.add_argument("--count", type=int, default=32)
    study.add_argument("--max-per-family", type=int, default=4)
    study.add_argument("--exclude-branch", action="append", default=[])
    study.add_argument("--project-goals", default="")
    study.add_argument("--selection-manifest", help="digest-bound explicit contribution IDs and evidence arms")
    study.add_argument("--selection-policy", choices=("candidate-availability-24-8-v2", "dependency-complete-majority-v1", "behavior-focused-v1"),
                       default="candidate-availability-24-8-v2")

    outcomes = commands.add_parser("outcomes", help="account for every object and render preservation tasks and review page")
    outcomes.add_argument("--repo", required=True)
    outcomes.add_argument("--inventory", required=True)
    outcomes.add_argument("--snapshot", required=True)
    outcomes.add_argument("--contributions", required=True)
    outcomes.add_argument("--presence")
    outcomes.add_argument("--coverage")
    outcomes.add_argument("--review")
    outcomes.add_argument("--review-approval", help="signed local receipt for the exact v2 review document")
    outcomes.add_argument("--delivery-observations", help="pinned metadata-only PR delivery observations")
    outcomes.add_argument("--out", required=True)

    outcome_review_approve = commands.add_parser(
        "outcome-review-approve", help="approve one exact v2 human review document by digest"
    )
    outcome_review_approve.add_argument("--review", required=True)
    outcome_review_approve.add_argument("--approved-review-sha256")
    outcome_review_approve.add_argument("--out")

    preservation_queue = commands.add_parser(
        "preservation-queue", help="build the canonical non-destructive queue from pinned outcomes"
    )
    preservation_queue.add_argument("--repo", required=True)
    preservation_queue.add_argument("--inventory", required=True)
    preservation_queue.add_argument("--outcomes", required=True)
    preservation_queue.add_argument("--candidates")
    preservation_queue.add_argument("--relations")
    preservation_queue.add_argument("--review")
    preservation_queue.add_argument("--out", required=True)

    candidates = commands.add_parser("candidates", help="build bounded deterministic relationship candidates")
    candidates.add_argument("--repo", required=True)
    candidates.add_argument("--inventory", required=True)
    candidates.add_argument("--out", required=True)
    candidates.add_argument("--max-candidates", type=int, default=200)
    candidates.add_argument("--allow-incomplete", action="store_true", help="explore recorded immutable tips from an explicitly incomplete snapshot; no cleanup verdict")

    relate = commands.add_parser("relate", help="preview or execute opt-in Jev relationship questions")
    relate.add_argument("--repo", required=True)
    relate.add_argument("--candidates", required=True)
    relate.add_argument("--out", required=True)
    relate.add_argument("--preview", action="store_true", help="write a local, exact request preview")
    relate.add_argument("--include-branch-labels", action="store_true")
    relate.add_argument("--evidence-profile", choices=EVIDENCE_PROFILES, default="minimal")
    relate.add_argument("--use-jev", action="store_true", help="send an already approved preview to Jev")
    relate.add_argument("--approved-preview")
    relate.add_argument("--approved-payload-sha256")
    relate.add_argument("--max-jev-requests", type=int, default=DEFAULT_MAX_JEV_REQUESTS)
    relate.add_argument("--max-jev-payload-bytes", type=int, default=DEFAULT_MAX_JEV_PAYLOAD_BYTES)
    code_relate = commands.add_parser("code-relate", help="transient opt-in Python excerpt preview or approved Jev request")
    code_relate.add_argument("--repo", required=True)
    code_relate.add_argument("--candidates", required=True)
    code_relate.add_argument("--selection", required=True, help="JSON mapping candidate IDs to pinned tips, optional refs, and ranges")
    code_relate.add_argument("--use-jev", action="store_true")
    code_relate.add_argument("--approved-payload-sha256")
    code_relate.add_argument("--approved-batch-sha256")
    code_relate.add_argument("--max-jev-requests", type=int, default=DEFAULT_MAX_JEV_REQUESTS)
    code_relate.add_argument("--max-jev-payload-bytes", type=int, default=DEFAULT_MAX_JEV_PAYLOAD_BYTES)
    code_relate.add_argument("--out", help="private directory for sanitized judgment record, only for live requests")

    plan = commands.add_parser("plan", help="render a non-destructive human review plan")
    plan.add_argument("--repo", required=True)
    plan.add_argument("--inventory", required=True)
    plan.add_argument("--candidates", required=True)
    plan.add_argument("--relations")
    plan.add_argument("--review")
    plan.add_argument("--out", required=True)
    batches = commands.add_parser("batches", help="prepare local previews for ambiguous candidate pairs")
    batches.add_argument("--repo", required=True)
    batches.add_argument("--candidates", required=True)
    batches.add_argument("--out", required=True)
    batches.add_argument("--batch-size", type=int, default=32)
    batches.add_argument("--previous-relations", action="append", default=[])
    batches.add_argument("--evidence-profile", choices=EVIDENCE_PROFILES, default="minimal")
    batches.add_argument("--equivalence", help="skip pairs whose two endpoints are exactly preserved")
    collect = commands.add_parser("collect", help="aggregate verified batch checkpoints for the viewer")
    collect.add_argument("--repo", required=True)
    collect.add_argument("--candidates", required=True)
    collect.add_argument("--batch-plan", action="append", required=True)
    collect.add_argument("--out", required=True)
    calibration = commands.add_parser("calibrate", help="score v3 judgments against owner labels without network access")
    calibration.add_argument("--repo", required=True)
    calibration.add_argument("--labels", required=True)
    calibration.add_argument("--relations", required=True)
    calibration.add_argument("--out", required=True)
    decisions = commands.add_parser("decisions", help="classify recorded branches from facts and Jev signals without cleanup")
    decisions.add_argument("--repo", required=True)
    decisions.add_argument("--inventory", required=True)
    decisions.add_argument("--candidates", required=True)
    decisions.add_argument("--relations", required=True)
    decisions.add_argument("--out", required=True)
    decisions.add_argument("--equivalence", help="optional exact local content verdicts")
    equivalence = commands.add_parser("equivalence", help="compare committed content locally without Jev")
    equivalence.add_argument("--repo", required=True)
    equivalence.add_argument("--inventory", required=True)
    equivalence.add_argument("--out", required=True)
    equivalence.add_argument("--approved-destination", action="append", default=[], help="non-main local branch approved as a preservation destination")
    equivalence.add_argument("--ignore-recent-hours", type=float, default=0, help="exclude branches with recent commit or ref activity; missing reflogs are excluded")
    coverage = commands.add_parser("coverage", help="prove per-path exact content coverage by pinned main")
    coverage.add_argument("--repo", required=True)
    coverage.add_argument("--inventory", required=True)
    coverage.add_argument("--out", required=True)
    coverage.add_argument("--recent-hours", type=float, default=24)
    residual = commands.add_parser("residual", help="simulate a pinned branch merge in an independent repository")
    residual.add_argument("--repo", required=True)
    residual.add_argument("--coverage", required=True)
    residual.add_argument("--branch", required=True)
    residual.add_argument("--out", required=True)
    cleanup = commands.add_parser("cleanup", help="prepare or inspect guarded local branch cleanup")
    cleanup_steps = cleanup.add_subparsers(dest="cleanup_command", required=True)
    cleanup_plan = cleanup_steps.add_parser("plan", help="prepare bounded exact cleanup manifest and recovery bundle")
    cleanup_plan.add_argument("--repo", required=True)
    cleanup_plan.add_argument("--coverage", required=True)
    cleanup_plan.add_argument("--out", required=True)
    cleanup_plan.add_argument("--max-branches", type=int, default=25)
    cleanup_approve = cleanup_steps.add_parser("approve", help="record approval of one reviewed cleanup manifest digest")
    cleanup_approve.add_argument("--repo", required=True)
    cleanup_approve.add_argument("--plan", required=True)
    cleanup_approve.add_argument("--approved-digest", required=True)
    cleanup_approve.add_argument("--out", required=True)
    cleanup_execute = cleanup_steps.add_parser("execute", help="check approval and lease gate; without an integrated lease, no refs are deleted")
    cleanup_execute.add_argument("--repo", required=True)
    cleanup_execute.add_argument("--plan", required=True)
    cleanup_execute.add_argument("--approved-digest", required=True)
    cleanup_execute.add_argument("--journal", help="owner-private action journal (defaults to user state)")
    cleanup_execute.add_argument("--fixture-inventory", help="explicit no-known-automation disposable-fixture inventory")
    cleanup_reconcile = cleanup_steps.add_parser("reconcile", help="restore or record one interrupted cleanup intent")
    cleanup_reconcile.add_argument("--repo", required=True)
    cleanup_reconcile.add_argument("--plan", required=True)
    cleanup_reconcile.add_argument("--journal", required=True)
    cleanup_reconcile.add_argument("--fixture-inventory", help="explicit no-known-automation disposable-fixture inventory")
    resume = commands.add_parser("resume", help="resume one approved Jev batch plan, retaining uncertain attempts")
    resume.add_argument("--repo", required=True)
    resume.add_argument("--batch-plan", required=True)
    resume.add_argument("--approved-plan-sha256", required=True)
    resume.add_argument("--max-jev-requests", type=int, required=True)
    resume.add_argument("--max-jev-payload-bytes", type=int, required=True)
    resume.add_argument("--max-total-requests", type=int, required=True)
    resume.add_argument("--use-jev", action="store_true", required=True)
    viewer = commands.add_parser("viewer", help="serve one explicit local artifact set on loopback")
    viewer.add_argument("--inventory", required=True)
    viewer.add_argument("--candidates", required=True)
    viewer.add_argument("--relations", required=True)
    viewer.add_argument("--review")
    viewer.add_argument("--equivalence")
    viewer.add_argument("--port", type=int, default=8877)
    return root


def _protected_output(repo: str, output: str) -> Path:
    return validate_output_path(output, protected_worktree_paths(repo))


def run(args: argparse.Namespace) -> str:
    if args.command == "credential":
        if args.credential_action == "cache":
            return serve_cache_form()
        if args.credential_action == "status":
            return credential_status()
        if args.credential_action == "clear":
            return "Jev Keychain cache cleared" if KeychainLease().clear() else "Jev Keychain cache was absent"
    if args.command == "group-relate":
        if args.execute and args.answers:
            raise JgError("choose either approved Jev execution or answer import")
        if bool(args.answers) != bool(args.answer_origin):
            raise JgError("answer import requires both --answers and --answer-origin")
        if args.execute and (not args.approved_payload_sha256 or not args.approved_approval_sha256):
            raise JgError("group presence execution requires approved payload and approval digests")
        if args.execute and (args.estimated_input_tokens is None or args.max_provider_tokens is None):
            if not args.auto_estimate_input_tokens or args.max_provider_tokens is None:
                raise JgError("group presence execution requires input and provider token budgets")
        object_repo, range_manifest, replay, preview = _build_group_presence_preview(args)
        if args.execute and not preview["request_budgets"].get("token_budget_established"):
            raise JgError("provider token budget could not be established")
        if args.execute and (args.approved_payload_sha256 != replay["payload_sha256"] or
                             args.approved_approval_sha256 != replay["approval_sha256"]):
            raise JgError("approved presence digests do not match the rebuilt request")
        target = validate_output_path(args.out, [object_repo])
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.stat().st_mode & 0o077:
            raise JgError("presence output directory must be owner-only")
        replay_path = target / "approved-presence-manifest.json"
        ranges_path = target / "presence-ranges.json"
        if replay_path.exists():
            previous = read_json(replay_path)
            if previous != replay:
                raise JgError("rebuilt presence request differs from the approved manifest")
        elif args.execute or args.answers:
            raise JgError("preview and persist the approved manifest before execution or answer import")
        else:
            write_json(replay_path, replay)
            write_json(ranges_path, range_manifest)
        if args.show_preview and not args.execute and not args.answers:
            serve_presence_preview(preview)
        if args.execute:
            if not args.checkpoint:
                raise JgError("--execute requires --checkpoint for durable fail-closed progress")
            checkpoint = validate_output_path(args.checkpoint, [object_repo]) if args.checkpoint else None
            result = execute_presence_preview(
                preview, args.approved_payload_sha256,
                approved_approval_sha256=args.approved_approval_sha256,
                max_workers=args.max_workers, checkpoint=checkpoint,
                code_evidence_repo=object_repo,
                token=resolve_provider_token(),
                execution_receipt_path=target / "presence-execution-receipt.json")
            response_path = target / "presence-execution.json"
            write_json(response_path, result)
            return str(response_path)
        if args.answers:
            response_import = read_json(args.answers)
            if (response_import.get("kind") != "branch-presence-response-import" or
                    response_import.get("schema_version") != 1 or
                    not isinstance(response_import.get("answers"), list)):
                raise JgError("presence response import has an unsupported schema")
            importer = import_synthetic_answers if args.answer_origin == "synthetic" else import_control_answers
            result = importer(preview, response_import["answers"])
            response_path = target / f"presence-{args.answer_origin}-answers.json"
            write_json(response_path, result)
            return str(response_path)
        return str(replay_path)
    if args.command == "presence-reconcile":
        result = reconcile_presence(
            read_json(args.contributions), read_json(args.groups), read_json(args.answers),
            execution_receipt=read_json(args.execution_receipt) if args.execution_receipt else None)
        write_json(Path(args.out), result)
        return args.out
    if args.command == "presence-calibrate":
        result = build_presence_calibration(
            read_json(args.labels), read_json(args.jev), read_json(args.control),
            jev_metadata=read_json(args.jev_metadata) if args.jev_metadata else None,
            control_metadata=read_json(args.control_metadata) if args.control_metadata else None,
        )
        write_json(Path(args.out), result)
        return args.out
    if args.command == "study":
        contributions = read_json(args.contributions)
        root, common, _runner = git.open_repository(args.repo)
        if contributions.get("repository_id") != opaque_path_id(root):
            raise JgError("contributions belong to a different local repository")
        target = validate_output_path(args.out, [*protected_worktree_paths(args.repo), common])
        if args.selection_manifest:
            selection = read_json(args.selection_manifest)
            groups = read_json(args.groups)
            result, range_manifest = build_selected_study(contributions, groups, selection)
            return str(write_selected_study_artifacts(result, range_manifest, target))
        return str(write_study(args.contributions, args.groups, target, args.count,
                               args.max_per_family, args.exclude_branch, args.project_goals, args.selection_policy))
    if args.command == "outcomes":
        inventory = read_json(args.inventory)
        root, common, _runner = git.open_repository(args.repo)
        if inventory.get("repository", {}).get("id") != opaque_path_id(root):
            raise JgError("inventory belongs to a different local repository")
        target = validate_output_path(args.out, [*protected_worktree_paths(args.repo), common])
        return str(write_outcomes(args.inventory, args.snapshot, args.contributions, target,
                                  args.presence, args.coverage, args.review, args.review_approval,
                                  args.delivery_observations))
    if args.command == "outcome-review-approve":
        review = read_json(args.review)
        review_sha256 = digest(review)
        if args.approved_review_sha256 is None:
            if args.out is not None:
                raise JgError("approval output requires the exact --approved-review-sha256")
            return canonical_json({"review_sha256": review_sha256,
                                   "approval_required": True}).decode("ascii")
        if args.out is None:
            raise JgError("exact-digest approval requires a private --out receipt path")
        receipt = approve_outcome_review(review, args.approved_review_sha256)
        write_json(Path(args.out).expanduser().resolve(), receipt)
        return args.out
    if args.command == "preservation-queue":
        inventory = read_json(args.inventory)
        root, common, _runner = git.open_repository(args.repo)
        if inventory.get("repository", {}).get("id") != opaque_path_id(root):
            raise JgError("inventory belongs to a different local repository")
        target = validate_output_path(args.out, [*protected_worktree_paths(args.repo), common])
        return str(write_preservation_plan(
            args.inventory, target, args.candidates, args.relations, args.review, args.outcomes,
        ))
    if args.command == "groups":
        contributions = read_json(args.contributions)
        root, common, _runner = git.open_repository(args.repo)
        if contributions.get("repository_id") != opaque_path_id(root):
            raise JgError("contributions belong to a different local repository")
        target = validate_output_path(args.out, [*protected_worktree_paths(args.repo), common])
        return str(write_groups(args.contributions, target, args.max_units, args.max_edges))
    if args.command == "snapshot":
        return str(write_snapshot(args.repo, args.inventory, args.out, recent_hours=args.recent_hours))
    if args.command == "contributions":
        snapshot, _store = load_snapshot(args.snapshot)
        root, common, _runner = git.open_repository(args.repo)
        if snapshot.get("repository_id") != opaque_path_id(root):
            raise JgError("snapshot belongs to a different local repository")
        target = validate_output_path(args.out, [*protected_worktree_paths(args.repo), common])
        checkpoint = (validate_output_path(args.checkpoint,
                                           [*protected_worktree_paths(args.repo), common])
                      if args.checkpoint else None)
        return str(write_contributions(
            args.snapshot, target, workers=args.workers,
            progress=lambda done, total, branch: print(
                f"contributions: {done}/{total} {branch}", file=sys.stderr),
            checkpoint_path=checkpoint,
            max_destination_blobs=args.max_destination_blobs))
    if args.command == "code-relate":
        candidates = read_json(args.candidates)
        root, _common, _runner = git.open_repository(args.repo)
        if candidates.get("kind") != "candidates" or candidates.get("repository_id") != opaque_path_id(root):
            raise JgError("candidates artifact belongs to a different local repository")
        selection = read_json(args.selection)
        if selection.get("kind") != "jev-code-selection" or not isinstance(selection.get("candidates"), dict):
            raise JgError("code selection must map candidate IDs to pinned tips and ranges")
        records = {}
        for candidate in candidates.get("candidates", []):
            candidate_id = candidate["id"]
            chosen = selection["candidates"].get(candidate_id)
            if not isinstance(chosen, dict):
                raise JgError(f"code selection is invalid for {candidate_id}")
            snapshot_mode = chosen.get("snapshot_mode")
            source_ref = chosen.get("source_ref")
            main_ref = chosen.get("main_ref")
            if snapshot_mode == "pinned_commits":
                if source_ref is not None or main_ref is not None:
                    raise JgError(f"pinned snapshot selection must omit live refs for {candidate_id}")
            elif snapshot_mode is None:
                if not source_ref or not main_ref:
                    raise JgError(f"code selection lacks pinned refs for {candidate_id}")
            else:
                raise JgError(f"unknown code snapshot mode for {candidate_id}")
            endpoints = candidate.get("endpoints", {})
            endpoint_tips = {item.get("tip") for item in endpoints.values() if isinstance(item, dict)}
            if endpoint_tips != {chosen.get("source_tip"), chosen.get("main_tip")} or len(endpoint_tips) != 2:
                raise JgError(f"code selection tips do not match candidate endpoints for {candidate_id}")
            records[candidate_id] = build_code_evidence(
                args.repo, chosen["source_tip"], chosen["main_tip"], chosen["ranges"],
                source_ref=source_ref, main_ref=main_ref)
        preview = build_transient_preview(candidates, records)
        approval = approve_code_batch(records.values(), preview["requests"])
        if not args.use_jev:
            if args.out:
                raise JgError("transient code preview cannot be written to an artifact directory")
            return canonical_json({"preview": preview, "approval": approval}).decode("ascii")
        if not args.out or not args.approved_payload_sha256 or not args.approved_batch_sha256:
            raise JgError("live code request requires --out and approved payload and batch digests")
        if args.approved_payload_sha256 != approval["payload_sha256"] or args.approved_batch_sha256 != approval["approval_sha256"]:
            raise JgError("approved code request digests do not match current inputs")
        target = _protected_output(args.repo, args.out)
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.stat().st_mode & 0o077:
            raise JgError("code response directory must be owner-only")
        result = execute_transient_preview(
            preview, args.approved_payload_sha256,
            approved_code_batch_sha256=args.approved_batch_sha256,
            code_evidence_repo=args.repo,
            max_requests=args.max_jev_requests,
            max_payload_bytes=args.max_jev_payload_bytes)
        path = target / "code-relations.json"
        write_json(path, result)
        return str(path)
    if args.command == "cleanup":
        if args.cleanup_command == "plan":
            return str(write_cleanup_plan(args.repo, args.coverage, args.out, max_branches=args.max_branches))
        if args.cleanup_command == "approve":
            plan = read_json(args.plan)
            if plan.get("plan_digest") != args.approved_digest:
                raise JgError("approved digest does not match cleanup plan")
            approved = approve_cleanup_plan(plan, approved_digest=args.approved_digest)
            target = _protected_output(args.repo, args.out)
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = target / "approved-cleanup-plan.json"
            write_json(path, approved)
            return str(path)
        if args.cleanup_command == "execute":
            plan = read_json(args.plan)
            lease, journal_path, diagnostic = _cleanup_executor_context(
                args.repo, plan, args.journal, args.fixture_inventory)
            result = execute_cleanup(args.repo, plan, approved_digest=args.approved_digest,
                                     lease_contract=lease, journal_path=journal_path)
            if diagnostic:
                result["capability_diagnostic"] = diagnostic
            return json.dumps(result, sort_keys=True)
        plan = read_json(args.plan)
        lease, journal_path, diagnostic = _cleanup_executor_context(
            args.repo, plan, args.journal, args.fixture_inventory)
        if diagnostic or lease is None:
            raise JgError(f"creator capability unavailable: {diagnostic or 'missing'}")
        journal = CleanupActionJournal(journal_path)
        result = reconcile_interrupted_cleanup(args.repo, plan, journal, lease)
        return json.dumps({"scope": lease.capability.scope, "reconciled": result}, sort_keys=True)
    if args.command == "decisions":
        destination = _protected_output(args.repo, args.out)
        return str(write_decisions(args.inventory, args.candidates, args.relations, destination, args.equivalence))
    if args.command == "equivalence":
        return str(write_equivalence(args.repo, args.inventory, args.out, args.approved_destination, args.ignore_recent_hours))
    if args.command == "coverage":
        return str(write_coverage(args.repo, args.inventory, args.out, args.recent_hours))
    if args.command == "residual":
        coverage = read_json(args.coverage)
        if coverage.get("kind") != "branch-coverage":
            raise JgError("residual requires a coverage artifact")
        branch = next((item for item in coverage["branches"] if item["name"] == args.branch), None)
        if branch is None or branch["verdict"] == "UNKNOWN":
            raise JgError("branch has no valid pinned coverage")
        target = _protected_output(args.repo, args.out)
        result = analyze_residual(args.repo, branch["tip"], coverage["main"]["tip"],
                                  [item["path"] for item in branch["paths"]])
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = target / "residual.json"
        write_json(path, result)
        return str(path)
    if args.command == "resume":
        _protected_output(args.repo, str(Path(args.batch_plan).parent))
        return str(resume_batches(args.batch_plan, args.approved_plan_sha256,
                                  max_requests=args.max_jev_requests,
                                  max_payload_bytes=args.max_jev_payload_bytes,
                                  max_total_requests=args.max_total_requests))
    if args.command == "viewer":
        artifacts = {"inventory": Path(args.inventory), "candidates": Path(args.candidates), "relations": Path(args.relations)}
        if args.review:
            artifacts["review"] = Path(args.review)
        if args.equivalence:
            artifacts["equivalence"] = Path(args.equivalence)
        serve_viewer(artifacts, args.port)
        return ""
    if args.command == "calibrate":
        destination = _protected_output(args.repo, args.out)
        return str(write_calibration(args.labels, args.relations, destination))
    if args.command == "collect":
        destination = _protected_output(args.repo, args.out)
        return str(collect_batches(args.batch_plan, args.candidates, destination))
    if args.command == "batches":
        destination = _protected_output(args.repo, args.out)
        return str(prepare_batches(args.candidates, destination, args.batch_size, args.previous_relations, args.evidence_profile, args.equivalence))
    if args.command == "inventory":
        return str(write_inventory(args.repo, args.out))
    if args.command == "candidates":
        if args.max_candidates < 1:
            raise JgError("--max-candidates must be greater than zero")
        return str(write_candidates(args.repo, args.inventory, args.out, args.max_candidates, args.allow_incomplete))
    if args.command == "relate":
        destination = _protected_output(args.repo, args.out)
        if args.use_jev:
            if not args.approved_preview or not args.approved_payload_sha256:
                raise JgError("--use-jev requires --approved-preview and --approved-payload-sha256 from a local preview")
            target = destination / "relations.json"
            with checkpoint_lock(target):
                execute_preview(
                    args.approved_preview,
                    args.approved_payload_sha256,
                    max_requests=args.max_jev_requests,
                    max_payload_bytes=args.max_jev_payload_bytes,
                    checkpoint=target,
                )
            return str(target)
        profile = "review" if args.include_branch_labels else args.evidence_profile
        return str(write_preview(args.candidates, destination, profile))
    if args.command == "plan":
        destination = _protected_output(args.repo, args.out)
        _json_path, markdown_path = write_plan(args.inventory, args.candidates, destination, args.relations, args.review)
        return str(markdown_path)
    raise JgError(f"unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        print(run(args))
        return 0
    except JgError as exc:
        print(f"jg: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
