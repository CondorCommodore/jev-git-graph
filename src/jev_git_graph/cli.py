from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .candidates import write_candidates
from .errors import JgError
from .inventory import protected_worktree_paths, write_inventory
from .jev import DEFAULT_MAX_JEV_PAYLOAD_BYTES, DEFAULT_MAX_JEV_REQUESTS, execute_preview, write_preview
from .plan import write_plan
from .safety import validate_output_path, write_json


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="jg", description="Local-first Git relationship review")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser("inventory", help="collect a read-only local Git snapshot")
    inventory.add_argument("--repo", required=True)
    inventory.add_argument("--out", required=True)

    candidates = commands.add_parser("candidates", help="build bounded deterministic relationship candidates")
    candidates.add_argument("--repo", required=True)
    candidates.add_argument("--inventory", required=True)
    candidates.add_argument("--out", required=True)
    candidates.add_argument("--max-candidates", type=int, default=200)

    relate = commands.add_parser("relate", help="preview or execute opt-in Jev relationship questions")
    relate.add_argument("--repo", required=True)
    relate.add_argument("--candidates", required=True)
    relate.add_argument("--out", required=True)
    relate.add_argument("--preview", action="store_true", help="write a local, exact request preview")
    relate.add_argument("--include-branch-labels", action="store_true")
    relate.add_argument("--use-jev", action="store_true", help="send an already approved preview to Jev")
    relate.add_argument("--approved-preview")
    relate.add_argument("--approved-payload-sha256")
    relate.add_argument("--max-jev-requests", type=int, default=DEFAULT_MAX_JEV_REQUESTS)
    relate.add_argument("--max-jev-payload-bytes", type=int, default=DEFAULT_MAX_JEV_PAYLOAD_BYTES)

    plan = commands.add_parser("plan", help="render a non-destructive human review plan")
    plan.add_argument("--repo", required=True)
    plan.add_argument("--inventory", required=True)
    plan.add_argument("--candidates", required=True)
    plan.add_argument("--relations")
    plan.add_argument("--out", required=True)
    return root


def _protected_output(repo: str, output: str) -> Path:
    return validate_output_path(output, protected_worktree_paths(repo))


def run(args: argparse.Namespace) -> str:
    if args.command == "inventory":
        return str(write_inventory(args.repo, args.out))
    if args.command == "candidates":
        if args.max_candidates < 1:
            raise JgError("--max-candidates must be greater than zero")
        return str(write_candidates(args.repo, args.inventory, args.out, args.max_candidates))
    if args.command == "relate":
        destination = _protected_output(args.repo, args.out)
        if args.use_jev:
            if not args.approved_preview or not args.approved_payload_sha256:
                raise JgError("--use-jev requires --approved-preview and --approved-payload-sha256 from a local preview")
            result = execute_preview(
                args.approved_preview,
                args.approved_payload_sha256,
                max_requests=args.max_jev_requests,
                max_payload_bytes=args.max_jev_payload_bytes,
            )
            target = destination / "relations.json"
            write_json(target, result)
            return str(target)
        return str(write_preview(args.candidates, destination, args.include_branch_labels))
    if args.command == "plan":
        destination = _protected_output(args.repo, args.out)
        _json_path, markdown_path = write_plan(args.inventory, args.candidates, destination, args.relations)
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
