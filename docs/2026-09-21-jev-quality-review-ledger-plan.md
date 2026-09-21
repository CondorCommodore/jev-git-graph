# Jev Judgment Quality and Review Workflow Plan

## Goal

Turn Jev Git Graph from a safe exploratory relationship viewer into a measurable
review system that produces useful multidimensional judgments, records exact
runtime and usage evidence, and lets a maintainer disposition every branch,
worktree, and stash without granting Jev cleanup authority.

Phase 1 must be complete before another large Jev run. Phase 2 builds the human
review workflow on the validated result format.

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

