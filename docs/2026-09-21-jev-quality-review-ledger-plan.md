# Jev Judgment Quality and Review Workflow Plan

## Goal

Turn Jev Git Graph from a safe exploratory relationship viewer into a measurable
review system that produces useful multidimensional judgments, records exact
runtime and usage evidence, and lets a maintainer disposition every branch,
worktree, and stash without granting Jev cleanup authority.

Phase 1 must be complete before another large Jev run. Phase 2 builds the human
review workflow on the validated result format.

## Pinned pilot finding and next question contract

The first 100-case metadata pilot yielded 99 validated responses. None met the
current `evidence_sufficient >= .75` routing gate. A blind 24-case Luna control
produced seven gated relation signals, six of which were explained by matching
normalized commit subjects; the seventh contradicted its own insufficient
evidence answer. This sample has no owner-labeled truth set, so it cannot measure
either model's accuracy. A scoped pinned-tree review of the highest Jev overlap
scores found concrete source work already present in the pinned destination,
but these checks did not prove whole-branch preservation. The detailed branch
names and payloads belong in private, owner-only pilot artifacts, not this spec.

Keep `branch-relationship-v3` immutable for historical interpretation. Before a
wider run, trial a separate versioned **branch-presence-v1** contract on pinned
source and destination commits. Its unit is a named source contribution, such
as a function, behavior, or test, rather than a source branch against all of
main. Ask independently:

1. Is the supplied evidence sufficient to identify this source contribution
   and inspect the relevant destination code? Low sufficiency means **unknown**,
   not false for the remaining questions.
2. Does the pinned destination implement or contain this named contribution?
3. Is any usable behavior from this named source contribution absent from the
   pinned destination?

Jev returns typed answers, not generated citations. Local code must assign
stable IDs to source contributions, destination units, and approved evidence
ranges before the request. A request's shared `state` should include a bounded
connected relationship group, with its known boundary edges, and its
`questions` should identify the contribution IDs being judged. Code joins each
typed answer back to those pinned IDs and hashes. A model answer cannot invent
an evidence range or prove one was inspected. Compare this group-context form
with the original pairwise form in the blinded calibration.

The revised code profile must provide approved, bounded **both-source-and-
destination** excerpts, with blob IDs and range hashes for each. The present
`code` profile supplies source text and a destination blob ID only; do not use
it to claim semantic presence. Local Git exact checks and AST matching should
run first. Dynamic references and ambiguous matches remain unknown. Jev and
the LLM control receive the same pinned evidence without seeing each other's
answers. Include owner-adjudicated positive and hard-negative cases, split by
task or PR family, and measure precision, recall, abstention, calibration, and
incremental review yield against exact Git, AST, and subject-match baselines.
Set any routing threshold only after this calibration. No model answer can
promote a branch to cleanup eligibility or authorize deletion.

## Phase 1 — judgment quality and observability

- Replace the mutually exclusive relationship Choice with
  `branch-relationship-v3`, using independent Nouls for same intent, partial
  overlap, both dependency directions, both supersession directions, and
  evidence sufficiency in one request.
- Centralize question text, criteria, thresholds, and version identifiers in one
  audit surface.
- Keep a metadata-only `minimal` evidence profile and add an explicit `review`
  profile for preview-visible labels, normalized subjects, bounded paths, and
  supplied task or PR identifiers.
- Identify a judgment by candidate evidence, question version, model, and evidence
  profile. Preserve earlier judgments while preventing accidental duplicate
  billing.
- Validate responses before marking an attempt successful and record exact timing,
  HTTP status, model, usage, and aggregate run statistics.
- Stratify pending work toward untouched branches and diverse evidence before
  global rank.
- Require a bounded, owner-reviewed calibration before a wider v3 run.

## Phase 2 — durable review and visualization

- Add a versioned `review.json` ledger tied to repository and artifact digests,
  exact tips or dirty-state fingerprints, reviewer rationale, and timestamps.
- Keep human dispositions authoritative. Jev may filter or rank but may never
  create a disposition.
- Let `jg plan` consume a review ledger, reject incompatible artifacts, and mark
  changed evidence stale.
- Let the browser-local viewer import, edit, and download review data while keeping
  facts, Jev dimensions, human decisions, stale decisions, and omitted frontiers
  visibly distinct.
- Validate the full artifact digest chain and report inventory, candidate, Jev,
  examination, disposition, stale, and unresolved coverage separately.

## Acceptance

- Every live attempt has exact timing and token usage or a sanitized uncertain
  outcome.
- Payload profiles remain explicit, previewed, bounded, and free of credentials,
  raw diffs, source content, local paths, and remote URLs.
- Existing v2 artifacts remain readable historical evidence.
- No cleanup candidate is produced solely from Jev.
- Every repository object remains represented in coverage and review counts.
- Changed Git or dirty-state evidence invalidates the corresponding human review.
