// Executed inside the attempt's private environment, after Harbor's setup and before the
// coding command. It installs the Claude treatment, then asks the CLI what it loaded through
// control requests only. It never sends a user message, so no model turn starts.
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawn, execFileSync } = require("node:child_process");
const readline = require("node:readline");

const [root, cwd] = process.argv.slice(2);
const payload = JSON.parse(fs.readFileSync(path.join(root, "treatment.json"), "utf8"));
const configDir = fs.realpathSync(process.env.CLAUDE_CONFIG_DIR);
const hash = bytes => crypto.createHash("sha256").update(bytes).digest("hex");
const check = (condition, message) => { if (!condition) throw new Error(message); };
const canonical = value => fs.realpathSync(value);
const inside = (value, parent) => value === parent || value.startsWith(parent + path.sep);
// Key-sorted JSON, so two values compare equal exactly when their content does.
const stable = value => JSON.stringify(value, (key, item) => (
  item && typeof item === "object" && !Array.isArray(item)
    ? Object.fromEntries(Object.keys(item).sort().map(name => [name, item[name]]))
    : item));
const sameSet = (left, right) => stable([...left].sort()) === stable([...right].sort());
// Control requests answered by discovery, in the order they are sent.
const REQUESTS = [
  "initialize", "get_context_usage", "get_hooks_listing", "get_settings",
  "get_memory_dialog", "get_skills_dialog", "get_session_cost",
];
// Offline, CLI 2.1.322 took 19 s to answer initialize and 18 s per plugin command.
const REQUEST_TIMEOUT_MS = 90000;
const CLI_TIMEOUT_MS = 90000;
const MANAGED_SETTINGS = "/etc/claude-code";
// The agents CLI 2.1.322 ships. The pinned launch lists all but claude-code-guide.
const BUILTIN_AGENTS = new Set([
  "Explore", "Plan", "claude", "claude-code-guide", "general-purpose", "statusline-setup",
]);

function cli(args) {
  return execFileSync("claude", args, {
    cwd, env: process.env, encoding: "utf8", timeout: CLI_TIMEOUT_MS, maxBuffer: 8 * 1024 * 1024,
  });
}

// `--json` on install and marketplace add prints one result object with an outcome.
function cliResult(args, command) {
  const result = JSON.parse(cli(args));
  check(result.command === command && result.outcome === "ok",
    "Claude plugin " + command + " did not succeed: " + JSON.stringify(result));
  return result;
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
  // Copies may widen modes. Restore and check the frozen ones.
  for (const entry of observed) {
    const original = expectedByPath.get(entry.path);
    check(original && original.kind === entry.kind && original.sha256 === entry.sha256
      && original.size === entry.size, "Installed treatment bytes differ: " + directory + "/" + entry.path);
    fs.chmodSync(path.join(directory, entry.path), original.mode);
    check((fs.statSync(path.join(directory, entry.path)).mode & 0o7777) === original.mode,
      "Installed treatment mode differs: " + directory + "/" + entry.path);
  }
}

function installPlugins() {
  const marketplace = path.join(root, "marketplace");
  const installs = [];
  let marketplaceAdd = null;
  if (payload.inventory.plugins.length) {
    marketplaceAdd = cliResult(["plugin", "marketplace", "add", marketplace, "--json"], "marketplace-add");
    for (const plugin of payload.inventory.plugins) {
      const id = plugin.name + "@" + payload.marketplace;
      installs.push({ id, output: cliResult(["plugin", "install", id, "--json"], "install"), expected: plugin });
    }
  }
  const recordFile = path.join(configDir, "plugins/installed_plugins.json");
  const records = fs.existsSync(recordFile) ? JSON.parse(fs.readFileSync(recordFile, "utf8")).plugins : {};
  check(records && typeof records === "object" && sameSet(Object.keys(records), installs.map(item => item.id)),
    "Claude plugin install record lists missing or undeclared plugins");
  const cache = path.join(configDir, "plugins/cache");
  for (const install of installs) {
    const entries = records[install.id];
    check(Array.isArray(entries) && entries.length === 1 && entries[0].scope === "user"
      && typeof entries[0].installPath === "string", "Claude plugin install record differs: " + install.id);
    install.cachePath = canonical(entries[0].installPath);
    check(inside(install.cachePath, cache), "Claude plugin installed outside the private cache: " + install.id);
  }
  const listed = JSON.parse(cli(["plugin", "list", "--json"]));
  // sameSet compares sorted lists, so a repeated entry differs too.
  check(Array.isArray(listed) && sameSet(listed.map(item => item.id), installs.map(item => item.id)),
    "Claude plugin list contains missing or undeclared plugins");
  for (const item of listed) {
    const install = installs.find(candidate => candidate.id === item.id);
    check(item.enabled === true && item.scope === "user" && !item.errors
      && canonical(item.installPath) === install.cachePath,
    "Claude plugin is not enabled in user scope: " + item.id);
    // A directory marketplace's plugin loads from its marketplace folder, not the cache copy.
    const staged = canonical(path.join(marketplace, "plugins", install.expected.name));
    check(typeof item.readFromFolder === "string" && canonical(item.readFromFolder) === staged,
      "Claude plugin is not loaded from its staged folder: " + item.id);
    install.loadedFrom = staged;
    verifyTree(install.loadedFrom, install.expected);
    verifyTree(install.cachePath, install.expected);
  }
  return { marketplace, marketplaceAdd, installs, listed };
}

async function discover() {
  const [command, ...args] = payload.discovery_argv;
  const child = spawn(command, args, { cwd, env: process.env, stdio: ["pipe", "pipe", "pipe"] });
  const events = [];
  let pending = null;
  let gone = null;
  let stderr = "";
  child.stderr.on("data", bytes => { stderr += bytes.toString(); });
  // A write after the CLI exits fails here; the close handler reports the exit instead.
  child.stdin.on("error", () => {});
  const lines = readline.createInterface({ input: child.stdout });
  lines.on("line", line => {
    let message;
    try { message = JSON.parse(line); } catch { message = { type: "malformed", subtype: line.slice(0, 200) }; }
    events.push(message);
    const response = message.type === "control_response" ? message.response : null;
    if (!pending || !response || response.request_id !== pending.id) return;
    if (response.subtype === "success") pending.resolve(response.response);
    else pending.reject(new Error("Claude discovery request failed: " + pending.id + " " + JSON.stringify(response.error)));
  });
  // `close` follows the last stdout line, so every event is recorded before it settles.
  const closed = new Promise(resolve => {
    const end = (error, exit) => {
      gone = error;
      if (pending) pending.reject(error);
      resolve(exit);
    };
    child.on("close", (code, signal) => end(new Error("Claude discovery exited before answering "
      + (pending ? pending.id : "every request") + ": " + code + " " + stderr.slice(0, 1000)), { code, signal }));
    child.on("error", error => end(error, { code: null, signal: null, error: String(error) }));
  });
  const request = subtype => new Promise((resolve, reject) => {
    if (gone) { reject(new Error(gone.message.replace("every request", subtype))); return; }
    const timer = setTimeout(() => pending.reject(new Error("Claude discovery timed out: " + subtype)),
      REQUEST_TIMEOUT_MS);
    const settle = action => value => { clearTimeout(timer); pending = null; action(value); };
    pending = { id: subtype, resolve: settle(resolve), reject: settle(reject) };
    child.stdin.write(JSON.stringify({ type: "control_request", request_id: subtype, request: { subtype } }) + "\n");
  });
  const responses = {};
  let exit;
  try {
    for (const subtype of REQUESTS) responses[subtype] = await request(subtype);
  } finally {
    child.stdin.end();
    const timer = setTimeout(() => child.kill("SIGKILL"), 10000);
    // The exit code after stdin ends without a user message is recorded, not enforced.
    exit = await closed;
    clearTimeout(timer);
  }
  return { responses, events, exit };
}

function expectedHooks(installs) {
  const hooks = [];
  for (const install of installs) {
    for (const config of install.expected.hooks) {
      for (const [event, groups] of Object.entries(config.hooks)) {
        for (const group of groups) {
          for (const hook of group.hooks) {
            hooks.push({ event, pluginName: install.id, matcher: group.matcher || "", commandText: hook.command });
          }
        }
      }
    }
  }
  return hooks;
}

function verifyDiscovery({ responses, events }, { marketplace, installs }, guidancePath) {
  const shape = (condition, subtype) => check(condition, "Claude " + subtype + " response changed shape");
  // Hook rows first, so a hook that ran is reported as such and not as an unknown event.
  check(!events.some(event => event.type === "system" && String(event.subtype).startsWith("hook_")),
    "Claude hook ran during discovery");
  const unexpected = events.find(event => event.type !== "control_response");
  check(!unexpected, "Unexpected Claude discovery event: " + (unexpected && unexpected.type + "/" + unexpected.subtype));

  const init = responses.initialize;
  shape(init && Array.isArray(init.commands) && Array.isArray(init.agents), "initialize");
  check(init.claude_code_version === payload.cli_version,
    "Claude CLI version " + init.claude_code_version + " differs from the pinned " + payload.cli_version);
  // CLI 2.1.322 reports a subscription token as tokenSource and omits apiKeySource. It
  // reports the same when an API key is also set, so the refused environment, checked
  // first, is what rules a key out.
  check(init.account && init.account.tokenSource === "CLAUDE_CODE_OAUTH_TOKEN"
    && !("apiKeySource" in init.account) && init.account.apiProvider === "firstParty",
    "Claude discovery is not authenticated by the subscription token alone");

  const expectedSkills = new Map(payload.inventory.skills.map(skill => [skill.name, "user"]));
  for (const install of installs) {
    for (const skill of install.expected.skills) expectedSkills.set(install.expected.name + ":" + skill.name, "plugin");
  }
  // A treatment declares no agents, and admission refuses plugin agents.
  const agents = init.agents.map(item => item.name);
  const undeclaredAgents = agents.filter(name => !BUILTIN_AGENTS.has(name));
  check(!undeclaredAgents.length, "Undeclared Claude agent discovered: " + undeclaredAgents.join(", "));
  const commands = init.commands.filter(item => item.builtin !== true).map(item => item.name);
  check(new Set(commands).size === commands.length, "Claude discovered duplicate commands");
  const undeclared = commands.filter(name => !expectedSkills.has(name));
  check(!undeclared.length, "Undeclared Claude command or skill discovered: " + undeclared.join(", "));
  const dialog = responses.get_skills_dialog;
  shape(dialog && Array.isArray(dialog.skills), "get_skills_dialog");
  // The dialog, not commands, proves presence: a `user-invocable: false` skill is on in the
  // dialog but absent from commands. The dialog names a loose skill by its folder and shows
  // its frontmatter name as display_name, which is the name commands use (CLI 2.1.322).
  check(sameSet(dialog.skills.map(skill => skill.display_name), expectedSkills.keys()),
    "Claude skills dialog differs from the declared skills");
  for (const skill of dialog.skills) {
    check(skill.state === "on" && skill.source === expectedSkills.get(skill.display_name),
      "Claude skill is not on from its declared source: " + skill.display_name);
  }

  const memory = responses.get_memory_dialog;
  // CLI 2.1.322 omits `memories` while auto-memory is off; when present it must be empty.
  const memories = memory && memory.memories === undefined ? [] : memory && memory.memories;
  shape(memory && Array.isArray(memory.files) && Array.isArray(memories) && memory.auto_memory, "get_memory_dialog");
  const resolved = file => (fs.existsSync(file) ? canonical(file) : file);
  const loaded = memory.files.filter(file => file.exists === true).map(file => resolved(file.path));
  check(stable(loaded) === stable(payload.inventory.guidance.length ? [guidancePath] : []) && !memories.length,
    "Claude memory files differ from the declared guidance: " + loaded.join(", "));
  check(memory.auto_memory.enabled === false, "Claude auto-memory is on");
  const context = responses.get_context_usage;
  shape(context && Array.isArray(context.categories), "get_context_usage");
  const memoryCategory = context.categories.find(category => category.id === "memory_files");
  const memoryTokens = memoryCategory ? memoryCategory.tokens : 0;
  shape(Number.isInteger(memoryTokens), "get_context_usage");
  check(payload.inventory.guidance.length ? memoryTokens > 0 : memoryTokens === 0,
    "Claude memory_files tokens (" + memoryTokens + ") disagree with the declared guidance");

  const listing = responses.get_hooks_listing;
  shape(listing && Array.isArray(listing.hooks) && listing.policy, "get_hooks_listing");
  check(listing.policy.allDisabled === true, "Claude discovery did not disable hooks");
  const observedHooks = listing.hooks.map(hook => ({
    event: hook.event, pluginName: hook.pluginName, matcher: hook.matcher || "", commandText: hook.commandText,
  }));
  check(listing.hooks.every(hook => hook.type === "command" && hook.source === "pluginHook")
    && sameSet(observedHooks.map(stable), expectedHooks(installs).map(stable)),
  "Claude hook listing differs from the declared hooks");

  const settings = responses.get_settings;
  shape(settings && settings.effective, "get_settings");
  const argv = payload.discovery_argv;
  const expectedSettings = JSON.parse(argv[argv.indexOf("--settings") + 1]);
  if (installs.length) {
    expectedSettings.enabledPlugins = Object.fromEntries(installs.map(install => [install.id, true]));
    expectedSettings.extraKnownMarketplaces = {
      [payload.marketplace]: { source: { source: "directory", path: marketplace } },
    };
  }
  check(stable(settings.effective) === stable(expectedSettings),
    "Claude effective settings differ from the pinned settings: " + stable(settings.effective));

  const cost = responses.get_session_cost;
  shape(cost && typeof cost.text === "string", "get_session_cost");
  const usage = cost.text.match(/Usage:\s+(\d+) input, (\d+) output, (\d+) cache read, (\d+) cache write/);
  shape(usage && /Total cost:\s+\$\d+\.\d+/.test(cost.text), "get_session_cost");
  check(usage.slice(1).every(count => Number(count) === 0) && /Total cost:\s+\$0\.0+\b/.test(cost.text),
    "Claude discovery reported model usage: " + cost.text);
  return { commands, memoryTokens };
}

async function main() {
  // The CLI runs with this process's environment, so check it and not the requested one.
  const refused = payload.refused_env.filter(name => name in process.env);
  check(!refused.length, "Claude treatment installation refuses " + refused.join(", "));
  const unpinned = Object.keys(payload.pinned_env).filter(name => process.env[name] !== payload.pinned_env[name]);
  check(!unpinned.length, "Claude treatment installation requires the pinned launch environment: "
    + unpinned.join(", "));
  // Managed settings outrank every pinned setting; preparation removes them.
  check(!fs.existsSync(MANAGED_SETTINGS), "Claude managed settings exist: " + MANAGED_SETTINGS);
  // User agents load from here and can replace a built-in agent of the same name.
  const agentsDir = path.join(configDir, "agents");
  check(!fs.existsSync(agentsDir) || !fs.readdirSync(agentsDir).length, "Undeclared Claude user agents exist");
  const guidancePath = path.join(configDir, "CLAUDE.md");
  check(!fs.existsSync(guidancePath), "Undeclared Claude user instructions already exist");
  // Guidance goes to the private user scope, never into the task source.
  if (payload.inventory.guidance.length) {
    const bytes = fs.readFileSync(path.join(root, "CLAUDE.md"));
    check(hash(bytes) === payload.guidance_sha256 && bytes.length === payload.guidance_bytes,
      "Staged instructions changed");
    fs.writeFileSync(guidancePath, bytes, { flag: "wx", mode: 0o600 });
  }
  for (const skill of payload.inventory.skills) {
    verifyTree(path.join(configDir, "skills", skill.directory), skill);
  }
  const plugins = installPlugins();
  const discovery = await discover();
  const { commands, memoryTokens } = verifyDiscovery(discovery, plugins, guidancePath);
  const { responses } = discovery;
  const receipt = {
    status: "verified", delivery: payload.delivery, inventory: payload.inventory,
    cli_version: responses.initialize.claude_code_version,
    account: responses.initialize.account,
    guidance: { path: payload.inventory.guidance.length ? guidancePath : null,
      sha256: payload.guidance_sha256, bytes: payload.guidance_bytes, memory_files_tokens: memoryTokens,
      scope: "Claude user instructions" },
    commands: commands.sort(),
    skills: responses.get_skills_dialog.skills,
    builtins: {
      commands: responses.initialize.commands.filter(item => item.builtin === true).map(item => item.name).sort(),
      agents: responses.initialize.agents.map(item => item.name).sort(),
    },
    marketplace: { path: plugins.marketplace, add: plugins.marketplaceAdd },
    plugin_installs: plugins.installs.map(item => ({
      id: item.id, loaded_from: item.loadedFrom, cache_path: item.cachePath, output: item.output,
    })),
    plugins: plugins.listed,
    hooks: { hooks: responses.get_hooks_listing.hooks, policy: responses.get_hooks_listing.policy },
    settings: responses.get_settings.effective,
    auto_memory: responses.get_memory_dialog.auto_memory,
    session_cost: responses.get_session_cost.text,
    discovery: { argv: payload.discovery_argv, requests: REQUESTS, exit_code: discovery.exit.code,
      signal: discovery.exit.signal },
    hook_execution: "suppressed in discovery by disableAllHooks; the coding stream must show it",
    skill_activation: "not observed by discovery; native availability is verified",
  };
  process.stdout.write(JSON.stringify(receipt) + "\n");
}

main().catch(error => {
  process.stderr.write(String(error.stack || error) + "\n");
  process.exitCode = 1;
});
