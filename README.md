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

`relate` writes only a local preview by default. A live Jev request requires an approved preview digest, `--use-jev`, and `TYPESAFE_API_KEY` in the process environment. It is hard-capped by default at **one request and 8,192 payload bytes**; raising either cap requires an explicit command-line override after reviewing the preview. There are no OpenAI, Codex, Claude, or agent-loop calls in this project. Read the preview before approving it.

Start with the [project specification](docs/spec.md), including the problem statement, five whys, requirements, and acceptance criteria.

Explore the [interactive visual concept](https://condorcommodore.github.io/jev-git-graph/). It walks through a synthetic repository from Git facts to candidate links, illustrative Jev judgments, and a human review plan. The demo makes no API calls.

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
