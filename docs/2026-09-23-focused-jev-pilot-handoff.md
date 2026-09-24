# Focused Jev comparison pilot handoff

## Implemented

- Focused source/main relationship evidence is checked against its evidence
  kind and pinned inputs before approval or provider dispatch.
- Source-only questions must leave destination presence `UNKNOWN`; they cannot
  infer integration readiness, duplicate work, preservation, or deletion.
- Relationship context is marked non-exhaustive when a study selects only part
  of a cohort. Selection scope and omitted full-cohort context are recorded in
  the request state.
- Every reconciled result records project utility as `UNKNOWN` when project
  requirements were not provided. A code-only judgment is advisory.
- Execution requires a durable, authenticated checkpoint, validates answer
  scope, and persists sanitized progress as bounded calls complete.
- The fake-transport test uses a temporary receipt-key directory and exercises
  the source-only and two-sided outcome contracts.

## Local pilot preparation

`scripts/prepare_focused_jev_pilot.py` prepares three bounded Python
source/main cases from an existing pinned snapshot and relationship projection.
It checks the projection measurement and pinned source/main blob IDs, selects
one eligible branch unit per branch with exactly one same-name main candidate,
and uses the existing request builders and preview digest logic. Same-name is
only a retrieval filter; it does not prove semantic equivalence or byte identity.

Example invocation:

```text
python scripts/prepare_focused_jev_pilot.py --run-dir PATH_TO_PINNED_RUN
```

The run directory must contain `snapshot.json`,
`relationship-projection.json`, and `projection-measurement.json`. The script
writes metadata-only selection and request-manifest files into a new output
directory with owner-only permissions. Bounded source excerpts are used in
memory to construct request digests and are not written to disk. Review and
approve the exact request and approval-binding digests before any provider
call; this preparation step does not authorize or dispatch requests.

## Verification and limits

The focused local verification command is:

```text
PYTHONPATH=src python3 -m pytest -q tests/test_presence.py tests/test_code_evidence.py
```

The pilot is deliberately small and advisory. It does not make project-utility
claims without supplied requirements and does not authorize branch deletion.
Provider output and local inventory details belong in a private run record, not
in this source-controlled handoff.
