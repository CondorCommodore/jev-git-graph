import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import viewer from "../docs/viewer-data.js";

const directory = path.resolve(process.argv[2] || "");
assert.ok(directory, "artifact directory is required");

function read(name) {
  return JSON.parse(fs.readFileSync(path.join(directory, name), "utf8"));
}

const inventory = read("inventory.json");
const candidates = read("candidates.json");
const relations = read("relations.json");
const model = viewer.normalize({ inventory, candidates, relations });

assert.deepEqual(model.errors, []);
assert.equal(model.counts.candidates, 3000);
assert.equal(model.counts.factResolved, 1000);
assert.equal(model.counts.evaluated, 1000);
assert.equal(model.counts.unresolved, 1000);
assert.ok(model.groups.filter((group) => group.count > 1).length >= 2, "expected distinct connected components");

const pageSize = 100;
const pageCount = Math.ceil(model.candidates.length / pageSize);
const firstPage = model.candidates.slice(0, pageSize);
const lastPage = model.candidates.slice((pageCount - 1) * pageSize);
assert.equal(pageCount, 30);
assert.equal(firstPage.length, 100);
assert.equal(lastPage.length, 100);
assert.equal(firstPage[0].candidateId, "fact-0000");
assert.equal(lastPage[0].candidateId, "unresolved-0900");
assert.equal(lastPage.at(-1).candidateId, "unresolved-0999");
assert.notEqual(firstPage[0].candidateId, lastPage[0].candidateId);

console.log(`L6 viewer acceptance: candidates=${model.counts.candidates} first=${firstPage[0].candidateId} last=${lastPage.at(-1).candidateId} groups=${model.groups.length}`);
