# Plan Template

Path: `docs/plans/YYYY-MM-DD-<topic>-plan.md`

The plan is the contract with the implementer and verifier, and the only record of execution state. Keep the headings, field names, and status keywords exactly as written so any agent can read the state.

```markdown
# <Topic> Plan

- Status: draft | approved | in-progress | blocked | done
- Spec: `docs/specs/YYYY-MM-DD-<topic>-design.md` | none
- Created: YYYY-MM-DD
- Approved: YYYY-MM-DD | pending
- Commits: per-task | none
- Full check: `<command that runs the whole test suite>` | unknown

## Context
Three to six lines an implementer needs before any task: stack, layout, conventions, and constraints that apply to every task.

## M1: <outcome the user can see or run>

### T1: <imperative title>
- Status: todo | in-progress | implemented | verified | blocked
- Depends on: none
- Covers: R1, AC1
- Files: `path/or/area`
- Do: what to build, precisely enough to implement without this conversation.
- Done when: observable result.
- Verify: `<command>` | unknown
- Attempts: 0
- Notes:

### T2: ...

## M2: ...
```

## Rules

- Task IDs are unique across the plan and never reused or renumbered. New work gets the next ID.
- `Depends on` lists task IDs. A task may start only when all of them are `verified`.
- `Covers` lists the requirement and acceptance-criterion IDs from the spec. Every requirement is covered by at least one task.
- `Done when` is observable; `Verify` is a command the verifier can run. For a new project, the first task establishes the build and test commands.
- `Attempts` counts failed verifications. A task that fails three times becomes `blocked`.
- `Notes` holds one line per event: the verifier's evidence on a pass, the findings on a fail, or the reason for a block.
- Only the orchestrator edits this file. The plan `Status` follows its tasks: `in-progress` once any task starts, `blocked` while a task is blocked, `done` when all are `verified`.
- `Status: approved` and the `Approved` date are set only after the user explicitly approves.
