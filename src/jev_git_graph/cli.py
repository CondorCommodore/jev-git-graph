from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import git
from .candidates import write_candidates
from .batches import prepare_batches, collect_batches
from .calibration import write_calibration
from .coverage import write_coverage
from .cleanup import approve_cleanup_plan, execute_cleanup, write_cleanup_plan
from .code_evidence import approve_code_batch, build_code_evidence, build_transient_preview
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
from .resume import resume_batches
from .safety import canonical_json, opaque_path_id, read_json, validate_output_path, write_json
from .viewer import serve as serve_viewer


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="jg", description="Local-first Git relationship review")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)

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

    groups = commands.add_parser("groups", help="build bounded local contribution context groups")
    groups.add_argument("--repo", required=True)
    groups.add_argument("--contributions", required=True)
    groups.add_argument("--out", required=True)
    groups.add_argument("--max-units", type=int, default=24)
    groups.add_argument("--max-edges", type=int, default=1000)

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
        return str(write_contributions(args.snapshot, target))
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
        result = execute_cleanup(args.repo, args.plan, approved_digest=args.approved_digest)
        return json.dumps(result, sort_keys=True)
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
