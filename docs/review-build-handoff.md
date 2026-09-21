# L6 offline integrated acceptance handoff

This bounded slice hardens the browser-local review export and its acceptance
harness. It creates a temporary Git fixture, runs the existing read-only
inventory/candidate/review/plan APIs, and removes the temporary fixture when
the test exits.

## Files changed

- `tests/test_l6_offline_acceptance.py` — end-to-end Python harness.
- `tests/l6_viewer_acceptance.mjs` — local Node probe for the 3,000-record
  viewer load, first/last pagination, and disconnected components.
- `docs/demo.js` — production browser-local v2 review editor/export guardrails.
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

Browser-created preservation dispositions fail closed unless they include a
meaningful destination, explicit verification, reviewer identity, and matching
source/destination proof. Legacy v1 imports and unsafe preservation decisions
are exported as v2 `UNRESOLVED` or stale records rather than being promoted to
a current preservation disposition.

When Chrome or Chromium is installed, the harness also writes a temporary
`file://` or loopback-only fixture directory and launches the direct Chrome or
Chromium binary with a unique temporary profile and DevTools endpoint bound to
`127.0.0.1` (never `open` or a global browser profile). It navigates the actual
`docs/index.html`, uses CDP `DOM.setFileInputFiles` on the production
`#inventory-file`, `#candidates-file`, `#relations-file`, and `#review-file`
inputs, waits for production counts, clicks `#candidates-view` and
`#page-next` through Page 30 of 30, checks `#graph-count`, and clicks
`#export-review`. The downloaded browser `review.json` is validated as schema
v2 with repository/inventory/candidate/relations provenance, source fingerprints,
reviewer identity, and preservation destination/proof fields, then passed to
the existing CLI plan path. If no supported browser is present, the test
records `NOT RUN`; if one is present and the probe fails, the test fails rather
than claiming browser coverage.

## Verification commands and results

Run from the repository root:

```text
PYTHONPATH=src python3 -m unittest tests.test_l6_offline_acceptance -v
```

Observed result:

```text
test_offline_integrated_acceptance_harness (...) ... ok
----------------------------------------------------------------------
Ran 1 test in 23.755s

OK
L6 browser probe: PASS (/Applications/Google Chrome.app/Contents/MacOS/Google Chrome; production DOM; Page 1 of 30 · 1-100 of 3000; Page 30 of 30 · 2901-3000 of 3000; graph=24 of 3000 candidates on graph page)
```

```text
node --test tests/test_viewer_data.mjs
```

Observed result: `16` tests passed, `0` failed.

```text
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

Observed result: `53` tests passed, `0` failed in `30.882s`. No package
installation, live Jev request, or network access was required.

## Limitations

The inaccessible-worktree case is injected through the existing `status_for`
boundary because filesystem permission behavior varies by platform and test
users. The harness verifies the resulting incomplete inventory and diagnostic
artifact without deleting or changing the worktree. Browser coverage is
conditional on an installed Chrome/Chromium binary; this environment passed
with Chrome 153.0.8010.48. The probe uses the production page and local file
inputs, but does not test a hosted deployment or live Jev request. There is no
separate `jg preservation` subcommand in this parent; the harness consumes the
browser-exported decision through `jg plan` and independently verifies
`build_preservation_plan`.

The test asserts fixture bytes and `git show-ref` output are identical before
and after the run, rejects network-client calls in the Python process, checks
the recorded Git command list for remote operations, and scans generated JSON
for the fixture's private content and remote URL.
