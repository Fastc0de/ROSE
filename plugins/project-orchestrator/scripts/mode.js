#!/usr/bin/env node
// Turns orchestrator mode on or off for one project by setting or removing the
// `agent` key in that project's .claude/settings.local.json.
//
//   node mode.js on      every new session in this project starts as the orchestrator
//   node mode.js off     new sessions start as a normal Claude Code session again
//   node mode.js status  report the current setting
//
// The project is the current working directory. Only the `agent` key is touched;
// every other setting in the file is preserved. The change applies to sessions
// started after it, not to the one that is already running.

const fs = require("fs");
const path = require("path");

const AGENT = "project-orchestrator:orchestrator";

function readJson(file) {
  if (!fs.existsSync(file)) return { exists: false, data: {} };
  const text = fs.readFileSync(file, "utf8").replace(/^﻿/, "");
  if (text.trim() === "") return { exists: true, data: {} };
  const data = JSON.parse(text);
  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    throw new Error("the file does not contain a JSON object");
  }
  return { exists: true, data };
}

function isOurs(value) {
  return value === AGENT || value === "orchestrator";
}

function run(operation, projectDir) {
  const dir = path.join(projectDir, ".claude");
  const localFile = path.join(dir, "settings.local.json");
  const sharedFile = path.join(dir, "settings.json");
  const shown = ".claude/settings.local.json";

  let local;
  try {
    local = readJson(localFile);
  } catch (err) {
    return { code: 1, message: "Could not read " + shown + " (" + err.message + "). Nothing was changed; fix the file and try again." };
  }

  let sharedAgent;
  try {
    sharedAgent = readJson(sharedFile).data.agent;
  } catch (err) {
    sharedAgent = undefined;
  }

  const current = local.data.agent;

  if (operation === "status") {
    const lines = [];
    if (isOurs(current)) lines.push("Orchestrator mode is ON for this project (" + shown + ").");
    else if (current !== undefined) lines.push("Orchestrator mode is OFF. " + shown + " sets a different agent: " + JSON.stringify(current) + ".");
    else if (isOurs(sharedAgent)) lines.push("Orchestrator mode is ON for this project, set in the shared .claude/settings.json.");
    else lines.push("Orchestrator mode is OFF for this project.");
    if (sharedAgent !== undefined && !isOurs(sharedAgent)) {
      lines.push("The shared .claude/settings.json sets agent " + JSON.stringify(sharedAgent) + ".");
    }
    lines.push("Project: " + projectDir);
    return { code: 0, message: lines.join("\n") };
  }

  if (operation === "on") {
    if (isOurs(current)) {
      return { code: 0, message: "Orchestrator mode is already ON for this project. Nothing was changed." };
    }
    if (current !== undefined) {
      return { code: 1, message: shown + " already sets a different agent (" + JSON.stringify(current) + "). Nothing was changed; remove that line first if you want the orchestrator instead." };
    }
    local.data.agent = AGENT;
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(localFile, JSON.stringify(local.data, null, 2) + "\n");
    return { code: 0, message: "Orchestrator mode is ON for this project (" + shown + ").\nIt applies to sessions you start in this folder from now on, not to the current one.\nProject: " + projectDir };
  }

  if (operation === "off") {
    if (current === undefined) {
      const extra = isOurs(sharedAgent) ? " Note: the shared .claude/settings.json still sets the orchestrator; remove the agent line there to turn it off for everyone." : "";
      return { code: 0, message: "Orchestrator mode is already OFF in " + shown + ". Nothing was changed." + extra };
    }
    if (!isOurs(current)) {
      return { code: 1, message: shown + " sets a different agent (" + JSON.stringify(current) + "), not the orchestrator. Nothing was changed." };
    }
    delete local.data.agent;
    fs.writeFileSync(localFile, JSON.stringify(local.data, null, 2) + "\n");
    return { code: 0, message: "Orchestrator mode is OFF for this project.\nIt applies to sessions you start in this folder from now on; the current session keeps its mode until it ends.\nProject: " + projectDir };
  }

  return { code: 1, message: "Usage: node mode.js on | off | status" };
}

if (require.main === module) {
  const result = run(process.argv[2], process.cwd());
  (result.code === 0 ? process.stdout : process.stderr).write(result.message + "\n");
  process.exit(result.code);
} else {
  module.exports = { run, AGENT };
}
