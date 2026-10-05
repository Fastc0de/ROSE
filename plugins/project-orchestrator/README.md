# project-orchestrator

A conversational orchestrator for software projects. You talk with it about the project and its ideas; it reads the code, writes specs and plans under `docs/`, and then runs the plan by delegating to subagents. It works together with the `project-memory` plugin.

## Start it

**Per project (recommended, works in the desktop app and the terminal).** In a session opened in the project folder, run:

```
/project-orchestrator:orchestrate on
```

From then on every new session in that project starts as the orchestrator, with its limits enforced by the guard. Other projects are unaffected. Turn it off with `/project-orchestrator:orchestrate off`, and check it with `/project-orchestrator:orchestrate status`. The switch applies to sessions started after it, not to the one already running.

It works by setting `"agent": "project-orchestrator:orchestrator"` in the project's `.claude/settings.local.json`, which is personal to you. Keep that file out of version control. To turn the mode on for everyone who clones the repository, put the same line in `.claude/settings.json` by hand instead.

**For one session only.** Type `/` and choose `orchestrate`, optionally followed by what you want to do:

```
/project-orchestrator:orchestrate
```

In this form the orchestrator's own limits are instructions only; see "Guard" below.

**From the terminal.** `claude --agent project-orchestrator:orchestrator` starts a single session as the orchestrator.

## How it works

Two modes, and it says which one it is in:

- **Conversation mode** (default). Discuss, explore the code, capture ideas in `docs/ideas.md`, write the spec and then the plan. Nothing outside `docs/` changes.
- **Execution mode**. Entered only when a plan is approved and you tell it to run. For each task: implementer, then verifier, then the result is recorded in the plan. It stops when the agreed stride is finished (one task, one milestone, or until blocked), when a task fails three verifications, or when the verifier reports that the plan itself is wrong.

State lives in files, so a new session picks up where the last one stopped:

| File | Written by | Purpose |
|---|---|---|
| `docs/specs/YYYY-MM-DD-<topic>-design.md` | orchestrator | What and why; requirements and acceptance criteria |
| `docs/plans/YYYY-MM-DD-<topic>-plan.md` | orchestrator | Milestones and tasks with status; the contract for subagents |
| `docs/ideas.md` | orchestrator | Ideas to keep but not build yet |
| `docs/project-memory/**` | project-memory archiver | Continuity across sessions |

Specs and plans need your explicit approval before their status becomes `approved`.

Each plan records a commit policy. With `Commits: per-task` the implementer commits each task and the orchestrator commits `docs/` after each verified milestone. With `Commits: none` nothing is committed. Nothing is ever pushed.

## Components

| Component | Role |
|---|---|
| `agents/orchestrator.md` | Main-session agent. Converses, reads code, writes docs, delegates. Never edits code. |
| `agents/explorer.md` | Read-only investigator for broad questions about the codebase. Model: `sonnet`. |
| `agents/implementer.md` | Implements one plan task with its tests. Model: inherits the session's. |
| `agents/verifier.md` | Independently checks one task and returns `pass`, `fail`, or `plan-defect`. Never fixes. Model: inherits the session's. |
| `skills/orchestrate/` | Switches orchestrator mode on or off for a project, or loads the role into the current session. |
| `scripts/mode.js` | The switch itself: sets or removes the `agent` line, preserving every other setting. |
| `references/` | Spec and plan templates. |
| `hooks/` | Guard that enforces each agent's limits. |

To change a subagent's model, edit the `model` line in its file.

## Guard

`hooks/scripts/role-guard.js` runs before Read, Write, Edit, and Bash calls and acts only on this plugin's agents:

| Agent | Write | Bash |
|---|---|---|
| orchestrator | only `docs/**`, never `docs/project-memory/**` | read-only git (`status`, `diff`, `log`, `show`, `ls-files`, `rev-parse`), `git add` and `git commit` limited to `docs/` paths, and this plugin's own mode switch |
| explorer | nothing | read-only git |
| implementer | anything except `docs/specs/**`, `docs/plans/**`, `docs/project-memory/**` | anything except `git push` and shell writes into those three folders |
| verifier | nothing | anything except git commands that change state |

None of them can Read `.env`, `.env.*`, `*.pem`, or `*.key` (example files such as `.env.example` are allowed).

Limits of the guard:

- The orchestrator is identified by the session's agent. Loaded through the skill in a normal session, the main session has no agent identity, so the orchestrator's limits are then only instructions. Subagents are always covered.
- Bash rules for the implementer and verifier are pattern checks, not a sandbox. Reading a secret through a shell command is not blocked.
- It needs `node` on the PATH. If the script fails for any reason it allows the call, so a broken guard never blocks work.

Run the tests with `node --test tests/role-guard.test.js tests/mode.test.js`.

## With project-memory

When the `project-memory` plugin is installed the orchestrator uses its skill: `load` at session start, `init` (offered once, after the first spec and plan are approved), and `sync` after a plan is approved and after each verified milestone. Without it, the orchestrator says so once and relies on specs and plans alone.

The spec and plan paths match the ones the project-memory schema links to.
