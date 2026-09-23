# Project specification

The next planned revision is [pinned contribution groups and measured review
utility](2026-09-23-group-context-revision-plan.md). Section 13 below defines its
additions to the original pairwise workflow; it does not claim those additions
are already implemented.

## 1. Problem statement

A repository can accumulate local and remote branches, linked worktrees, stashes, and uncommitted changes faster than maintainers can reconcile them. Git reliably tells us ancestry and exact object identity, but it does not answer whether two independent branches represent the same task, whether one supersedes another, or where a partial change was preserved. As a result, an apparently tidy cleanup can lose unique work, while avoiding cleanup leaves the canonical checkout dirty and the remaining work hard to understand.

The desired outcome is a reproducible, reviewable account of **what exists, how it relates, what must be preserved, and which next action is safe**. “Clean” means the canonical checkout is on the intended default branch and has no uncommitted changes; every other branch, worktree, and stash is classified as active work, preserved history, a reviewed cleanup candidate, or unresolved. A clean state does not require deleting all branches.

### Five whys: working causal hypothesis

The observed symptom is branch and worktree accumulation with a dirty canonical checkout. The answers below are hypotheses about process, not claims established by Git data. The tool must gather evidence that can confirm or revise them.

| Why | Working answer | Evidence to collect |
| --- | --- | --- |
| 1. Why is the repository not clean? | Multiple work streams have left uncommitted changes and many refs, worktrees, or stashes needing decisions. | Porcelain status, worktree list, refs, stash metadata. |
| 2. Why do those items remain? | Creation is easy, but completion does not consistently include a preservation and closure step. | Age, upstream and PR state, last activity, worktree attachment. |
| 3. Why is closure hard? | The maintainer cannot see a reliable link from each piece of work to its task, PR, successor branch, or place it landed. | Commit ancestry, patch equivalence, changed paths, PR links, task IDs. |
| 4. Why do existing Git commands not settle it? | “Merged” describes ancestry; cherry-picks, rebases, partial reuse, and semantic overlap can leave related work unmerged by that test. | Unique commits, patch IDs, overlapping hunks, content and intent evidence. |
| 5. Why is there no repeatable decision? | There is no shared, evidence-backed disposition record or explicit cleanup gate for unique work. | A review ledger and repeat runs showing whether unresolved items reach disposition. |

The product hypothesis is that a Git fact graph plus narrowly scoped semantic judgments can reduce unresolved items and produce a safe plan. We will measure that on labeled examples before trusting model suggestions.

## 2. Users and jobs

The primary user is a maintainer responsible for a busy Git repository. They need to:

1. See every potentially unpreserved unit of work, including dirty worktrees and stashes.
2. Find likely relationships among branches and PRs without comparing every pair manually.
3. Ask why the tool considers work duplicated, dependent, superseded, or independent.
4. Decide where each unique change should live.
5. Verify that the canonical checkout is clean and no work remains unaccounted for.

## 3. Scope and boundary

### First version

- Operates locally on any Git repository or linked worktree named by `--repo PATH`, including private repositories. The implementation must not contain repository-specific rules or names.
- Inventories refs, worktrees, dirty state, stashes, unique commits, changed paths, and exact patch equivalence.
- Builds a bounded candidate graph from deterministic signals.
- Optionally asks Jev typed questions about candidate relationships, with explicit consent to send a previewed payload.
- Produces a review report and machine-readable evidence. The analysis and review operations are read-only; the separately approved cleanup executor below is an explicit later extension.

### Outside the first version

- Dropping stashes, removing worktrees, resetting files, force pushing, or merging code. Local branch deletion is available only through the separately approved, lease-gated cleanup extension below.
- Automatically choosing a canonical branch for unique work.
- Indexing full source files or sending raw diffs to a model by default.
- A required graph or relational database. The first graph is in memory and exported as files.
- Claiming that model confidence proves a branch is safe to delete.

### Local-only contract

The first version is safe to point at a private repository because collection, candidate generation, report rendering, and review planning run only on the local machine.

- It does not run `git fetch`, `pull`, `push`, `remote update`, or any command that contacts a Git remote.
- It does not call GitHub, GitLab, a hosted issue tracker, analytics, telemetry, crash reporting, or an update service.
- It does not create commits, refs, notes, stashes, worktrees, index entries, files, or directories inside the inspected repository or any linked worktree.
- It writes artifacts only to an explicit `--out DIR` outside every inspected worktree. It rejects an output path inside an inspected worktree, including through a resolved symlink.
- It does not read credential files, environment values, or Git remote URLs containing credentials. File contents are read only under the explicitly selected `code` evidence profile described below, from committed Git blobs and bounded Python ranges.

The only network path is the opt-in Jev adapter. It has no default API key lookup and cannot run until the operator both supplies credentials through their own environment or secret store and passes `--use-jev`. Before one metadata request, `jg relate --preview` renders the exact JSON payload, its byte count, and the destination host. A live metadata call sends only the fields listed in the payload contract below; it never uploads a full repository, raw working-tree diff, source file, credential, or report. The explicit `code` profile may send only approved bounded committed excerpts through the transient preview flow. There is no OpenAI, Codex, Claude, generic LLM, agent, model-router, or fallback API client in the project.

The default inventory is metadata-only: ref names, object IDs, commit subjects, timestamps, path names, patch IDs, and worktree/stash state. The optional, versioned `code` evidence profile is a narrow exception to the no-file-content rule: it reads only explicitly selected committed Python line ranges from pinned source blobs and records the corresponding main blob ID. Sensitive paths and content are denied, scanning errors fail closed, and line, excerpt, and byte limits apply. `jg code-relate` prints the exact request JSON as a transient local preview and never writes that preview or raw excerpts to an artifact. A live request needs both the approved payload digest and batch digest; pinned commits, blobs, ranges, and hashes are revalidated immediately before every send. A selection may additionally bind named refs and reject their movement, or explicitly use `snapshot_mode: pinned_commits` with no named refs; the latter compares immutable objects even when a branch label moves. Cleanup always requires fresh live-ref checks. The persistent response record contains only validated answers, model, usage, status, timings, and digests. Provider raw responses, echoed code, preview bytes, and error bodies are never persisted. Metadata-only remains the default.

## 4. Core model

### Nodes

`Repository`, `Branch`, `Commit`, `Worktree`, `Stash`, `PullRequest` (optional), and `Task` (optional). Each has a stable identifier, observed timestamp, source command, and source revision. A branch name is a mutable label; its tip SHA is recorded separately.

### Factual edges

`POINTS_TO`, `CHECKED_OUT_AT`, `PARENT_OF`, `CONTAINS_COMMIT`, `TRACKS`, `HAS_STASH`, `LINKED_TO_PR`, `PATCH_EQUIVALENT`, and `CHANGES_PATH`. Their provenance is a Git command or a named external source. Factual edges are never inferred from Jev.

### Proposed edges

`SAME_INTENT`, `A_DEPENDS_ON_B`, `A_SUPERSEDES_B`, `PARTIAL_OVERLAP`, and `UNRELATED`. Each proposed edge stores both endpoints' immutable tips, the exact evidence packet, question version, model/version, answer, probability or confidence when supplied, and review status. `INSUFFICIENT_EVIDENCE` is a valid result. Directional questions preserve direction; overlap and dependence may coexist.

### Dispositions

Each branch, dirty worktree, and stash gets one reviewed disposition: `ACTIVE`, `PRESERVE_IN_PR`, `PRESERVE_IN_BRANCH`, `PRESERVE_IN_ARCHIVE`, `CLEANUP_CANDIDATE`, or `UNRESOLVED`. A cleanup candidate is a proposal, not permission to delete. The ledger records who decided, when, why, and which exact SHAs and dirty-state fingerprints were reviewed.

## 5. Processing flow

1. **Snapshot.** Resolve the repository root, default branch, refs and tips, linked worktrees, status in each worktree, and stashes. Capture command exit status and snapshot time. Fail closed if inventory is incomplete.
2. **Prove exact relationships.** Compute ancestry, merge bases, unique commit sets, and exact patch equivalence. Exact duplication can be shown without Jev.
3. **Generate candidates.** Use bounded signals such as shared task IDs, PR links, patch IDs, changed paths, commit subjects, and recent common ancestry. Record why a pair entered the candidate set and report coverage limits. Avoid a quadratic all-pairs model call.
4. **Ask relational questions.** For each uncertain pair, send a compact, previewed state packet. Separate questions ask shared intent, directional dependence, directional supersession, and partial overlap. Require an explicit unknown outcome. Jev does not invent nodes or execute actions.
5. **Assemble clusters.** Combine reviewed factual and proposed edges into a graph. A cluster may contain several branches and multiple independent pieces of work; do not force a single winner.
6. **Plan preservation.** For each item, show unique commits and dirty content at risk, possible destination, and remaining uncertainty. A human records the disposition.
7. **Recheck.** Before any future cleanup executor acts, refresh refs and dirty-state fingerprints; invalidate decisions whose inputs changed.

### Example relational questions

The API layer will encode these as supported Jev typed primitives after its current schema is verified. The question text and answer set will be versioned.

- “Do these two branches implement the same intended change?” → yes probability, with an evidence-sufficiency check.
- “Does either branch require work unique to the other?” → `A_REQUIRES_B`, `B_REQUIRES_A`, `BOTH`, `NEITHER`, `UNKNOWN`.
- “Has one branch replaced the other's intended change?” → `A_REPLACES_B`, `B_REPLACES_A`, `NEITHER`, `UNKNOWN`.
- “What kind of overlap remains after exact patch matches are removed?” → `NONE`, `PARTIAL`, `SUBSTANTIAL`, `UNKNOWN`.

These are relationship questions, not a request for Jev to decide what to delete. If responses conflict, the pair remains unresolved.

## 6. Interfaces and artifacts

Provisional CLI:

```text
jg inventory --repo PATH --out DIR
jg candidates --repo PATH --inventory DIR/inventory.json --out DIR
jg relate --repo PATH --candidates DIR/candidates.json --preview --out DIR
jg relate --repo PATH --candidates DIR/candidates.json --use-jev --approved-preview DIR/jev-preview.json --approved-payload-sha256 SHA256 --out DIR
jg plan --repo PATH --inventory DIR/inventory.json --candidates DIR/candidates.json --out DIR
```

`--repo` accepts a repository root or any linked worktree. It resolves the Git common directory and inventories that repository's refs, linked worktrees, and stashes. The command accepts no remote name or hosted-service argument in the first version.

`--out` is required for commands that create artifacts. It is an operator-owned local directory, not a directory in the inspected repository. The command resolves both paths before it reads Git data and fails with a clear error if the output would be inside an inspected worktree. This keeps a private repository clean even when the tool is run from that repository.

An output directory contains `manifest.json` (schema version, repository identity, timestamp, command outcomes), `inventory.json`, `candidates.json`, `relations.json`, and `plan.md`. The manifest identifies a repository by a locally generated opaque run identifier plus a hash of its canonical path; it does not publish the path or remote URL. JSON records contain evidence identifiers and source SHAs so a report can be reproduced and a stale report detected. Artifacts contain no credentials or environment variable values.

The preview shows the exact fields and byte count sent to Jev, with a redaction check. Live use is opt-in for each run and defaults to a maximum of **one request and 8,192 total payload bytes**. The only way to raise either limit is an explicit `--max-jev-requests` or `--max-jev-payload-bytes` command-line override, after preview review. API credentials come from the process environment or an operator-managed secret store and never enter output artifacts.

### Jev payload contract

For each candidate pair, the default payload may include: synthetic candidate ID; endpoint tip SHAs; branch labels if the operator permits them; merge-base SHA; counts of unique commits and patch-equivalent commits; changed-path hashes or operator-approved path names; task or PR identifiers deliberately supplied by the operator; and a compact list of normalized commit subjects. It also carries question version, answer choices, and evidence identifiers.

The default payload excludes: repository path and remote URL; author name, email, or signing data; commit body; source file content; diff hunks; uncommitted file content; stash content; environment values; and all local artifact paths. The preview is the authority for a live request: if a field is absent there, it must be absent from the request.

## 7. Safety invariants

- No command in the first version mutates the inspected repository, its refs, worktrees, stashes, index, or working files.
- A Git error, inaccessible worktree, changed ref, or incomplete stash inventory produces an incomplete report, never a “safe” verdict.
- A branch with unique commits, a dirty worktree, or a referenced stash cannot be classified as safe to remove solely from semantic similarity.
- Every proposed relation is traceable to immutable inputs; changed inputs invalidate it.
- Absence of a discovered relation is not evidence of independence when candidate coverage is incomplete.
- A default local run opens no network connection and modifies no file within the inspected repository or linked worktree.
- An artifact path nested under an inspected worktree is rejected before data collection starts.
- A live Jev request cannot occur without `--use-jev`, successful payload preview, and an operator-provided credential outside the artifact directory.
- Public fixtures contain synthetic or explicitly public data only.

## 8. Validation and success measures

Build a labeled fixture set covering merged branches, cherry-picks, rebases, stacked branches, partial extraction, independent branches touching the same file, renamed paths, gone upstreams, dirty worktrees, and stashes. Tests must prove inventory completeness on these fixtures and ensure no unique work is omitted from the plan. Compare candidate recall and Jev judgments against maintainer labels; report per-relation errors and unknowns rather than one aggregate score. Calibrate any confidence threshold from the fixture results.

Privacy and isolation tests must prove that a normal inventory run makes no network request, performs no Git remote operation, leaves the repository status and ref set byte-for-byte unchanged, and writes nothing inside the repository or linked worktrees. Tests must also prove rejection of a nested output path and verify that a Jev preview and live-request fixture contain only the declared payload fields.

Success for a real repo is measurable: the canonical checkout is clean and current; every remaining branch, worktree, and stash has a recorded disposition; no unique work is lost; and a repeat snapshot detects new or stale items. Time spent reviewing and the number of unresolved items should decline across repeated runs.

## 9. Build sequence

1. **Local inventory CLI:** Git-only read-only snapshot for an arbitrary `--repo PATH`, output-path guard, completeness warnings, JSON schema, and synthetic fixtures.
2. **Candidate graph:** factual edges, patch equivalence, candidate generation, and coverage report.
3. **Jev adapter:** payload preview, typed question versions, fake client tests, bounded live evaluation on public fixtures.
4. **Review report:** cluster view, evidence links, dispositions, and stale-input detection.
5. **Pilot:** run read-only on one real repository, review every proposed disposition, refine recall and question wording.
6. **Future executor decision:** only after the pilot, specify separate commands and explicit human approval for each destructive action.

## 10. Open decisions

- Which language should implement the CLI? Choose after a small prototype compares Git plumbing ergonomics and the supported TypeSafe SDKs.
- Which metadata may leave a private repository in a Jev request? Define a default allowlist and validate it with the pilot owner.
- Should the review ledger live beside the output artifacts or in a separate operator-owned repository?
- What minimum evidence and calibrated probability justify showing a relation as a strong suggestion?

## 11. References

- [s1s](https://github.com/cpaczek/s1s) demonstrates a local reference graph and bounded, evidence-backed Jev judgments.
- [neo4jev](https://github.com/jexp/neo4jev) demonstrates graph navigation over supplied candidate relationships.
- [TypeSafe agent skills](https://github.com/typesafe-ai/skills) documents typed System One question design.

## 12. Addendum — From relationships to decisions and a clean worktree

Added 2026-09-21.

The end-to-end workflow is **inventory → Git evidence → Jev relationships → human decisions → preservation → cleanup → verification**. This addendum expands the processing flow in §5; it does not expand the first-version mutation authority defined in §3.

The outcome remains the definition in §1: the canonical checkout is on the intended default branch with no uncommitted changes, and every remaining branch, worktree, and stash has a recorded disposition. Keeping active branches is compatible with being clean.

### Workflow and evidence gates

| Stage | Action | Evidence required to advance | Existing specification |
| --- | --- | --- | --- |
| 1. Establish the baseline | Refresh refs, branch tips, linked worktrees, staged and unstaged changes, untracked files, and stashes. Write a new inventory outside the repository. | Complete inventory with collection errors resolved. Incomplete snapshots remain exploratory. | §5.1, §7 |
| 2. Establish exact relationships | Check ancestry, identical tips, unique commits, and patch equivalence. Identify branches already represented in the intended destination. | Recorded Git evidence for each pair; outstanding unique work remains visible. | §4, §5.2 |
| 3. Find ambiguous relationships | Generate candidates from overlapping paths, subjects, patch IDs, and supplied task or PR identifiers. Track omitted pairs. | Candidate reasons and coverage limits. Missing connections never imply independence. | §5.3 |
| 4. Use Jev | Preview a bounded batch using the approved evidence profile. Ask about intent, overlap, dependencies, supersession, and evidence sufficiency. Check a small owner-labeled sample before expanding. | Valid responses tied to immutable tips and versioned questions; uncertain or conflicting answers remain unresolved. | §5.4, §6, §8 |
| 5. Review connected work | Inspect each group, including what each branch uniquely contributes and its attached worktrees. A group may contain several useful branches. | Proposed preservation destinations and explanations for remaining uncertainty. | §5.5–6 |
| 6. Record decisions | Assign each object a disposition in `review.json`; export it and generate the review plan. | Human decision, rationale, reviewer identity, timestamp, exact tips or fingerprints, and preservation destination where needed. | §4 |
| 7. Preserve work | Carry out the approved preservation: retain active branches, prepare PRs, consolidate selected changes, or create verified archives. Preserve dirty changes and stashes too. | The destination actually contains the work; relevant checks pass; recovery is possible. | §2, §5.6 |
| 8. Recheck and clean up | Refresh affected refs and dirty state. Execute only specifically approved removals after preservation is verified. | Targets still match reviewed inputs; changed items return to review. | §5.7, §9.6 |
| 9. Verify the outcome | Put the canonical checkout on the intended default branch after its changes are preserved. Repeat inventory and reconcile it with the decisions. | Clean status, accounted-for remaining objects, no lost unique work, and explicit unresolved items. | §1, §8 |

### Decisions and follow-through

| Disposition | Meaning and required follow-through |
| --- | --- |
| `ACTIVE` | Retain the work and identify its owner and next task. |
| `PRESERVE_IN_PR` | Prepare or verify the PR and its branch. A PR proposal alone does not prove the work landed. |
| `PRESERVE_IN_BRANCH` | Retain a named destination branch and verify the required work is there. |
| `PRESERVE_IN_ARCHIVE` | Create a durable archive and verify recovery before removing the original. |
| `CLEANUP_CANDIDATE` | Review the preservation proof and approve the exact removal. The disposition itself grants no permission to delete. |
| `UNRESOLVED` | Retain the work and state what evidence or decision is missing. |

Jev is most useful at stages 4–5: explaining relationships that ancestry cannot settle and directing attention toward likely duplicates or successors. A high supersession probability still leaves the practical question: **where are this branch's unique changes preserved?**

### Execution boundary and implementation gaps

The first version ends at a reviewed plan. As specified in §3, it excludes commits, merges, branch deletion, worktree removal, and stash dropping. Stages 7–9 require a separately authorized execution workflow. The current CLI must not be described as an automatic cleanup tool. Local evidence also does not establish that a checkout is current with a Git remote; that claim requires a separately authorized remote check.

### Local exact-content prepass

`jg coverage` is the strict cleanup evidence. It pins each recorded source tip and the local default-branch tip, walks the source's net-changed paths from its merge base, and compares Git tree entries (blob ID, mode, and deletion) at those two tips. Each path is `EXACT_PRESENT` or `DISTINCT`; incomplete or stale inputs are `UNKNOWN`. Whole-branch `EXACT` requires every changed path to be present exactly. It excludes branches with activity in the prior 24 hours or unverifiable activity by default. Ancestry, patch IDs, and percentages never promote a branch to `EXACT`.

`jg residual` uses an independently restored disposable Git repository to simulate a merge and inspect Python AST definitions. Its conflict and moved-definition observations are advisory, and dynamic references and other file types remain uncertain. It never upgrades coverage. Analysis creates no objects in the inspected repository.

### Separately approved cleanup

`jg cleanup plan` consumes strict coverage only. It selects at most 25 older local refs with whole-branch `EXACT` proof and excludes checked-out refs, dirty or status-unavailable worktrees, stash-linked refs, and recent or unverifiable activity. Before emitting a digest for approval, it creates a self-contained bundle outside the inspected repository and proves every pinned target can be restored in an independent repository. The approval applies to that exact plan digest.

`jg cleanup execute` is a distinct operation. It requires a branch-specific cooperative lease covering known automated worktree creators, fresh exact proof and safety checks, and an atomic compare-and-delete against the expected source SHA and destination SHA. If the lease integration cannot be established, the result is a deletion-ready plan only. The executor never removes remote refs, worktrees, or stashes. Jev judgments do not grant cleanup authority.

This executor is an explicit exception to the first version's read-only operations. The inventory, coverage, residual, review, and Jev-preview paths remain read-only with respect to the inspected repository.

`jg equivalence --repo PATH --inventory inventory.json --out DIR` performs a read-only, Git-only comparison of every recorded local branch against the recorded default branch. Repeat `--approved-destination NAME` to permit a specific other local branch as a preservation destination. The result is `equivalence.json`, bound to the inventory digest and exact tips. It records `ALREADY_PRESERVED`, `UNIQUE_WORK_REMAINS`, or `UNPROVEN` for committed content, separately from worktree occupancy and inventory completeness. Proofs may be identical tips, ancestry, identical Git trees, no net content change from the merge base, or identical blob/mode/deletion state on every path changed by the source branch. Patch-ID overlap and duplicate clusters are only supporting signals. A stale tip or unavailable Git evidence cannot produce a preservation proof. These verdicts never authorize cleanup; checked-out or dirty worktrees remain independent holds. Optional `--equivalence` on `jg decisions`, `jg batches`, and `jg viewer` surfaces these results; batches skip a pair only when both endpoints are exactly preserved.

For an active checkout, `--ignore-recent-hours 24` excludes non-default branches whose tip commit or latest local reflog update falls within the prior 24 hours. Missing commit/reflog activity evidence and branches attached to dirty or status-unavailable worktrees are also excluded, never assumed old. The artifact records the cutoff, observed activity, and per-branch scope reason. Out-of-scope branches are not evaluated for exact content; they remain in optional Jev batch preparation because their semantic relationship is unresolved. This scoped exclusion does not turn an incomplete inventory into a complete one, nor does it relax worktree or deletion gates.

At the time of this addendum, the review implementation records dispositions, rationale, timestamps, and fingerprints, but does not yet enforce all reviewer identity, preservation destination, and preservation proof requirements above. Those gaps must be addressed before an execution handoff relies on the ledger.

Inventory-digest checks can reject an older exported review outright. Carrying unchanged decisions into a refreshed inventory therefore needs deliberate reconciliation: retain the prior decision's provenance, verify the reviewed object inputs, and invalidate changed inputs rather than silently treating all old decisions as current.

### Pilot order and completion

For a busy repository such as the Home Lab pilot:

1. Obtain a complete fresh inventory.
2. Review integrated branches and identical-tip aliases first.
3. Use a calibrated Jev batch for ambiguous groups containing unique work.
4. Record destinations and decisions, then preserve the canonical checkout's uncommitted work through the separately authorized workflow.
5. Execute approved cleanup in small batches and resnapshot after each.

Finishing every pending Jev request is not a prerequisite for progress. The canonical checkout can be cleaned and straightforward branches closed while explicitly unresolved work is retained. Completion means a clean canonical checkout and a disposition for every remaining object, including documented unresolved items; it does not mean deleting every branch or forcing every relationship to a confident answer.

## 13. Revision 2 — shared context and contribution preservation

Planned 2026-09-23. The [revision plan](2026-09-23-group-context-revision-plan.md)
is the implementation sequence for these requirements. The existing v3 pairwise
API and records remain readable as historical evidence.

1. **Pin advisory inputs.** Capture explicit source and destination commits in
   an independently restorable local snapshot outside inspected repositories.
   Advisory results describe those immutable objects even if live branch names
   move. Live cleanup eligibility is separately revalidated.
2. **Account for contributions.** Map every changed path to source units,
   destination candidates, or explicit uncertainty. Local Git/AST discovery
   precedes model review; structural similarity cannot establish whole-branch
   exact preservation. Record unsupported languages and ambiguous references.
3. **Supply connected context.** Build a local graph across the eligible
   snapshot, then prepare bounded groups with dependencies, known boundary
   edges, and discovery/truncation limits. Shared state supports multiple named
   questions in one request. Neither a missing edge nor a context partition
   proves independence. Main must not collapse every group into one component.
4. **Ask typed contribution questions.** Version presence, usable-delta,
   dependency, and evidence-sufficiency questions separately from v3. Code
   assigns all contribution/evidence IDs; Jev returns typed answers. Code also
   checks scope, contradictions, and missing evidence before routing review.
5. **Measure utility.** Compare Jev with a blinded inexpensive LLM control and
   deterministic baselines on labeled pinned cases. Separate context and
   evidence effects. Import normalized control answers offline; the product
   does not gain an automatic generic-model fallback. Report errors, abstention,
   coverage, uncertainty, cost, review effort, and verified preservation work.
6. **Produce actions.** Every branch, worktree, and stash gets an accounted-for
   ledger entry, destination or hold reason, and next step. Usable work becomes
   a concrete preservation task; only strict exact coverage can enter the
   existing cleanup planner. Enable execution only after the separate real
   cooperative-lease integration and exact action approval.

The planned privacy extension permits local committed-blob parsing for
contribution extraction and a versioned code profile containing explicitly
approved bounded source AND destination Python excerpts. It excludes dirty or
stash content, retains sensitive-content scanning and fail-closed behavior,
and never stores raw excerpts in preview/response artifacts. Metadata remains
the default. The exact state, questions, ranges, and digests are previewed and
approved before any disclosure; changing context is a new payload. Persist
only validated typed answers, provenance IDs/digests, model, timing, usage, and
status. These permissions apply equally to a separately authorized LLM control.

This revision's acceptance is progress toward section 1: fewer unresolved
objects, verified destinations for usable work, a complete review ledger, and
a recoverable cleanup dry-run. Raw confidence scores or a completed request
queue cannot substitute for those outcomes.
