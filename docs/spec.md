# Project specification

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
- Produces a review report and machine-readable evidence. All operations are read-only.

### Outside the first version

- Deleting branches, dropping stashes, removing worktrees, resetting files, force pushing, or merging code.
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
- It does not read credential files, environment values, Git remote URLs containing credentials, or file contents from the inspected repository.

The only network path is the opt-in Jev adapter. It has no default API key lookup and cannot run until the operator both supplies credentials through their own environment or secret store and passes `--use-jev`. Before one request, `jg relate --preview` renders the exact JSON payload, its byte count, and the destination host. A live call sends only the fields listed in the payload contract below; it never uploads a full repository, raw working-tree diff, source file, credential, or report. There is no OpenAI, Codex, Claude, generic LLM, agent, model-router, or fallback API client in the project.

The default inventory is metadata-only: ref names, object IDs, commit subjects, timestamps, path names, patch IDs, and worktree/stash state. Commit bodies and changed-line excerpts are disabled by default. Their inclusion requires a separate explicit flag and is shown in the Jev preview.

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
