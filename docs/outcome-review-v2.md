# Outcome review ledger v2

`jg outcomes` renders an operator review page from one pinned inventory,
snapshot, contribution artifact, and optional presence and coverage results.
The page shows each branch's contribution-level typed presence findings,
evidence IDs, dependency observations, reasons, origin, and routing scope. It
does not include source excerpts.

Synthetic and control results remain visible as advisory observations. Only a
validated production Jev result may propose integration tasks, and only for
units that pass the existing evidence and context checks. Contradictory,
incomplete, missing, and unsupported evidence stays unresolved. Project utility
remains `UNKNOWN` when repository requirements are not supplied. No outcome or
human review authorizes deletion; live cleanup gates remain separate.

The review page exports `outcome-review` schema version 2. Every decision is
bound to the repository, inventory, pinned snapshot, contribution artifact,
optional presence and coverage digests, the object's source fingerprint, and a
fingerprint of its current evidence. Decisions contain a disposition,
rationale, reviewer ID, review time, and optional proposed destination.
`preservation_proof` must remain null; this ledger records a proposal, not proof
that work was integrated or archived.

On import with `jg outcomes --review`, the CLI rejects malformed provenance,
duplicate objects, unsupported dispositions, and claimed preservation proof.
It marks a decision current only when its full input provenance and the
object-specific fingerprints still match. Otherwise it preserves the decision
as stale and asks for re-review. Version 1 outcome reviews are retained as
historical-limited and never override current derived actions.

Use a new output directory for each refreshed ledger so prior reports remain
available for comparison. The JSON and HTML outputs are local review artifacts;
the page does not write Git refs, execute preservation tasks, or call Jev.

To carry the ledger into the canonical object queue, run:

```text
jg preservation-queue --repo PATH --inventory INVENTORY.json \
  --outcomes OUTCOMES.json --out NEW_PRIVATE_DIR
```

The queue verifies the outcome digest, repository and inventory pins, object
fingerprints, and contribution/task bindings. Per-unit evidence with advisory,
unknown, or unresolved routing is retained as a hold. A Jev task is marked
`READY_FOR_IMPLEMENTATION` only when its contribution is a validated production
candidate and the matching object has a current human `INTEGRATE` decision.
Missing and stale reviews remain visible with a blocked reason. This is a
proposed implementation task; package tests and outcome verification still
have to pass before the work can be called integrated or preserved. The queue
does not execute integration, certify preservation, or authorize cleanup.
