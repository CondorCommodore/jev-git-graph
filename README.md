# jev-git-graph

Find the relationships among Git branches, commits, worktrees, stashes, and pull requests so a maintainer can return a repository to a clean, understood state without losing work.

The project begins as a **read-only local CLI**. Git provides the facts and an in-memory graph. Jev answers small, typed questions about relationships that Git ancestry alone cannot establish. A maintainer reviews the resulting preservation and cleanup plan. The first version does not execute that plan.

## Use with a private repository

Point the tool at any local repository with `--repo PATH`. The default workflow stays on the machine: it does not contact Git remotes or hosted services, and it writes reports only to an output directory outside the inspected repository. That means it can inspect a private repository without publishing its inventory, history, worktree state, or report.

Jev use is separate and opt-in. Before a live request, the tool shows the exact payload it would send. The default payload excludes source files, raw diffs, stash contents, credentials, repository paths, and remote URLs. The full contract is in the [specification](docs/spec.md#local-only-contract).

## Run it

The initial CLI has no runtime dependency beyond Python and Git. Install it into an environment you control, then choose an artifact directory that is outside the repository being inspected:

```bash
python -m pip install -e .

jg inventory --repo /path/to/repository --out /path/to/private-artifacts
jg candidates --repo /path/to/repository --inventory /path/to/private-artifacts/inventory.json --out /path/to/private-artifacts
jg relate --repo /path/to/repository --candidates /path/to/private-artifacts/candidates.json --preview --out /path/to/private-artifacts
jg plan --repo /path/to/repository --inventory /path/to/private-artifacts/inventory.json --candidates /path/to/private-artifacts/candidates.json --out /path/to/private-artifacts
```

`relate` writes only a local preview by default. A live Jev request requires an approved preview digest, `--use-jev`, and `TYPESAFE_API_KEY` in the process environment. It is hard-capped by default at **one request and 8,192 payload bytes**; raising either cap requires an explicit command-line override after reviewing the preview. The default `--evidence-profile minimal` keeps labels, commit subjects, and path names out of the request. The explicit `review` profile adds preview-visible labels, normalized subjects, and bounded paths when the operator decides that context may leave the machine. There are no OpenAI, Codex, Claude, or agent-loop calls in this project. Read the preview before approving it.

### Large, active repositories

Use a private artifact directory outside every inspected worktree. For a local Home Lab pilot on a MacBook:

```bash
umask 077
install -d -m 700 "$HOME/.local/share/jev-git-graph/runs/home-lab"
RUN_DIR="$(mktemp -d "$HOME/.local/share/jev-git-graph/runs/home-lab/pilot.XXXXXX")"
PYTHONPATH=src python3 -m jev_git_graph inventory --repo /Users/mikebook/code/home-lab --out "$RUN_DIR"
PYTHONPATH=src python3 -m jev_git_graph candidates --repo /Users/mikebook/code/home-lab --inventory "$RUN_DIR/inventory.json" --out "$RUN_DIR"
PYTHONPATH=src python3 -m jev_git_graph plan --repo /Users/mikebook/code/home-lab --inventory "$RUN_DIR/inventory.json" --candidates "$RUN_DIR/candidates.json" --out "$RUN_DIR"
```

Run the next command only after the previous one succeeds. Inventory records locally known remote-tracking refs without fetching. If refs or worktrees change during collection, or a worktree status is unavailable, inventory exits nonzero and retains an owner-only `inventory.json` and `manifest.json` marked incomplete. Candidate and plan commands reject that snapshot. Candidate generation bounds its pair search and reports truncated coverage; omitted pairs are not evidence of independence. No Jev request occurs in this sequence.

Start with the [project specification](docs/spec.md), including the problem statement, five whys, requirements, and acceptance criteria.

The [local artifact viewer](docs/index.html) starts empty and renders only JSON selected by the operator. It makes no API calls.

## View local artifacts

After any batches finish, run `jg collect --repo PATH --candidates ORIGINAL_CANDIDATES --batch-plan BATCH_DIR/batches.json --out NEW_PRIVATE_DIR`. The combined `relations.json` verifies every response against its batch preview and attempt ledger, reports missing batches and unattempted requests, and loads beside the original inventory and candidates in the viewer. Existing outputs are preserved; choose a new aggregate directory for each update.

Prepare reviewable Jev batches with `jg batches --repo PATH --candidates candidates.json --out NEW_PRIVATE_DIR --batch-size 32`. This creates `batches.json` and separate candidate/preview files for each batch without making API calls. Identical and ancestry-contained tips are recorded as factual comparisons and excluded from this semantic queue. Review each batch preview before executing `relate` with that batch's digest and explicit request/byte limits. To prepare subsequent work, repeat `--previous-relations PATH` for existing checkpoint ledgers; both successful and uncertain attempts are excluded. A new nonempty batch directory is never overwritten.

For exploratory relationships from an already recorded incomplete snapshot, explicitly use `candidates --allow-incomplete`. The resulting coverage retains `inventory_complete: false`; the preservation-plan command still rejects the snapshot. This mode compares recorded commit tips and does not claim that current refs or worktrees are complete.

Candidate selection favors the strongest discovered connection for uncovered branches before filling the remaining global rank. `coverage.branches` accounts for every recorded branch, including branches with no discovered candidate or pairs omitted by the limit. This is discovery coverage, not proof that no other relationship exists.

Live `relate` runs checkpoint each request in the private output directory. Reusing that directory with the same approved preview resumes remaining requests and preserves completed responses. An interrupted, invalid, or failed attempt remains `uncertain` and is not resent automatically, because the API may already have processed it. Successful attempts record model, HTTP status, timestamps, latency, and token usage; aggregate statistics report cost as unavailable unless a versioned pricing basis is supplied. A different preview requires a different output directory. Request limits still apply to the full preview. The checkpoint lock prevents concurrent CLI writers to the same output directory.

`jg resume --repo PATH --batch-plan BATCH_DIR/batches.json --approved-plan-sha256 SHA256 --max-jev-requests 32 --max-jev-payload-bytes BYTES --max-total-requests COUNT --use-jev` checks every preview and the total remaining budget before the first live request. It uses the existing per-request checkpoints, skips completed or uncertain attempts, and stops on the first failure. A plan digest is approval for the exact collection of previews; inspect the payload fields, destination, and byte limits before running it. Afterward, run `jg collect` into a new directory.

`jg decisions --repo PATH --inventory INVENTORY --candidates CANDIDATES --relations RELATIONS --out PRIVATE_DIR` writes `decisions.json` for every recorded branch. It retains the default and checked-out branches, marks fully merged branches without unique commits as cleanup candidates only when inventory is complete, and holds other work. Jev labels are attached as relationship signals; they cannot authorize removal. An incomplete inventory blocks cleanup candidacy.

Open `docs/index.html` from a local static server, then choose `inventory.json`, `candidates.json`, and `relations.json` from the same run. An optional `review.json` can also be loaded. The viewer reads files selected by the browser only: it does not upload them, fetch a repository, or call Jev. Human dispositions remain browser-local until **Export review.json** downloads a private ledger; pass that file back to `jg plan --review PATH`. It keeps incomplete inventories and candidate pairs without a loaded judgment visibly unresolved.

The viewer has separate **Connected components**, **Candidate relationships**, and **Jev judgments** views. Each view has record pagination, and the graph has its own page controls; a graph page is a presentation slice, not a data limit. The coverage strip reports loaded candidates, judged records, and the authoritative pending-request count from a batch aggregate when available. Candidate discovery before the configured candidate limit is shown separately.

```bash
cd docs
python3 -m http.server 8765 --bind 127.0.0.1
```

Visit `http://127.0.0.1:8765` and use the artifact selectors. Port 8765 avoids the workspace's existing Surface UI service on port 8000. Inventory is useful on its own; candidate, relation, and review files can be added later. The grouped canvas overview and the virtualized explorer remain bounded when a run has thousands of objects. The focused inspector joins branch endpoints by immutable tip SHA and joins Jev responses by candidate ID.

The v3 question contract asks independent, criteria-backed judgments for evidence sufficiency, same intent, partial overlap, both dependency directions, and both supersession directions. These probabilities are review signals, never cleanup permission. The implementation plan and calibration gate are recorded in [docs/2026-09-21-jev-quality-review-ledger-plan.md](docs/2026-09-21-jev-quality-review-ledger-plan.md).

Before a wider v3 run, create a small owner-labeled `relationship-labels` artifact and score it locally. This command performs no network access:

```bash
PYTHONPATH=src python3 -m jev_git_graph calibrate \
  --repo /path/to/repo \
  --labels /private/artifacts/labels.json \
  --relations /private/artifacts/relations.json \
  --out /private/artifacts/calibration
```

For the browser-independent normalization checks, run:

```bash
node --test tests/test_viewer_data.mjs
```

## Design principles

- Preserve every unique commit, uncommitted change, and stash until its disposition is explicit.
- Show evidence and uncertainty for each proposed relationship.
- Keep an authoritative Git inventory separate from model judgments.
- Keep repository content local by default; disclose the exact payload before any optional Jev request.
- Make a clean canonical checkout and an accounted-for WIP inventory the measurable outcome.

## Prior art

- [s1s](https://github.com/cpaczek/s1s): local code index and reference graph with typed Jev judgments over selected evidence.
- [neo4jev](https://github.com/jexp/neo4jev): navigates supplied graph relationships with Jev choices and bounded search.
- [TypeSafe agent skills](https://github.com/typesafe-ai/skills): official guidance for typed System One questions.

These are design references, not dependencies or code incorporated into this repository.

## License and contributions

Copyright 2026 CondorCommodore. This project's code and documentation are licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE) for attribution.

Comments, suggestions, and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution terms.
