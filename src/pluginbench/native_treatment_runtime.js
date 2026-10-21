// Executed inside the attempt's private environment, before the coding command.
// This uses the CLI's installation and discovery APIs; it never starts a model turn.
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawn, execFileSync } = require("node:child_process");
const readline = require("node:readline");

const [root, cwd] = process.argv.slice(2);
const payload = JSON.parse(fs.readFileSync(path.join(root, "treatment.json"), "utf8"));
const codexHome = fs.realpathSync(process.env.CODEX_HOME);
const home = fs.realpathSync(process.env.HOME);
const hash = bytes => crypto.createHash("sha256").update(bytes).digest("hex");
const check = (condition, message) => { if (!condition) throw new Error(message); };
const canonical = value => fs.realpathSync(value);
const inside = (value, parent) => value === parent || value.startsWith(parent + path.sep);

function cli(args) {
  return JSON.parse(execFileSync("codex", args, {
    cwd, env: process.env, encoding: "utf8", timeout: 30000, maxBuffer: 8 * 1024 * 1024,
  }));
}

function treeEntries(directory, relative = ".") {
  const file = path.join(directory, relative);
  const stat = fs.lstatSync(file);
  check(!stat.isSymbolicLink(), "Installed treatment contains a symlink: " + file);
  check(stat.isDirectory() || stat.isFile(), "Installed treatment has a nonregular asset: " + file);
  const entry = { path: relative, kind: stat.isDirectory() ? "directory" : "file", mode: stat.mode & 0o7777 };
  if (stat.isFile()) {
    const bytes = fs.readFileSync(file);
    Object.assign(entry, { sha256: hash(bytes), size: bytes.length });
  }
  const entries = [entry];
  if (stat.isDirectory()) {
    for (const name of fs.readdirSync(file).sort()) {
      entries.push(...treeEntries(directory, relative === "." ? name : relative + "/" + name));
    }
  }
  return entries;
}

function verifyTree(directory, expected) {
  const observed = treeEntries(directory);
  const expectedByPath = new Map(expected.entries.map(entry => [entry.path, entry]));
  check(observed.length === expectedByPath.size, "Installed treatment inventory differs: " + directory);
  // Harbor's upload step grants readability. Restore and check the frozen modes.
  for (const entry of observed) {
    const original = expectedByPath.get(entry.path);
    check(original && original.kind === entry.kind && original.sha256 === entry.sha256
      && original.size === entry.size, "Installed treatment bytes differ: " + directory + "/" + entry.path);
    fs.chmodSync(path.join(directory, entry.path), original.mode);
    check((fs.statSync(path.join(directory, entry.path)).mode & 0o7777) === original.mode,
      "Installed treatment mode differs: " + directory + "/" + entry.path);
  }
}

async function discover() {
  const child = spawn("codex", ["app-server"], { cwd, env: process.env, stdio: ["pipe", "pipe", "pipe"] });
  const pending = new Map();
  let nextId = 1;
  let stderr = "";
  child.stderr.on("data", bytes => { stderr += bytes.toString(); });
  const lines = readline.createInterface({ input: child.stdout });
  const fail = error => {
    for (const item of pending.values()) item.reject(error);
    pending.clear();
  };
  child.on("error", fail);
  child.on("exit", code => fail(new Error("Native discovery exited: " + code + " " + stderr.slice(0, 1000))));
  lines.on("line", line => {
    let message;
    try { message = JSON.parse(line); } catch { fail(new Error("Malformed native discovery JSON")); return; }
    if (!Object.hasOwn(message, "id")) return;
    const item = pending.get(message.id);
    if (!item) return;
    pending.delete(message.id);
    if (message.error) item.reject(new Error(JSON.stringify(message.error)));
    else item.resolve(message.result);
  });
  const request = (method, params) => new Promise((resolve, reject) => {
    const id = nextId++;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error("Native discovery timed out: " + method));
    }, 20000);
    pending.set(id, {
      resolve: value => { clearTimeout(timer); resolve(value); },
      reject: error => { clearTimeout(timer); reject(error); },
    });
    child.stdin.write(JSON.stringify({ id, method, params }) + "\n");
  });
  try {
    await request("initialize", { clientInfo: { name: "pluginbench", version: "1" }, capabilities: { experimentalApi: true } });
    child.stdin.write(JSON.stringify({ method: "initialized", params: {} }) + "\n");
    const skills = await request("skills/list", { cwds: [cwd], forceReload: true });
    const hooks = await request("hooks/list", { cwds: [cwd] });
    return { skills, hooks };
  } finally {
    lines.close();
    child.stdin.end();
    if (child.exitCode === null && child.signalCode === null) {
      await new Promise(resolve => {
        const timer = setTimeout(() => { child.kill("SIGKILL"); }, 1000);
        child.once("exit", () => { clearTimeout(timer); resolve(); });
        child.kill("SIGTERM");
      });
    }
  }
}

async function main() {
  const guidancePath = path.join(codexHome, "AGENTS.md");
  check(!fs.existsSync(guidancePath), "Undeclared native global instructions already exist");
  // The prepared source must remain unchanged. Native global instructions apply
  // to this single attempt and cannot become part of its source patch.
  if (payload.inventory.guidance.length) {
    const bytes = fs.readFileSync(path.join(root, "AGENTS.md"));
    check(hash(bytes) === payload.guidance_sha256 && bytes.length === payload.guidance_bytes,
      "Staged instructions changed");
    fs.writeFileSync(guidancePath, bytes, { flag: "wx", mode: 0o600 });
  }
  const expectedSkills = new Map();
  for (const skill of payload.inventory.skills) {
    const directory = path.join(home, ".agents/skills", skill.directory);
    verifyTree(directory, skill);
    expectedSkills.set(canonical(path.join(directory, "SKILL.md")), { name: skill.name, pluginId: null });
  }
  const installs = [];
  if (payload.inventory.plugins.length) {
    const added = cli(["plugin", "marketplace", "add", path.join(root, "marketplace"), "--json"]);
    check(added.marketplaceName === payload.marketplace, "Native marketplace identity differs");
    for (const plugin of payload.inventory.plugins) {
      const id = plugin.name + "@" + payload.marketplace;
      const installed = cli(["plugin", "add", id, "--json"]);
      check(installed.pluginId === id && typeof installed.installedPath === "string", "Native plugin identity differs");
      const installedRoot = canonical(installed.installedPath);
      check(inside(installedRoot, path.join(codexHome, "plugins/cache")), "Native plugin installed outside private cache");
      verifyTree(installedRoot, plugin);
      installs.push({ ...installed, expected: plugin });
      for (const skill of plugin.skills) {
        expectedSkills.set(canonical(path.join(installedRoot, skill.path, "SKILL.md")), {
          name: plugin.name + ":" + skill.name, pluginId: id,
        });
      }
    }
  }
  const listed = cli(["plugin", "list", "--json"]);
  check(Array.isArray(listed.installed) && listed.installed.length === installs.length,
    "Native plugin inventory contains missing or undeclared installations: " + JSON.stringify({
      expected: installs.map(item => item.pluginId),
      installed: listed.installed?.map(item => ({
        pluginId: item.pluginId, installed: item.installed, enabled: item.enabled,
        marketplaceName: item.marketplaceName, installPolicy: item.installPolicy,
        installPolicySource: item.installPolicySource,
      })),
    }));
  for (const installed of installs) {
    check(listed.installed.some(item => item.pluginId === installed.pluginId && item.installed === true && item.enabled === true),
      "Native plugin is not installed and enabled: " + installed.pluginId);
  }
  const discovery = await discover();
  const skillsEntry = discovery.skills.data?.find(entry => canonical(entry.cwd) === canonical(cwd));
  const hooksEntry = discovery.hooks.data?.find(entry => canonical(entry.cwd) === canonical(cwd));
  check(skillsEntry && skillsEntry.errors.length === 0, "Native skill discovery failed");
  check(hooksEntry && hooksEntry.errors.length === 0 && hooksEntry.warnings.length === 0,
    "Native hook discovery failed or warned");
  const found = new Set();
  for (const skill of skillsEntry.skills) {
    const location = canonical(skill.path);
    const expected = expectedSkills.get(location);
    if (!expected) {
      check(skill.scope === "system" && inside(location, path.join(codexHome, "skills/.system")),
        "Undeclared native skill discovered: " + skill.name);
      continue;
    }
    check(skill.enabled === true && skill.name === expected.name && (skill.pluginId || null) === expected.pluginId,
      "Native skill identity/availability differs: " + skill.name);
    found.add(location);
  }
  check(found.size === expectedSkills.size, "Native runtime did not discover every declared skill");
  let expectedHookCount = 0;
  for (const plugin of installs) {
    const expected = [];
    for (const config of plugin.expected.hooks) {
      for (const [event, groups] of Object.entries(config.hooks || config)) {
        for (const group of groups) for (const hook of group.hooks) expected.push({ event, group, hook });
      }
    }
    const observed = hooksEntry.hooks.filter(hook => hook.pluginId === plugin.pluginId);
    check(observed.length === expected.length, "Native plugin hook inventory differs: " + plugin.pluginId);
    for (const { event, group, hook } of expected) {
      const eventName = event[0].toLowerCase() + event.slice(1);
      check(observed.some(item => item.eventName === eventName && item.enabled === true
        && item.source === "plugin" && item.handlerType === "command" && !item.async
        && item.command === hook.command && (item.matcher || null) === (group.matcher || null)
        && inside(canonical(item.sourcePath), canonical(plugin.installedPath))),
      "Native plugin hook was not registered faithfully: " + plugin.pluginId + "/" + event);
    }
    expectedHookCount += expected.length;
  }
  check(hooksEntry.hooks.length === expectedHookCount, "Undeclared native hooks discovered");
  const receipt = {
    status: "verified", delivery: payload.delivery, inventory: payload.inventory,
    guidance: { path: payload.inventory.guidance.length ? guidancePath : null,
      sha256: payload.guidance_sha256, bytes: payload.guidance_bytes, scope: "native global instructions" },
    plugins: listed.installed, skills: skillsEntry, hooks: hooksEntry,
    hook_trust: "per-coding-invocation bypass for the verified frozen package sources",
    hook_execution: "not observed by discovery; target execution evidence is separate",
    skill_activation: "not observed by discovery; native availability is verified",
    effective_config: fs.readFileSync(path.join(codexHome, "config.toml"), "utf8"),
  };
  process.stdout.write(JSON.stringify(receipt) + "\n");
}

main().catch(error => {
  process.stderr.write(String(error.stack || error) + "\n");
  process.exitCode = 1;
});
