"use strict";

// This page is an illustrative design study. All names, Git facts, and Jev-shaped
// responses below are synthetic. It makes no network or TypeSafe API requests.
const stages = [
  { title: "Git inventory", subtitle: "Find every ref, worktree and stash.", badge: "01 / 04 · Git inventory" },
  { title: "Candidate links", subtitle: "Connect work that might be related.", badge: "02 / 04 · Candidate links" },
  { title: "Jev judgments", subtitle: "Ask small, directional questions.", badge: "03 / 04 · Jev judgments" },
  { title: "Human plan", subtitle: "Preserve unique work, then review.", badge: "04 / 04 · Human plan" },
];

const nodes = [
  { id: "main", title: "main", subtitle: "canonical branch", kind: "main", icon: "◆", x: 135, y: 325, tag: "CLEAN", detail: "The canonical checkout is clean in this synthetic snapshot." },
  { id: "pr", title: "PR #214", subtitle: "merged", kind: "merged", icon: "✓", x: 370, y: 130, tag: "MERGED", detail: "Git and PR metadata prove this change landed in main." },
  { id: "auth-old", title: "auth/v1", subtitle: "2 commits ahead", kind: "branch", icon: "⑂", x: 350, y: 330, tag: "REVIEW", detail: "This branch has one patch equivalent to auth/v2 and one unique commit that still needs a destination." },
  { id: "auth-new", title: "auth/v2", subtitle: "active branch", kind: "branch", icon: "⑂", x: 650, y: 230, tag: "ACTIVE", detail: "The likely successor to auth/v1. Its relationship is a model suggestion, not a Git fact." },
  { id: "validator", title: "validator", subtitle: "stacked work", kind: "branch", icon: "⑂", x: 795, y: 390, tag: "WAITING", detail: "The validator branch appears to require auth/v2 behavior. The dependency still needs maintainer review." },
  { id: "stash", title: "stash@{0}", subtitle: "uncommitted work", kind: "stash", icon: "▤", x: 595, y: 535, tag: "PRESERVE", detail: "The stash contains work from auth/v1. It stays accounted for until a maintainer preserves or discards it explicitly." },
  { id: "metrics", title: "telemetry", subtitle: "independent work", kind: "branch", icon: "⑂", x: 255, y: 525, tag: "ACTIVE", detail: "A separate active branch. Shared ancestry with main does not make it part of the auth cluster." },
];

const edges = [
  { id: "main-pr", from: "main", to: "pr", type: "fact", label: "merged into", path: "M 157 305 Q 215 153 346 137", evidence: ["PR #214 merge commit is reachable from main", "Merge SHA is recorded in the inventory"] },
  { id: "main-old", from: "main", to: "auth-old", type: "fact", label: "diverged from", path: "M 159 326 Q 245 308 326 330", evidence: ["Known merge base", "2 commits unique to auth/v1"] },
  { id: "main-new", from: "main", to: "auth-new", type: "fact", label: "diverged from", path: "M 151 303 Q 345 105 625 221", evidence: ["Known merge base", "3 commits unique to auth/v2"] },
  { id: "old-stash", from: "auth-old", to: "stash", type: "fact", label: "stash origin", path: "M 371 349 Q 420 506 572 531", evidence: ["Stash parent points to auth/v1 tip at creation", "Uncommitted content is present"] },
  { id: "main-metrics", from: "main", to: "metrics", type: "fact", label: "diverged from", path: "M 143 349 Q 157 475 240 511", evidence: ["Known merge base", "Telemetry commits are unique"] },
  {
    id: "supersedes", from: "auth-old", to: "auth-new", type: "proposed", label: "supersedes", path: "M 373 316 Q 477 180 625 232", pill: { x: 505, y: 220, text: "91%" },
    question: "Has one branch replaced the other's intended change?",
    answer: "B_REPLACES_A", distribution: [["B replaces A", 91], ["Neither", 6], ["Unknown", 3]],
    evidence: ["Both branches reference task AUTH-42", "One commit is patch equivalent", "Both change session validation", "One commit remains unique to auth/v1"],
    implication: "Review the unique auth/v1 commit before closing that branch."
  },
  {
    id: "depends", from: "auth-new", to: "validator", type: "proposed", label: "depends on", path: "M 671 248 Q 799 250 797 366", pill: { x: 787, y: 297, text: "86%" },
    question: "Does either branch require work unique to the other?",
    answer: "VALIDATOR_REQUIRES_AUTH_V2", distribution: [["Requires auth/v2", 86], ["Neither", 9], ["Unknown", 5]],
    evidence: ["Validator imports the new session interface", "Its tests reference behavior added on auth/v2", "The branches are not ancestry-linked"],
    implication: "Keep validator active and review this dependency before integration."
  },
];

const stageList = document.querySelector("#stage-list");
const graph = document.querySelector("#graph");
const inspector = document.querySelector("#inspector-content");
const phaseBadge = document.querySelector("#phase-badge");
const count = document.querySelector("#graph-count");
const focusIndex = document.querySelector("#focus-index");
const playButton = document.querySelector("#play-button");
const playLabel = document.querySelector("#play-label");
const playIcon = document.querySelector("#play-icon");
let stage = 0;
let selected = null;
let timer = null;

const svgNS = "http://www.w3.org/2000/svg";
function svgElement(name, attributes = {}, value = "") {
  const element = document.createElementNS(svgNS, name);
  for (const [key, attribute] of Object.entries(attributes)) element.setAttribute(key, attribute);
  if (value) element.textContent = value;
  return element;
}

function makeStages() {
  stageList.replaceChildren();
  stages.forEach((item, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `stage${index === stage ? " active" : ""}${index < stage ? " complete" : ""}`;
    button.setAttribute("aria-current", index === stage ? "step" : "false");
    const number = document.createElement("span");
    number.className = "stage-index";
    number.textContent = index < stage ? "✓" : String(index + 1).padStart(2, "0");
    const copy = document.createElement("span");
    copy.className = "stage-copy";
    const title = document.createElement("strong");
    title.textContent = item.title;
    const description = document.createElement("small");
    description.textContent = item.subtitle;
    copy.append(title, description);
    button.append(number, copy);
    button.addEventListener("click", () => { stopPlayback(); setStage(index); });
    stageList.append(button);
  });
}

function makeGraph() {
  graph.replaceChildren();
  const edgeLayer = svgElement("g", { "aria-label": "Relationships" });
  edges.forEach((edge) => {
    const hidden = edge.type === "proposed" && stage === 0;
    const judged = edge.type === "proposed" && stage >= 2;
    const group = svgElement("g", {
      class: `graph-edge ${edge.type === "proposed" ? "proposed" : "fact"}${judged ? " judged" : ""}${edge.id === "depends" ? " dependency" : ""}${hidden ? " hidden" : ""}${selected === edge.id ? " selected" : ""}`,
      role: "button", tabindex: hidden ? "-1" : "0", "aria-hidden": hidden ? "true" : "false", "data-edge": edge.id,
      "aria-label": `${edge.from} ${edge.label} ${edge.to}${judged ? `, ${edge.pill.text} illustrative confidence` : ""}`,
    });
    group.append(svgElement("path", { class: "edge-visible", d: edge.path }));
    group.append(svgElement("path", { class: "edge-hit", d: edge.path }));
    if (edge.pill) {
      const pill = svgElement("g", { class: "edge-pill" });
      pill.append(svgElement("rect", { class: "edge-pill-bg", x: edge.pill.x - 24, y: edge.pill.y - 14, width: 48, height: 27, rx: 13 }));
      pill.append(svgElement("text", { class: "edge-pill-text", x: edge.pill.x, y: edge.pill.y + 5 }, edge.pill.text));
      group.append(pill);
    }
    group.addEventListener("click", () => select(edge.id));
    group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(edge.id); } });
    edgeLayer.append(group);
  });
  graph.append(edgeLayer);

  const nodeLayer = svgElement("g", { "aria-label": "Git objects" });
  nodes.forEach((node) => {
    const atRisk = stage === 3 && ["auth-old", "stash"].includes(node.id);
    const group = svgElement("g", {
      class: `graph-node ${node.kind}${selected === node.id ? " selected" : ""}${atRisk ? " at-risk" : ""}`,
      role: "button", tabindex: "0", "data-node": node.id,
      "aria-label": `${node.title}, ${node.subtitle}. ${node.detail}`,
    });
    group.append(svgElement("circle", { class: "node-halo", cx: node.x, cy: node.y, r: 33 }));
    group.append(svgElement("circle", { class: "node-circle", cx: node.x, cy: node.y, r: 23 }));
    group.append(svgElement("text", { class: "node-icon", x: node.x, y: node.y + 1 }, node.icon));
    group.append(svgElement("text", { class: "node-title", x: node.x, y: node.y + 48 }, node.title));
    group.append(svgElement("text", { class: "node-subtitle", x: node.x, y: node.y + 65 }, node.subtitle));
    if (stage === 3 || node.kind === "stash") {
      const width = Math.max(55, node.tag.length * 7 + 18);
      group.append(svgElement("rect", { class: "node-tag-bg", x: node.x - width / 2, y: node.y + 74, width, height: 20, rx: 5 }));
      group.append(svgElement("text", { class: "node-tag-text", x: node.x, y: node.y + 88 }, node.tag));
    }
    group.addEventListener("click", () => select(node.id));
    group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(node.id); } });
    nodeLayer.append(group);
  });
  graph.append(nodeLayer);
}

function list(items) {
  const ul = document.createElement("ul");
  ul.className = "evidence-list";
  items.forEach((item) => { const li = document.createElement("li"); li.textContent = item; ul.append(li); });
  return ul;
}

function heading(kind, title, description) {
  const kicker = document.createElement("span");
  kicker.className = `focus-kicker ${kind}`;
  kicker.textContent = kind === "fact" ? "GIT FACT" : kind === "plan" ? "REVIEW PLAN" : "JEV QUESTION";
  const h2 = document.createElement("h2");
  h2.textContent = title;
  const p = document.createElement("p");
  p.textContent = description;
  inspector.append(kicker, h2, p);
}

function label(value) {
  const element = document.createElement("span");
  element.className = "inspector-label";
  element.textContent = value;
  inspector.append(element);
}

function relationArrow(from, to) {
  const row = document.createElement("div");
  row.className = "relation-arrow";
  const left = document.createElement("span"); left.textContent = from;
  const arrow = document.createElement("b"); arrow.textContent = "→";
  const right = document.createElement("span"); right.textContent = to;
  row.append(left, arrow, right);
  inspector.append(row);
}

function answerCard(edge) {
  const card = document.createElement("div");
  card.className = "answer-card";
  const eyebrow = document.createElement("span"); eyebrow.className = "inspector-label"; eyebrow.textContent = "ILLUSTRATIVE TYPED ANSWER";
  const answer = document.createElement("strong"); answer.textContent = edge.answer;
  const note = document.createElement("small"); note.textContent = "Synthetic probabilities shown for this design study.";
  card.append(eyebrow, answer, note);
  edge.distribution.forEach(([name, probability]) => {
    const row = document.createElement("div"); row.className = "bar-row";
    const title = document.createElement("span"); title.textContent = name;
    const track = document.createElement("div"); track.className = "bar-track";
    const fill = document.createElement("div"); fill.className = "bar-fill"; fill.style.width = `${probability}%`;
    const value = document.createElement("b"); value.textContent = `${probability}%`;
    track.append(fill); row.append(title, track, value); card.append(row);
  });
  inspector.append(card);
}

function conclusion(title, text) {
  const box = document.createElement("div"); box.className = "conclusion";
  const strong = document.createElement("strong"); strong.textContent = title;
  const body = document.createElement("span"); body.textContent = text;
  box.append(strong, body); inspector.append(box);
}

function renderInspector() {
  inspector.replaceChildren();
  const edge = edges.find((item) => item.id === selected);
  const node = nodes.find((item) => item.id === selected);
  focusIndex.textContent = selected ? "SELECTED" : `${String(stage + 1).padStart(2, "0")} / 04`;

  if (edge) {
    const proposed = edge.type === "proposed";
    const canJudge = proposed && stage >= 2;
    heading(proposed ? "" : "fact", proposed ? edge.question : edge.label, proposed ? "A bounded question about one possible link between two branches." : "This edge is established by Git or named repository metadata.");
    relationArrow(edge.from, edge.to);
    label("EVIDENCE IN VIEW");
    inspector.append(list(edge.evidence));
    if (canJudge) { answerCard(edge); conclusion("Preservation check", edge.implication); }
    else if (proposed) conclusion("Candidate only", "The matching signals justify a closer look. No model judgment is shown yet.");
    return;
  }
  if (node) {
    heading(node.kind === "main" || node.kind === "merged" || node.kind === "stash" ? "fact" : "plan", node.title, node.detail);
    label("INVENTORY RECORD");
    inspector.append(list([`Kind: ${node.kind}`, `Observed state: ${node.subtitle}`, `Plan label: ${node.tag.toLowerCase()}`]));
    if (node.id === "auth-old" || node.id === "stash") conclusion("Unique work remains", "This item stays visible in the plan until a maintainer records where its work went.");
    return;
  }

  if (stage === 0) {
    heading("fact", "Start with what Git knows.", "Every object in this small repository is present before we ask Jev anything.");
    label("IN THIS SNAPSHOT");
    inspector.append(list(["4 local branches and main", "1 merged pull request", "1 stash containing uncommitted work", "5 factual relationships"]));
    conclusion("What is still unknown?", "Git alone cannot establish whether auth/v2 supersedes auth/v1 or validator depends on it.");
  } else if (stage === 1) {
    heading("", "Find the likely links.", "Deterministic signals narrow the search to two candidate pairs.");
    label("WHY THESE PAIRS?");
    inspector.append(list(["Shared task ID and paths: auth/v1 ↔ auth/v2", "Shared interface use: auth/v2 ↔ validator", "One patch equivalent commit, checked directly"]));
    conclusion("Coverage is explicit", "Candidate generation limits the questions Jev sees. Missing candidates remain a reported uncertainty.");
  } else if (stage === 2) {
    const proposed = edges.find((item) => item.id === "supersedes");
    selected = proposed.id;
    makeGraph();
    renderInspector();
  } else {
    heading("plan", "Preserve first. Close later.", "The proposed plan keeps every unique piece of work visible for a maintainer.");
    label("REVIEW QUEUE");
    inspector.append(list(["auth/v2: remain active", "auth/v1: review 1 unique commit before closure", "stash@{0}: preserve uncommitted work", "validator: review dependency on auth/v2", "telemetry: independent active work"]));
    conclusion("Human decision required", "No branch or stash is deleted by this visual. A future run must recheck the Git snapshot before any cleanup.");
  }
}

function render() {
  phaseBadge.textContent = stages[stage].badge;
  count.textContent = `7 objects · 5 facts${stage >= 1 ? " · 2 candidates" : ""}${stage >= 2 ? " · 2 judgments" : ""}`;
  makeStages();
  makeGraph();
  renderInspector();
}

function setStage(index) {
  stage = index;
  selected = index === 2 ? "supersedes" : null;
  render();
}

function select(id) {
  selected = id;
  makeGraph();
  renderInspector();
}

function stopPlayback() {
  if (timer) clearInterval(timer);
  timer = null;
  playLabel.textContent = stage === 3 ? "Replay the analysis" : "Play the analysis";
  playIcon.textContent = "▶";
}

playButton.addEventListener("click", () => {
  if (timer) { stopPlayback(); return; }
  if (stage === 3) setStage(0);
  playLabel.textContent = "Pause";
  playIcon.textContent = "Ⅱ";
  timer = setInterval(() => {
    if (stage < 3) setStage(stage + 1);
    if (stage === 3) stopPlayback();
  }, 2200);
});

render();
