"use strict";

// This viewer deliberately has no fetch(), XMLHttpRequest, WebSocket, storage,
// or network client. JSON is read only from files chosen by the operator.
(() => {
  const data = window.JevGraphData;
  const state = { artifacts: {}, reviews: new Map(), fileErrors: [], model: data.normalize({}), filter: "all", viewMode: "components", query: "", group: null, selected: null, graphTargets: [], page: 0, graphPage: 0, pageSize: 100, graphPageSize: 24 };
  const $ = (selector) => document.querySelector(selector);
  const elements = {
    notices: $("#notices"), loadStatus: $("#load-status"), coverageCount: $("#coverage-count"), coverageDetail: $("#coverage-detail"), search: $("#search"), virtualList: $("#virtual-list"), visibleCount: $("#visible-count"), pagePrev: $("#page-prev"), pageNext: $("#page-next"), pageStatus: $("#page-status"), graph: $("#graph"), graphWrap: $("#graph-wrap"), graphEmpty: $("#graph-empty"), graphHint: $("#graph-hint"), graphCount: $("#graph-count"), graphPrev: $("#graph-prev"), graphNext: $("#graph-next"), graphPageStatus: $("#graph-page-status"), inspector: $("#inspector-content"), focusIndex: $("#focus-index"), overview: $("#overview-button"),
  };
  const summary = [
    ["object-count", "object-detail", "objects", (counts) => `${counts.branches} branches · ${counts.worktrees} worktrees · ${counts.stashes} stashes`],
    ["fact-count", "fact-detail", "facts", () => "Observed Git relationships"],
    ["candidate-count", "candidate-detail", "candidates", (counts) => `${counts.branchesWithCandidates.toLocaleString()} branches represented`],
    ["evaluated-count", "evaluated-detail", "evaluated", (counts) => `${counts.branchesEvaluated.toLocaleString()} branches touched`],
    ["unresolved-count", "unresolved-detail", "unresolved", (counts) => `${counts.likelyDuplicates.toLocaleString()} likely dupes · ${counts.superseded.toLocaleString()} superseded`],
  ];

  function plural(count, word) {
    const forms = { branch: "branches", stash: "stashes" };
    return `${count.toLocaleString()} ${count === 1 ? word : (forms[word] || `${word}s`)}`;
  }
  function fragment(...children) { const value = document.createDocumentFragment(); children.flat().filter(Boolean).forEach((child) => value.append(child)); return value; }
  function node(name, className, value) { const element = document.createElement(name); if (className) element.className = className; if (value !== undefined) element.textContent = value; return element; }
  function detail(label, value) { const row = node("div", "detail-row"); row.append(node("span", "detail-label", label), node("code", "detail-value", value)); return row; }
  function list(values, className = "evidence-list") { const ul = node("ul", className); values.filter(Boolean).forEach((value) => ul.append(node("li", "", value))); return ul; }
  function percentage(value) { if (typeof value !== "number" || !Number.isFinite(value)) return "not reported"; const normalized = value <= 1 ? value * 100 : value; return `${Math.round(normalized)}%`; }
  function truncate(value, length = 28) { const text = String(value || ""); return text.length > length ? `${text.slice(0, length - 1)}…` : text; }

  async function readFile(kind, input) {
    const file = input.files && input.files[0];
    if (!file) return;
    state.fileErrors = state.fileErrors.filter((error) => !error.startsWith(`${kind}.json:`));
    try {
      const document = JSON.parse(await file.text());
      const problems = data.validate(kind, document);
      if (problems.length) throw new Error(problems.join(" "));
      if (kind === "review" && state.artifacts.inventory?.repository?.id && document.repository_id !== state.artifacts.inventory.repository.id) throw new Error("review repository mismatch");
      state.artifacts[kind] = document;
      if (kind === "review") state.reviews = new Map(document.decisions.map((decision) => [decision.object_id, decision]));
      $(`#${kind}-file-name`).textContent = file.name;
    } catch (error) {
      state.fileErrors.push(`${kind}.json: ${error instanceof SyntaxError ? "Invalid JSON; previous loaded file retained." : "Could not load this artifact; check its schema and file access."}`);
      input.value = "";
    }
    rebuild();
  }

  function rebuild() {
    state.model = data.normalize(state.artifacts);
    state.selected = state.selected ? [...state.model.objects, ...state.model.candidates].find((item) => item.id === state.selected.id) || null : null;
    if (!state.model.objects.length && state.filter !== "candidate" && state.filter !== "unresolved") state.selected = null;
    renderAll();
  }

  function filteredItems() {
    const term = state.query.trim().toLowerCase();
    const items = state.viewMode === "candidates" ? state.model.candidates : state.viewMode === "judgments" ? state.model.candidates.filter((item) => item.relation) : state.model.objects;
    return items.filter((item) => {
      const kindMatch = state.filter === "all" ||
        (state.filter === "inactive" ? item.inactiveWithWork :
        state.filter === "integrated" ? item.branch?.merged_into_default === true :
        state.filter === "unique" ? item.kind === "branch" && (item.branch?.unique_commits || []).length > 0 :
        state.filter === "likely-duplicate" ? item.likelyDuplicate :
        state.filter === "superseded" ? item.superseded :
        state.filter === "reviewed" ? reviewStatus(item) === "current" :
        state.filter === "stale-review" ? reviewStatus(item) === "stale" :
        state.filter === "unresolved" ? item.kind === "candidate" && !item.resolved : item.kind === state.filter);
      const groupMatch = !state.group || item.groupKey === state.group;
      const textMatch = !term || item.searchable.includes(term) || item.title.toLowerCase().includes(term);
      return kindMatch && groupMatch && textMatch;
    }).sort((a, b) => a.kind.localeCompare(b.kind) || a.title.localeCompare(b.title));
  }

  function renderAll() {
    renderStatus(); renderNotices(); renderSummary(); renderCoverage(); renderList(true); renderInspector(); drawGraph();
  }

  function renderStatus() {
    const loaded = ["inventory", "candidates", "relations", "review"].filter((kind) => state.artifacts[kind]);
    const missing = ["inventory", "candidates", "relations", "review"].filter((kind) => !state.artifacts[kind]);
    elements.loadStatus.textContent = loaded.length ? `Loaded ${loaded.join(", ")}. ${missing.length ? `Still optional or missing: ${missing.join(", ")}.` : "All three artifact files are loaded locally."}` : "Nothing has been loaded. Choose inventory.json first; candidates.json and relations.json are optional and may be loaded afterward.";
    const attempts = state.model.relationsDocument.attempts;
    if (Array.isArray(attempts)) {
      elements.loadStatus.textContent += ` Jev attempts: ${attempts.length}; succeeded: ${attempts.filter((item) => item.status === "succeeded").length}; uncertain: ${attempts.filter((item) => item.status === "uncertain").length}.`;
    }
    const reviewCounts = [...state.model.objects].reduce((counts, item) => {
      const status = reviewStatus(item); if (status === "current") counts.current += 1; else if (status === "stale") counts.stale += 1; return counts;
    }, { current: 0, stale: 0 });
    if (state.reviews.size) elements.loadStatus.textContent += ` Human reviews: ${reviewCounts.current} current; ${reviewCounts.stale} stale; ${state.reviews.size - reviewCounts.current - reviewCounts.stale} unmatched.`;
  }

  function renderNotices() {
    elements.notices.replaceChildren();
    const notices = [
      ...state.fileErrors.map((message) => ({ kind: "error", message })),
      ...state.model.errors.map((message) => ({ kind: "error", message })),
      ...state.model.warnings.map((message) => ({ kind: "warning", message })),
    ];
    notices.forEach(({ kind, message }) => {
      const item = node("div", `notice ${kind}`); item.append(node("strong", "", kind === "error" ? "Cannot safely join these files" : "Limitation"), node("span", "", message)); elements.notices.append(item);
    });
  }

  function renderSummary() {
    const inventoryLoaded = Boolean(state.artifacts.inventory);
    const candidatesLoaded = Boolean(state.artifacts.candidates);
    const relationsLoaded = Boolean(state.artifacts.relations);
    summary.forEach(([numberId, detailId, key, description], index) => {
      const available = index < 2 ? inventoryLoaded : index === 2 ? candidatesLoaded : index === 3 ? relationsLoaded : candidatesLoaded;
      $(`#${numberId}`).textContent = available ? state.model.counts[key].toLocaleString() : "—";
      $(`#${detailId}`).textContent = available ? description(state.model.counts) : index < 2 ? "No inventory" : index === 2 ? "Not loaded" : index === 3 ? "Not loaded" : "No candidate set";
    });
  }

  function renderCoverage() {
    const counts = state.model.counts;
    if (!state.artifacts.candidates) {
      elements.coverageCount.textContent = "Load candidates.json to see coverage.";
      elements.coverageDetail.textContent = "Candidate records, Jev judgments, and pending work are counted separately.";
      return;
    }
    elements.coverageCount.textContent = `${counts.candidates.toLocaleString()} candidates · ${counts.evaluated.toLocaleString()} judged · ${counts.pending.toLocaleString()} pending`;
    const discovered = counts.candidateCountBeforeLimit !== counts.candidates ? ` ${counts.candidateCountBeforeLimit.toLocaleString()} discovered before the candidate limit.` : "";
    elements.coverageDetail.textContent = `Showing the loaded candidate artifact; pending is the run-reported Jev request count.${discovered} Use the three relationship views and pagination to inspect every loaded record.`;
  }

  function renderList(resetScroll) {
    const items = filteredItems();
    if (resetScroll) elements.virtualList.scrollTop = 0;
    elements.visibleCount.textContent = items.length.toLocaleString();
    const pageCount = Math.max(1, Math.ceil(items.length / state.pageSize));
    state.page = Math.min(state.page, pageCount - 1);
    const pageStart = state.page * state.pageSize;
    const pageItems = items.slice(pageStart, pageStart + state.pageSize);
    elements.pageStatus.textContent = `Page ${state.page + 1} of ${pageCount} · ${pageStart + 1}-${Math.min(items.length, pageStart + state.pageSize)} of ${items.length}`;
    elements.pagePrev.disabled = state.page === 0;
    elements.pageNext.disabled = state.page >= pageCount - 1;
    const rowHeight = 58;
    const viewport = Math.max(260, elements.virtualList.clientHeight || 440);
    const start = Math.max(0, Math.floor(elements.virtualList.scrollTop / rowHeight) - 4);
    const end = Math.min(items.length, Math.ceil((elements.virtualList.scrollTop + viewport) / rowHeight) + 5);
    const spacer = node("div", "list-spacer"); spacer.style.height = `${pageItems.length * rowHeight}px`;
    const rows = node("div", "list-rows"); rows.style.transform = `translateY(${start * rowHeight}px)`;
    for (const item of pageItems.slice(start, end)) {
      const button = node("button", `record ${item.kind}${state.selected && state.selected.id === item.id ? " selected" : ""}`);
      button.type = "button";
      button.title = item.title;
      button.append(node("span", "record-kind", item.kind === "candidate" ? (item.relation ? "JEV" : "PAIR") : item.kind.toUpperCase()), node("strong", "", truncate(item.title, 42)), node("small", "", truncate(item.subtitle, 58)));
      button.addEventListener("click", () => select(item));
      rows.append(button);
    }
    elements.virtualList.replaceChildren(spacer, rows);
  }

  function select(item) {
    state.selected = item;
    state.group = null;
    renderList(false); renderInspector(); drawGraph();
  }

  function pagedGraphItems() {
    const items = state.viewMode === "candidates" ? state.model.candidates : state.viewMode === "judgments" ? state.model.candidates.filter((item) => item.relation) : state.model.groups;
    const pageCount = Math.max(1, Math.ceil(items.length / state.graphPageSize));
    state.graphPage = Math.min(state.graphPage, pageCount - 1);
    const start = state.graphPage * state.graphPageSize;
    elements.graphPageStatus.textContent = `Page ${state.graphPage + 1} of ${pageCount}`;
    elements.graphPrev.disabled = state.graphPage === 0;
    elements.graphNext.disabled = state.graphPage >= pageCount - 1;
    return { items, page: items.slice(start, start + state.graphPageSize), start, pageCount };
  }

  function renderInspector() {
    const model = state.model;
    const selected = state.selected;
    elements.inspector.replaceChildren();
    if (!selected) {
      elements.focusIndex.textContent = model.objects.length ? "OVERVIEW" : "WAITING";
      if (!model.objects.length) {
        elements.inspector.append(node("div", "empty-focus", "Choose inventory.json to start. The viewer reads files selected from this device and does not upload them."));
        return;
      }
      elements.inspector.append(node("span", "focus-kicker fact", "LOCAL SNAPSHOT"), node("h2", "", "Explore before deciding."), node("p", "", "Groups connect branches through identical tips and candidate links. A group may include uncertain hypotheses; it is not a declaration that every member is a duplicate. Search or select a group to inspect its evidence."), node("span", "inspector-label", "IN THIS LOAD"), list([plural(model.counts.branches, "branch"), plural(model.counts.worktrees, "worktree"), plural(model.counts.stashes, "stash"), plural(model.counts.facts, "Git fact"), plural(model.counts.candidates, "candidate pair"), plural(model.counts.evaluated, "Jev judgment")]), node("div", "conclusion", "A missing judgment is unresolved. It does not mean the pair is unrelated."));
      return;
    }
    elements.focusIndex.textContent = selected.kind === "candidate" ? (selected.relation ? "JEV JUDGED" : "UNRESOLVED") : selected.kind.toUpperCase();
    if (selected.kind === "candidate") renderCandidateInspector(selected); else renderObjectInspector(selected);
  }

  function renderObjectInspector(selected) {
    const type = selected.kind === "branch" ? "GIT BRANCH" : selected.kind === "worktree" ? "GIT WORKTREE" : "GIT STASH";
    elements.inspector.append(node("span", "focus-kicker fact", type), node("h2", "", selected.title), node("p", "", selected.subtitle), node("span", "inspector-label", "IMMUTABLE OR OBSERVED DETAILS"));
    if (selected.branch) {
      const branch = selected.branch;
      elements.inspector.append(fragment(detail("Tip SHA", data.shortSha(branch.tip)), detail("Merge base", data.shortSha(branch.merge_base)), detail("Unique commits", String((branch.unique_commits || []).length)), detail("Merged into default", branch.merged_into_default ? "yes" : "no"), detail("Changed paths", String((branch.changed_paths || []).length))));
      elements.inspector.append(detail("Tip age at snapshot", selected.ageDays === null ? "unknown" : `${selected.ageDays} days`));
      elements.inspector.append(detail("Identical tips", String(selected.identicalTips.length)));
      elements.inspector.append(detail("Discovery coverage", selected.coverage?.status || "not recorded"));
      const worktrees = state.model.objects.filter((item) => item.worktree?.branch === selected.title);
      elements.inspector.append(detail("Attached worktrees", String(worktrees.length)));
      for (const worktree of worktrees) elements.inspector.append(detail("Worktree state", worktree.subtitle));
      elements.inspector.append(detail("Evaluated pairs", String(selected.relatedPairs.filter((pair) => pair.relation).length)));
      elements.inspector.append(detail("Unresolved pairs", String(selected.relatedPairs.filter((pair) => !pair.resolved).length)));
      if (selected.inactiveWithWork) elements.inspector.append(node("div", "conclusion", "Tip is at least 30 days old and has commits outside its merge base with default. Review remaining work before disposition."));
      for (const other of selected.identicalTips.slice(0, 20)) {
        const button = node("button", "quiet-button", `Identical tip: ${other.title}`);
        button.addEventListener("click", () => select(other));
        elements.inspector.append(button);
      }
      for (const pair of selected.relatedPairs.slice(0, 40)) {
        const button = node("button", "quiet-button", pair.title);
        button.addEventListener("click", () => select(pair));
        elements.inspector.append(button);
      }
      const factTypes = state.model.facts.filter((fact) => fact.branch === branch.name || fact.commit === branch.tip).map((fact) => fact.type);
      elements.inspector.append(node("span", "inspector-label", "FACTUAL RELATIONSHIPS"), factTypes.length ? list([...new Set(factTypes)]) : node("p", "muted-copy", "No directly indexed fact is attached to this branch record."));
    } else if (selected.worktree) {
      const worktree = selected.worktree;
      elements.inspector.append(fragment(detail("Branch", worktree.branch || "detached"), detail("Head SHA", data.shortSha(worktree.head)), detail("Status", selected.subtitle), detail("Locked", worktree.locked ? "yes" : "no"), detail("Path identity", data.shortSha(worktree.path_id))));
    } else if (selected.stash) {
      const stash = selected.stash;
      elements.inspector.append(fragment(detail("Reference", stash.reference || "unknown"), detail("Commit SHA", data.shortSha(stash.sha)), detail("Subject", stash.subject || "not recorded")));
    }
    const related = relatedCandidates(selected).length;
    elements.inspector.append(node("div", "conclusion", related ? `${plural(related, "candidate pair")} is available for bounded graph expansion.` : "No candidate pair is loaded for this object. This is not evidence of independence."));
    renderReviewEditor(selected);
  }

  const dispositions = ["ACTIVE", "PRESERVE_IN_PR", "PRESERVE_IN_BRANCH", "PRESERVE_IN_ARCHIVE", "CLEANUP_CANDIDATE", "UNRESOLVED"];
  function reviewIdentity(item) {
    if (item.branch) return { id: `branch:${item.branch.name}`, kind: "branch", observed: { kind: "branch", name: item.branch.name, tip: item.branch.tip } };
    if (item.worktree) return { id: `worktree:${item.worktree.path_id}`, kind: "worktree", observed: { kind: "worktree", path_id: item.worktree.path_id ?? null, head: item.worktree.head ?? null, branch: item.worktree.branch ?? null, status: item.worktree.status ?? null } };
    if (item.stash) return { id: `stash:${item.stash.reference}:${item.stash.sha}`, kind: "stash", observed: { kind: "stash", reference: item.stash.reference, sha: item.stash.sha } };
    return null;
  }
  function reviewStatus(item) {
    const identity = reviewIdentity(item); if (!identity) return "none";
    const prior = state.reviews.get(identity.id); if (!prior) return "none";
    if (prior.reconciliation?.status === "stale" || prior.reconciliation?.status === "historical-limited") return "stale";
    if (prior.reconciliation?.status === "current") return "current";
    if (typeof prior.source_fingerprint === "string" && typeof prior.fingerprint === "string" && prior.source_fingerprint === prior.fingerprint) return "current";
    return stable(prior.observed) === stable(identity.observed) ? "current" : "stale";
  }
  function scalar(value) { return JSON.stringify(value).replace(/[\u007f-\uffff]/g, (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`); }
  function stable(value) {
    if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
    if (value && typeof value === "object") return `{${Object.keys(value).sort().map((key) => `${scalar(key)}:${stable(value[key])}`).join(",")}}`;
    return scalar(value);
  }
  async function sha256(value) {
    const bytes = new TextEncoder().encode(stable(value));
    const result = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(result)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }
  function renderReviewEditor(item) {
    const identity = reviewIdentity(item); if (!identity) return;
    const prior = state.reviews.get(identity.id);
    const current = prior && stable(prior.observed) === stable(identity.observed);
    const editor = node("div", "review-editor");
    editor.append(node("span", "inspector-label", "HUMAN DISPOSITION"));
    if (prior) editor.append(node("div", "conclusion", current ? `Current review · ${prior.reviewed_at}` : "Stale review · object inputs changed; save a new decision."));
    const select = document.createElement("select"); dispositions.forEach((value) => { const option = node("option", "", value); option.value = value; select.append(option); }); select.value = current ? prior.disposition : "UNRESOLVED";
    const rationale = document.createElement("textarea"); rationale.placeholder = "Required rationale"; rationale.value = current ? prior.rationale : "";
    const save = node("button", "quiet-button", "Save browser-local decision"); save.type = "button";
    save.addEventListener("click", async () => {
      if (!rationale.value.trim()) { rationale.focus(); return; }
      state.reviews.set(identity.id, { object_id: identity.id, kind: identity.kind, fingerprint: await sha256(identity.observed), observed: identity.observed, disposition: select.value, rationale: rationale.value.trim(), reviewed_at: new Date().toISOString() });
      save.textContent = "Saved locally";
    });
    editor.append(select, rationale, save); elements.inspector.append(editor);
  }

  async function exportReview() {
    const inventory = state.artifacts.inventory; if (!inventory) return;
    const documentValue = { kind: "relationship-review", schema_version: 1, repository_id: inventory.repository?.id || "", inventory_digest: await sha256(inventory), candidate_digest: state.artifacts.candidates ? await sha256(state.artifacts.candidates) : null, relations_digest: state.artifacts.relations ? await sha256(state.artifacts.relations) : null, exported_at: new Date().toISOString(), decisions: [...state.reviews.values()].sort((a, b) => a.object_id.localeCompare(b.object_id)) };
    const url = URL.createObjectURL(new Blob([JSON.stringify(documentValue, null, 2) + "\n"], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "review.json"; link.click(); URL.revokeObjectURL(url);
  }

  function renderCandidateInspector(selected) {
    const endpoints = selected.candidate.endpoints || {};
    const a = endpoints.a || {}; const b = endpoints.b || {};
    const headingKind = selected.relation ? "judgment" : selected.factResolved ? "fact" : "candidate";
    const headingText = selected.relation ? "JEV JUDGMENT" : selected.factResolved ? "GIT RELATIONSHIP" : "CANDIDATE HYPOTHESIS";
    elements.inspector.append(node("span", `focus-kicker ${headingKind}`, headingText), node("h2", "", selected.title), node("p", "", selected.relation ? "A typed Jev response is attached to this candidate ID." : selected.factResolved ? "Git proves a structural connection between these immutable tips." : "This pair has deterministic evidence but no loaded Jev response."), node("span", "inspector-label", "IMMUTABLE ENDPOINTS"), fragment(detail("A", `${a.branch || "unknown"} · ${data.shortSha(a.tip)}`), detail("B", `${b.branch || "unknown"} · ${data.shortSha(b.tip)}`), detail("Candidate ID", selected.candidateId)));
    const evidence = selected.candidate.evidence || {};
    if (typeof evidence.a_ancestor_of_b === "boolean") {
      elements.inspector.append(node("span", "inspector-label", "GIT ANCESTRY FACTS"),
        detail("A ancestor of B", evidence.a_ancestor_of_b ? "yes" : "no"),
        detail("B ancestor of A", evidence.b_ancestor_of_a ? "yes" : "no"),
        detail("A commits outside B", String(evidence.a_commits_not_in_b)),
        detail("B commits outside A", String(evidence.b_commits_not_in_a)));
    }
    elements.inspector.append(node("span", "inspector-label", "RELATIONSHIP EVIDENCE"), list([...(selected.candidate.reasons || []), `Shared paths: ${(evidence.shared_paths || []).length}`, `Shared patch IDs: ${(evidence.shared_patch_ids || []).length}`, `Shared subject tokens: ${(evidence.shared_subject_tokens || []).length}`]));
    if (!selected.relation) {
      elements.inspector.append(node("div", "conclusion", selected.factResolved ? "Git resolves the structural connection. Semantic labels such as supersession still require review." : "No Jev judgment is loaded. Keep this pair unresolved; the viewer never translates missing data into unrelated."));
      return;
    }
    const answer = selected.answer;
    const sameIntent = typeof answer.sameIntent === "number" ? percentage(answer.sameIntent) : answer.sameIntent || "not reported";
    elements.inspector.append(node("span", "inspector-label", "TYPED JEV ANSWER"), fragment(detail("Relationship", answer.choice || "not reported"), detail("Confidence", percentage(answer.confidence)), detail("Same intent", sameIntent)));
    const dimensions = answer.isV3 ? Object.entries(answer.dimensions || {}).filter(([, value]) => typeof value === "number") : [];
    if (dimensions.length) {
      const card = node("div", "answer-card"); card.append(node("small", "", "Independent v3 judgments"));
      dimensions.forEach(([label, value]) => { const row = node("div", "bar-row"); const track = node("div", "bar-track"); const fill = node("div", "bar-fill"); fill.style.width = `${value * 100}%`; track.append(fill); row.append(node("span", "", label), track, node("b", "", percentage(value))); card.append(row); });
      elements.inspector.append(card);
    }
    const probabilities = Object.entries(answer.probabilities).filter(([, value]) => typeof value === "number");
    if (probabilities.length) {
      const card = node("div", "answer-card");
      card.append(node("small", "", "Reported relationship probabilities"));
      probabilities.forEach(([label, value]) => {
        const row = node("div", "bar-row"); const track = node("div", "bar-track"); const fill = node("div", "bar-fill"); fill.style.width = `${Math.min(100, value <= 1 ? value * 100 : value)}%`; track.append(fill); row.append(node("span", "", label), track, node("b", "", percentage(value))); card.append(row);
      });
      elements.inspector.append(card);
    }
    elements.inspector.append(node("div", "conclusion", "Jev labels this pair only. It does not authorize a disposition or cleanup action."));
  }

  function relatedCandidates(item) {
    if (item.kind === "candidate") return [item];
    if (item.kind === "branch") return state.model.candidates.filter((candidate) => candidate.aNode && candidate.bNode && (candidate.aNode.id === item.id || candidate.bNode.id === item.id));
    if (item.kind === "worktree") return state.model.candidates.filter((candidate) => candidate.aNode && candidate.bNode && (candidate.aNode.title === item.title || candidate.bNode.title === item.title));
    return [];
  }

  function resizeCanvas() {
    const canvas = elements.graph; const rect = elements.graphWrap.getBoundingClientRect(); const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(rect.width * ratio)); canvas.height = Math.max(1, Math.floor(rect.height * ratio)); canvas.style.width = `${rect.width}px`; canvas.style.height = `${rect.height}px`;
    const context = canvas.getContext("2d"); context.setTransform(ratio, 0, 0, ratio, 0, 0); return { context, width: rect.width, height: rect.height };
  }

  function drawGraph() {
    const { context, width, height } = resizeCanvas(); const model = state.model; const selected = state.selected;
    context.clearRect(0, 0, width, height); state.graphTargets = [];
    elements.graphEmpty.hidden = Boolean(model.objects.length); elements.graphHint.hidden = !model.objects.length;
    if (!model.objects.length) { elements.graphCount.textContent = "No local data"; return; }
    if (!selected) { state.viewMode === "components" ? drawOverview(context, width, height) : drawRelationshipOverview(context, width, height); return; }
    drawFocus(context, width, height, selected);
  }

  function drawOverview(context, width, height) {
    const graph = pagedGraphItems(); const groups = graph.page; const shown = groups.length; const columns = Math.max(3, Math.min(6, Math.ceil(Math.sqrt(Math.max(shown, 1) * (width / Math.max(height, 1)))))); const rows = Math.ceil(shown / columns); const gapX = width / (columns + 1); const gapY = height / (rows + 1);
    context.fillStyle = "#90a3b9"; context.font = "600 12px system-ui"; context.fillText("Connected groups · facts and hypotheses", 22, 28);
    groups.forEach((group, index) => {
      const column = index % columns; const row = Math.floor(index / columns); const x = gapX * (column + 1); const y = gapY * (row + 1) + 15; const radius = Math.max(18, Math.min(54, 13 + Math.sqrt(group.count) * 2.1));
      context.beginPath(); context.fillStyle = "rgba(139,230,197,.14)"; context.strokeStyle = "#8be6c5"; context.lineWidth = 1.5; context.arc(x, y, radius, 0, Math.PI * 2); context.fill(); context.stroke();
      context.fillStyle = "#f3f5f8"; context.font = "700 11px system-ui"; context.textAlign = "center"; context.fillText(truncate(group.name, 18), x, y - 2); context.fillStyle = "#91a0b4"; context.font = "600 10px system-ui"; context.fillText(plural(group.count, "branch"), x, y + 14); context.textAlign = "start";
      state.graphTargets.push({ type: "group", group: group.key, x, y, radius });
    });
    elements.graphCount.textContent = `${plural(state.model.counts.objects, "object")} · ${plural(state.model.groups.length, "group")} · graph page ${graph.page.length} of ${graph.items.length}`;
  }

  function drawRelationshipOverview(context, width, height) {
    const graph = pagedGraphItems();
    const endpoints = new Map();
    graph.page.forEach((candidate) => { if (candidate.aNode) endpoints.set(candidate.aNode.id, candidate.aNode); if (candidate.bNode) endpoints.set(candidate.bNode.id, candidate.bNode); });
    const nodes = [...endpoints.values()];
    const center = { x: width / 2, y: height / 2 }; const radius = Math.min(width, height) * .34;
    nodes.forEach((item, index) => { const angle = Math.PI * 2 * index / Math.max(nodes.length, 1) - Math.PI / 2; item.__graphX = center.x + Math.cos(angle) * radius; item.__graphY = center.y + Math.sin(angle) * radius; });
    graph.page.forEach((candidate) => { if (!candidate.aNode || !candidate.bNode) return; drawEdge(context, { x: candidate.aNode.__graphX, y: candidate.aNode.__graphY }, { x: candidate.bNode.__graphX, y: candidate.bNode.__graphY }, candidate.relation, candidate.factResolved); });
    nodes.forEach((item) => drawGraphNode(context, item, item.__graphX, item.__graphY, item.kind));
    context.fillStyle = "#90a3b9"; context.font = "600 11px system-ui"; context.fillText(state.viewMode === "judgments" ? "Jev judgments · paginated" : "Candidate relationships · paginated", 22, 28);
    elements.graphCount.textContent = `${graph.page.length} of ${graph.items.length} ${state.viewMode === "judgments" ? "judgments" : "candidates"} on graph page`;
  }

  function drawFocus(context, width, height, selected) {
    const allCandidates = relatedCandidates(selected);
    const center = { x: width * .5, y: height * .5 };
    const focal = selected.kind === "candidate" ? null : selected;
    if (selected.kind === "candidate") {
      elements.graphPageStatus.textContent = "Single candidate"; elements.graphPrev.disabled = true; elements.graphNext.disabled = true;
      const left = selected.aNode || { title: "Endpoint A", id: "missing-a", kind: "branch" }; const right = selected.bNode || { title: "Endpoint B", id: "missing-b", kind: "branch" };
      drawEdge(context, { x: width * .25, y: center.y }, { x: width * .75, y: center.y }, selected.relation); drawGraphNode(context, left, width * .25, center.y, "branch"); drawGraphNode(context, right, width * .75, center.y, "branch");
      elements.graphCount.textContent = `${selected.relation ? "Jev judgment" : "Candidate only"} · joined by candidate ID and tip SHA`;
      return;
    }
    drawGraphNode(context, focal, center.x, center.y, focal.kind, true);
    const connections = (selected.identicalTips || []).map((other) => ({ other, fact: "Identical tip" }));
    if (selected.branch) {
      for (const item of state.model.objects) {
        if (item.worktree?.branch === selected.title && item.worktree.head === selected.branch.tip) connections.push({ other: item, fact: "Checked out at" });
        if (selected.branch.merged_into_default && item.branch?.name === state.model.inventory.repository.default_branch) connections.push({ other: item, fact: "Merged into default" });
      }
    }
    for (const candidate of allCandidates) {
      const other = candidate.aNode?.id === focal.id ? candidate.bNode : candidate.aNode;
      if (other) connections.push({ other, candidate });
    }
    const graphCount = Math.max(1, Math.ceil(connections.length / state.graphPageSize));
    state.graphPage = Math.min(state.graphPage, graphCount - 1);
    elements.graphPageStatus.textContent = `Page ${state.graphPage + 1} of ${graphCount}`;
    elements.graphPrev.disabled = state.graphPage === 0; elements.graphNext.disabled = state.graphPage >= graphCount - 1;
    const graphStart = state.graphPage * state.graphPageSize;
    const visible = connections.slice(graphStart, graphStart + state.graphPageSize);
    const radius = Math.min(width, height) * .34;
    visible.forEach(({ other, fact, candidate }, index) => {
      const angle = (Math.PI * 2 * index / Math.max(visible.length, 1)) - Math.PI / 2;
      const x = center.x + Math.cos(angle) * radius; const y = center.y + Math.sin(angle) * radius;
      drawEdge(context, center, { x, y }, candidate?.relation, Boolean(fact || candidate?.factResolved));
      drawGraphNode(context, other, x, y, other.kind);
      if (candidate) state.graphTargets.push({ type: "candidate", item: candidate, x: (center.x + x) / 2, y: (center.y + y) / 2, radius: 16 });
    });
    context.fillStyle = "#90a3b9"; context.font = "600 11px system-ui";
    context.fillText(`Showing ${visible.length} of ${connections.length} recorded connections`, 20, 28);
    elements.graphCount.textContent = `${plural(visible.length, "connection")} · graph page ${state.graphPage + 1} · ${connections.length} total`;
  }

  function drawEdge(context, from, to, relation, fact = false) {
    context.save(); context.beginPath(); context.moveTo(from.x, from.y); context.lineTo(to.x, to.y); context.lineWidth = relation ? 3 : 2; context.strokeStyle = fact ? "#8aa6bd" : relation ? "#baa9ff" : "#8292a9"; context.setLineDash(fact || relation ? [] : [7, 7]); context.stroke(); context.restore();
  }

  function drawGraphNode(context, item, x, y, kind, focal = false) {
    const palette = kind === "stash" ? "#f4c57c" : kind === "worktree" ? "#8ec5ff" : "#8be6c5"; const radius = focal ? 29 : 21;
    context.beginPath(); context.fillStyle = "#17263a"; context.strokeStyle = palette; context.lineWidth = focal ? 3 : 2; context.arc(x, y, radius, 0, Math.PI * 2); context.fill(); context.stroke(); context.fillStyle = "#f3f5f8"; context.font = focal ? "700 12px system-ui" : "700 10px system-ui"; context.textAlign = "center"; context.fillText(truncate(item.title, focal ? 26 : 16), x, y + 4); context.textAlign = "start";
    state.graphTargets.push({ type: "item", item, x, y, radius: radius + 8 });
  }

  function clickGraph(event) {
    const bounds = elements.graph.getBoundingClientRect(); const x = event.clientX - bounds.left; const y = event.clientY - bounds.top;
    const target = [...state.graphTargets].reverse().find((item) => Math.hypot(item.x - x, item.y - y) <= item.radius);
    if (!target) return;
    if (target.type === "group") { state.group = target.group; state.selected = null; state.query = ""; elements.search.value = ""; renderList(true); renderInspector(); drawGraph(); return; }
    if (target.item) select(target.item);
  }

  document.querySelectorAll("input[type=file]").forEach((input) => input.addEventListener("change", () => readFile(input.id.replace("-file", ""), input)));
  elements.search.addEventListener("input", () => { state.query = elements.search.value; state.group = null; state.page = 0; renderList(true); });
  elements.virtualList.addEventListener("scroll", () => renderList(false));
  $("#kind-filters").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-kind]"); if (!button) return; state.filter = button.dataset.kind; state.page = 0; document.querySelectorAll(".filter").forEach((item) => item.classList.toggle("active", item === button)); renderList(true);
  });
  document.querySelector("#view-modes").addEventListener("click", (event) => { const button = event.target.closest("button[data-view]"); if (!button) return; state.viewMode = button.dataset.view; state.page = 0; state.graphPage = 0; state.selected = null; state.group = null; state.filter = "all"; document.querySelectorAll(".view-mode").forEach((item) => { const active = item === button; item.classList.toggle("active", active); item.setAttribute("aria-selected", String(active)); }); document.querySelectorAll(".filter").forEach((item) => item.classList.toggle("active", item.dataset.kind === "all")); renderAll(); });
  elements.pagePrev.addEventListener("click", () => { state.page -= 1; renderList(true); });
  elements.pageNext.addEventListener("click", () => { state.page += 1; renderList(true); });
  elements.graphPrev.addEventListener("click", () => { state.graphPage -= 1; drawGraph(); });
  elements.graphNext.addEventListener("click", () => { state.graphPage += 1; drawGraph(); });
  elements.overview.addEventListener("click", () => { state.selected = null; state.group = null; state.query = ""; state.graphPage = 0; elements.search.value = ""; renderList(true); renderInspector(); drawGraph(); });
  elements.graph.addEventListener("click", clickGraph);
  $("#export-review").addEventListener("click", exportReview);
  window.addEventListener("resize", drawGraph);
  renderAll();
})();
