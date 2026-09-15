#!/usr/bin/env node

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "..", "Model.js"), "utf8");
const model = {};
vm.createContext(model);
vm.runInContext(source, model);

const entries = [
  { description: "#42 remove footer headlines", projectId: "storefront" },
  { description: "#420 unrelated ticket", projectId: "other" },
  { description: "#42 update checkout", projectId: "checkout" },
];

assert.equal(model.ticketNumber("#42 remove"), "42");
assert.equal(model.ticketNumber("Ticket #42"), "");

const ticketMatches = model.ticketSuggestions(entries, "#42", 5);
assert.deepEqual(
  Array.from(ticketMatches, (entry) => entry.description),
  ["#42 remove footer headlines", "#42 update checkout"],
);

const prefixFirst = model.ticketSuggestions(entries, "#42 update", 5);
assert.equal(prefixFirst[0].description, "#42 update checkout");
assert.equal(model.ticketSuggestions(entries, "remove", 5).length, 0);

const remembered = model.rememberRecentEntry(entries, {
  description: "#42 remove footer headlines",
  projectId: "new-project",
}, 100);
assert.equal(remembered.length, 3);
assert.equal(remembered[0].projectId, "new-project");

console.log("Model tests passed");
