# L6 offline integrated acceptance handoff

This bounded slice adds a test-only acceptance harness. It creates a temporary
Git fixture, runs the existing read-only inventory/candidate/review/plan APIs,
and removes the temporary fixture when the test exits. No production file was
changed.

## Files changed

- `tests/test_l6_offline_acceptance.py` — end-to-end Python harness.
- `tests/l6_viewer_acceptance.mjs` — local Node probe for the 3,000-record
  viewer load, first/last pagination, and disconnected components.
- `docs/review-build-handoff.md` — this handoff.

## Coverage delivered

The temporary fixture contains a merged branch, a cherry-pick/patch-equivalent
pair, a dependency/ancestry stack, two independent same-file branches, two
linked worktrees (one dirty/untracked), a stash, and a simulated inaccessible
worktree status. The generated metadata-only viewer artifact contains exactly
3,000 unique candidates: 1,000 Git-factual, 1,000 Jev-like offline responses,
and 1,000 unresolved records.

The Node probe loads only local JSON and `docs/viewer-data.js`. It verifies
factual, evaluated, and unresolved counts, two or more multi-branch connected
components, and candidate page 1 plus page 30 reachability. The Python harness
also exports and re-imports a v2 review decision, reconciles unchanged inputs
as current, changes one branch tip and observes a stale decision, builds the
preservation queue, and invokes `jg plan` with the re-imported decision. The
CLI plan must report `PRESERVE_IN_BRANCH` and `current` for `patch-source`.

## Verification commands and results

Run from the repository root:

```text
PYTHONPATH=src python3 -m unittest tests.test_l6_offline_acceptance -v
```

Observed result:

```text
test_offline_integrated_acceptance_harness (...) ... ok
----------------------------------------------------------------------
Ran 1 test in 1.880s

OK
```

```text
node --test tests/test_viewer_data.mjs
```

Observed result: `16` tests passed, `0` failed.

```text
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

Observed result: `53` tests passed, `0` failed in `8.862s`. No package
installation, live Jev request, or network access was required.

## Limitations

The inaccessible-worktree case is injected through the existing `status_for`
boundary because filesystem permission behavior varies by platform and test
users. The harness verifies the resulting incomplete inventory and diagnostic
artifact without deleting or changing the worktree. Pagination is exercised by
the same local page slicing contract used by the viewer; a browser DOM or live
HTTP server is not started. There is no separate `jg preservation` subcommand
in this parent; the harness consumes the exported decision through `jg plan`
and independently verifies `build_preservation_plan`.

The test asserts fixture bytes and `git show-ref` output are identical before
and after the run, rejects network-client calls in the Python process, checks
the recorded Git command list for remote operations, and scans generated JSON
for the fixture's private content and remote URL.
