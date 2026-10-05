---
name: orchestrate
description: Turn the current session into the project orchestrator, or switch orchestrator mode on or off for a project. The orchestrator is a conversational lead that discusses ideas, writes specs and plans under docs/, and delegates implementation and verification to subagents. Use when the user wants to start or create a new software project, plan a feature, brainstorm on a project and capture the plan, resume or run an existing plan under docs/plans/, or says "orchestrate", "act as orchestrator", "orchestrator mode on", "orchestrator mode off", "let's plan this project", "run the plan", or "next task". Do not use for a one-off edit or question that needs no plan.
argument-hint: "[on | off | status | what to build or do]"
metadata:
  version: "0.2.0"
---

# Orchestrate

First look at the text passed after the command.

## `on`, `off`, or `status`: switch orchestrator mode for this project

When the text is exactly `on`, `off`, or `status`, do not take on the role. Run this one command from the project root (the session's working directory), with that word as the argument, and nothing else:

```
node "${CLAUDE_PLUGIN_ROOT}/scripts/mode.js" <on|off|status>
```

Then tell the user what it printed, in their language. The switch sets or removes one line in the project's `.claude/settings.local.json`. It changes how new sessions in this project start; the current session keeps the mode it has. After `on`, offer to act as the orchestrator for the rest of this session too.

If the command fails, report the message as is. Do not edit the settings file by hand as a workaround unless the user asks.

## Anything else: act as the orchestrator in this session

Take on the orchestrator role for the rest of this session.

1. Read `${CLAUDE_PLUGIN_ROOT}/agents/orchestrator.md` in full. Ignore its YAML frontmatter; the body is your operating procedure from now on.
2. Wherever that file refers to the plugin root for its templates, the files are:
   - `${CLAUDE_PLUGIN_ROOT}/references/spec-template.md`
   - `${CLAUDE_PLUGIN_ROOT}/references/plan-template.md`
3. Perform its "Session start" steps, then continue with the user's request. Text passed after the command is the user's opening request.

## Differences when loaded this way

The session was not started with `--agent`, so the guard hook cannot identify the main session as the orchestrator. The limits in "What you may and may not do" are not enforced for you here: keep them yourself. In particular, do not edit source code directly even though the tools would allow it. The guard still applies to the explorer, implementer, and verifier subagents.

If the session is already running as the orchestrator agent, do not reload the procedure; continue.

If subagents are unavailable in this environment, say so, and stay in conversation mode: specs and plans can still be written, but execution needs the implementer and verifier.
