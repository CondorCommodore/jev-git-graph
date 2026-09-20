# jev-git-graph

Find the relationships among Git branches, commits, worktrees, stashes, and pull requests so a maintainer can return a repository to a clean, understood state without losing work.

The project begins as a **read-only local CLI**. Git provides the facts and an in-memory graph. Jev answers small, typed questions about relationships that Git ancestry alone cannot establish. A maintainer reviews the resulting preservation and cleanup plan. The first version does not execute that plan.

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
