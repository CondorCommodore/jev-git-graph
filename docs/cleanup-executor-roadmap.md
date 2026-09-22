# Future preservation and cleanup executor

This roadmap records the remaining work around the preservation and cleanup
boundary in specification sections 3, 9.6, and 12. The repository now has a
read-only inventory and review flow plus a guarded local branch cleanup library.
The implementation deliberately stops at the coordinator boundary described
below; this document tracks the work needed to make that boundary operational.

## Current implementation

The local CLI and library currently provide these guarantees:

* Inventory covers local branches, linked worktrees, worktree status, and stashes.
  It detects changes during collection and marks the artifact incomplete rather
  than making a cleanup decision from a moving snapshot.
* Coverage and review artifacts keep exact Git evidence separate from Jev
  suggestions and human decisions. A Jev judgment never authorizes removal, and
  unresolved objects remain retained.
* `cleanup plan` accepts only a complete strict coverage artifact, selects at
  most 25 old, inactive, non-default branches with exact content evidence, and
  re-proves that evidence against live Git state. Checked-out, dirty, unavailable,
  recently active, changed, and stash-linked branches are held.
* The plan records pinned source and default-branch SHAs, observed exclusions,
  activity policy, and a private recovery bundle. The bundle is restored in a
  disposable repository and every pinned tip is resolved before approval.
* `cleanup approve` binds approval to the unapproved plan digest and emits a new
  approved digest. `cleanup execute` revalidates that digest and the bundle hash.
* With an established cooperative lease, execution rereads live refs, worktrees,
  statuses, stash links, activity, and exact content immediately before each
  action. It uses an atomic compare-and-delete transaction and restores a removed
  ref with compare-and-create when a readback or lease release is uncertain.
* The CLI has no integrated worktree-creator lease. Its execute command therefore
  returns a deletion-ready plan-only result and leaves refs unchanged. The
  executor never fetches, pushes, deletes remote refs, merges, or changes PRs.

## Admission and preservation

Keep the existing admission rules as the contract for any future coordinator:

1. Require an accepted review-product pilot, a complete fresh inventory, an
   explicit default branch, and human-reviewed decisions tied to immutable
   inputs. Refreshing any input invalidates the plan and requires new approval.
2. Preserve tracked modifications, staged changes, untracked files, and stashes
   before removing their original location. A porcelain status fingerprint alone
   cannot detect changed contents. Content verification needs its own explicit
   local-read contract; those contents must never be uploaded through Jev.
3. Every preservation destination must identify the source object, expected source
   SHA or content fingerprint, destination, evidence, preconditions, recovery
   reference, and dependencies on earlier preservation actions. An archive must
   pass a restore test in a temporary location before its source is eligible for
   removal.
4. Keep review, preservation, and cleanup authority separate. A semantic
   relationship, ancestry result, patch ID, or human disposition can prioritize
   review but cannot substitute for verified preservation and exact cleanup
   approval.

## Remaining coordinator work

The next implementation milestone is a concrete coordinator that participates in
`jev-git-graph/cooperative-branch-lease-v1`:

* Bind lease acquisition and renewal to every known worktree creator, including
  concurrent branch creation and checkout paths. The coordinator must establish
  the lease before invoking execution and release it after the result is recorded.
* Journal intent before each mutation and record the observed result afterward.
  An interrupted action must reconcile actual Git state before retrying. Use
  expected-old-value ref operations and retain recovery data under a documented
  retention policy instead of relying only on an expiring reflog.
* Execute small batches with a new inventory after each batch. Add an explicit
  operator-facing disposition for every branch, worktree, and stash that remains.
* Keep local branch removal separate from future worktree removal and stash
  dropping. Each requires its own preservation contract, lease participation,
  recovery proof, and explicit authorization. Remote deletion, fetch/push,
  merge, and PR operations also require separate authorization and remote
  evidence; local refs cannot establish remote freshness.
* Never force-remove a dirty or active worktree as a fallback. A coordinator
  unable to prove ownership, lock state, source content, destination state, or
  recovery availability must stop and report the unresolved item.

## Acceptance

Use synthetic repositories and adversarial integration tests to cover moved refs,
dirty-content changes with unchanged status, staged and untracked preservation,
duplicate actions, concurrent changes, permission failures, lease loss, and
interruption before and after each mutation. Test restoration from every archive
form and verify unrelated refs and worktrees remain untouched.

Completion proof is a canonical checkout on the intended default branch with clean
status, verified preservation destinations, and a disposition for every remaining
branch, worktree, and stash. Report unresolved work explicitly. Remote-current
status requires separately authorized remote evidence. The current repository is
not complete against this roadmap until the cooperative lease integration and its
creator coordination tests are delivered.
