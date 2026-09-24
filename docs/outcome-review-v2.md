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
