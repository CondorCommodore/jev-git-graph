# Guarded local branch cleanup

`jev_git_graph.cleanup` is the narrow execution boundary for the strict
`branch-coverage` artifact. It is intentionally separate from the CLI and
from Jev review decisions. A relationship judgment, ancestry result, patch
ID, or human disposition does not make a branch removable.

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

`execute_cleanup(repo, plan, approved_digest=..., lease_contract=...)` checks
the exact plan digest and the bundle hash and approval first. If the lease
contract is missing or not established, it returns a deletion-ready
plan-only result and makes no ref change. A lease must be the concrete
`CooperativeLease` adapter with the
`jev-git-graph/cooperative-branch-lease-v1` contract marker, an established
state, and cooperative `acquire(name, tip)` and `release(name, tip)` hooks
bound to the coordinator that creates the lease. A loose mapping or boolean
cannot authorize deletion. Acquisition must succeed for each branch.

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
stops without changing refs. Operators must review the plan, preserve any
required work, and approve its exact digest. A future integrated coordinator
must supply the cooperative lease contract before local branch deletion can
run.
