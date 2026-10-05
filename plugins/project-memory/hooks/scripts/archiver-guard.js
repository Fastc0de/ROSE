#!/usr/bin/env node
"use strict";
// PreToolUse guard for the project-archiver subagent.
//
// Replaces the permission block of the original opencode agent:
//   - Write/Edit: only docs/project-memory/**
//   - Read:       never .env, .env.*, *.pem, *.key
//   - Grep:       not available to the archiver (a content search could surface
//                 secrets); it reads the specific files it needs instead
//   - Bash:       only `git status`, `git log`, `git diff`, `git ls-files` in
//                 forms that list files and changes without printing file
//                 contents, and the plugin's own archive script
//
// It acts ONLY when the tool call comes from the project-archiver subagent.
// Every other call (main session, other agents) is allowed untouched.
// Exit 0 = allow. Exit 2 = block, with the reason on stderr.
// Any unexpected error allows the call, so a broken guard never blocks work.

const fs = require("fs");
const path = require("path");

const ARCHIVE_SCRIPT = path.resolve(
  __dirname, "..", "..", "skills", "project-memory", "scripts", "archive.js"
);
const SECRET_TEXT = "secret files (.env, .env.*, *.pem, *.key)";

function allow() {
  process.exit(0);
}

function block(reason) {
  process.stderr.write("project-archiver guard: " + reason + "\n");
  process.exit(2);
}

function isArchiver(agentType) {
  if (typeof agentType !== "string") return false;
  return agentType === "project-archiver" || agentType.endsWith(":project-archiver");
}

// ---------- paths ----------

function normalize(filePath, cwd) {
  let p = filePath;
  // Git Bash spelling of a Windows drive: /c/Users/... -> c:/Users/...
  if (process.platform === "win32") p = p.replace(/^\/([a-zA-Z])\//, "$1:/");
  const resolved = path.resolve(cwd || process.cwd(), p);
  return resolved.replace(/\\/g, "/").replace(/\/+/g, "/");
}

function isSecretFile(filePath) {
  const base = filePath.replace(/\\/g, "/").split("/").pop().toLowerCase();
  return (
    base === ".env" ||
    base.startsWith(".env.") ||
    base.endsWith(".pem") ||
    base.endsWith(".key")
  );
}

function isMemoryPath(filePath, cwd) {
  return normalize(filePath, cwd).toLowerCase().includes("/docs/project-memory/");
}

function samePath(a, b, cwd) {
  const real = (p) => {
    const n = normalize(p, cwd);
    try {
      return fs.realpathSync(n).replace(/\\/g, "/");
    } catch (err) {
      return n;
    }
  };
  const x = real(a);
  const y = real(b);
  return process.platform === "win32" ? x.toLowerCase() === y.toLowerCase() : x === y;
}

// ---------- shell ----------

// Splits one simple command into words. Returns null for anything that is not
// a single plain command: chaining, pipes, redirection, substitution, newlines,
// unbalanced quotes, or a backslash outside quotes.
function tokenize(command) {
  const text = command.trim();
  const tokens = [];
  let current = "";
  let started = false;
  let quote = null;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === "\n" || ch === "\r" || ch === "`") return null;
    if (quote === "'") {
      if (ch === "'") quote = null;
      else current += ch;
      continue;
    }
    if (ch === "$" && text[i + 1] === "(") return null;
    if (quote === '"') {
      if (ch === '"') quote = null;
      else if (ch === "\\" && (text[i + 1] === '"' || text[i + 1] === "\\")) current += text[++i];
      else current += ch;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      started = true;
    } else if (/\s/.test(ch)) {
      if (started) tokens.push(current);
      current = "";
      started = false;
    } else if (";&|<>\\".includes(ch)) {
      return null;
    } else {
      current += ch;
      started = true;
    }
  }
  if (quote) return null;
  if (started) tokens.push(current);
  return tokens;
}

const WRITES_OR_RUNS = /^(--output(=.*)?|--ext-diff|--no-index|--exec-path(=.*)?|--config-env(=.*)?)$/;
const PRINTS_CONTENT = /^(-p|-u|-c|-W|--patch|--patch-with-stat|--patch-with-raw|--cc|--binary|--function-context|--remerge-diff|--diff-merges(=.*)?|--unified(=.*)?|--word-diff(=.*)?|--color-words(=.*)?|-U\d*|-L.*)$/;
const DIFF_SUMMARY = /^(--stat(=.*)?|--shortstat|--numstat|--name-only|--name-status|--summary|--compact-summary|--dirstat(=.*)?|--raw|--quiet)$/;

// True when a bundle of one-letter flags (-sb, -pu) contains any of `letters`.
// -S<text> and -G<text> are searches whose text is not a bundle of flags.
function shortCluster(token, letters) {
  if (!/^-[A-Za-z]+$/.test(token) || /^-[SG]/.test(token)) return false;
  return token.slice(1).split("").some((c) => letters.includes(c));
}

// Returns null when the command is allowed, or the reason it is not.
function checkGit(tokens) {
  let i = 1;
  if (tokens[i] === "-C") i += 2;
  const sub = tokens[i];
  if (sub !== "status" && sub !== "diff" && sub !== "log" && sub !== "ls-files") {
    return "only `git status`, `git diff`, `git log`, and `git ls-files` are permitted.";
  }
  // Everything after a bare `--` is a path, not an option.
  const rest = tokens.slice(i + 1);
  const end = rest.indexOf("--");
  const flags = end === -1 ? rest : rest.slice(0, end);
  if (flags.some((t) => WRITES_OR_RUNS.test(t))) {
    return "git options that write files, run programs, or read outside the repository are not permitted.";
  }
  if (sub === "status") {
    if (flags.some((t) => t === "--verbose" || shortCluster(t, "v"))) {
      return "`git status -v` prints file contents; use plain `git status` or `git status --short`.";
    }
    return null;
  }
  if (sub === "ls-files") return null;
  if (flags.some((t) => PRINTS_CONTENT.test(t) || shortCluster(t, "pucW"))) {
    return "git options that print file contents (-p, --patch, -U, --word-diff, -L ...) are not permitted. Use --stat or --name-status, then Read the files you need.";
  }
  if (sub === "diff" && !flags.some((t) => DIFF_SUMMARY.test(t))) {
    return "`git diff` must use a summary form: --stat, --name-status, --name-only, --numstat, or --shortstat. Read the files you need afterwards.";
  }
  return null;
}

function checkNode(tokens, cwd) {
  const usage = "the only script permitted is the plugin's archive script: node \"<skill dir>/scripts/archive.js\" \"<project root>\" [--dry-run].";
  if (tokens.length < 3 || !samePath(tokens[1], ARCHIVE_SCRIPT, cwd)) return usage;
  const rest = tokens.slice(2);
  const roots = rest.filter((t) => !t.startsWith("-"));
  const flags = rest.filter((t) => t.startsWith("-"));
  if (roots.length !== 1 || flags.some((f) => f !== "--dry-run")) return usage;
  return null;
}

function checkBash(command, cwd) {
  const tokens = tokenize(command);
  if (!tokens || !tokens.length) {
    return "only one plain command at a time is permitted: no pipes, redirection, chaining, substitution, or unquoted backslashes (quote Windows paths).";
  }
  const program = tokens[0].toLowerCase();
  if (program === "git") return checkGit(tokens);
  if (program === "node" || program === "node.exe") return checkNode(tokens, cwd);
  return "only `git status`, `git diff --stat`, `git log`, `git ls-files`, and the plugin's archive script are permitted. Use Glob or `git ls-files` to find files.";
}

// ---------- decision ----------

function decide(input) {
  if (!isArchiver(input.agent_type)) return allow();

  const tool = input.tool_name;
  const args = input.tool_input || {};
  const cwd = input.cwd;

  if (tool === "Read") {
    if (typeof args.file_path === "string" && isSecretFile(args.file_path)) {
      return block("reading " + SECRET_TEXT + " is not permitted.");
    }
    return allow();
  }

  if (tool === "Grep") {
    return block("content search is not available to this agent. Use Glob or `git ls-files` to find files, then Read the ones you need.");
  }

  if (tool === "Write" || tool === "Edit" || tool === "MultiEdit" || tool === "NotebookEdit") {
    const target = args.file_path || args.notebook_path;
    if (typeof target !== "string" || !isMemoryPath(target, cwd)) {
      return block("writes are limited to docs/project-memory/**. Report the operation as incomplete instead of writing elsewhere.");
    }
    return allow();
  }

  if (tool === "Bash") {
    const reason = typeof args.command === "string" ? checkBash(args.command, cwd) : "missing command.";
    if (reason) return block(reason);
    return allow();
  }

  return allow();
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  raw += chunk;
});
process.stdin.on("end", () => {
  try {
    decide(JSON.parse(raw));
  } catch (err) {
    allow();
  }
});
process.stdin.on("error", allow);
