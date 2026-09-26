# Reviewed verified-train execution chain

Source reviewed: Home Lab `50247d669d69ef480bbac1259b95258863f6c712`.
This change adds byte pins, not cleanup approval or runtime activation.

## Execution boundary

`attested_shell_supervisor.py` -> `start-train-construction.sh` ->
`shared/require-python314.sh` -> `run_verified_train_runtime.py` ->
embedded child bootstrap -> `train_construction_driver.py` /
`train_shadow_runner.py` -> `train_builder.py` / `train_promotion_driver.py`.
The cooperative lease module supplies snapshot validation, canonical common-dir
branch locks, inherited lock validation and startup attestation.

The supervisor, construction driver and builder were already pinned. The
registry now requires the verifier helper, shadow runner, promotion driver and
Python selector as well. Exact, path-bound variants admit the reviewed launcher
and lease module bytes; existing reviewed variants remain accepted. Missing,
symlinked or modified new files fail closed, including on older releases that
have not installed this chain. All hook records participate in existing runtime
capability and freshness checks.

## Review evidence

- Launcher replaces direct execution from its mutable checkout with the helper;
  missing/dirty checkout exits become typed refusals. It retains dedicated
  linked-worktree and common-directory checks, origin/main checkout and required
  attestation. Its optional sourced environment remains blocked by Jev's gate.
- Helper checks a clean dedicated linked checkout, common directory, pinned
  lease-commit ancestry, optional exact origin/main HEAD, and exact hashes of
  its five lease/driver files. It archives the verified commit and rechecks
  those files before executing snapshot code.
- Embedded bootstrap validates inherited lease identity, branch names and keys;
  the parent holds branch locks and passes their file descriptors for the
  child's lifetime. Other requested branch sets use the normal lock function.
- Driver selection is closed to construction and shadow. Shadow denies mutation
  operations. Existing builder/construction hashes are unchanged; promotion and
  shadow bytes match the helper's code-owned hash table. The Python selector
  enforces Python >=3.14; its exact shell bytes now receive the same gate.
- The reviewed lease variant changes PID-generation detection and replaces stale
  receipts before validating old process metadata. It does not change branch
  locking. No caller-supplied hashes or unreviewed digest fallback are added.

These are the lease-sensitive execution files, not a claim that every transitive
application import has been independently reviewed. The existing runtime commit,
cleanliness, loaded-process and source-attestation checks remain required.

## Remaining blocker: process-generation receipt compatibility

The newer lease module emits `darwin:pid:seconds:microseconds` or
`linux:boot-id:start-ticks` in `process_start`. Jev currently obtains `ps lstart`
and requires exact receipt equality. This PR deliberately leaves that equality
check intact. A newer receipt will therefore remain blocked; byte acceptance
must not be mistaken for a successful live creator capability or deletion
permission. A separate, scoped generation-reader compatibility review is needed
before claiming runtime readiness. No runtime was restarted for this PR.

## Verification

`PYTHONPATH=src python3 -m unittest discover -s tests -p test_cleanup.py -k verified_train -v`: 2 passed.

Full cleanup file: 44 passed, 1 failed. The failure also reproduces on unchanged
main: `test_running_creator_failures_have_hashed_diagnostics_and_wrapper_still_fails`
(`reviewed_wrapper`) expects `process_image_mismatch` but the safe diagnostic
returns `other`. This pre-existing diagnostic mismatch is not changed here.

The new tests require all chain paths and their reviewed hashes, verify path
binding, exercise real file reads, mutate helper and dependency bytes, and
check missing/symlinked helpers fail closed. Synthetic fixtures contain no
Home Lab source or private inventory.
