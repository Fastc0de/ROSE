#!/usr/bin/env node
"use strict";
// Lossless archive for docs/project-memory/.
//
//   node archive.js <project-root> [--dry-run]
//
// Sessions:  keeps the newest ten in sessions.md, moves the rest to
//            archive/sessions-YYYY.md (year of the session date).
// Decisions: moves entries whose status is superseded or deprecated to
//            archive/decisions-YYYY.md (year of the decision date).
//
// An entry is removed from the active file only after it has been re-read,
// complete, from its archive file. Entry text is never rewritten.
// Prints a JSON report. Exit 0 = complete or nothing to do. Exit 1 = incomplete.

const fs = require("fs");
const path = require("path");

const KEEP_SESSIONS = 10;
const INACTIVE = new Set(["superseded", "deprecated"]);

// ---------- text helpers ----------

function readText(file) {
  let text = fs.readFileSync(file, "utf8");
  const bom = text.charCodeAt(0) === 0xfeff;
  if (bom) text = text.slice(1);
  return { text, bom, eol: text.includes("\r\n") ? "\r\n" : "\n" };
}

function writeAtomic(file, content) {
  const tmp = file + ".tmp-" + process.pid;
  fs.writeFileSync(tmp, content, "utf8");
  fs.renameSync(tmp, file);
}

function trimBlank(lines) {
  const out = lines.slice();
  while (out.length && out[out.length - 1].trim() === "") out.pop();
  return out;
}

// Splits a document into the lines before the first level-two heading and
// one entry per level-two heading. Headings inside code fences are ignored.
function parse(text) {
  const lines = text.split(/\r?\n/);
  const preamble = [];
  const entries = [];
  let current = null;
  let fence = null;
  for (const line of lines) {
    const mark = line.match(/^\s*(```+|~~~+)/);
    if (mark) {
      const kind = mark[1][0];
      if (fence === null) fence = kind;
      else if (fence === kind) fence = null;
    }
    if (fence === null && !mark && /^## (?!#)/.test(line)) {
      current = { heading: line.trim(), lines: [line] };
      entries.push(current);
    } else if (current) {
      current.lines.push(line);
    } else {
      preamble.push(line);
    }
  }
  for (const entry of entries) entry.lines = trimBlank(entry.lines);
  return { preamble: trimBlank(preamble), entries };
}

function fingerprint(entry) {
  return entry.lines.map((l) => l.replace(/\s+$/, "")).join("\n").trim();
}

function render(preamble, entries, eol, bom) {
  const blocks = [];
  if (preamble.length) blocks.push(preamble.join(eol));
  for (const entry of entries) blocks.push(entry.lines.join(eol));
  return (bom ? "﻿" : "") + blocks.join(eol + eol) + eol;
}

// ---------- entry classification ----------

function sessionInfo(entry) {
  const m = entry.heading.match(/^## Session (\d{4})-(\d{2})-(\d{2})(?=\D|$)/);
  if (!m) return null;
  return { key: entry.heading, year: m[1], date: m[1] + "-" + m[2] + "-" + m[3] };
}

function decisionInfo(entry) {
  const m = entry.heading.match(/^## (PM-ADR-\d+)(?=\D|$)/);
  if (!m) return null;
  let status = null;
  let year = null;
  for (const line of entry.lines) {
    if (status === null) {
      const s = line.match(/^\s*-\s*Status:\s*([A-Za-z]+)\s*$/);
      if (s) status = s[1].toLowerCase();
    }
    if (year === null) {
      const d = line.match(/^\s*-\s*Date:\s*(\d{4})-\d{2}-\d{2}/);
      if (d) year = d[1];
    }
  }
  return { key: m[1], status, year };
}

// ---------- the move ----------

// candidates: [{ entry, key, year }] in the order they should be archived.
// keyOf: maps an archive entry to its identity (heading or decision ID).
// Returns which candidates are safely preserved and may leave the active file.
function moveToArchive(kind, candidates, keyOf, ctx) {
  const result = { moved: [], alreadyArchived: [], conflicts: [], preserved: new Set() };
  const byYear = new Map();
  for (const c of candidates) {
    if (!byYear.has(c.year)) byYear.set(c.year, []);
    byYear.get(c.year).push(c);
  }

  for (const [year, group] of byYear) {
    const rel = "archive/" + kind + "-" + year + ".md";
    const file = path.join(ctx.memoryDir, "archive", kind + "-" + year + ".md");
    const exists = fs.existsSync(file);
    const existing = exists ? readText(file) : null;
    const eol = existing ? existing.eol : ctx.eol;
    const known = new Map();
    if (existing) {
      for (const e of parse(existing.text).entries) known.set(keyOf(e), fingerprint(e));
    }

    const toAppend = [];
    for (const c of group) {
      if (known.has(c.key)) {
        if (known.get(c.key) === fingerprint(c.entry)) {
          result.alreadyArchived.push({ entry: c.entry.heading, in: rel });
          result.preserved.add(c.entry);
        } else {
          result.conflicts.push({
            entry: c.entry.heading,
            in: rel,
            reason: "an entry with the same identity but different text already exists; left active",
          });
        }
      } else {
        toAppend.push(c);
        known.set(c.key, fingerprint(c.entry));
      }
    }
    if (!toAppend.length) continue;

    if (!ctx.dryRun) {
      const title = kind === "sessions" ? "# Archived Sessions " + year : "# Archived Decisions " + year;
      const head = existing ? existing.text.replace(/\s+$/, "") : title;
      const body = toAppend.map((c) => c.entry.lines.join(eol)).join(eol + eol);
      fs.mkdirSync(path.dirname(file), { recursive: true });
      writeAtomic(file, (existing && existing.bom ? "﻿" : "") + head + eol + eol + body + eol);

      // Verify from disk before anything is removed from the active file.
      const check = new Map();
      for (const e of parse(readText(file).text).entries) check.set(keyOf(e), fingerprint(e));
      for (const c of toAppend) {
        if (check.get(c.key) !== fingerprint(c.entry)) {
          throw new Error("verification failed for \"" + c.entry.heading + "\" in " + rel + "; active files were not changed");
        }
      }
      ctx.filesChanged.add("docs/project-memory/" + rel);
    }
    for (const c of toAppend) {
      result.moved.push({ entry: c.entry.heading, to: rel });
      result.preserved.add(c.entry);
    }
  }
  return result;
}

function archiveSessions(ctx) {
  const file = path.join(ctx.memoryDir, "sessions.md");
  const report = { activeBefore: 0, activeAfter: 0, moved: [], alreadyArchived: [], conflicts: [] };
  if (!fs.existsSync(file)) {
    ctx.warnings.push("sessions.md not found; skipped");
    return report;
  }
  const src = readText(file);
  const doc = parse(src.text);
  report.activeBefore = report.activeAfter = doc.entries.length;

  const infos = doc.entries.map((entry, index) => ({ entry, index, info: sessionInfo(entry) }));
  const bad = infos.filter((x) => !x.info);
  if (bad.length) {
    throw new Error(
      "sessions.md has level-two headings that are not `## Session YYYY-MM-DD: title`: " +
        bad.map((x) => x.entry.heading).join(" | ") + "; nothing was changed"
    );
  }
  const sorted = infos.slice().sort((a, b) =>
    a.info.date < b.info.date ? -1 : a.info.date > b.info.date ? 1 : a.index - b.index
  );
  if (sorted.some((x, i) => x.index !== i)) {
    ctx.warnings.push("sessions.md is not in chronological order; oldest entries were chosen by date and the file order was kept");
  }
  if (sorted.length <= KEEP_SESSIONS) return report;

  const candidates = sorted
    .slice(0, sorted.length - KEEP_SESSIONS)
    .map((x) => ({ entry: x.entry, key: x.info.key, year: x.info.year }));
  const res = moveToArchive("sessions", candidates, (e) => e.heading, { ...ctx, eol: src.eol });
  const remaining = doc.entries.filter((e) => !res.preserved.has(e));
  if (!ctx.dryRun && res.preserved.size) {
    writeAtomic(file, render(doc.preamble, remaining, src.eol, src.bom));
    ctx.filesChanged.add("docs/project-memory/sessions.md");
  }
  report.activeAfter = remaining.length;
  report.moved = res.moved;
  report.alreadyArchived = res.alreadyArchived;
  report.conflicts = res.conflicts;
  return report;
}

function archiveDecisions(ctx) {
  const file = path.join(ctx.memoryDir, "decisions.md");
  const report = { activeBefore: 0, activeAfter: 0, moved: [], alreadyArchived: [], conflicts: [] };
  if (!fs.existsSync(file)) {
    ctx.warnings.push("decisions.md not found; skipped");
    return report;
  }
  const src = readText(file);
  const doc = parse(src.text);
  report.activeBefore = report.activeAfter = doc.entries.length;

  const candidates = [];
  for (const entry of doc.entries) {
    const info = decisionInfo(entry);
    if (!info || !INACTIVE.has(info.status)) continue;
    if (!info.year) {
      report.conflicts.push({ entry: entry.heading, reason: "no `- Date: YYYY-MM-DD` line; left active" });
      continue;
    }
    candidates.push({ entry, key: info.key, year: info.year });
  }
  if (!candidates.length) return report;

  const keyOf = (e) => {
    const info = decisionInfo(e);
    return info ? info.key : e.heading;
  };
  const res = moveToArchive("decisions", candidates, keyOf, { ...ctx, eol: src.eol });
  const remaining = doc.entries.filter((e) => !res.preserved.has(e));
  if (!ctx.dryRun && res.preserved.size) {
    writeAtomic(file, render(doc.preamble, remaining, src.eol, src.bom));
    ctx.filesChanged.add("docs/project-memory/decisions.md");
  }
  report.activeAfter = remaining.length;
  report.moved = res.moved;
  report.alreadyArchived = res.alreadyArchived;
  report.conflicts = report.conflicts.concat(res.conflicts);
  return report;
}

// ---------- entry point ----------

function main(argv) {
  const args = argv.slice(2);
  const dryRun = args.includes("--dry-run");
  const positional = args.filter((a) => !a.startsWith("--"));
  const unknown = args.filter((a) => a.startsWith("--") && a !== "--dry-run");
  const report = { status: "incomplete", dryRun, sessions: null, decisions: null, filesChanged: [], warnings: [] };

  const finish = (code) => {
    process.stdout.write(JSON.stringify(report, null, 2) + "\n");
    process.exit(code);
  };

  if (positional.length !== 1 || unknown.length) {
    report.error = "usage: node archive.js <project-root> [--dry-run]";
    return finish(1);
  }
  const memoryDir = path.join(path.resolve(positional[0]), "docs", "project-memory");
  if (!fs.existsSync(memoryDir) || !fs.statSync(memoryDir).isDirectory()) {
    report.error = "no docs/project-memory/ directory under " + path.resolve(positional[0]);
    return finish(1);
  }

  const ctx = { memoryDir, dryRun, filesChanged: new Set(), warnings: report.warnings };
  try {
    report.sessions = archiveSessions(ctx);
    report.decisions = archiveDecisions(ctx);
  } catch (err) {
    report.error = err.message;
    report.filesChanged = Array.from(ctx.filesChanged);
    return finish(1);
  }
  report.filesChanged = Array.from(ctx.filesChanged);

  const moved = report.sessions.moved.length + report.sessions.alreadyArchived.length +
    report.decisions.moved.length + report.decisions.alreadyArchived.length;
  const conflicts = report.sessions.conflicts.length + report.decisions.conflicts.length;
  if (conflicts || report.sessions.activeAfter > KEEP_SESSIONS) report.status = "incomplete";
  else report.status = moved ? "complete" : "nothing-to-do";
  return finish(report.status === "incomplete" ? 1 : 0);
}

main(process.argv);
