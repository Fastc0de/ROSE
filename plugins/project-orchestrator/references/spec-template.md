# Spec Template

Path: `docs/specs/YYYY-MM-DD-<topic>-design.md`

Keep the headings and the `Status` keywords exactly as written. Omit a section only when it truly does not apply, and never fill one with guesses: undecided points go under `Open Questions`.

```markdown
# <Topic> Design

- Status: draft | approved
- Date: YYYY-MM-DD
- Approved: YYYY-MM-DD | pending

## Goal
One or two paragraphs: what is being built or changed, for whom, and why now.

## Scope
### In
- ...
### Out
- ...

## Requirements
- R1: ...
- R2: ...

## Approach
How it will work: components and their boundaries, main flow, data and persistence, external integrations, and the stack with the reason for each non-obvious choice. Reference existing code by repository-relative path.

## Alternatives Considered
- Option: why it was not chosen.

## Risks
- Risk: how it will be reduced or detected early.

## Open Questions
- None.

## Acceptance Criteria
- AC1 (R1): observable result that shows the requirement is met.
```

## Rules

- A requirement states what must be true, not how to build it.
- Every requirement has at least one acceptance criterion, and every criterion is something a verifier can observe or run.
- `Status: approved` and the `Approved` date are set only after the user explicitly approves.
- After approval, change the spec only with the user's agreement, and note the change under a `## Amendments` heading with its date.
