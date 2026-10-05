#!/usr/bin/env node
// PreToolUse guard for the project-orchestrator agents.
//
// Acts only on tool calls made by one of this plugin's four agents, identified
// by the hook input's `agent_type`. Every other call is allowed untouched.
//
//   orchestrator  write: only docs/** (never docs/project-memory/**)
//                 bash:  read-only git, plus `git add` / `git commit` limited
//                        to docs/ paths, plus this plugin's own mode switch
//                        (`node <plugin>/scripts/mode.js on|off|status`)
//   explorer      write: nothing
//                 bash:  read-only git
//   implementer   write: anything except docs/specs/**, docs/plans/**,
//                        docs/project-memory/**
//                 bash:  anything except `git push` and shell writes into
//                        those three folders
//   verifier      write: nothing
//                 bash:  anything except git commands that change state
//   all four      read:  never .env, .env.*, *.pem, *.key (example files allowed)
//
// The orchestrator is only identifiable when the session was started with
// `--agent` (or the `agent` setting). Loaded through the skill in a normal
// session it has no `agent_type`, so its limits are then prompt-only.
//
// Exit 0 = allow. Exit 2 = block, with the reason on stderr.
// Any unexpected error allows the call, so a broken guard never blocks work.

const path = require("path");

const PLUGIN = "project-orchestrator";
const ROLES = ["orchestrator", "explorer", "implementer", "verifier"];
const WRITE_TOOLS = ["Write", "Edit", "MultiEdit", "NotebookEdit"];
const ENV_EXAMPLES = [".env.example", ".env.sample", ".env.template", ".env.dist"];

function roleOf(agentType) {
  if (typeof agentType !== "string") return null;
  for (const role of ROLES) {
    if (agentType === role || agentType === PLUGIN + ":" + role) return role;
  }
  return null;
}

// Absolute, forward-slash, lower-case path with `.` and `..` resolved.
// Done by hand so Windows paths are handled the same on every platform.
function normalize(filePath, cwd) {
  let p = String(filePath).replace(/\\/g, "/");
  const absolute = p.startsWith("/") || /^[A-Za-z]:\//.test(p);
  if (!absolute) {
    const base = String(cwd || process.cwd()).replace(/\\/g, "/");
    p = base + "/" + p;
  }
  return path.posix.normalize(p).toLowerCase();
}

function isSecretFile(filePath) {
  const base = String(filePath).replace(/\\/g, "/").split("/").pop().toLowerCase();
  if (ENV_EXAMPLES.includes(base)) return false;
  return (
    base === ".env" ||
    base.startsWith(".env.") ||
    base.endsWith(".pem") ||
    base.endsWith(".key")
  );
}

// Path of the target relative to the session directory when it is inside it,
// so a `docs` folder above the project never counts as the project's docs/.
function projectPath(filePath, cwd) {
  const full = normalize(filePath, cwd);
  if (cwd) {
    const base = normalize(cwd, cwd).replace(/\/+$/, "");
    if (full.startsWith(base + "/")) return full.slice(base.length);
  }
  return full;
}

function under(projectRelative, dir) {
  return projectRelative.includes("/" + dir + "/");
}

function isReadOnlyGit(command) {
  const trimmed = command.trim();
  // No chaining, piping, redirection, substitution, or multi-line commands.
  if (/[;&|<>`\n\r]|\$\(/.test(trimmed)) return false;
  // Flags that make git write a file or run another program.
  if (/(^|\s)(--output(=|\s|$)|--ext-diff(\s|$)|-c(\s|$)|--exec-path|--config-env)/.test(trimmed)) {
    return false;
  }
  return /^git(\s+-C\s+("[^"]+"|'[^']+'|\S+))?\s+(status|diff|log|show|ls-files|rev-parse)(\s|$)/.test(trimmed);
}

// `git add [--] docs...` and `git commit -m "<message>" [-m "..."] -- docs...`,
// so the orchestrator can commit the documents it owns and nothing else.
// Quoted strings are checked and set aside first, so a message may contain
// characters such as `<`, `>`, or a newline without opening a second command.
function isDocsCommit(command) {
  const quoted = [];
  const bare = command.trim().replace(/"[^"]*"|'[^']*'/g, (match) => {
    quoted.push(match);
    return "Q";
  });
  for (const q of quoted) {
    // Inside double quotes the shell still expands these.
    if (q.startsWith('"') && /[`\\]|\$\(/.test(q)) return false;
  }
  // Anything left outside quotes must be plain words separated by spaces.
  if (/[;&|<>`"'$\\\n\r(){}*?~!#]/.test(bare)) return false;
  if (/(^|[ \t/])\.\.([ \t/]|$)/.test(bare)) return false;
  const git = "^git([ \\t]+-C[ \\t]+\\S+)?[ \\t]+";
  const docsPaths = "docs(/\\S*)?([ \\t]+docs(/\\S*)?)*$";
  const add = new RegExp(git + "add[ \\t]+(--[ \\t]+)?" + docsPaths);
  const commit = new RegExp(git + "commit([ \\t]+-m[ \\t]+Q)+[ \\t]+--[ \\t]+" + docsPaths);
  return add.test(bare) || commit.test(bare);
}

// `node "<this plugin>/scripts/mode.js" on|off|status` and nothing else, so a
// session pinned to the orchestrator can still switch its own mode off.
const MODE_SCRIPT = path.resolve(__dirname, "..", "..", "scripts", "mode.js");

function samePath(a, b) {
  const clean = (p) => path.posix.normalize(String(p).replace(/\\/g, "/")).toLowerCase();
  if (clean(a) === clean(b)) return true;
  try {
    const fs = require("fs");
    return clean(fs.realpathSync(a)) === clean(fs.realpathSync(b));
  } catch (err) {
    return false;
  }
}

function isModeSwitch(command) {
  const match = /^node[ \t]+(?:"([^"]+)"|'([^']+)'|(\S+))[ \t]+(on|off|status)$/.exec(command.trim());
  if (!match) return false;
  const given = match[1] || match[2] || match[3];
  // Absolute paths only, so the result never depends on a working directory.
  if (!/^(\/|[A-Za-z]:[\\/])/.test(given)) return false;
  return samePath(given, MODE_SCRIPT);
}

// A shell command that both names a protected docs folder and contains
// something that writes, moves, or deletes.
const PROTECTED_DOCS = /docs[\\/]+(specs|plans|project-memory)([\\/]|\s|["']|$)/i;
const SHELL_WRITE = /(>|(^|[\s;&|(])(tee|rm|mv|cp|truncate|dd|touch|del|rmdir|sed\s+(-\S+\s+)*-i\S*)(\s|$))/;

const GIT_PREFIX = "(^|[\\s;&|(])git(\\s+-C\\s+(\"[^\"]+\"|'[^']+'|\\S+))?\\s+";
const GIT_PUSH = new RegExp(GIT_PREFIX + "push(\\s|$)");
const GIT_MUTATING = new RegExp(
  GIT_PREFIX +
    "(commit|add|reset|checkout|switch|restore|stash|merge|rebase|clean|push|pull|cherry-pick|revert|rm|mv|apply|am)(\\s|$)"
);

function decide(input) {
  const role = roleOf(input.agent_type);
  if (!role) return { allow: true };

  const tool = input.tool_name;
  const args = input.tool_input || {};
  const cwd = input.cwd;

  if (tool === "Read") {
    if (typeof args.file_path === "string" && isSecretFile(args.file_path)) {
      return { allow: false, reason: "reading secret files (.env, .env.*, *.pem, *.key) is not permitted." };
    }
    return { allow: true };
  }

  if (WRITE_TOOLS.includes(tool)) {
    const target = args.file_path || args.notebook_path;
    if (role === "explorer" || role === "verifier") {
      return { allow: false, reason: "the " + role + " is read-only and never writes files. Report the finding instead." };
    }
    if (typeof target !== "string") {
      return { allow: false, reason: "the write target could not be determined." };
    }
    const normalized = projectPath(target, cwd);
    if (role === "orchestrator") {
      if (under(normalized, "docs/project-memory")) {
        return { allow: false, reason: "docs/project-memory/** is written only through the project-memory skill." };
      }
      if (!under(normalized, "docs")) {
        return { allow: false, reason: "the orchestrator writes only under docs/**. Make this change a plan task for the implementer." };
      }
      return { allow: true };
    }
    // implementer
    for (const dir of ["docs/specs", "docs/plans", "docs/project-memory"]) {
      if (under(normalized, dir)) {
        return { allow: false, reason: dir + "/** belongs to the orchestrator. Report progress or problems instead of editing it." };
      }
    }
    return { allow: true };
  }

  if (tool === "Bash") {
    const command = args.command;
    if (typeof command !== "string") {
      return { allow: false, reason: "the command could not be determined." };
    }
    if (role === "orchestrator" || role === "explorer") {
      if (role === "orchestrator" && (isDocsCommit(command) || isModeSwitch(command))) return { allow: true };
      if (!isReadOnlyGit(command)) {
        return {
          allow: false,
          reason:
            "only plain read-only git commands (status, diff, log, show, ls-files, rev-parse) are permitted, without pipes, redirection, or chaining." +
            (role === "orchestrator"
              ? " You may also commit your own documents with `git add docs` and `git commit -m \"...\" -- docs`. Delegate anything else to the implementer or verifier."
              : ""),
        };
      }
      return { allow: true };
    }
    if (role === "implementer" && GIT_PUSH.test(command)) {
      return { allow: false, reason: "git push is not permitted. Leave publishing to the user." };
    }
    if (role === "implementer" && PROTECTED_DOCS.test(command) && SHELL_WRITE.test(command)) {
      return { allow: false, reason: "docs/specs, docs/plans, and docs/project-memory belong to the orchestrator and must not be changed from the shell. Report progress or problems instead." };
    }
    if (role === "verifier" && GIT_MUTATING.test(command)) {
      return { allow: false, reason: "the verifier never changes Git state. Use status, diff, log, or show." };
    }
    return { allow: true };
  }

  return { allow: true };
}

function main() {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => {
    raw += chunk;
  });
  process.stdin.on("end", () => {
    try {
      const result = decide(JSON.parse(raw));
      if (result.allow) process.exit(0);
      process.stderr.write("project-orchestrator guard: " + result.reason + "\n");
      process.exit(2);
    } catch (err) {
      process.exit(0);
    }
  });
  process.stdin.on("error", () => process.exit(0));
}

if (require.main === module) {
  main();
} else {
  module.exports = { decide, roleOf, normalize, projectPath, isSecretFile, isReadOnlyGit, isDocsCommit, isModeSwitch, MODE_SCRIPT };
}
