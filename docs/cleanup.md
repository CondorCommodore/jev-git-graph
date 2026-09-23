# Guarded local branch cleanup

`jev_git_graph.cleanup` is the narrow execution boundary for the strict
`branch-coverage` artifact. It is intentionally separate from the CLI and
from Jev review decisions. A relationship judgment, ancestry result, patch
ID, or human disposition does not make a branch removable.

## Current integration status

The production creator gate remains closed. The package now defines a strict
creator registry and concrete before-operation lease interface, but the known
Home Lab entry points do not yet register and hold it. The required integration
files are `scripts/bootstrap-worktree.sh` (also the L1 path through
`scripts/l1_drain/workspace.py`), `scripts/new-worktree.sh`,
`scripts/overnight-codex-backlog-round.sh`, `scripts/ahc_app/coord_wake.py`,
`scripts/process-safe-prs.sh`, `scripts/pr_gate/guard_execution.py`,
`scripts/merge_train_parts/prescreen.py`, and
`scripts/train_construction_driver.py`. The `jg cleanup execute` CLI also does
not yet pass a journal path or a complete registered adapter. Until the hooks
and CLI wiring are reviewed, execution remains plan-only.

## Plan

Call `build_cleanup_plan(repo, coverage, bundle_dir=...)` with a complete
`coverage.json`. The artifact must be schema version 1, local-only, and carry
the exact default branch tip. The planner reads current local refs, linked
worktrees, statuses, and stashes without contacting a remote.

At most 25 non-default branches are selected, oldest first. A branch must
have an `EXACT` coverage verdict, exact path evidence, and an activity epoch
older than the coverage cutoff. The planner holds branches whose ref moved or
disappeared, whose activity is recent or unverifiable, or which are checked
out, dirty, status-unavailable, or named by a stash subject. Excluded records
remain in `observed` with a reason; they are never silently dropped.

The planner creates `cleanup.bundle` in an owner-only directory outside every
inspected worktree. It includes the pinned default tip and every selected tip.
The bundle is verified and fetched into a fresh disposable repository. Every
tip must resolve to its exact SHA before the bundle is considered restorable.
The returned plan intentionally has `manifest_approved: false`. Call
`approve_cleanup_plan(plan, approved_digest=...)` only after reviewing that
proof; the helper validates the original digest supplied by the operator,
records approval in a copy, and issues a new
exact `plan_digest`. The plan contains the bundle SHA-256 and a digest
covering the complete approved or unapproved plan.

## Execution gates

`execute_cleanup` accepts a `CooperativeBranchLeaseAdapter` and an explicit
`journal_path`. The adapter uses a cross-process lock keyed by repository and
branch. Each creator registers its exact adapter id at startup and holds
`creator_operation(creator_id, branch, expected_tip)` around the whole operation,
starting before any branch/ref creation, worktree creation, or checkout and
ending after publication. Cleanup holds `cleanup_operation` across reproof,
intent journaling, compare-and-delete, result journaling, and release. A callback
or registration made after the operation does not count as creator participation.

The `CleanupActionJournal` is owner-only append-only JSONL. It fsyncs an intent
before each ref mutation and a result afterward. Keep its path outside every
inspected worktree, alongside the recovery bundle. After interruption, call
`reconcile_interrupted_cleanup(repo, approved_plan, journal, lease)` before a
new execution. Reconciliation checks the exact branch under the cooperative
lock, restores an absent branch from the verified bundle only with
compare-and-create, and records moved or recreated refs without replacing them.
It never retries deletion.

`execute_cleanup(repo, plan, approved_digest=..., lease_contract=...,
journal_path=...)` checks the exact plan digest and bundle hash and approval
first. If the lease contract is missing or not established, it returns a
plan-only result and makes no ref change. A lease must be the concrete
`CooperativeBranchLeaseAdapter` with the
`jev-git-graph/cooperative-branch-lease-v1` contract marker, complete creator
registration, and cooperative `acquire(name, tip)` and `release(name, tip)`
hooks. A loose mapping or boolean cannot authorize deletion. Acquisition must
succeed for each branch.

Immediately before each removal, the executor rereads refs, worktrees,
statuses, stash links, and current commit/reflog activity against the recent
activity cutoff. It re-proves the source content against the destination
using the recorded literal paths. It stops on any uncertainty.
Removal uses one Git `update-ref --stdin` transaction containing a destination
`verify` and source compare-and-delete: the source is deleted only if both the
destination and source still equal their approved SHAs. The destination tip is
also checked in the live reproof. After deletion the source ref is checked;
if it reappears, the executor records uncertainty and never overwrites it.
Any future recovery path must use compare-and-create against an absent ref.
The executor never performs remote operations.

The CLI exposes `jg cleanup plan`, `jg cleanup approve`, and `jg cleanup execute`.
It does not provide a worktree-creator lease adapter, so its execute command
stops without changing refs. The library also hard-gates deletion until known
worktree creators participate in the lease contract; a caller-supplied callback
alone cannot enable it. Operators must review the plan, preserve any
required work, and approve its exact digest. A future integrated coordinator
must supply the cooperative lease contract before local branch deletion can
run.
