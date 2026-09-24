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

The exported file is untrusted input, including its reviewer name and claimed
decision. A current `INTEGRATE` decision remains
`PROPOSED_AWAITING_HUMAN_VERIFICATION` until the operator explicitly approves
that exact review file by digest. First ask the CLI to calculate the digest:

```text
jg outcome-review-approve --review outcome-review.json
```

Inspect the file and returned SHA-256, then create the owner-only local receipt
by repeating the command with that exact digest and a new private output path:

```text
jg outcome-review-approve --review outcome-review.json \
  --approved-review-sha256 REVIEW_SHA256 --out review-approval.json
jg outcomes --repo PATH --inventory INVENTORY.json --snapshot SNAPSHOT.json \
  --contributions CONTRIBUTIONS.json --presence PRESENCE.json \
  --review outcome-review.json --review-approval review-approval.json \
  --out NEW_PRIVATE_DIR
```

The receipt is signed with the local private presence key and binds the review
digest, repository, and review provenance. The resulting outcome artifact also
has a signed receipt over its complete body and pinned evidence. Recomputing
the public JSON digest after changing an outcome cannot make it trusted. A
receipt for a stale review remains visible as stale and cannot make a task
ready. Treat the approval receipt as local private data.

Use a new output directory for each refreshed ledger so prior reports remain
available for comparison. The JSON and HTML outputs are local review artifacts;
the page does not write Git refs, execute preservation tasks, or call Jev.

To carry the ledger into the canonical object queue, run:

```text
jg preservation-queue --repo PATH --inventory INVENTORY.json \
  --outcomes OUTCOMES.json --out NEW_PRIVATE_DIR
```

The queue verifies the outcome digest, signed outcome receipt, repository and
inventory pins, object fingerprints, and contribution/task bindings. Per-unit
evidence with advisory, unknown, or unresolved routing is retained as a hold.
A Jev task is marked `READY_FOR_IMPLEMENTATION` only when its contribution is a
validated production candidate and the matching object has both a current
human `INTEGRATE` decision and a valid exact-review approval receipt.
Missing and stale reviews remain visible with a blocked reason. This is a
proposed implementation task; package tests and outcome verification still
have to pass before the work can be called integrated or preserved. The queue
does not execute integration, certify preservation, or authorize cleanup.

## Metadata-only delivery observations

`jg outcomes --delivery-observations delivery-observations.json` accepts an
optional sidecar with `kind: "delivery-observations"`, schema version 1, and
exact repository, snapshot, and contribution digests. Each row binds one full
contribution ID to its source branch/tip/path/blob and the pinned snapshot-main
branch/tip. PR metadata records the exact GitHub URL, repository, number, head,
observed base, `OPEN` or `MERGED` status, and a merge SHA for `MERGED` only; the
row also records an offset-bearing observation time, observer identity/type,
and evidence reference plus SHA-256. Unknown fields, duplicate or unknown IDs,
stale pins, malformed URLs/SHAs, and inconsistent merge metadata fail closed.

The sidecar is an observation record. Its `OPEN`/`MERGED` status and PR head/base
are reported metadata; the CLI does not query GitHub or validate that those refs
contain the contribution or match the separately pinned analysis destination.
The outcomes and preservation-queue pages display it under a separate delivery
observation heading. It does not create a proposed task, change any disposition,
review fingerprint, readiness gate, or preservation proof. A merge observation
does not independently verify destination content. No raw source or provider
request is read by this path.
