import assert from "node:assert/strict";
import test from "node:test";
import viewer from "../docs/viewer-data.js";

const inventory = {
  repository: { id: "repo-local-id" },
  collection: { complete: false },
  branches: [
    { name: "feature/alpha", tip: "a".repeat(40), merge_base: "m".repeat(40), unique_commits: ["c1"], changed_paths: ["src/a.py"], merged_into_default: false },
    { name: "fix/beta", tip: "b".repeat(40), merge_base: "m".repeat(40), unique_commits: [], changed_paths: [], merged_into_default: false },
  ],
  worktrees: [{ branch: "feature/alpha", head: "a".repeat(40), path_id: "opaque-worktree", status: "dirty", locked: false }],
  stashes: [{ reference: "stash@{0}", sha: "s".repeat(40), subject: "WIP" }],
  facts: [{ type: "POINTS_TO", branch: "feature/alpha", commit: "a".repeat(40) }],
};

const candidates = {
  repository_id: "repo-local-id",
  candidates: [{
    id: "pair-1",
    endpoints: { a: { branch: "feature/alpha", tip: "a".repeat(40) }, b: { branch: "fix/beta", tip: "b".repeat(40) } },
    reasons: ["shared-subject-token"],
    evidence: { shared_paths: ["src/a.py"], shared_patch_ids: [], shared_subject_tokens: ["alpha"] },
  }],
  coverage: { strategy: "incomplete first pass" },
};

const relations = {
  relations: [{
    candidate_id: "pair-1",
    response: { answers: { relationship: { choice: "PARTIAL_OVERLAP", confidence: 0.82, probabilities: { PARTIAL_OVERLAP: 0.82, UNKNOWN: 0.18 } }, same_intent: { noul: 0.79 } } },
  }],
};

test("normalizes local artifacts by immutable tip SHA and candidate ID", () => {
  const model = viewer.normalize({ inventory, candidates, relations });
  assert.deepEqual(model.errors, []);
  assert.equal(model.counts.objects, 4);
  assert.equal(model.counts.candidates, 1);
  assert.equal(model.counts.evaluated, 1);
  assert.equal(model.counts.unresolved, 0);
  assert.equal(model.candidates[0].aNode.title, "feature/alpha");
  assert.equal(model.candidates[0].bNode.title, "fix/beta");
  assert.equal(model.candidates[0].answer.choice, "PARTIAL_OVERLAP");
  assert.equal(model.candidates[0].answer.confidence, 0.82);
  assert.equal(model.candidates[0].answer.isV3, false);
  assert.equal(model.candidates[0].likelyDuplicate, true);
  assert.ok(model.warnings.some((message) => message.includes("incomplete")));
});

test("does not create a relationship when endpoint SHA is absent", () => {
  const wrongTip = structuredClone(candidates);
  wrongTip.candidates[0].endpoints.a.tip = "z".repeat(40);
  const model = viewer.normalize({ inventory, candidates: wrongTip, relations });
  assert.equal(model.candidates[0].aNode, null);
  assert.equal(model.candidates[0].relation, null);
  assert.ok(model.warnings.some((message) => message.includes("endpoint SHA")));
});

test("duplicate judgments are quarantined", () => {
  const duplicates = { relations: [relations.relations[0], relations.relations[0]] };
  const model = viewer.normalize({ inventory, candidates, relations: duplicates });
  assert.equal(model.counts.evaluated, 0);
  assert.equal(model.counts.unresolved, 1);
});

test("keeps a partial Jev batch unresolved rather than unrelated", () => {
  const model = viewer.normalize({ inventory, candidates });
  assert.equal(model.counts.candidates, 1);
  assert.equal(model.counts.evaluated, 0);
  assert.equal(model.counts.unresolved, 1);
  assert.ok(model.warnings.some((message) => message.includes("remain unresolved")));
});

test("reports mismatched artifacts and malformed documents", () => {
  const wrongRepo = structuredClone(candidates);
  wrongRepo.repository_id = "different-repository";
  assert.ok(viewer.normalize({ inventory, candidates: wrongRepo }).errors.some((message) => message.includes("different repository")));
  assert.deepEqual(viewer.validate("relations", { relations: "not-an-array" }), ["relations.json is missing relations[]."]);
});

test("identical tips preserve branch identity and connect across naming prefixes", () => {
  const snapshot = structuredClone(inventory);
  snapshot.observed_at = "2026-09-20T00:00:00Z";
  snapshot.branches[0].committed_at = "2026-07-01T00:00:00Z";
  snapshot.branches.push({ ...snapshot.branches[0], name: "archive/same-tip" });
  snapshot.worktrees[0].status = [];
  const model = viewer.normalize({ inventory: snapshot, candidates, relations });
  const original = model.objects.find((item) => item.title === "feature/alpha");
  const duplicate = model.objects.find((item) => item.title === "archive/same-tip");
  assert.equal(model.candidates[0].aNode, original);
  assert.equal(original.identicalTips[0], duplicate);
  assert.equal(original.group, duplicate.group);
  assert.equal(original.inactiveWithWork, true);
  assert.equal(model.objects.find((item) => item.kind === "worktree").subtitle, "clean");
});

test("inconclusive responses count as evaluated but remain unresolved", () => {
  const unknown = structuredClone(relations);
  unknown.relations[0].response.answers.relationship.choice = "INSUFFICIENT_EVIDENCE";
  const model = viewer.normalize({ inventory, candidates, relations: unknown });
  assert.equal(model.counts.evaluated, 1);
  assert.equal(model.counts.unresolved, 1);
  assert.equal(model.candidates[0].resolved, false);
});

test("ancestry resolves structural coverage without pretending Jev evaluated it", () => {
  const factual = structuredClone(candidates);
  factual.candidates[0].evidence.a_ancestor_of_b = true;
  const model = viewer.normalize({ inventory, candidates: factual });
  assert.equal(model.counts.evaluated, 0);
  assert.equal(model.counts.factResolved, 1);
  assert.equal(model.counts.unresolved, 0);
  assert.equal(model.candidates[0].resolutionKind, "git_fact");
});

test("v3 judgments require sufficient evidence and preserve independent dimensions", () => {
  const v3 = {
    repository_id: "repo-local-id",
    relations: [{
      candidate_id: "pair-1", judgment_id: "judgment-v3", question_version: "branch-relationship-v3",
      response: { answers: {
        evidence_sufficient: { noul: 0.91 }, same_intent: { noul: 0.88 }, partial_overlap: { noul: 0.2 },
        a_depends_on_b: { noul: 0.1 }, b_depends_on_a: { noul: 0.1 },
        a_supersedes_b: { noul: 0.84 }, b_supersedes_a: { noul: 0.05 },
      } },
    }],
  };
  const model = viewer.normalize({ inventory, candidates, relations: v3 });
  assert.equal(model.counts.evaluated, 1);
  assert.equal(model.counts.unresolved, 0);
  assert.equal(model.counts.likelyDuplicates, 1);
  assert.equal(model.counts.superseded, 1);
  assert.equal(model.candidates[0].answer.dimensions.a_supersedes_b, 0.84);
  assert.equal(model.candidates[0].answer.isV3, true);
});

test("v3 judgment history supersedes v2 without duplicate quarantine", () => {
  const history = { relations: [
    { ...relations.relations[0], judgment_id: "judgment-v2", question_version: "branch-relationship-v2" },
    { candidate_id: "pair-1", judgment_id: "judgment-v3", question_version: "branch-relationship-v3",
      response: { answers: Object.fromEntries(["evidence_sufficient", "same_intent", "partial_overlap", "a_depends_on_b", "b_depends_on_a", "a_supersedes_b", "b_supersedes_a"].map((key) => [key, { noul: key === "evidence_sufficient" ? .9 : .1 }])) } },
  ] };
  const model = viewer.normalize({ inventory, candidates, relations: history });
  assert.equal(model.counts.evaluated, 1);
  assert.equal(model.candidates[0].relation.question_version, "branch-relationship-v3");
  assert.equal(model.counts.unresolved, 0);
});

test("rejects a relation artifact produced from different candidate content", () => {
  const withDigest = { ...candidates, content_digest: "candidate-digest" };
  const wrong = { ...relations, candidate_content_digest: "other-digest" };
  const model = viewer.normalize({ inventory, candidates: withDigest, relations: wrong });
  assert.ok(model.errors.some((message) => message.includes("different candidate artifact")));
});

test("uses authoritative pending request count when a batch aggregate reports it", () => {
  const aggregate = { ...relations, unattempted_requests: 2316 };
  const model = viewer.normalize({ inventory, candidates, relations: aggregate });
  assert.equal(model.counts.candidates, 1);
  assert.equal(model.counts.pending, 2316);
});
