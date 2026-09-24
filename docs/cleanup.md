# Guarded local branch cleanup

`jev_git_graph.cleanup` is the narrow execution boundary wrapped by the CLI
for the strict `branch-coverage` artifact. It remains separate from Jev review
decisions. A relationship judgment, ancestry result, patch
ID, or human disposition does not make a branch removable.

## Current integration status

The CLI passes a production lease adapter and durable journal only after the
capability resolver verifies the closed Home Lab creator-hook digest set and
the configured and loaded LaunchAgents. Running jobs must expose an active PID,
exact process start time, reviewed process path, and private per-PID startup
receipt. The receipt binds that process generation to the runtime root and
commit, reviewed source and shared lease-helper digests, and loaded Python code.
The reviewed Python shell supervisor opens each required shell source and
passes that file descriptor to Bash; its receipt binds the exact bytes held
open for execution. A Bash-supplied path or PID remains insufficient. Idle
interval jobs are admitted only when launchd's loaded command matches the
reviewed plist and source; a start or exit changes the capability generation
and stops an in-progress cleanup before another ref transaction. A running
job with a missing or unsafe receipt, stale source, or unreviewed process
remains plan-only. An unloaded job is admitted only when its reviewed plist is
installed and launchd explicitly reports the label disabled; an enabled but
unloaded job blocks execution. Receipts become available only after the
relevant creator starts with the attestation-enabled launcher; no running process is
retroactively trusted. The train-construction job loads code from a
separate worktree after fetching `origin/main`; its live worktree must be clean,
registered with the same Git common directory, exactly at the current local
`origin/main` commit, and match the reviewed hook digests. An absent dedicated
worktree is recorded as inactive because the reviewed launcher exits before
creator execution when it is missing; its appearance requires a fresh snapshot
proof. Runtime overrides or drift keep production execution in plan-only mode.
Capability is rechecked
during execution, so changes to the creator runtime stop later branch actions.
A disposable installed-wheel fixture exercises execute and interrupted-action
reconciliation, including post-delete capability drift; it does not establish
production creator participation or authorize Home Lab deletion.

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
The CLI keeps that bundle at `recovery/cleanup.bundle` beside its private plan
file. Keep the output directory through approval, execution, and any later
reconciliation. Use a new output directory for a new plan; an existing recovery
bundle is never replaced.
The returned plan intentionally has `manifest_approved: false`. Call
`approve_cleanup_plan(plan, approved_digest=...)` only after reviewing that
proof; the helper validates the original digest supplied by the operator,
records approval in a copy, and issues a new
exact `plan_digest`. The plan contains the bundle SHA-256 and a digest
covering the complete approved or unapproved plan.

## Execution gates

`execute_cleanup` accepts a `CooperativeBranchLeaseAdapter` and an explicit
`journal_path`. Home Lab's shared `cooperative_branch_lease.py` helper and the
Jev adapter use the same cross-process lock key, derived from the Git common
directory and branch name. Creator wrappers hold the lock before ref/worktree
creation or checkout and through publication. Cleanup holds `cleanup_operation`
across reproof, intent journaling, compare-and-delete, result journaling, and
release. The adapter's in-memory registration API is diagnostic; production
authority comes from the verified installed creator capability above.

The `CleanupActionJournal` is owner-only append-only JSONL. It fsyncs an intent
before each ref mutation and a result afterward. Keep its path outside every
inspected worktree, alongside the recovery bundle. After interruption, call
`reconcile_interrupted_cleanup(repo, approved_plan, journal, lease)` before a
new execution. Reconciliation checks the exact branch under the cooperative
lock, restores an absent branch from the verified bundle only with
compare-and-create, and records moved or recreated refs without replacing them.
It never retries deletion. Each intent/result also records a sanitized creator
capability digest and, for production, the verified train-construction runtime
commit. Reconciliation requires the pending intent's capability metadata to
match the current verified capability before restoring or recording it.

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
The implemented recovery path restores a missing approved ref only with
compare-and-create against an absent ref.
The executor never performs remote operations.

The CLI exposes `jg cleanup plan`, `jg cleanup approve`, `jg cleanup execute`,
and interrupted-action reconciliation. Execute resolves the production
adapter from independently verified creator participation records, or accepts
an explicit disposable-fixture inventory for a disposable repository. Missing
or stale production participant proof leaves the plan-only gate in place. The
library hard-gates deletion until known worktree creators participate in the
lease contract; a caller-supplied callback alone cannot enable it. Operators
must review the plan, preserve any required work, and approve its exact digest.
