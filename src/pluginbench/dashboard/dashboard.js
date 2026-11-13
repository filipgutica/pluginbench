// `pluginbench dashboard` page: reads the local JSON API and never writes.
// Every value from a run is set as text, never parsed as HTML.
"use strict";

const LIST_INTERVAL = 10000;
const RUN_INTERVAL = 2000;

const PHASES = {
  starting: ["Starting", "info"],
  coding: ["Coding", "info"],
  measuring: ["Measuring", "info"],
  reviewing: ["Reviewing", "info"],
  reporting: ["Reporting", "info"],
  complete: ["Complete", "success"],
  stopped: ["Stopped", "warning"],
  interrupted: ["Interrupted", "warning"],
  failed: ["Failed", "danger"],
  not_started: ["Not started", ""],
};
const OUTCOMES = {
  pass: ["Pass", "success"],
  overlap_pass: ["Overlap pass", "warning"],
  fail: ["Fail", "danger"],
  empty: ["Empty patch", "danger"],
  unknown: ["Unknown", ""],
};
const STATES = {
  pending: ["Pending", ""],
  running: ["Running", "info"],
  interrupted: ["Interrupted", "warning"],
};

// The status JSON keeps the recorded state. With no live owner, nothing is still running:
// show such work as interrupted, as `pluginbench status` does.
function shownState(state, active) {
  return !active && (state === "running" || state === "collecting") ? "interrupted" : state;
}

let timer = null;
let generation = 0;

function h(tag, attributes, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") element.className = value;
    else element.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

function badge([label, tone], title) {
  return h("span", { class: tone ? `badge ${tone}` : "badge", title }, label);
}

function phaseBadge(phase) {
  return badge(PHASES[phase] || [phase || "Unreadable", ""]);
}

function count(value) {
  if (typeof value !== "number") return "—";
  for (const [size, suffix] of [[1e9, "B"], [1e6, "M"], [1e3, "k"]]) {
    if (value >= size) return `${(value / size).toFixed(1)}${suffix}`;
  }
  return String(value);
}

function tokens(total) {
  if (!total) return "—";
  let text = count(total.total_tokens);
  if (total.lower_bound) text = `at least ${text}`;
  if (total.unvalidated) text += " (unvalidated)";
  return text;
}

function age(heartbeat) {
  if (!heartbeat || typeof heartbeat.age_seconds !== "number") return "—";
  const seconds = Math.max(0, heartbeat.age_seconds);
  if (seconds < 60) return `${seconds} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

function tally(counts, labels) {
  const parts = Object.entries(counts || {})
    .filter(([, value]) => value)
    .map(([key, value]) => `${value} ${(labels[key] || [key])[0].toLowerCase()}`);
  return parts.length ? parts.join(", ") : "—";
}

function runner(value) {
  if (!value) return "—";
  const parts = [value.harness, value.model, value.reasoning && `${value.reasoning} effort`];
  return parts.filter(Boolean).join(", ") || "—";
}

async function getJSON(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `${response.status} ${response.statusText}`);
  return body;
}

function setMain(...children) {
  // replaceChildren would show a skipped (null) section as the text "null".
  document.getElementById("main").replaceChildren(...children.filter(Boolean));
}

function markUpdated(active) {
  const time = new Date().toLocaleTimeString();
  document.getElementById("updated").textContent = active
    ? `Updated ${time}, refreshing`
    : `Updated ${time}`;
}

function failure(error, retrying) {
  return h(
    "div",
    { class: "alert danger", role: "alert" },
    h("p", null, `Could not read the dashboard server: ${error.message}.`),
    h(
      "p",
      null,
      retrying
        ? ["The page tries again automatically. If it keeps failing, check that ", h("code", null, "pluginbench dashboard"), " is still running."]
        : ["Check that ", h("code", null, "pluginbench dashboard"), " is still running, then reload."],
    ),
  );
}

function schedule(render, interval, token) {
  clearTimeout(timer);
  timer = setTimeout(() => {
    if (token === generation) render(token);
  }, interval);
}

// Runs list ------------------------------------------------------------------

let phaseFilter = "";

const RUN_COLUMNS = ["Run", "Phase", "Complete", "Outcomes", "Tokens", "Harness, model, effort", "Last activity"];

function runRow(run) {
  const total = run.attempts_total;
  const complete = (run.states || {}).complete || 0;
  return h(
    "tr",
    null,
    h(
      "th",
      { scope: "row" },
      h("a", { href: `#/run/${run.key}` }, run.name || "Unnamed run"),
      h("span", { class: "sub" }, run.path),
    ),
    h(
      "td",
      { "data-label": RUN_COLUMNS[1] },
      run.phase ? phaseBadge(run.phase) : badge(["Unreadable", "warning"]),
      run.stale
        ? h("span", { class: "sub" }, run.phase ? `Last good read shown; the latest failed: ${run.error}` : `Not read yet: ${run.error}`)
        : null,
    ),
    h("td", { class: "num", "data-label": RUN_COLUMNS[2] }, typeof total === "number" ? `${complete} of ${total}` : "—"),
    h("td", { "data-label": RUN_COLUMNS[3] }, tally(run.outcomes, OUTCOMES)),
    h("td", { class: "num", "data-label": RUN_COLUMNS[4] }, tokens(run.tokens)),
    h("td", { "data-label": RUN_COLUMNS[5] }, runner(run.runner)),
    h("td", { "data-label": RUN_COLUMNS[6], id: `age-${run.key}` }, age(run.heartbeat)),
  );
}

function renderRuns(data) {
  const runs = [...data.runs].sort(
    (a, b) =>
      Number(Boolean(b.active)) - Number(Boolean(a.active)) ||
      ((a.heartbeat || {}).age_seconds ?? Infinity) - ((b.heartbeat || {}).age_seconds ?? Infinity),
  );
  const phases = [...new Set(runs.map((run) => run.phase).filter(Boolean))];
  if (phaseFilter && !phases.includes(phaseFilter)) phaseFilter = "";
  const shown = phaseFilter ? runs.filter((run) => run.phase === phaseFilter) : runs;
  const active = runs.filter((run) => run.active).length;

  const select = h(
    "select",
    { class: "select", id: "phase-filter" },
    h("option", { value: "" }, `All phases (${runs.length})`),
    phases.map((phase) =>
      h(
        "option",
        { value: phase, selected: phase === phaseFilter },
        `${(PHASES[phase] || [phase])[0]} (${runs.filter((run) => run.phase === phase).length})`,
      ),
    ),
  );
  select.addEventListener("change", () => {
    phaseFilter = select.value;
    renderRuns(data);
    document.getElementById("phase-filter").focus();
  });

  const head = h(
    "div",
    { class: "page-head" },
    h("h1", null, "Runs"),
    h("span", { class: "muted" }, active ? `${active} running` : "None running"),
  );
  if (!runs.length) {
    setMain(
      head,
      h(
        "p",
        null,
        "No run directories under ",
        data.roots.join(", "),
        ". A run directory holds evaluation.json. Start one with ",
        h("code", null, "pluginbench run"),
        ", or restart the dashboard with the folder that holds your runs.",
      ),
    );
    return;
  }
  setMain(
    head,
    h("p", { class: "muted roots" }, `Runs under ${data.roots.join(", ")}`),
    h(
      "div",
      { class: "toolbar" },
      h("label", { class: "field" }, h("span", { class: "field-label" }, "Phase"), select),
    ),
    h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        { class: "stack" },
        h("caption", null, `${shown.length} of ${runs.length} runs. Running runs first, then by last activity.`),
        h(
          "thead",
          null,
          h(
            "tr",
            null,
            RUN_COLUMNS.map(
              (label, index) =>
                h("th", { scope: "col", class: index === 2 || index === 4 ? "num" : null }, label),
            ),
          ),
        ),
        h("tbody", null, shown.map(runRow)),
      ),
    ),
  );
}

async function loadRuns(token) {
  try {
    const data = await getJSON("/api/runs");
    if (token !== generation) return;
    // Redraw only when a run changed, so focus and an open filter stay put. Heartbeat ages
    // change on every read, so they are updated in place instead.
    const signature = JSON.stringify(data.runs.map((run) => ({ ...run, heartbeat: null })).concat(data.roots));
    if (signature !== loadRuns.last) {
      loadRuns.last = signature;
      renderRuns(data);
    } else {
      for (const run of data.runs) {
        const cell = document.getElementById(`age-${run.key}`);
        if (cell) cell.textContent = age(run.heartbeat);
      }
    }
    markUpdated(data.runs.some((run) => run.active));
  } catch (error) {
    if (token !== generation) return;
    loadRuns.last = null;
    setMain(failure(error, true));
  }
  schedule(loadRuns, LIST_INTERVAL, token);
}

// Run page -------------------------------------------------------------------

function attemptLabel(attempt, active) {
  if (attempt.state === "complete") return OUTCOMES[attempt.outcome] || [attempt.outcome || "Complete", ""];
  const state = shownState(attempt.state, active);
  return STATES[state] || [state, ""];
}

function shownStates(states, active) {
  const shown = {};
  for (const [state, value] of Object.entries(states || {})) {
    const name = shownState(state, active);
    shown[name] = (shown[name] || 0) + value;
  }
  return shown;
}

function matrix(status) {
  const arms = Object.keys(status.arms || {});
  const tasks = [...new Set(status.attempts.map((attempt) => attempt.task))].sort();
  const cells = new Map();
  for (const attempt of status.attempts) {
    const key = `${attempt.task}\u0000${attempt.arm}`;
    if (!cells.has(key)) cells.set(key, []);
    cells.get(key).push(attempt);
  }
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      null,
      h("caption", null, "Each cell lists the task's repeats for that arm: state, or outcome once complete."),
      h("thead", null, h("tr", null, h("th", { scope: "col" }, "Task"), arms.map((arm) => h("th", { scope: "col" }, arm)))),
      h(
        "tbody",
        null,
        tasks.map((task) =>
          h(
            "tr",
            null,
            h("th", { scope: "row" }, task),
            arms.map((arm) => {
              const attempts = (cells.get(`${task}\u0000${arm}`) || []).sort((a, b) => a.repeat - b.repeat);
              return h(
                "td",
                null,
                attempts.length
                  ? h(
                      "ul",
                      { class: "cell-list" },
                      attempts.map((attempt) =>
                        h(
                          "li",
                          null,
                          h("span", { class: "muted" }, `Repeat ${attempt.repeat}`),
                          badge(attemptLabel(attempt, status.active), attempt.outcome_reason || null),
                          attempt.reused ? h("span", { class: "muted" }, "reused") : null,
                          attempt.tokens ? h("span", { class: "muted" }, `${tokens({ total_tokens: (attempt.tokens || {}).total_tokens, lower_bound: ["lower_bound", "unknown"].includes(attempt.tokens_status) })} tokens`) : null,
                        ),
                      ),
                    )
                  : "—",
              );
            }),
          ),
        ),
      ),
    ),
  );
}

function armTable(status) {
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      null,
      h(
        "thead",
        null,
        h(
          "tr",
          null,
          ["Arm", "Attempts", "States", "Outcomes", "Reused", "Tokens"].map((label, index) =>
            h("th", { scope: "col", class: [1, 4, 5].includes(index) ? "num" : null }, label),
          ),
        ),
      ),
      h(
        "tbody",
        null,
        Object.entries(status.arms || {}).map(([arm, row]) =>
          h(
            "tr",
            null,
            h("th", { scope: "row" }, arm),
            h("td", { class: "num" }, row.attempts),
            h("td", null, tally(shownStates(row.states, status.active), { ...STATES, complete: ["Complete"] })),
            h("td", null, tally(row.outcomes, OUTCOMES)),
            h("td", { class: "num" }, row.reused),
            h("td", { class: "num" }, tokens(row.tokens)),
          ),
        ),
      ),
    ),
  );
}

function discardedTable(status) {
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      null,
      h("thead", null, h("tr", null, ["Attempt", "Task", "Arm", "Repeat", "Replaced by", "Tokens"].map((label) => h("th", { scope: "col" }, label)))),
      h(
        "tbody",
        null,
        status.discarded.map((row) =>
          h(
            "tr",
            null,
            h("td", null, h("code", null, row.id)),
            h("td", null, row.task),
            h("td", null, row.arm),
            h("td", null, row.repeat),
            h("td", null, row.replaced_by ? h("code", null, row.replaced_by) : "—"),
            h("td", null, tokens({ total_tokens: (row.tokens || {}).total_tokens, lower_bound: true })),
          ),
        ),
      ),
    ),
  );
}

function revisions(measures, active) {
  const rows = [
    ...(measures.measurement_revisions || []).map((row) => ["Measurement", row]),
    ...(measures.review_revisions || []).map((row) => ["Review", row]),
  ];
  if (!rows.length) return null;
  return h(
    "ul",
    { class: "cell-list" },
    rows.map(([kind, row]) =>
      h(
        "li",
        null,
        `${kind} ${row.revision}: ${shownState(row.status, active) || "unknown"}`,
        row.assigned !== undefined ? `, ${row.reviewed} of ${row.assigned} reviewed` : "",
        row.selected ? badge(["Selected", "info"]) : null,
      ),
    ),
  );
}

function containersPanel(key) {
  const output = h("div", { "aria-live": "polite" });
  const button = h("button", { class: "btn", type: "button" }, "Check for containers left running");
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      const result = await getJSON(`/api/runs/${key}/containers`);
      const left = result.left_running;
      if (result.active || !left) {
        output.replaceChildren(h("p", null, "A live process owns this run, so its containers are its own."));
      } else if (!left.checked) {
        output.replaceChildren(h("div", { class: "alert warning" }, h("p", null, `Could not check Docker: ${left.error}`)));
      } else if (!left.containers.length) {
        output.replaceChildren(h("p", null, "No containers of this run are running."));
      } else {
        output.replaceChildren(
          h(
            "div",
            { class: "alert warning" },
            h("p", null, `${left.containers.length} containers of this run are still running. The next resume stops them, or stop them now:`),
            h("p", null, h("code", null, left.stop_command)),
          ),
        );
      }
    } catch (error) {
      output.replaceChildren(failure(error, false));
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  });
  return h("div", null, h("div", { class: "actions" }, button), output);
}

function renderRun(data) {
  const status = data.status;
  const back = h("a", { class: "back", href: "#/" }, "All runs");
  if (!status) {
    // The server answered; the run's files could not be read yet.
    setMain(
      back,
      h(
        "div",
        { class: "alert warning", role: "status" },
        h("p", null, `This run could not be read yet: ${data.error || "no status"}.`),
        h("p", null, "The page tries again automatically. A file caught mid-write reads correctly on a later try."),
      ),
    );
    return;
  }
  const total = status.attempts_total;
  const complete = (status.states || {}).complete || 0;
  const children = [
    back,
    h("div", { class: "page-head" }, h("h1", null, status.name || "Unnamed run"), phaseBadge(status.phase)),
    data.stale
      ? h("div", { class: "alert warning", role: "status" }, h("p", null, `Showing the last good read. The latest read failed: ${data.error}. The page keeps trying.`))
      : null,
    status.error ? h("div", { class: "alert danger" }, h("p", null, `The run failed: ${status.error}`)) : null,
    h(
      "dl",
      { class: "facts" },
      [
        ["Directory", h("code", null, status.run)],
        ["Harness", (data.runner || {}).harness || "—"],
        ["Model", (data.runner || {}).model || "—"],
        ["Requested effort", (data.runner || {}).reasoning || "—"],
        ["CLI version", (data.runner || {}).cli_version || "—"],
        ["Owner", status.owner ? `Process ${status.owner.pid}, since ${status.owner.started_at}` : "No live process"],
        ["Last activity", h("span", { id: "last-activity" }, age(status.heartbeat))],
        ["Complete", typeof total === "number" ? `${complete} of ${total} attempts` : "—"],
        ["Coding tokens", tokens(status.tokens)],
        status.resume_command ? ["Resume", h("code", null, status.resume_command)] : null,
      ]
        .filter(Boolean)
        .map(([term, value]) => [h("dt", null, term), h("dd", null, value)]),
    ),
    h(
      "div",
      { class: "actions" },
      status.report ? h("a", { class: "btn", href: `/runs/${data.key}/report.html` }, "Open the report") : h("span", { class: "muted" }, "No report yet. The run builds it when it finishes; ", h("code", null, "pluginbench report"), " rebuilds it."),
    ),
    h("h2", null, "Attempts by task and arm"),
    status.attempts.length ? matrix(status) : h("p", null, "No attempts are planned yet."),
    h("h2", null, "Arms"),
    armTable(status),
  ];
  if (status.discarded && status.discarded.length) {
    children.push(h("h2", null, "Discarded attempts"), h("p", { class: "muted" }, "Interrupted attempts that a resume replaced. Their tokens count toward spend."), discardedTable(status));
  }
  const revisionList = revisions(status.measures || {}, status.active);
  if (revisionList) children.push(h("h2", null, "Revisions"), revisionList);
  if (!status.active) children.push(h("h2", null, "Containers"), containersPanel(data.key));
  setMain(...children);
}

async function loadRun(token, key) {
  let active = false;
  try {
    const data = await getJSON(`/api/runs/${key}`);
    if (token !== generation) return;
    // Redraw only when the status moves, so a containers result stays on screen. The
    // heartbeat age changes on every read, so it is updated in place instead.
    const heartbeat = data.status ? data.status.heartbeat : null;
    const signature = JSON.stringify([{ ...data.status, heartbeat: null }, data.stale, data.error]);
    if (signature !== loadRun.last) {
      loadRun.last = signature;
      renderRun(data);
    } else {
      const shown = document.getElementById("last-activity");
      if (shown) shown.textContent = age(heartbeat);
    }
    active = Boolean(data.status && data.status.active);
    markUpdated(active);
  } catch (error) {
    if (token !== generation) return;
    setMain(h("a", { class: "back", href: "#/" }, "All runs"), failure(error, true));
    loadRun.last = null;
    active = true;
  }
  // A run that no process owns changes only when a command starts on it; check it slowly.
  schedule((next) => loadRun(next, key), active ? RUN_INTERVAL : LIST_INTERVAL, token);
}

// Routing --------------------------------------------------------------------

function route() {
  generation += 1;
  clearTimeout(timer);
  loadRun.last = null;
  const token = generation;
  const match = location.hash.match(/^#\/run\/([0-9a-f]{16})$/);
  for (const link of document.querySelectorAll(".nav-link")) {
    link.toggleAttribute("aria-current", !match && link.dataset.view === "runs");
    if (link.hasAttribute("aria-current")) link.setAttribute("aria-current", "page");
  }
  document.title = match ? "Run · PluginBench" : "Runs · PluginBench";
  if (match) loadRun(token, match[1]);
  else loadRuns(token);
}

window.addEventListener("hashchange", () => {
  route();
  document.getElementById("main").focus();
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") route();
  else clearTimeout(timer);
});
route();
