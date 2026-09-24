# Revision 2: pinned contribution groups and measured review utility

Status: implementation in progress; see the
[delivery ledger](outcome-delivery-ledger.md) for current verification. The
bounded contribution extractor, groups, evidence, typed presence requests,
offline imports, reconciliation, and outcomes review are implemented. Owner
labels, measured utility, accepted real provider execution, verified integration,
and creator-gated cleanup remain pending. This plan records the design and
acceptance criteria; historical v2/v3 artifacts and question meanings remain
unchanged.

## Outcome and scope

Advance the specification's outcome: account for every branch, worktree, and
stash; identify usable work and its preservation destination; produce the next
reviewable action. The measure is fewer unresolved objects and less maintainer
review effort with no lost work. Completing a number of model calls is not an
acceptance criterion.

The implementation remains repository-independent. Home Lab is the private
pilot. Its selected source commits and destination commit remain frozen for
advisory analysis. Branches active within 24 hours or with unverifiable activity
remain excluded. Existing worktrees and stashes remain inventoried and held;
this revision does not send their uncommitted content to a provider.

## 0. Close old work before starting

Completed in PR #9:

- Integrated the pinned-commit CLI mode, pilot/control findings, corrected typed
  question design, tests, and README into main.
- Verified the merged tree equals the integration branch's tree.
- Removed the consumed local/remote branch and extra worktree. The repository
  had no stashes or uncommitted work to preserve.
- Passed 93 Python tests with the declared SDK dependency, including the offline
  integrated/browser probe, and 17 viewer data tests.

Ship this plan and its spec links as a separate documentation PR and remove its
temporary branch. Each later slice starts from clean main, lands through its
own PR, and ends with no consumed branch, worktree, stash, or hidden WIP. Keep
private pilot artifacts outside the source repository as evidence, not as code
holding places. An intentionally held implementation item needs a named owner,
reason, and next action; do not create speculative branches for future slices.

## Findings that change the design

The pilot supplied one source/main pair per request. Its batches scheduled
independent calls; they did not provide a shared graph. All 99 validated Jev
responses fell below the v3 evidence-sufficiency routing gate. The blind 24-case
Luna control was enriched toward high Jev scores: six of its seven routed
signals coincided with normalized subject matches, and the seventh contradicted
its own insufficiency answer. There are no owner labels establishing either
model's accuracy or comparative utility.

These observations show inadequate evidence and unmeasured utility for this
workflow. They do not isolate graph context as the cause: wording, source
content, destination retrieval, and task definition also changed the problem.
The next experiment must distinguish those factors.

The current implementation also has concrete limits:

- `candidates.py` discovers non-default branch pairs from paths and subjects,
  skips very common signals, caps discovery, and calculates patch overlap only
  after selecting pairs. It does not construct a complete preservation map to
  main. Unknown or omitted edges cannot mean independence.
- `residual.py` reports top-level Python definition moves and possible names;
  it does not yet account for every changed contribution. Its `--all` bundle
  export also cannot guarantee retention of an unreferenced historical pin.
- Code evidence v1 supplies source text and a destination blob ID, not text
  from both sides. It cannot support a behavioral comparison by itself.
- `calibration.py` scores the legacy pair contract. A shared probability above
  .75 has not been calibrated for the proposed presence or review tasks.
- `decisions.py` gates semantic signals on evidence sufficiency, while
  `preservation.py` currently queues high relation scores without that gate.
  The revision needs one version-aware interpretation across consumers.
- The CLI cleanup executor still lacks the cooperating worktree-creator lease.
  Semantic review cannot bypass that implementation gap.

## Prior art and adopted choices

| Evidence | Design choice for this revision |
| --- | --- |
| [Git patch-id](https://git-scm.com/docs/git-patch-id) locates likely copied commits; [range-diff](https://git-scm.com/docs/git-range-diff) matches two commit series through an assignment problem. | Compute cheap candidate signals locally. Keep range-diff as a review aid; its output is not a stable machine interface and patch matching is not whole-branch exact proof. |
| [GitHub stacked PRs](https://docs.github.com/en/pull-requests/get-started/about-stacked-prs) retain dependency chains between focused changes. | Preserve branch ancestry and contribution dependencies when assembling review context. A branch can contain both inherited and new work. |
| [s1s](https://github.com/cpaczek/s1s) uses a lexical index and reference graph to select evidence, with typed Jev judgments. | Separate local discovery coverage from model relevance. Report which neighbors were examined and which remain outside the request. |
| [neo4jev](https://github.com/jexp/neo4jev) supplies bounded neighboring relationships to Jev and explicitly documents edges omitted by its caps. | Use stable graph IDs and bounded context expansion. Keep omitted boundary records; a selected neighborhood is not the whole graph. |
| [RepoGraph](https://arxiv.org/abs/2410.14684) retrieves code subgraphs for repository tasks. | Test connected code context against the pair baseline; its coding results are not evidence of cleanup correctness. |
| [TypeSafe workflows](https://evals.typesafe.ai/) ask narrow typed questions and compute rules in code. | Put a group's related evidence in shared state; ask multiple questions over it; apply reconciliation and routing locally. Typed outputs do not guarantee factual correctness or joint consistency. |

These are design references, not new dependencies or evidence that the proposed
cleanup workflow is already validated. Keep the graph in local artifacts; a
graph database is not required.

## 1. Reproducible snapshot and contribution evidence

Implement a versioned `snapshot.json` manifest and independent local object
store outside the inspected repository. Include repository ID, source commits,
destination commits/trees, inventory digest, activity cutoff, exclusion reasons,
and object-store digest. Restore the complete object closure of every selected
commit into a disposable repository and verify all pins there. Do not rely on
current named refs, `--all`, alternates, or shared hard-linked object storage.

Moving live main or a source label does not invalidate an advisory result about
those commits. Missing objects or changed payloads do. Live activity, refs,
worktree status, and stash holds are checked again when planning actual actions.
There is no requirement to pause the inspected repository during analysis.

Build `contributions.json` from source changes relative to their recorded merge
bases. Record added, modified, deleted, renamed, and mode-changed paths, plus
Python definitions and statically resolvable references. Retain file-level
records for changes outside definitions, non-Python files, binaries, parse
failures, ambiguous bindings, decorators, imports, and module initialization.
Do not silently omit unsupported work or label an AST name match as a call edge.

Index the pinned destination once per tree; cache parsing by blob and parser
version. Search exact blobs and AST fingerprints before retrieving approximate
candidates. A moved or identical AST unit is advisory structural evidence:
bindings, callers, execution environment, and tests can still differ. Preserve
all ambiguous matches rather than overwriting them in a fingerprint dictionary.

Each source contribution and destination candidate gets an ID tied to commits,
path/mode, blob, ranges, and extraction version. Every changed path must map to
accounted contributions or an explicit unknown record. Strict whole-branch
`EXACT` remains the existing byte/mode/deletion proof from `coverage.py`.

## 2. Shared graph context with visible coverage limits

Build `groups.json` over the entire eligible snapshot before preparing requests.
Separate verified Git facts, static-analysis observations, heuristic candidate
edges, and model judgments. Record provenance for every edge. Index patch and
symbol evidence before candidate ranking so copied work can be discovered when
branch names or paths differ.

Group related contributions using ancestry, patch candidates, shared symbols,
resolvable references, and deliberately supplied task/PR identifiers. Main is a
destination index, not a universal grouping edge: connecting every branch to a
single main node must not collapse all work into one useless component. Common
paths and broad terms also need explicit candidate budgets and omitted counts.

A request contains a bounded group, relevant destination candidates, dependent
neighbors, and known boundary edges. Include the discovery scope, truncation
reasons, and unresolved/excluded neighbors. Record independent budgets for
objects, edges, excerpts, questions, request bytes, and provider token limits.
Estimate with documented tooling and record estimation limits; fail before
dispatch if the approved/provider budget cannot be established. Splitting a
large component must retain cross-partition IDs and boundary coverage. Expand
uncertain neighborhoods only through a new reviewed request plan.

Sending every branch in one JSON is optional when it fits these requirements;
it is not a correctness guarantee. Neither pairwise calls nor arbitrary
25-branch partitions are the default context strategy. The planner chooses
context by relationships and records what it leaves out.

## 3. Typed questions and a two-sided evidence profile

Add `branch-presence-v1` beside the immutable v3 relationship contract. The unit
is a named source contribution and enumerated destination evidence, within the
supplied group. Proposed questions:

| Question | Typed answer | Meaning |
| --- | --- | --- |
| `evidence_sufficient` | Noul | Can the supplied source, destination, and dependency evidence support this particular comparison? |
| `presence` | Choice: `PRESENT`, `PARTIAL`, `ABSENT`, `UNKNOWN` | To what extent does the named destination evidence contain the contribution's required behavior? Absence is scoped to the supplied search, not all possible code. |
| `usable_delta` | Noul | Does the source contain a concrete behavior or test missing from the supplied destination evidence and potentially worth preservation? |
| `dependency_relevant` | Noul per selected edge | Does this contribution require the named neighbor's behavior or interface? |

Question instructions must name their contribution/edge IDs explicitly; map
keys alone are not model instructions. Local code assigns all source/evidence
IDs and constructs the typed answer-to-evidence mapping. Jev does not generate
citations, new nodes, destination paths, or preservation claims. No model answer
is proof that an excerpt was inspected or a test executed.

Source purpose and technical delta are distinct from project priority. Record
owner-supplied goals and constraints when available. Without those, useful-work
priority remains a human decision; an apparently obsolete feature is retained
until explicitly dispositioned.

Add a versioned code-evidence v2 profile with explicitly selected source AND
destination Python ranges. Keep sensitive-path/content rejection, strict size
limits, fail-closed scanning, transient no-store exact previews, and approval
of exact payload/batch digests. Revalidate immutable objects, ranges, hashes,
question version, and request bytes immediately before each request. Extend
the local-read contract for AST extraction explicitly; raw source stays local
unless its bounded excerpt appears in that approved preview.

Related questions share one `state` through the existing pooled official SDK.
Independent answers do not condition on one another; shared context does not
make them jointly consistent. A deterministic reconciler marks low-sufficiency,
conflicting, unsupported, or incomplete-context answers unresolved. The proposed
result format records snapshot/group/contribution/evidence/request digests,
question/model versions, validated typed answers, status, timing, and usage.
Never persist raw provider responses, echoed source, or error bodies.

## 4. Blinded evaluation before scale

Create a small fixed corpus of approximately 24-40 contributions in 8-12 task
families. Include independent synthetic known-answer fixtures and real pinned
pilot examples. Include source work present in main, partial preservation,
usable missing work, a third-branch dependency, same subjects with different
behavior, moved definitions with changed bindings, reverted work, and missing
or excluded boundary evidence. Classify unsupported languages separately.

Prepare owner-reviewable evidence cards and labels before showing model answers.
An independent code review can propose labels; record who accepted them and
leave disagreements unknown. A model vote is not ground truth. The original
99 judgments remain a historical metadata baseline, not labels.

Compare the following on the same held-out units:

- Deterministic Git/AST/normalized-subject baselines.
- Jev and a cheap Luna control, blind to one another's answers.
- Paired versus grouped context with the SAME question contract.
- Metadata versus approved two-sided evidence, independently of context shape.

Pre-register the factorial contrasts. For each context comparison, fix the
pinned contribution/destination IDs, retrieval candidate and excerpt pool,
question text, evidence profile, model settings, and input budget. Compare
pair packaging with shared relationship context under that matched budget;
do not silently truncate either arm. A separate natural-budget arm may retrieve
more neighbors, but report its extra evidence/tokens and overall utility
separately from the context-only effect. Score unique contributions with the
same denominators and deduplicate overlapping group answers. Count total
request tokens/cost as well as per-contribution quality so batching savings
are measured rather than assumed.

Keep families together when splitting development and held-out cases. Fix the
question wording, thresholds, scoring rules, and model versions before the
held-out run. Randomize display order without breaking graph identity. Record
actual model/reasoning settings and input/request digests for reproducibility.
Raw self-reported model scores are not assumed equally calibrated.
Use the already inspected 99-case pilot and its related families for development
only; select untouched families for held-out real cases. Otherwise their known
scores and the code spot checks would leak into the comparison.

Extend `calibrate` to import strict normalized control records offline; do not
add a generic LLM client or silent fallback to the product. Running the control
is a separate explicitly authorized evaluation step, and any source disclosure
has the same reviewed evidence boundary as the Jev run. No new provider calls
follow from this plan or from approval of an earlier metadata payload.

Report per-class precision/recall and denominators, abstention, contradictions,
candidate-retrieval recall, Brier score on labeled binary questions, token/call
cost when known, wall time, maintainer review time, and resolved contributions
per reviewed group. Report uncertainty intervals; a tiny sample cannot establish
rare-error safety. Break out the effect of context, evidence, and model instead
of attributing every gain to Jev.

The first expansion gate requires: no omitted inventory objects; all missing
context marked unknown; all pilot dispositions reviewed; no unsupported
preservation assertion among accepted recommendations; and a measured reduction
in review effort or increase in verified useful outcomes over the deterministic
baseline. Select routing thresholds from development labels and report held-out
performance without retuning. If incremental benefit is absent or unresolved,
retain deterministic review and revise the experiment; do not run the full set.
Any later increase is a bounded tranche with a new exact request manifest and
its measured request count, rather than a fixed one-call-per-branch target.

## 5. Turn evidence into project actions

Extend the existing viewer, review ledger, preservation queues, and `jg plan`.
Show each object's current status, contributions, candidate destinations, exact
versus semantic evidence, boundary limitations, dependencies, and next action.
Reconcile overlapping group judgments by immutable contribution ID; never count
the same contribution twice or resolve contradictory answers by last write.
Use one version-aware interpretation contract for CLI decisions, preservation
queues, calibration, and the viewer so evidence insufficiency and unknowns have
the same meaning throughout the product.
Preserve historical question definitions and stored answers; version any change
in how the application routes those answers. Low-sufficiency v3 results must
not appear as a routed semantic recommendation in one view while another calls
them unknown. Add cross-consumer fixtures for this discrepancy and new-contract
scope, ID, and contradiction checks.

Produce concrete review queues:

- **Exact coverage:** prepare a fresh cleanup dry-run for eligible inactive refs.
- **Likely preserved:** show the destination and missing verification steps.
- **Usable work remains:** propose the specific units to integrate and their
  prerequisite work; a later authorized preservation operation proves them.
- **Context missing or active:** retain the object with a reason and next step.

Every branch, worktree, and stash remains represented, including exclusions and
items with no discovered relationships. Report changes in unresolved counts
without claiming that newly discovered uncertainty is a regression. Historical
snapshot judgments remain reproducible; current action eligibility has a separate
freshness state. Do not label unique commit history as proven unique code.

Semantic preservation does not promote a branch to `EXACT`. A useful partial
change may need an integration PR or verified archive before any later cleanup
policy applies. This revision produces reviewable preservation work and strict
cleanup manifests; it does not automatically remove such branches.
If useful work is semantically present but exact coverage still fails, retain
the branch with an explicit unresolved or verified-archive disposition and
the remaining gap. Neither a Jev answer nor a human semantic vote makes it
cleanup-ready. Removing archive-backed non-EXACT refs would require a separately
specified and approved preservation policy beyond the current executor.

After the review-product pilot is accepted, implement the existing
[cooperative lease roadmap](cleanup-executor-roadmap.md) as a separate slice.
Prove integration with every known worktree creator before enabling the local
executor. At cleanup time, recheck live exact coverage, activity, worktrees,
stashes, destination SHA, recovery bundle, and approval digest. A moving main
may change that action plan without invalidating the earlier advisory study.
Worktree removal and stash dropping require their own future preservation work.

## Delivery slices and verification

| Slice | Main modules/artifacts | Required result |
| --- | --- | --- |
| R2.0 — this plan | Spec, quality-plan status, cleanup roadmap, README | One current revision plan on clean main; previous branch consumed. |
| R2.1 — fixed evidence | New snapshot/contribution modules; `residual.py`, `coverage.py` | Restore all explicit pins independently; inventory every changed unit; zero writes to inspected repositories. |
| R2.2 — context and preview | `candidates.py`, new group planner, `questions.py`, `code_evidence.py`, `jev.py`, artifact validators | Offline grouped previews with two-sided ranges, boundary coverage, strict digests and typed synthetic responses. No live calls needed for acceptance. |
| R2.3 — control and calibration | `calibration.py`, normalized control import, private evidence cards | Labeled blinded comparison; measured go/revise decision and concrete approved pilot payloads before live evaluation. |
| R2.4 — actionable review | `review.py`, `preservation.py`, `decisions.py`, viewer, `plan.py` | One accounted-for row per object; specific destinations and preservation tasks; a strict cleanup dry-run. |
| R2.5 — operational cleanup | Existing cleanup library and a real cooperating coordinator | Lease integration, verified recovery, live gates and approved execution; otherwise explicit plan-only status. |

Proposed CLI additions are `jg snapshot`, `jg contributions`, `jg groups`, and
`jg group-relate`; extend existing `jg calibrate`, `jg plan`, and the viewer.
Each schema and CLI slice must document which commands are implemented versus
planned. Preserve old artifacts with explicit version dispatch rather than
changing the global v3 question constant and silently reinterpreting history.

Required fixtures cover: main/source labels moving after pinning; unreferenced
pins and missing objects; cherry-picks/rebases/squashes/reverts; renames/modes/
deletions/binaries; AST ambiguity and module-level changes; a dependency outside
the selected group; a giant shared-main component; candidate truncation and
unsupported file types; secret-bearing source and destination excerpts; altered
digests and unknown IDs; contradictory model answers; incomplete/uncertain SDK
attempts; calibration leakage; old schema compatibility; and a model suggestion
attempting to enter cleanup. Existing live-race/bundle/lease tests remain gates
when the executor integration is touched.

For each slice, run focused Python tests and viewer tests when affected, then
the existing integrated offline acceptance gate before landing. Do not repeat
provider calls as tests. Publish the verification commands and exact head in
the PR, verify the merged tree, and remove its consumed branch/worktree.

The private pilot finishes with an accounted-for object ledger, accepted
preservation tasks, measured model utility, and a recovery-backed cleanup
dry-run. Any actual cleanup is a later approved action on live inputs. The
source repository finishes every slice on clean main with zero unaccounted WIP.
