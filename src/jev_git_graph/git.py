from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .errors import JgError


@dataclass
class GitRunner:
    root: Path
    commands: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, *args: str, input_bytes: bytes | None = None) -> str:
        command = ("git", "-C", str(self.root), *args)
        self.commands.append(command)
        completed = subprocess.run(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode:
            detail = completed.stderr.decode("utf-8", "replace").splitlines()[0] if completed.stderr else "Git command failed"
            raise JgError(f"unable to inspect repository: {detail}")
        return completed.stdout.decode("utf-8", "replace")

    def try_run(self, *args: str) -> str | None:
        try:
            return self.run(*args)
        except JgError:
            return None


def open_repository(path: str | Path) -> tuple[Path, Path, GitRunner]:
    requested = Path(path).expanduser().resolve()
    if not requested.exists():
        raise JgError(f"repository does not exist: {requested}")
    probe = GitRunner(requested)
    root_text = probe.try_run("rev-parse", "--show-toplevel")
    if root_text is None:
        raise JgError(f"not a Git worktree: {requested}")
    root = Path(root_text.strip()).resolve()
    runner = GitRunner(root)
    common_text = runner.run("rev-parse", "--path-format=absolute", "--git-common-dir")
    return root, Path(common_text.strip()).resolve(), runner


def worktrees(runner: GitRunner) -> list[dict[str, str | bool]]:
    entries: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for line in runner.run("worktree", "list", "--porcelain").splitlines():
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = str(Path(value).resolve())
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key in {"bare", "detached", "locked", "prunable"}:
            current[key] = True
    if current:
        entries.append(current)
    return entries


def status_for(path: Path) -> list[str]:
    completed = subprocess.run(
        ("git", "-C", str(path), "status", "--porcelain=v1"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        return ["<unavailable>"]
    return completed.stdout.decode("utf-8", "replace").splitlines()


def local_branches(runner: GitRunner) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    # `for-each-ref` uses `%xx` rather than pretty-format's `%xXX`.
    # Unit Separator cannot occur in a ref name and keeps subjects intact.
    format_string = "%(refname:short)%1f%(objectname)%1f%(committerdate:iso-strict)%1f%(subject)"
    for line in runner.run("for-each-ref", "--format=" + format_string, "refs/heads").splitlines():
        name, sha, timestamp, subject = (line.split("\x1f") + ["", "", "", ""])[:4]
        result.append({"name": name, "tip": sha, "committed_at": timestamp, "subject": subject})
    return result


def default_branch(runner: GitRunner, branch_names: set[str]) -> str | None:
    remote_head = runner.try_run("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if remote_head:
        candidate = remote_head.strip().removeprefix("origin/")
        if candidate in branch_names:
            return candidate
    for candidate in ("main", "master"):
        if candidate in branch_names:
            return candidate
    head = runner.try_run("symbolic-ref", "--quiet", "--short", "HEAD")
    if head and head.strip() in branch_names:
        return head.strip()
    return sorted(branch_names)[0] if branch_names else None


def merge_base(runner: GitRunner, first: str, second: str) -> str | None:
    return_text = runner.try_run("merge-base", first, second)
    return return_text.strip() if return_text else None


def is_ancestor(runner: GitRunner, ancestor: str, descendant: str) -> bool:
    command = ("git", "-C", str(runner.root), "merge-base", "--is-ancestor", ancestor, descendant)
    runner.commands.append(command)
    return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0


def commits_since(runner: GitRunner, base: str | None, tip: str) -> list[dict[str, str]]:
    revision = f"{base}..{tip}" if base else tip
    format_string = "%H%x1f%s"
    commits: list[dict[str, str]] = []
    for line in runner.run("log", "--reverse", "--format=" + format_string, revision).splitlines():
        if "\x1f" not in line:
            continue
        sha, subject = line.split("\x1f", 1)
        commits.append({"sha": sha, "subject": subject})
    return commits


def changed_paths(runner: GitRunner, base: str | None, tip: str) -> list[str]:
    if not base:
        return []
    return [line for line in runner.run("diff", "--name-only", f"{base}...{tip}").splitlines() if line]


def patch_id(runner: GitRunner, commit: str) -> str | None:
    command = ("git", "-C", str(runner.root), "show", "--pretty=format:", "--binary", commit)
    runner.commands.append(command)
    shown = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if shown.returncode:
        return None
    patch = subprocess.run(
        ("git", "patch-id", "--stable"),
        input=shown.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if patch.returncode or not patch.stdout:
        return None
    return patch.stdout.decode("utf-8", "replace").split()[0]


def stashes(runner: GitRunner) -> list[dict[str, str]]:
    format_string = "%H%x1f%gd%x1f%gs"
    entries: list[dict[str, str]] = []
    for line in runner.run("stash", "list", "--format=" + format_string).splitlines():
        sha, reference, subject = (line.split("\x1f") + ["", "", ""])[:3]
        entries.append({"sha": sha, "reference": reference, "subject": subject})
    return entries
