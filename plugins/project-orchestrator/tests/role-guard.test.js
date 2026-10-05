// Run with: node --test tests/role-guard.test.js
const test = require("node:test");
const assert = require("node:assert");
const { spawnSync } = require("node:child_process");
const path = require("node:path");

const GUARD = path.join(__dirname, "..", "hooks", "scripts", "role-guard.js");
const CWD = "/work/proj";
const WIN = "C:\\Users\\Alejandro\\proj";

function run(agentType, toolName, toolInput, cwd) {
  const input = { tool_name: toolName, tool_input: toolInput, cwd: cwd || CWD };
  if (agentType) input.agent_type = agentType;
  const result = spawnSync("node", [GUARD], { input: JSON.stringify(input), encoding: "utf8" });
  return { code: result.status, stderr: result.stderr };
}
const allowed = (...a) => assert.strictEqual(run(...a).code, 0);
const blocked = (...a) => assert.strictEqual(run(...a).code, 2);

test("calls from outside the plugin's agents are never touched", () => {
  allowed(undefined, "Write", { file_path: "src/app.js" });
  allowed(undefined, "Bash", { command: "rm -rf build && npm test" });
  allowed("project-memory:project-archiver", "Write", { file_path: "src/app.js" });
  allowed("other-plugin:explorer", "Write", { file_path: "src/app.js" });
  allowed("Explore", "Bash", { command: "ls | wc -l" });
});

test("both bare and plugin-scoped agent names are recognized", () => {
  blocked("orchestrator", "Write", { file_path: "src/app.js" });
  blocked("project-orchestrator:orchestrator", "Write", { file_path: "src/app.js" });
});

test("orchestrator writes only under docs/, never project memory", () => {
  const o = "project-orchestrator:orchestrator";
  allowed(o, "Write", { file_path: "docs/specs/2026-10-04-auth-design.md" });
  allowed(o, "Edit", { file_path: "/work/proj/docs/plans/2026-10-04-auth-plan.md" });
  allowed(o, "Write", { file_path: "docs/ideas.md" });
  allowed(o, "Write", { file_path: "packages/api/docs/notes.md" });
  blocked(o, "Write", { file_path: "docs/project-memory/status.md" });
  blocked(o, "Write", { file_path: "README.md" });
  blocked(o, "Write", { file_path: "CLAUDE.md" });
  blocked(o, "Edit", { file_path: "src/docs.ts" });
  blocked(o, "Write", { file_path: "docs" });
  blocked(o, "Write", {});
});

test("orchestrator path traversal out of docs/ is blocked", () => {
  const o = "project-orchestrator:orchestrator";
  blocked(o, "Write", { file_path: "docs/../src/app.js" });
  blocked(o, "Write", { file_path: "docs\\..\\src\\app.js" });
  blocked(o, "Write", { file_path: "/work/proj/docs/specs/../../package.json" });
  blocked(o, "Write", { file_path: "docs/specs/../project-memory/status.md" });
});

test("orchestrator Windows paths", () => {
  const o = "project-orchestrator:orchestrator";
  allowed(o, "Write", { file_path: WIN + "\\docs\\specs\\a-design.md" }, WIN);
  allowed(o, "Write", { file_path: "docs\\plans\\a-plan.md" }, WIN);
  blocked(o, "Write", { file_path: WIN + "\\src\\index.ts" }, WIN);
  blocked(o, "Write", { file_path: WIN + "\\Docs\\Project-Memory\\status.md" }, WIN);
  blocked(o, "Write", { file_path: WIN + "\\docs\\..\\src\\index.ts" }, WIN);
});

test("a docs folder above the project does not count as the project's docs/", () => {
  const o = "project-orchestrator:orchestrator";
  const cwd = "/home/me/docs/proj";
  blocked(o, "Write", { file_path: "/home/me/docs/proj/src/app.js" }, cwd);
  blocked(o, "Write", { file_path: "src/app.js" }, cwd);
  allowed(o, "Write", { file_path: "/home/me/docs/proj/docs/ideas.md" }, cwd);
  const winCwd = "C:\\Users\\Alejandro\\Docs\\proj";
  blocked(o, "Write", { file_path: winCwd + "\\src\\app.ts" }, winCwd);
  allowed(o, "Write", { file_path: winCwd + "\\docs\\specs\\a-design.md" }, winCwd);
  const i = "project-orchestrator:implementer";
  allowed(i, "Write", { file_path: "src/app.js" }, "/home/me/docs/plans/proj");
  blocked(i, "Write", { file_path: "docs/plans/a-plan.md" }, "/home/me/docs/plans/proj");
});

test("orchestrator and explorer Bash is read-only git", () => {
  for (const a of ["project-orchestrator:orchestrator", "project-orchestrator:explorer"]) {
    allowed(a, "Bash", { command: "git status" });
    allowed(a, "Bash", { command: "git log --oneline -20" });
    allowed(a, "Bash", { command: "git diff HEAD~1 -- src/" });
    allowed(a, "Bash", { command: "git show abc123" });
    allowed(a, "Bash", { command: "git ls-files" });
    allowed(a, "Bash", { command: 'git -C "C:/Users/Alejandro/proj" status' });
    blocked(a, "Bash", { command: "npm test" });
    blocked(a, "Bash", { command: "git commit -m x" });
    blocked(a, "Bash", { command: "git init" });
    blocked(a, "Bash", { command: "git status; rm -rf ." });
    blocked(a, "Bash", { command: "git status && npm test" });
    blocked(a, "Bash", { command: "git log | head" });
    blocked(a, "Bash", { command: "git diff > out.patch" });
    blocked(a, "Bash", { command: "git diff --output=out.patch" });
    blocked(a, "Bash", { command: "git -c core.pager=evil log" });
    blocked(a, "Bash", { command: "git log $(whoami)" });
    blocked(a, "Bash", { command: "git status\nrm x" });
    blocked(a, "Bash", { command: "find . -name '*.ts'" });
    blocked(a, "Bash", {});
  }
});

test("orchestrator may commit only its own docs", () => {
  const o = "project-orchestrator:orchestrator";
  allowed(o, "Bash", { command: "git add docs" });
  allowed(o, "Bash", { command: "git add -- docs/plans docs/specs" });
  allowed(o, "Bash", { command: "git add docs/plans/2026-10-04-auth-plan.md" });
  allowed(o, "Bash", { command: 'git commit -m "docs: M1 verified" -- docs' });
  allowed(o, "Bash", { command: "git commit -m 'docs: plan approved' -- docs/plans docs/specs" });
  allowed(o, "Bash", { command: 'git -C "C:/Users/Alejandro/proj" add docs' });
  blocked(o, "Bash", { command: "git add ." });
  blocked(o, "Bash", { command: "git add -A" });
  blocked(o, "Bash", { command: "git add docs src" });
  blocked(o, "Bash", { command: "git add docs/../src" });
  blocked(o, "Bash", { command: "git add docsx" });
  blocked(o, "Bash", { command: 'git commit -m "x"' });
  blocked(o, "Bash", { command: 'git commit -am "x" -- docs' });
  blocked(o, "Bash", { command: 'git commit -m "x" -- docs src' });
  blocked(o, "Bash", { command: 'git commit -m "x" -- .' });
  blocked(o, "Bash", { command: 'git commit --amend -m "x" -- docs' });
  blocked(o, "Bash", { command: 'git commit -m "x" -- docs && git push' });
  blocked(o, "Bash", { command: 'git commit -m "$(cat secret)" -- docs' });
  blocked(o, "Bash", { command: "git push" });
  // several -m flags, and messages with <, > or a newline inside the quotes
  allowed(o, "Bash", {
    command: 'git commit -m "docs: M1 verified" -m "Co-Authored-By: Someone <a@b.co>\nSession: https://x.y/z" -- docs',
  });
  allowed(o, "Bash", { command: "git commit -m 'docs: a > b; c | d' -- docs" });
  blocked(o, "Bash", { command: 'git commit -m "x"\n-- docs' });
  blocked(o, "Bash", { command: 'git commit -m "x" -- docs\nrm -rf src' });
  blocked(o, "Bash", { command: 'git commit -m "`whoami`" -- docs' });
  blocked(o, "Bash", { command: 'git commit -m "a \\" -- docs; rm -rf src; echo \\"" -- docs' });
  blocked(o, "Bash", { command: 'git commit -m "x" -- docs/*' });
  blocked(o, "Bash", { command: 'git commit -m "x" -- docs $HOME' });
  blocked(o, "Bash", { command: 'git commit -m "x" -- "docs/../src"' });
  blocked(o, "Bash", { command: 'git commit -m x -- docs' });
  // the explorer gets no such exception
  blocked("project-orchestrator:explorer", "Bash", { command: "git add docs" });
});

test("orchestrator may run only this plugin's mode switch", () => {
  const o = "project-orchestrator:orchestrator";
  const script = path.join(__dirname, "..", "scripts", "mode.js");
  const fwd = script.replace(/\\/g, "/");
  allowed(o, "Bash", { command: 'node "' + fwd + '" off' });
  allowed(o, "Bash", { command: 'node "' + fwd + '" on' });
  allowed(o, "Bash", { command: "node '" + fwd + "' status" });
  allowed(o, "Bash", { command: 'node "' + fwd.toUpperCase().replace("MODE.JS", "mode.js") + '" status' });
  allowed(o, "Bash", { command: 'node "' + path.join(__dirname, "..", "hooks", "..", "scripts", "mode.js") + '" status' });
  blocked(o, "Bash", { command: 'node "' + fwd + '"' });
  blocked(o, "Bash", { command: 'node "' + fwd + '" off; rm -rf src' });
  blocked(o, "Bash", { command: 'node "' + fwd + '" off && npm test' });
  blocked(o, "Bash", { command: 'node "' + fwd + '" off extra' });
  blocked(o, "Bash", { command: 'node -e "x" "' + fwd + '" off' });
  blocked(o, "Bash", { command: 'node "/tmp/other/scripts/mode.js" off' });
  blocked(o, "Bash", { command: "node scripts/mode.js off" });
  blocked(o, "Bash", { command: 'node "' + fwd + '/../../evil.js" off' });
  // only the orchestrator gets it
  blocked("project-orchestrator:explorer", "Bash", { command: 'node "' + fwd + '" off' });
});

test("implementer cannot change protected docs from the shell", () => {
  const i = "project-orchestrator:implementer";
  blocked(i, "Bash", { command: "echo done > docs/plans/a-plan.md" });
  blocked(i, "Bash", { command: "cat > docs/specs/a-design.md <<'EOF'\nx\nEOF" });
  blocked(i, "Bash", { command: "sed -i 's/todo/verified/' docs/plans/a-plan.md" });
  blocked(i, "Bash", { command: "rm -rf docs/project-memory" });
  blocked(i, "Bash", { command: "mv docs/plans/a-plan.md /tmp/" });
  blocked(i, "Bash", { command: "echo x | tee docs\\plans\\a-plan.md" });
  allowed(i, "Bash", { command: "cat docs/plans/a-plan.md" });
  allowed(i, "Bash", { command: "grep -n T3 docs/plans/a-plan.md docs/specs/a-design.md" });
  allowed(i, "Bash", { command: "git add docs/api.md src && git commit -m 'T2: api'" });
  allowed(i, "Bash", { command: "echo hi > docs/api.md" });
  allowed(i, "Bash", { command: "mkdir -p src && cat > src/app.js <<'EOF'\nx\nEOF" });
});

test("explorer and verifier never write", () => {
  for (const a of ["project-orchestrator:explorer", "project-orchestrator:verifier"]) {
    blocked(a, "Write", { file_path: "docs/notes.md" });
    blocked(a, "Edit", { file_path: "src/app.js" });
    blocked(a, "NotebookEdit", { notebook_path: "nb.ipynb" });
  }
});

test("implementer writes code but not specs, plans, or memory", () => {
  const i = "project-orchestrator:implementer";
  allowed(i, "Write", { file_path: "src/app.js" });
  allowed(i, "Edit", { file_path: "package.json" });
  allowed(i, "Write", { file_path: "README.md" });
  allowed(i, "Write", { file_path: "docs/api.md" });
  allowed(i, "Write", { file_path: ".env.example" });
  blocked(i, "Edit", { file_path: "docs/plans/2026-10-04-auth-plan.md" });
  blocked(i, "Write", { file_path: "docs/specs/2026-10-04-auth-design.md" });
  blocked(i, "Write", { file_path: "docs/project-memory/sessions.md" });
  blocked(i, "Edit", { file_path: "src/../docs/plans/x-plan.md" });
  blocked(i, "Edit", { file_path: WIN + "\\docs\\plans\\x-plan.md" }, WIN);
});

test("implementer Bash is free except git push", () => {
  const i = "project-orchestrator:implementer";
  allowed(i, "Bash", { command: "npm install && npm test" });
  allowed(i, "Bash", { command: "git init" });
  allowed(i, "Bash", { command: "git add src/app.js && git commit -m 'T1: skeleton'" });
  allowed(i, "Bash", { command: "node scripts/push-fixtures.js" });
  blocked(i, "Bash", { command: "git push" });
  blocked(i, "Bash", { command: "git push origin main --force" });
  blocked(i, "Bash", { command: "npm test && git push" });
  blocked(i, "Bash", { command: "git -C /work/proj push" });
});

test("verifier Bash runs checks but never changes git state", () => {
  const v = "project-orchestrator:verifier";
  allowed(v, "Bash", { command: "npm test" });
  allowed(v, "Bash", { command: "git status" });
  allowed(v, "Bash", { command: "git diff HEAD~1..HEAD | head -200" });
  allowed(v, "Bash", { command: "git show --stat HEAD" });
  allowed(v, "Bash", { command: "pytest -k commit" });
  blocked(v, "Bash", { command: "git commit -am fix" });
  blocked(v, "Bash", { command: "git add ." });
  blocked(v, "Bash", { command: "npm test; git reset --hard" });
  blocked(v, "Bash", { command: "git checkout -- src/app.js" });
  blocked(v, "Bash", { command: "git stash" });
  blocked(v, "Bash", { command: "git push" });
});

test("no agent of the plugin reads secret files", () => {
  for (const r of ["orchestrator", "explorer", "implementer", "verifier"]) {
    const a = "project-orchestrator:" + r;
    blocked(a, "Read", { file_path: ".env" });
    blocked(a, "Read", { file_path: "/work/proj/.env.local" });
    blocked(a, "Read", { file_path: WIN + "\\.env.production" }, WIN);
    blocked(a, "Read", { file_path: "certs/server.pem" });
    blocked(a, "Read", { file_path: "certs/SERVER.KEY" });
    allowed(a, "Read", { file_path: ".env.example" });
    allowed(a, "Read", { file_path: "src/env.ts" });
    allowed(a, "Read", { file_path: "docs/keys.md" });
  }
});

test("the guard fails open on malformed input", () => {
  const result = spawnSync("node", [GUARD], { input: "not json", encoding: "utf8" });
  assert.strictEqual(result.status, 0);
  const empty = spawnSync("node", [GUARD], { input: "", encoding: "utf8" });
  assert.strictEqual(empty.status, 0);
});

test("block messages explain the rule", () => {
  const r = run("project-orchestrator:orchestrator", "Write", { file_path: "src/app.js" });
  assert.match(r.stderr, /only under docs/);
});
