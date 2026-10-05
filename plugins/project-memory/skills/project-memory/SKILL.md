---
name: project-memory
description: Maintain compact, evidence-based memory for software projects across sessions. Use whenever a user initializes or resumes a project, asks for project status or history, completes an approved spec or plan, finishes verified implementation work, or needs to synchronize/archive docs/project-memory; also use when docs/project-memory already exists and substantial project work is about to begin. Trigger on "project memory", "init project memory", "sync memory", "archive memory", "project status", or "where did we leave off". Do not use for one-off questions unrelated to an ongoing software project.
argument-hint: "[init | load | sync | archive | status]"
metadata:
  version: "0.2.0"
---

# Project Memory

Maintain a small, trustworthy continuity layer for software projects. The repository remains the source of truth; memory summarizes evidence and links to detailed artifacts.

## Boundaries

- Apply only to software projects.
- Store project data only under `docs/project-memory/`.
- Cooperate with every applicable process skill; never replace or bypass a required gate such as design approval, verification, or review.
- Never create a project-memory directory merely because a repository was visited. Initialize only when the user requests tracking or accepts it.
- Never modify source code, manifests, tests, or unrelated documentation during a memory-only operation.

Read `references/memory-schema.md` (in this skill's directory) before `init`, `sync`, or `archive`.

## Select the Operation

Honor an explicit operation first. When this skill is invoked as a slash command, the text after the command is the operation; when the command is invoked with no text, use `status`.

- `init`: create memory for a new or existing software project.
- `load`: silently load active memory before substantial work.
- `sync`: update memory after a verified or approved milestone.
- `archive`: bound active history without losing records.
- `status`: report current phase, objective, blockers, verification, and next steps.

Infer an operation only when intent is clear:

- Existing `docs/project-memory/` plus substantial resumed work -> `load`.
- Approved spec, completed plan, verified implementation, or completed branch -> `sync`.
- More than ten active sessions, inactive decisions, or an explicit history cleanup request -> `archive`.
- Questions about project progress, state, continuity, or history -> `status`.

If memory is absent during `load` or `status`, say so and offer `init`. Do not block an unrelated task.

## Evidence Hierarchy

Resolve conflicts in this order:

1. Current user instructions.
2. Recent verification output from the current session.
3. Repository code, manifests, tests, and configuration.
4. Approved specs and plans.
5. Existing project memory.
6. Archived history.

Mark unsupported values `unknown`. Do not turn assumptions into facts. Treat user-supplied test results as supplied evidence unless they were run in the current session.

## Load

1. Confirm `docs/project-memory/` exists.
2. Read `overview.md`, `architecture.md`, `status.md`, `decisions.md`, and `sessions.md` when present. A session-start notice may already have supplied `status.md`; read the other four regardless.
3. Do not read `archive/` unless answering a specific historical question.
4. Internalize the context without emitting a ceremonial summary.
5. Continue with normal repository exploration and the applicable task process.

Memory is orientation, not a substitute for reading the implementation relevant to the task.

## Initialize

Inspect enough evidence to describe the project accurately without cataloging every file:

1. Read project instructions (`CLAUDE.md`, `AGENTS.md`) and primary README files.
2. Read manifests and discover confirmed build, test, and run commands.
3. Inspect entry points, major component directories, persistence boundaries, and integrations.
4. Inspect representative tests and current specs/plans when present.
5. Use non-destructive Git status/history queries when available.
6. Prepare all five active documents from `references/memory-schema.md`.

For a planned project, use the approved spec as evidence and mark implementation facts `unknown`. For an existing project, describe observed components and current state rather than reconstructing a speculative history.

## Synchronize

Synchronize only at a substantive checkpoint. Do not log individual edits.

1. Gather an evidence bundle containing only what the milestone needs:
   - Current objective and outcome.
   - Changed components or repository-relative paths.
   - Verification commands and their recent results.
   - Approved spec and plan paths, when they exist.
   - Durable decisions created, superseded, or deprecated.
   - Known blockers and next steps.
   - Whether each piece of evidence was observed in the current session or supplied by the user.
2. Compare the milestone with active memory and the repository.
3. Rewrite `overview.md`, `architecture.md`, or `status.md` only where current reality changed.
4. Add or update a decision only when a durable choice warrants it.
5. Append exactly one session entry for the milestone.
6. Archive after synchronization if active history exceeds its bounds.

Link specs and plans. Do not copy their contents, mirror todos, paste diffs, or preserve verbose command output.

## Archive

Archive with the script in this skill's directory. It performs the lossless move procedure from `references/memory-schema.md` and removes an entry from an active file only after re-reading it, complete, from its archive file.

```text
node "<skill dir>/scripts/archive.js" "<project root>" --dry-run
node "<skill dir>/scripts/archive.js" "<project root>"
```

1. Run it with `--dry-run` and read the JSON plan.
2. Run it without `--dry-run`.
3. Read the JSON report. `status` is `complete`, `nothing-to-do`, or `incomplete`.
4. On `incomplete`, report the listed `conflicts` or `error` and leave the affected entries active. Never delete an entry by hand to finish the job.

Only when `node` is unavailable, perform the same procedure by hand from `references/memory-schema.md`.

The rules the script applies:

- Keep the newest ten sessions active.
- Move older sessions to `archive/sessions-YYYY.md`.
- Keep governing accepted decisions active.
- Move superseded and deprecated decisions to `archive/decisions-YYYY.md`.
- Verify the destination contains each complete entry before removing it from the active file.
- Do not touch files outside `docs/project-memory/`.

## Status and Historical Questions

For current status, read active memory and report:

- Phase and active objective.
- Completed and in-progress work.
- Blockers and next steps.
- Last verification and synchronization scope.
- Any obvious staleness or contradiction discovered from evidence already in context.

For a historical question, search archive headings first, then read only matching entries. Distinguish historical state from current state.

## Writing Contract

For `init`, `sync`, or `archive`, dispatch the `project-archiver` subagent (listed as `project-memory:project-archiver`) whenever it is available in the current session, however small the project or the change. Do not write memory files directly while the subagent is available. Give it:

- The operation.
- The absolute project root.
- The evidence bundle.
- Applicable spec/plan paths.
- The expected output files.
- The absolute path of this skill's directory, so it reads the same `references/memory-schema.md` and runs the same `scripts/archive.js`.

The archiver cannot search file contents and cannot run project commands. For `init` and `sync`, do the exploration and run the verification yourself, then put the repository-relative paths and results it must check in the evidence bundle. It verifies by reading those files.

`load` and `status` are read-only; perform them directly and never delegate them.

If subagent delegation is unavailable, perform the same bounded operation directly:

- Apply the exact schema.
- Use `scripts/archive.js` for any archival.
- Verify evidence before writing.
- Restrict edits to `docs/project-memory/**`.
- Preserve unrelated worktree changes.
- Report incomplete work instead of inventing missing facts.

This fallback keeps memory usable in environments without subagents, or where a newly installed agent loads only after restart.

## Safety

- Exclude secrets, tokens, credentials, private keys, `.env` values, and sensitive personal data.
- In a dirty worktree, include only changes demonstrably related to the current objective.
- Never rewrite archived history silently. Add a labeled correction note when needed.
- Never claim synchronization, verification, or archival succeeded without checking the resulting files.
- If required evidence is missing, leave the operation incomplete and state exactly what could not be verified.

## Completion Report

After a write operation, report only:

- Operation performed.
- Memory files created or changed.
- Evidence used and whether it was observed or user-supplied.
- Decisions or sessions added/moved.
- Unknowns, contradictions, or incomplete work.
