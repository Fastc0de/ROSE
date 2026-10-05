// Run with: node --test tests/mode.test.js
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const SCRIPT = path.join(__dirname, "..", "scripts", "mode.js");
const AGENT = "project-orchestrator:orchestrator";

function project(files) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "po-mode-"));
  for (const [name, content] of Object.entries(files || {})) {
    fs.mkdirSync(path.dirname(path.join(dir, name)), { recursive: true });
    fs.writeFileSync(path.join(dir, name), content);
  }
  return dir;
}
function mode(dir, op) {
  const r = spawnSync("node", op === undefined ? [SCRIPT] : [SCRIPT, op], { cwd: dir, encoding: "utf8" });
  return { code: r.status, out: r.stdout + r.stderr };
}
const local = (dir) => path.join(dir, ".claude", "settings.local.json");
const read = (dir) => JSON.parse(fs.readFileSync(local(dir), "utf8"));

test("on creates the file in a project without .claude", () => {
  const dir = project();
  const r = mode(dir, "on");
  assert.strictEqual(r.code, 0);
  assert.deepStrictEqual(read(dir), { agent: AGENT });
  assert.match(r.out, /is ON/);
});

test("on preserves every other setting", () => {
  const dir = project({ ".claude/settings.local.json": '{\n  "permissions": {"allow": ["Bash(npm test)"]},\n  "outputStyle": "Concise"\n}\n' });
  assert.strictEqual(mode(dir, "on").code, 0);
  assert.deepStrictEqual(read(dir), { permissions: { allow: ["Bash(npm test)"] }, outputStyle: "Concise", agent: AGENT });
});

test("on twice changes nothing the second time", () => {
  const dir = project();
  mode(dir, "on");
  const before = fs.readFileSync(local(dir), "utf8");
  const r = mode(dir, "on");
  assert.strictEqual(r.code, 0);
  assert.match(r.out, /already ON/);
  assert.strictEqual(fs.readFileSync(local(dir), "utf8"), before);
});

test("on refuses to replace a different agent", () => {
  const dir = project({ ".claude/settings.local.json": '{"agent": "code-reviewer"}' });
  const r = mode(dir, "on");
  assert.strictEqual(r.code, 1);
  assert.deepStrictEqual(read(dir), { agent: "code-reviewer" });
});

test("off removes only the agent key", () => {
  const dir = project({ ".claude/settings.local.json": JSON.stringify({ agent: AGENT, outputStyle: "Concise" }) });
  const r = mode(dir, "off");
  assert.strictEqual(r.code, 0);
  assert.deepStrictEqual(read(dir), { outputStyle: "Concise" });
  assert.match(r.out, /is OFF/);
});

test("off when already off does not create files", () => {
  const dir = project();
  const r = mode(dir, "off");
  assert.strictEqual(r.code, 0);
  assert.match(r.out, /already OFF/);
  assert.strictEqual(fs.existsSync(path.join(dir, ".claude")), false);
});

test("off leaves a different agent alone", () => {
  const dir = project({ ".claude/settings.local.json": '{"agent": "code-reviewer"}' });
  const r = mode(dir, "off");
  assert.strictEqual(r.code, 1);
  assert.deepStrictEqual(read(dir), { agent: "code-reviewer" });
});

test("off mentions the shared settings file when it still sets the orchestrator", () => {
  const dir = project({ ".claude/settings.json": JSON.stringify({ agent: AGENT }) });
  const r = mode(dir, "off");
  assert.strictEqual(r.code, 0);
  assert.match(r.out, /shared \.claude\/settings\.json still sets/);
});

test("invalid JSON is never overwritten", () => {
  const broken = '{ "permissions": { "allow": [ }';
  const dir = project({ ".claude/settings.local.json": broken });
  for (const op of ["on", "off", "status"]) {
    assert.strictEqual(mode(dir, op).code, 1);
    assert.strictEqual(fs.readFileSync(local(dir), "utf8"), broken);
  }
});

test("a file with a BOM or empty content is handled", () => {
  const bom = project({ ".claude/settings.local.json": '﻿{"outputStyle": "Concise"}' });
  assert.strictEqual(mode(bom, "on").code, 0);
  assert.deepStrictEqual(read(bom), { outputStyle: "Concise", agent: AGENT });
  const empty = project({ ".claude/settings.local.json": "" });
  assert.strictEqual(mode(empty, "on").code, 0);
  assert.deepStrictEqual(read(empty), { agent: AGENT });
});

test("status reports on, off, and other agents without writing", () => {
  const off = project();
  assert.match(mode(off, "status").out, /is OFF/);
  assert.strictEqual(fs.existsSync(path.join(off, ".claude")), false);
  const on = project();
  mode(on, "on");
  assert.match(mode(on, "status").out, /is ON/);
  const other = project({ ".claude/settings.local.json": '{"agent": "code-reviewer"}' });
  assert.match(mode(other, "status").out, /different agent/);
  const shared = project({ ".claude/settings.json": JSON.stringify({ agent: AGENT }) });
  assert.match(mode(shared, "status").out, /is ON.*shared/);
});

test("unknown or missing operation prints usage and fails", () => {
  const dir = project();
  assert.strictEqual(mode(dir, "toggle").code, 1);
  assert.strictEqual(mode(dir).code, 1);
  assert.strictEqual(fs.existsSync(path.join(dir, ".claude")), false);
});
