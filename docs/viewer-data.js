"use strict";

// Pure, browser-local normalization for the static viewer. No fetch, storage,
// telemetry, or network calls belong in this module.
(function exposeViewerData(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.JevGraphData = api;
})(globalThis, function viewerDataFactory() {
  function array(value) { return Array.isArray(value) ? value : []; }
  function object(value) { return value && typeof value === "object" && !Array.isArray(value) ? value : {}; }
  function text(value) { return typeof value === "string" ? value : ""; }
  function shortSha(value) { return text(value).slice(0, 10) || "unknown"; }
  function unique(items) { return [...new Set(items)]; }

  function validate(kind, document) {
    const value = object(document);
    if (!Object.keys(value).length) return [`${kind}.json must contain a JSON object.`];
    if (kind === "inventory") {
      const errors = [];
      if (!Array.isArray(value.branches)) errors.push("inventory.json is missing branches[].");
      if (!Array.isArray(value.worktrees)) errors.push("inventory.json is missing worktrees[].");
      if (!Array.isArray(value.stashes)) errors.push("inventory.json is missing stashes[].");
      return errors;
    }
    if (kind === "candidates" && !Array.isArray(value.candidates)) return ["candidates.json is missing candidates[]."];
    if (kind === "relations" && !Array.isArray(value.relations)) return ["relations.json is missing relations[]."];
    if (kind === "review") {
      if (value.kind !== "relationship-review" || value.schema_version !== 1 || !Array.isArray(value.decisions)) return ["review.json has an unsupported schema or is missing decisions[]."];
      if (value.decisions.some((decision) => !decision || typeof decision.object_id !== "string" || typeof decision.fingerprint !== "string" || typeof decision.rationale !== "string" || typeof decision.reviewed_at !== "string")) return ["review.json contains an invalid decision."];
    }
    return [];
  }

  function namespace(name) {
    const first = text(name).split("/")[0];
    return first || "(root)";
  }

  function answerSummary(relation) {
    const response = object(relation.response);
    const answers = object(response.answers);
    const relationship = object(answers.relationship);
    const sameIntent = object(answers.same_intent);
    const dimensions = Object.fromEntries(["evidence_sufficient", "same_intent", "partial_overlap", "a_depends_on_b", "b_depends_on_a", "a_supersedes_b", "b_supersedes_a"].map((key) => [key, typeof object(answers[key]).noul === "number" ? object(answers[key]).noul : null]));
    const v3 = dimensions.evidence_sufficient !== null;
    const strongest = Object.entries(dimensions).filter(([key, value]) => key !== "evidence_sufficient" && typeof value === "number").sort((a, b) => b[1] - a[1])[0];
    const choice = text(relationship.choice) || (v3 && strongest && strongest[1] >= .75 ? strongest[0].toUpperCase() : v3 ? "MULTIDIMENSIONAL" : "");
    const confidence = typeof relationship.confidence === "number" ? relationship.confidence : null;
    const sameIntentValue = typeof sameIntent.noul === "number" ? sameIntent.noul : text(sameIntent.noul);
    const parts = [];
    if (choice) parts.push(choice);
    if (typeof sameIntentValue === "number") parts.push(`same intent: ${Math.round(sameIntentValue * 100)}%`);
    else if (sameIntentValue) parts.push(`same intent: ${sameIntentValue}`);
    return { choice, confidence, sameIntent: sameIntentValue, probabilities: object(relationship.probabilities), dimensions, evidenceSufficient: dimensions.evidence_sufficient, isV3: v3, label: parts.join(" · ") || "Response recorded" };
  }

  function normalize(artifacts) {
    const inventory = artifacts.inventory || null;
    const candidatesDocument = artifacts.candidates || null;
    const relationsDocument = artifacts.relations || null;
    const errors = [];
    const warnings = [];
    for (const [kind, document] of [["inventory", inventory], ["candidates", candidatesDocument], ["relations", relationsDocument]]) {
      if (document) errors.push(...validate(kind, document));
    }
    if (errors.length) return emptyModel(errors, warnings);

    const branchByTip = new Map();
    const branchByIdentity = new Map();
    const objects = [];
    const inventoryObject = object(inventory);
    for (const branch of array(inventoryObject.branches)) {
      const item = object(branch);
      const name = text(item.name) || "(unnamed branch)";
      const tip = text(item.tip);
      const node = { id: `branch:${tip}:${name}`, kind: "branch", title: name, subtitle: `${array(item.unique_commits).length} unique commits · ${shortSha(tip)}`, searchable: [name, tip, item.subject, array(item.changed_paths).join(" ")].join(" ").toLowerCase(), branch: item, group: namespace(name) };
      objects.push(node);
      branchByIdentity.set(`${tip}:${name}`, node);
      if (tip) {
        if (!branchByTip.has(tip)) branchByTip.set(tip, []);
        branchByTip.get(tip).push(node);
      }
    }
    for (const worktree of array(inventoryObject.worktrees)) {
      const item = object(worktree);
      const title = text(item.branch) || "Detached worktree";
      const status = Array.isArray(item.status) ? (item.status.length ? `dirty · ${item.status.length} entries` : "clean") : "status unavailable";
      objects.push({ id: `worktree:${text(item.path_id) || title}`, kind: "worktree", title, subtitle: status, searchable: [title, item.head, item.path_id, status].join(" ").toLowerCase(), worktree: item, group: "Worktrees" });
    }
    for (const stash of array(inventoryObject.stashes)) {
      const item = object(stash);
      const title = text(item.reference) || "Stash";
      objects.push({ id: `stash:${title}:${text(item.sha)}`, kind: "stash", title, subtitle: shortSha(item.sha), searchable: [title, item.sha, item.subject].join(" ").toLowerCase(), stash: item, group: "Stashes" });
    }

    const relationByCandidate = new Map();
    const judgmentIds = new Set();
    const conflictingRelations = new Set();
    for (const relation of array(object(relationsDocument).relations)) {
      const item = object(relation);
      const candidateId = text(item.candidate_id);
      if (!candidateId) { warnings.push("A relation without candidate_id cannot be joined and is not shown as a judgment."); continue; }
      const judgmentId = text(item.judgment_id);
      if ((judgmentId && judgmentIds.has(judgmentId)) || (!judgmentId && relationByCandidate.has(candidateId))) {
        conflictingRelations.add(candidateId);
        warnings.push("Duplicate judgments were quarantined; their pairs remain unresolved.");
      }
      if (judgmentId) judgmentIds.add(judgmentId);
      const existing = relationByCandidate.get(candidateId);
      if (!existing || text(item.question_version).localeCompare(text(existing.question_version)) >= 0) relationByCandidate.set(candidateId, item);
    }

    const candidates = [];
    for (const candidate of array(object(candidatesDocument).candidates)) {
      const item = object(candidate);
      const id = text(item.id);
      if (!id) { warnings.push("A candidate without id cannot be joined to a relation."); continue; }
      const endpoints = object(item.endpoints);
      const a = object(endpoints.a);
      const b = object(endpoints.b);
      const aTip = text(a.tip);
      const bTip = text(b.tip);
      const aNode = branchByIdentity.get(`${aTip}:${text(a.branch)}`) || (branchByTip.get(aTip)?.length === 1 ? branchByTip.get(aTip)[0] : null);
      const bNode = branchByIdentity.get(`${bTip}:${text(b.branch)}`) || (branchByTip.get(bTip)?.length === 1 ? branchByTip.get(bTip)[0] : null);
      if (!aNode || !bNode) warnings.push(`Candidate ${id} has endpoint SHA(s) not found in the loaded inventory.`);
      const relation = aNode && bNode && !conflictingRelations.has(id) ? relationByCandidate.get(id) || null : null;
      const factResolved = Boolean(item.evidence?.identical_tips || item.evidence?.a_ancestor_of_b || item.evidence?.b_ancestor_of_a);
      const answer = relation ? answerSummary(relation) : null;
      const sufficient = answer?.evidenceSufficient === null || answer?.evidenceSufficient === undefined || answer.evidenceSufficient >= .75;
      const likelyDuplicate = Boolean(sufficient && typeof answer?.sameIntent === "number" && answer.sameIntent >= 0.75 && !["UNRELATED", "INSUFFICIENT_EVIDENCE"].includes(answer.choice));
      const superseded = Boolean(sufficient && (["A_SUPERSEDES_B", "B_SUPERSEDES_A"].includes(answer?.choice) || answer?.dimensions?.a_supersedes_b >= .75 || answer?.dimensions?.b_supersedes_a >= .75));
      candidates.push({ id: `candidate:${id}`, candidateId: id, kind: "candidate", title: `${text(a.branch) || shortSha(aTip)} ↔ ${text(b.branch) || shortSha(bTip)}`, subtitle: relation ? answer.label : factResolved ? "Git fact · ancestry or identical tips" : "Candidate only · no Jev judgment loaded", searchable: [id, a.branch, a.tip, b.branch, b.tip, array(item.reasons).join(" ")].join(" ").toLowerCase(), candidate: item, aNode, bNode, relation, answer, factResolved, likelyDuplicate, superseded, group: "Candidate pairs" });
    }
    const candidateIds = new Set(candidates.map((candidate) => candidate.candidateId));
    for (const candidateId of relationByCandidate.keys()) {
      if (!candidateIds.has(candidateId)) warnings.push("A judgment has no matching candidate in this load and was not attached.");
    }

    const inventoryId = text(object(inventoryObject.repository).id);
    const candidateRepositoryId = text(object(candidatesDocument).repository_id);
    if (inventoryId && candidateRepositoryId && inventoryId !== candidateRepositoryId) return emptyModel(["candidates.json belongs to a different repository than inventory.json."], []);
    const relationRepositoryId = text(object(relationsDocument).repository_id);
    if (candidateRepositoryId && relationRepositoryId && candidateRepositoryId !== relationRepositoryId) return emptyModel(["relations.json belongs to a different repository than candidates.json."], []);
    const candidateContentDigest = text(object(candidatesDocument).content_digest);
    const relationCandidateDigest = text(object(relationsDocument).candidate_content_digest);
    if (candidateContentDigest && relationCandidateDigest && candidateContentDigest !== relationCandidateDigest) return emptyModel(["relations.json was produced from a different candidate artifact."], []);
    const collection = object(inventoryObject.collection);
    if (inventory && collection.complete !== true) warnings.unshift("This inventory is incomplete. It is shown for investigation only; it cannot establish a complete repository state.");
    if (candidatesDocument && !relationsDocument) warnings.push("No relations.json is loaded. Every candidate remains unresolved, not unrelated.");
    const evaluatedCandidates = candidates.filter((candidate) => candidate.relation).length;
    if (candidates.length > evaluatedCandidates) warnings.push(`${candidates.length - evaluatedCandidates} candidate pair(s) have no loaded Jev judgment. They remain unresolved.`);
    const coverage = object(object(candidatesDocument).coverage);
    const coverageByBranch = new Map(array(coverage.branches).map((entry) => [`${entry.tip}:${entry.branch}`, entry]));
    if (coverage.truncated === true || text(coverage.strategy).includes("incomplete")) warnings.push("Candidate coverage is partial. Omitted pairs are not evidence of independence.");

    const branches = objects.filter((item) => item.kind === "branch");
    const parents = new Map(branches.map((item) => [item.id, item.id]));
    function root(id) {
      let current = id;
      while (parents.get(current) !== current) current = parents.get(current);
      while (parents.get(id) !== id) { const next = parents.get(id); parents.set(id, current); id = next; }
      return current;
    }
    function join(a, b) { parents.set(root(a.id), root(b.id)); }
    const observed = Date.parse(inventoryObject.observed_at);
    for (const branch of branches) {
      const equivalents = branchByTip.get(branch.branch.tip) || [];
      branch.identicalTips = equivalents.filter((item) => item !== branch);
      for (const other of equivalents.slice(1)) join(equivalents[0], other);
      const committed = Date.parse(branch.branch.committed_at);
      branch.ageDays = Number.isFinite(observed) && Number.isFinite(committed) ? Math.max(0, Math.floor((observed - committed) / 86400000)) : null;
      branch.inactiveWithWork = branch.ageDays !== null && branch.ageDays >= 30 && array(branch.branch.unique_commits).length > 0;
      branch.relatedPairs = [];
      branch.coverage = coverageByBranch.get(`${branch.branch.tip}:${branch.title}`) || null;
    }
    for (const candidate of candidates) {
      const dimensionValues = Object.entries(candidate.answer?.dimensions || {}).filter(([key, value]) => key !== "evidence_sufficient" && typeof value === "number").map(([, value]) => value);
      const v3Resolved = candidate.answer?.evidenceSufficient >= .75 && (dimensionValues.some((value) => value >= .75) || (dimensionValues.length && dimensionValues.every((value) => value <= .25)));
      const jevResolved = Boolean(v3Resolved || (candidate.answer?.evidenceSufficient == null && candidate.answer?.choice && !["UNKNOWN", "INSUFFICIENT_EVIDENCE"].includes(candidate.answer.choice)));
      candidate.resolved = Boolean(candidate.aNode && candidate.bNode && (candidate.factResolved || jevResolved));
      candidate.resolutionKind = candidate.factResolved ? "git_fact" : jevResolved ? "jev" : "unresolved";
      if (candidate.aNode) candidate.aNode.relatedPairs.push(candidate);
      if (candidate.bNode && candidate.bNode !== candidate.aNode) candidate.bNode.relatedPairs.push(candidate);
      if (candidate.aNode && candidate.bNode && (candidate.factResolved || (jevResolved && candidate.answer.choice !== "UNRELATED"))) join(candidate.aNode, candidate.bNode);
    }
    const components = new Map();
    for (const branch of branches) {
      const id = root(branch.id);
      if (!components.has(id)) components.set(id, []);
      components.get(id).push(branch);
    }
    let componentNumber = 0;
    const groups = [];
    for (const members of components.values()) {
      const name = members.length > 1 ? `Connected group ${++componentNumber}` : "No loaded connection";
      for (const member of members) member.group = name;
      const existing = groups.find((group) => group.name === name);
      if (existing) existing.count += members.length;
      else groups.push({ name, count: members.length });
    }
    groups.sort((a, b) => b.count - a.count);
    const unresolved = candidates.filter((candidate) => !candidate.resolved).length;
    const factResolved = candidates.filter((candidate) => candidate.factResolved).length;
    const branchesWithCandidates = objects.filter((item) => item.kind === "branch" && item.relatedPairs.length).length;
    const branchesEvaluated = objects.filter((item) => item.kind === "branch" && item.relatedPairs.some((pair) => pair.relation)).length;
    const likelyDuplicates = candidates.filter((candidate) => candidate.likelyDuplicate).length;
    const superseded = candidates.filter((candidate) => candidate.superseded).length;
    const reportedPending = Number(object(relationsDocument).unattempted_requests);
    const pending = Number.isFinite(reportedPending) ? Math.max(0, reportedPending) : Math.max(0, candidates.length - evaluatedCandidates);
    return { errors, warnings: unique(warnings), inventory: inventoryObject, candidatesDocument: object(candidatesDocument), relationsDocument: object(relationsDocument), objects, candidates, groups, facts: array(inventoryObject.facts), counts: { branches: array(inventoryObject.branches).length, worktrees: array(inventoryObject.worktrees).length, stashes: array(inventoryObject.stashes).length, objects: objects.length, facts: array(inventoryObject.facts).length, candidates: candidates.length, candidateCountBeforeLimit: Number(object(candidatesDocument).candidate_count_before_limit) || candidates.length, evaluated: evaluatedCandidates, pending, factResolved, unresolved, branchesWithCandidates, branchesEvaluated, likelyDuplicates, superseded } };
  }

  function emptyModel(errors, warnings) {
    return { errors, warnings, inventory: {}, candidatesDocument: {}, relationsDocument: {}, objects: [], candidates: [], groups: [], facts: [], counts: { branches: 0, worktrees: 0, stashes: 0, objects: 0, facts: 0, candidates: 0, candidateCountBeforeLimit: 0, evaluated: 0, pending: 0, factResolved: 0, unresolved: 0, branchesWithCandidates: 0, branchesEvaluated: 0, likelyDuplicates: 0, superseded: 0 } };
  }
  return { answerSummary, normalize, shortSha, validate };
});
