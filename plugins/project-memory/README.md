# project-memory

Compact, evidence-based memory for software projects across sessions. A port of the OpenCode `project-memory` system to a Claude plugin. Same file layout and schema, so a repository's `docs/project-memory/` works with both tools.

The repository stays the source of truth. Memory summarizes evidence and links to detailed artifacts; it never replaces reading the code.

## Components

| Component | Name | Purpose |
| --- | --- | --- |
| Skill | `project-memory` | The five operations (`init`, `load`, `sync`, `archive`, `status`), the evidence hierarchy, and the safety rules. Also the slash command. |
| Reference | `references/memory-schema.md` | Exact schema of the five active files and the archive procedure. |
| Script | `scripts/archive.js` | Deterministic, lossless archive. |
| Agent | `project-archiver` | Bounded writer for `init`, `sync`, and `archive`. Verifies evidence, then writes only under `docs/project-memory/`. |
| Hook | `session-start` | Puts the project's `status.md` in context when a session starts. |
| Hook | `archiver-guard` | Enforces the archiver's limits. Does nothing outside that agent. |

## Usage

```text
/project-memory:project-memory init      create memory for this repository
/project-memory:project-memory load      read active memory before substantial work
/project-memory:project-memory sync      record a verified or approved milestone
/project-memory:project-memory archive   move old sessions and inactive decisions to archive/
/project-memory:project-memory status    phase, objective, blockers, next steps
/project-memory:project-memory           same as status
```

The skill never creates the directory unless you ask for tracking.

## Automatic load

When a session starts, resumes, or is compacted in a directory whose project has `docs/project-memory/`, the `session-start` hook adds `status.md` to the context (up to 6,000 characters), marked as project data that may be stale, with a reminder to load the rest before substantial work. It looks in the working directory and its parents, up to the repository root. Projects without memory get nothing.

## What it writes

```text
docs/project-memory/
  overview.md        identity, scope, stack, essential commands
  architecture.md    components, flows, persistence, integrations
  status.md          phase, objective, blockers, next steps
  decisions.md       PM-ADR-NNNN records for durable choices
  sessions.md        one entry per milestone, newest ten kept
  archive/
    sessions-YYYY.md
    decisions-YYYY.md
```

## Archive script

```text
node skills/project-memory/scripts/archive.js <project-root> [--dry-run]
```

- Keeps the newest ten sessions in `sessions.md` and moves the rest to `archive/sessions-YYYY.md`.
- Moves `superseded` and `deprecated` decisions to `archive/decisions-YYYY.md`. `accepted` and `proposed` stay.
- Writes the archive file first, re-reads it, and removes an entry from the active file only if the complete entry is there.
- Never rewrites entry text. Keeps line endings and a byte-order mark as found.
- Running it twice changes nothing the second time.
- If an entry with the same heading or decision ID but different text is already archived, it leaves the entry active and reports a conflict.
- If `sessions.md` has a level-two heading that is not `## Session YYYY-MM-DD: title`, it stops without changing anything.
- Prints a JSON report. Exit code 0 means complete or nothing to do; 1 means incomplete.

You can run it yourself; it needs only `node`.

## Archiver limits

The original OpenCode agent declared its permissions in frontmatter. Claude has no per-path permissions for plugin agents, so the limits are split in two:

- The agent's tool list: `Read`, `Glob`, `Write`, `Edit`, `Bash`. No content search, no skills, no web, no delegation, no questions.
- The guard hook, which applies only to calls made by `project-archiver`:
  - `Write` / `Edit` outside `docs/project-memory/` is blocked.
  - `Read` of `.env`, `.env.*`, `*.pem`, `*.key` is blocked.
  - `Grep` is blocked.
  - `Bash` is limited to single plain commands:
    - `git status` (not `-v`), `git log`, `git ls-files`.
    - `git diff` only with `--stat`, `--name-status`, `--name-only`, `--numstat`, or `--shortstat`.
    - No git option that prints file contents (`-p`, `--patch`, `-U`, `--word-diff`, `-L`), writes a file (`--output`), or reads outside the repository (`--no-index`).
    - The plugin's own `archive.js` with a project root and optional `--dry-run`.

The archiver therefore sees which files changed, never a diff, and reads content only through `Read`, where the secret-file block applies. Because it cannot search or run project commands, the skill does the exploration and passes the paths to verify.

Both hooks need `node` on the PATH. If it is missing or a hook fails, calls are allowed and nothing is loaded at session start; the agent's written instructions remain the only limit. The guard relies on the `agent_type` field Claude Code passes to hooks; in an environment that does not send it, the guard is inactive.

## Setup

No environment variables or connectors. The archiver runs on `sonnet`; change `model` in `agents/project-archiver.md` to `inherit`, `opus`, or `haiku` if you prefer.

## Differences from the OpenCode version

- The `/project-memory` command is folded into the skill, which accepts the operation as its argument.
- `status.md` is loaded automatically at session start.
- Archival is done by a script instead of by the model.
- The archiver has no content search, and its git access is narrower: change listings only, where OpenCode allowed any `git diff` and `git log`.
- The archiver model is `sonnet` instead of `openai/gpt-5.6-luna`.
- Permissions are enforced by the guard hook instead of agent frontmatter.

## Changelog

- 0.2.0: automatic load at session start; `archive.js`; guard covers `Grep` and content-printing git options; `Skill` and `Grep` removed from the archiver.
- 0.1.0: initial port.
