# Project Memory Schema

Use these schemas exactly so active memory stays predictable across projects and agents.

## General Conventions

- Store memory in `docs/project-memory/`.
- Use repository-relative paths in backticks.
- Use `YYYY-MM-DD` dates.
- Write `unknown` when repository evidence or explicit user input is unavailable.
- Never copy secrets, tokens, credentials, private keys, or environment-file values.
- Keep current-state files concise and rewrite them when reality changes.
- Preserve historical entry text when moving it into `archive/`.
- Link approved specs and plans; do not reproduce their bodies.
- Do not include a file-by-file inventory.

## `overview.md`

```markdown
# Project Overview

## Identity
- Name: ...
- Purpose: ...

## Scope
- Repository scope: ...

## Stack
- Runtime/platform: ...
- Primary technologies: ...

## Essential Dependencies
- ...

## Essential Commands
- Build: `...`
- Test: `...`
- Run: `...`

## Constraints
- ...

## Source Documents
- `README.md`
- `package.json`
```

Omit command labels that do not apply. Do not infer versions or deployment targets that are not evidenced.

## `architecture.md`

```markdown
# Project Architecture

## Summary
...

## Components
- Component: boundary and purpose. Evidence: `path/`.

## Main Flows
- Entry -> processing -> output/persistence.

## Persistence
- ...

## External Integrations
- ...

## Dependency Rules
- ...

## Active Risks
- ...

## Evidence
- `path/to/stable/source`
```

Describe components, boundaries, and data flow. Do not document every file, every public method, or speculative architecture.

## `status.md`

```markdown
# Project Status

## Phase
planned | building | maintaining | paused | complete

## Active Objective
...

## Completed
- ...

## In Progress
- ...

## Blockers
- None recorded.

## Next Steps
- ...

## Last Verification
- Command/evidence: `...`
- Result: ...

## Last Synchronization
- Date: YYYY-MM-DD
- Scope: ...
```

Use `complete` for a project or objective that is built and verified, not merely implemented. Record supplied verification as supplied evidence when it was not run in the current session.

## `decisions.md`

```markdown
# Project Decisions

## PM-ADR-0001: Descriptive title

- Status: proposed | accepted | superseded | deprecated
- Date: YYYY-MM-DD
- Scope: ...

### Context
...

### Decision
...

### Consequences
- Positive: ...
- Negative: ...

### Supersedes
PM-ADR-NNNN | none

### Evidence
- `docs/specs/YYYY-MM-DD-topic-design.md`
- `path/to/implementation`
```

Assign the next integer after the highest ID in active and archived decisions. Create a decision only for a durable choice with meaningful alternatives or consequences. Do not create one for routine edits, local bug fixes, formatting, or facts already represented by current-state files.

Accepted decisions remain active while they govern the project. Update status metadata when superseding or deprecating a decision, then archive inactive entries during archival.

## `sessions.md`

```markdown
# Project Sessions

## Session YYYY-MM-DD: Outcome-oriented title

### Objective
...

### Outcome
...

### Changed Areas
- ...

### Verification
- Command/evidence: `...`
- Result: ...

### Links
- `docs/specs/...`
- `docs/plans/...`

### Follow-ups
- ...
```

Keep entries in chronological order, oldest first. Record one entry per substantive milestone, not per command or edit. Keep at most ten entries active.

## Archival

Use:

```text
docs/project-memory/archive/sessions-YYYY.md
docs/project-memory/archive/decisions-YYYY.md
```

Archive sessions when `sessions.md` has more than ten entries:

1. Identify the oldest entries beyond the newest ten.
2. Group them by the year in each entry date.
3. Append each complete entry to its year file unless that exact heading already exists.
4. Remove only successfully preserved entries from `sessions.md`.
5. Confirm `sessions.md` contains exactly the newest ten entries.

Archive decisions whose status is `superseded` or `deprecated`:

1. Group them by decision date year.
2. Append each complete entry to its year file unless its ID already exists.
3. Remove only successfully preserved entries from `decisions.md`.
4. Keep all governing `accepted` decisions active.

Never rewrite historical entry content during a move. If a factual correction is necessary, append a clearly labeled correction note instead of silently changing the archived record.
