"use strict";

/* ---------- Basics ---------- */
const byId = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="console-token"]').content;
const state = { config: null, plan: null, file: null, uploadId: null, kind: "local", busy: false, refreshing: false, plans: [], seen: new Map() };
const statusNames = { planned: "Awaiting approval", submitting: "Submitting", deploying: "Deploying", succeeded: "Live", failed: "Failed", unknown: "Needs a check" };
const sourceNames = { local: "Local folder", github: "GitHub", zip: "ZIP upload" };
const frameworkNames = { "google-adk": "Google ADK", custom: "Custom Python", langchain: "LangChain", langgraph: "LangGraph", ag2: "AG2", llama_index: "LlamaIndex" };
const frameworkColors = { "google-adk": "#1a85ff", custom: "#ff7a3d", langchain: "#2eb872", langgraph: "#7c5cff", ag2: "#0b7285", llama_index: "#d6609a" };
const avatarColors = ["#1a73e8", "#188038", "#7c3aed", "#d9480f", "#c2185b", "#0b7285", "#5f3dc4", "#2b8a3e", "#a61e4d", "#1864ab"];
const checkNames = { submission: "Deploys unlocked", billing: "Billing", vertex_api: "Vertex AI API", runtime_account: "Runtime account", psc_attachment: "Private network", sandbox_engine: "Sandbox host", credentials: "Google credentials" };

function el(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined && text !== null) node.textContent = text; return node; }
function icon(name, spinning = false) { const node = document.createElement("i"); node.dataset.lucide = name; if (spinning) node.classList.add("spinning"); return node; }
function icons() { if (globalThis.lucide) globalThis.lucide.createIcons(); }
function setText(id, value) { byId(id).textContent = value; }
function readableBytes(value) { return value < 1024 ? `${value} B` : value < 1048576 ? `${(value / 1024).toFixed(1)} KiB` : `${(value / 1048576).toFixed(2)} MiB`; }
function duration(seconds) { const s = Math.max(0, Math.round(seconds)); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`; }
function clock(seconds) { const s = Math.max(0, Math.floor(seconds)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
function relativeTime(value) {
  const time = typeof value === "number" ? value * 1000 : Date.parse(value); if (!time) return "unknown";
  const seconds = (Date.now() - time) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  if (seconds < 86400 * 30) return `${Math.round(seconds / 86400)} days ago`;
  return new Date(time).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
function cloudConsoleUrl(project) { return `https://console.cloud.google.com/agent-platform/runtimes?project=${encodeURIComponent(project)}`; }
function hash(text) { let value = 2166136261; for (const char of text) { value ^= char.charCodeAt(0); value = Math.imul(value, 16777619) >>> 0; } return value; }
function initials(name) { const parts = String(name || "?").split(/[-_\s.]+/).filter(Boolean); return ((parts[0]?.[0] || "?") + (parts[1]?.[0] || parts[0]?.[1] || "")).toUpperCase(); }
function paintAvatar(node, name) { node.textContent = initials(name); node.style.background = avatarColors[hash(String(name)) % avatarColors.length]; return node; }
function avatar(name, large = false) { return paintAvatar(el("span", large ? "avatar lg" : "avatar"), name); }
function swatch(framework) { const node = el("span", "swatch"); node.style.background = frameworkColors[framework] || "#9a988f"; return node; }
function metaLine(parts) { const line = el("p", "item-meta"); for (const part of parts.filter(Boolean)) { const span = el("span"); if (typeof part === "string") span.textContent = part; else span.append(...part); line.append(span); } return line; }

async function api(path, options = {}) {
  const headers = { "X-Console-Token": token, ...(options.headers || {}) };
  if (options.body && !(options.body instanceof File)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  const data = await response.json();
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((entry) => `${entry.loc.at(-1)}: ${entry.msg}`).join("; ") : data.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return data;
}

function alertBox(kind, text) {
  const box = el("div", `alert ${kind}`);
  box.append(icon(kind === "error" ? "circle-alert" : kind === "warning" ? "triangle-alert" : kind === "success" ? "circle-check" : "info"), el("span", "", text));
  return box;
}
function showError(message) { setText("notice", message); byId("notice").hidden = false; }
function clearError() { byId("notice").hidden = true; }
async function copyText(text, label = "Copied") {
  try { await navigator.clipboard.writeText(text); toast({ title: label, timeout: 1800 }); }
  catch { toast({ title: "Clipboard unavailable", kind: "error" }); }
}

/* ---------- Toasts ---------- */
function toast({ title, text = "", kind = "info", action = null, confetti = false, timeout = 6000 }) {
  const node = el("div", "toast");
  node.setAttribute("role", kind === "error" ? "alert" : "status");
  const mark = el("span", "mark");
  const tile = el("span", `tile ${kind === "success" ? "green" : kind === "error" ? "red" : "blue"}`);
  tile.append(icon(kind === "success" ? "check" : kind === "error" ? "circle-alert" : "info"));
  mark.append(tile);
  if (confetti) {
    const burst = el("span", "confetti"); burst.setAttribute("aria-hidden", "true");
    [["#4285f4", -26, -22], ["#ea4335", 22, -26], ["#fbbc04", -30, 8], ["#34a853", 28, 4], ["#7c5cff", 4, -32]].forEach(([color, dx, dy], index) => {
      const bit = el("i"); bit.style.background = color; bit.style.setProperty("--dx", `${dx}px`); bit.style.setProperty("--dy", `${dy}px`);
      if (index % 2) bit.style.borderRadius = "50%";
      burst.append(bit);
    });
    mark.append(burst);
  }
  const body = el("div", "body");
  const words = el("div"); words.append(el("strong", "", title)); if (text) words.append(el("span", "serif", text));
  body.append(words);
  const dismiss = () => { if (!node.isConnected) return; node.classList.add("leaving"); setTimeout(() => node.remove(), 180); };
  if (action) {
    const actions = el("div", "actions");
    const go = el("button", "go press", action.label); go.type = "button"; go.addEventListener("click", () => { dismiss(); action.run(); });
    const later = el("button", "later press", "Later"); later.type = "button"; later.addEventListener("click", dismiss);
    actions.append(go, later); body.append(actions);
  }
  node.append(mark, body);
  byId("toasts").append(node);
  while (byId("toasts").children.length > 3) byId("toasts").firstElementChild.remove();
  icons();
  if (timeout) setTimeout(dismiss, action ? Math.max(timeout, 10000) : timeout);
}

/* ---------- Views ---------- */
const viewTitles = { deploy: "Ship an agent", history: "Deployments", agents: "Agent garden", environment: "Environment" };
function showView() {
  const requested = location.hash.slice(1);
  const view = viewTitles[requested] ? requested : "deploy";
  document.querySelectorAll("section.view").forEach((section) => { section.hidden = section.dataset.view !== view; });
  document.querySelectorAll(".nav-pill[data-view]").forEach((link) => {
    const active = link.dataset.view === view;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
  document.title = `${viewTitles[view]} · Agent Platform`;
  if (view === "agents" && !garden.loading && (!garden.loaded || Date.now() - garden.fetchedAt > 60000)) loadGarden();
  if (requested && requested !== view) return;
  window.scrollTo({ top: 0 });
}
window.addEventListener("hashchange", showView);

/* ---------- Ship: form ---------- */
function setBusy(busy, label = "Packaging…") {
  state.busy = busy;
  byId("deployment-form").querySelectorAll("input, button, select").forEach((input) => { input.disabled = busy; });
  const button = byId("review-button");
  button.classList.toggle("busy", busy);
  button.querySelector(".label-text").textContent = busy ? label : "Review snapshot";
  byId("new-button").disabled = busy;
  updateApproval();
}

function invalidatePlan() {
  if (state.busy) return;
  state.plan = null;
  byId("review-content").hidden = true;
  byId("review-empty").hidden = false;
  byId("approve-deployment").checked = false;
  byId("confirm-project").value = "";
  updateApproval();
}

function selectSource(kind) {
  state.kind = kind;
  for (const source of ["local", "github", "zip"]) byId(`${source}-fields`).hidden = source !== kind;
  byId("local-path").required = kind === "local";
  byId("github-url").required = kind === "github";
  byId("github-sha").required = kind === "github";
  selectRequiredFields();
  invalidatePlan();
}
function selectRequiredFields() {
  byId("local-path").disabled = state.kind !== "local" || state.busy;
  byId("github-url").disabled = state.kind !== "github" || state.busy;
  byId("github-sha").disabled = state.kind !== "github" || state.busy;
}

function selectFile(file) {
  if (!file) return;
  state.uploadId = null;
  if (!file.name.toLowerCase().endsWith(".zip") || file.size > 8 * 1024 * 1024) {
    state.file = null; byId("zip-file").value = "";
    setText("upload-name", "Drop a ZIP here, or click to choose"); setText("upload-size", "Up to 8 MiB");
    showError("Choose a ZIP archive no larger than 8 MiB.");
    invalidatePlan(); return;
  }
  state.file = file;
  setText("upload-name", file.name); setText("upload-size", readableBytes(file.size));
  clearError(); invalidatePlan();
}

function updateApproval() {
  const sandboxStatus = state.plan?.sandbox?.status;
  const sandboxOk = !state.config?.sandbox_required || sandboxStatus === "passed";
  const canDeploy = state.config?.deployment_enabled && state.plan?.status === "planned" && !state.busy && sandboxOk
    && byId("approve-deployment").checked && byId("confirm-project").value === state.config.project;
  byId("sandbox-button").disabled = !state.config?.sandbox_enabled || !state.plan || state.busy || sandboxStatus === "running";
  byId("deploy-button").disabled = !canDeploy;
  byId("lock-state").hidden = Boolean(state.config?.deployment_enabled) || state.plan?.status !== "planned";
}

/* ---------- Ship: review ---------- */
function timelineSteps(plan) {
  const sandbox = plan.sandbox || { status: "not_run" };
  const planned = plan.status === "planned";
  const elapsed = plan.submitted_at ? clock(Date.now() / 1000 - plan.submitted_at) : "";
  const sandboxStep = sandbox.status === "passed" ? ["done", "Sandbox test passed", "Loaded cleanly in an isolated sandbox", ""]
    : ["failed", "error"].includes(sandbox.status) ? ["failed", "Sandbox test failed", sandbox.message || "See the sandbox output below", ""]
    : sandbox.status === "running" ? ["current", "Testing in a sandbox", "Loading your code in isolation", ""]
    : planned ? ["waiting", "Sandbox test", state.config?.sandbox_enabled ? "Optional, and catches missing packages early" : "Off in this console", ""]
    : ["skipped", "Sandbox test skipped", "", ""];
  const approve = planned ? [sandbox.status === "running" ? "waiting" : "current", "Approve", "Tick the box and type the project ID", ""]
    : ["done", "Approved", plan.approved_by ? `by ${plan.approved_by}` : "by you", ""];
  const build = planned ? ["waiting", "Build on Agent Engine", "Installs packages and starts the container", ""]
    : ["submitting", "deploying"].includes(plan.status) ? ["current", "Building on Agent Engine", "Installing packages, warming up the container", elapsed]
    : plan.status === "failed" ? ["failed", "Build failed", "See what went wrong below", ""]
    : plan.status === "unknown" ? ["failed", "Couldn't confirm the build", "Check Google Cloud before retrying", ""]
    : ["done", "Built on Agent Engine", "", plan.completed_at && plan.submitted_at ? duration(plan.completed_at - plan.submitted_at) : ""];
  const live = plan.status === "succeeded" ? ["done", "Live", plan.resource ? `Engine ${plan.resource.split("/").pop()}` : "", ""] : ["waiting", "Live", "Usually 3–5 minutes after approval", ""];
  return [["done", "Snapshot packaged", `${plan.files.length} files · ${readableBytes(plan.total_bytes)}`, ""], sandboxStep, approve, build, live];
}

function renderTimeline(plan) {
  byId("progress-steps").replaceChildren(...timelineSteps(plan).map(([stepState, title, detail, when]) => {
    const item = el("li", stepState);
    const mark = el("span", "mark");
    if (stepState === "done") mark.append(icon("check")); else if (stepState === "failed") mark.append(icon("x")); else if (stepState === "skipped") mark.append(icon("minus"));
    const label = el("span", "label"); label.append(el("strong", "", title)); if (detail) label.append(el("small", "", detail));
    if (stepState === "current" && title.startsWith("Building")) { const bar = el("span", "progress"); bar.append(el("span")); label.append(bar); }
    const whenNode = el("span", "when", when); if (stepState === "current" && title.startsWith("Building")) whenNode.id = "build-elapsed";
    item.append(mark, label, whenNode); return item;
  }));
}

const logs = { planId: "", loading: false };
async function loadLogs(plan) {
  if (logs.loading) return;
  logs.loading = true; logs.planId = plan.id;
  byId("logs-refresh").disabled = true;
  setText("logs-meta", "Reading the container's output from Cloud Logging…");
  try {
    const data = await api(`/api/plans/${plan.id}/logs`);
    byId("logs-explorer").href = plan.logs_url || data.explorer_url;
    byId("logs-hints").replaceChildren(...data.hints.map((hint) => alertBox("info", `Likely cause: ${hint}`)));
    const output = byId("logs-output");
    if (!data.lines.length) {
      setText("logs-meta", `No log lines yet for engine ${data.engine_id}. They can take a minute to arrive.`);
      output.replaceChildren(); output.hidden = true;
    } else {
      setText("logs-meta", `${data.lines.length} most recent lines from engine ${data.engine_id}`);
      output.hidden = false;
      output.replaceChildren(...data.lines.map((line) => {
        const severe = ["ERROR", "CRITICAL", "ALERT", "EMERGENCY"].includes(line.severity) || /Traceback|Error|Exception/.test(line.text);
        return el("span", severe ? "log-line severe" : "log-line", `${line.time ? line.time.slice(11, 19) : ""}  ${line.text}\n`);
      }));
      output.scrollTop = output.scrollHeight;
    }
  } catch (error) { setText("logs-meta", error.message); byId("logs-output").hidden = true; }
  finally { logs.loading = false; byId("logs-refresh").disabled = false; icons(); }
}

function querySnippet(plan) {
  const head = `import vertexai\n\nclient = vertexai.Client(project="${plan.project}", location="${plan.region}")\nagent = client.agent_engines.get(name="${plan.resource}")\n`;
  if (plan.framework === "google-adk") return `import asyncio\n${head}\nasync def main():\n    async for event in agent.async_stream_query(user_id="demo", message="Hello"):\n        print(event)\n\nasyncio.run(main())`;
  return `${head}print(agent.query(message="Hello"))`;
}

function setStatusMark(id, kind) { byId(id).className = `status-mark ${kind}`; }

const specFrameworks = { adk: "Google ADK", langgraph: "LangGraph", langchain: "LangChain", custom: "Custom Python" };
// The agent's AgentSpec (agentspec.yaml), when its folder has one: who owns it and what it needs.
function renderSpec(plan) {
  const spec = plan.agentspec;
  byId("spec-section").hidden = !spec;
  if (!spec) return;
  setText("spec-badge", `AgentSpec · ${spec.name} v${spec.version}`);
  setText("spec-file", `from ${plan.agentspec_file} (${spec.api_version})`);
  const resources = spec.resources;
  const rows = [
    ["Owner", `${spec.owner.name} · ${spec.owner.team}`],
    ["Framework", specFrameworks[spec.framework] || spec.framework],
    ["Data class", spec.data_classification],
    ["Models", spec.models.join(", ") || "None"],
    ["Tools", [...spec.mcp_servers.map((url) => `MCP ${new URL(url).host}`), ...spec.connectors].join(", ") || "None"],
    ["Runtime", `Python ${spec.python} · ${resources.cpu} CPU · ${resources.memory} · ${resources.min_instances}–${resources.max_instances} instances · ${spec.network} network`],
    ["Telemetry", spec.telemetry ? "Enabled" : "Not requested"],
    ["Evaluation", spec.evaluation.join(", ") || "None declared"],
  ];
  byId("spec-fields").replaceChildren(...rows.map(([label, value]) => {
    const row = el("div"); row.append(el("dt", "", label), el("dd", "", value)); return row;
  }));
}

function showPlan(plan, resetApproval = false) {
  state.plan = plan;
  byId("review-empty").hidden = true;
  byId("review-content").hidden = false;
  paintAvatar(byId("review-avatar"), plan.display_name);
  setText("snapshot-name", plan.display_name);
  setText("review-badge", statusNames[plan.status] || plan.status);
  byId("review-badge").className = `pill ${plan.status}`;
  setText("review-digest", plan.digest.slice(0, 8)); byId("review-digest").title = plan.digest;
  setText("review-project", plan.project);
  setText("review-entrypoint", plan.entrypoint);
  setText("review-dependencies", plan.dependency_format === "uv" ? "uv.lock, exported" : (plan.requirements_file || "requirements.txt"));
  setText("review-size", `${readableBytes(plan.total_bytes)} · ${plan.files.length} files`);
  setText("files-summary", `Files (${plan.files.length})`);
  byId("file-list").replaceChildren(...plan.files.map((file) => { const row = el("li"); row.append(el("span", "", file.path), el("span", "", readableBytes(file.bytes))); return row; }));
  renderTimeline(plan);
  renderSpec(plan);

  const alerts = [];
  if (plan.status === "failed") alerts.push(alertBox("error", plan.message));
  else if (plan.status === "unknown") alerts.push(alertBox("warning", plan.message));
  if (plan.status_check) alerts.push(alertBox("warning", `${plan.status_check}. The deployment may still be running in Google Cloud.`));
  for (const warning of plan.warnings || []) alerts.push(alertBox("warning", warning));
  byId("plan-alerts").replaceChildren(...alerts);

  const showLogs = ["failed", "unknown"].includes(plan.status) && Boolean(plan.operation);
  byId("logs-panel").hidden = !showLogs;
  if (showLogs && logs.planId !== plan.id) { byId("logs-hints").replaceChildren(); byId("logs-output").replaceChildren(); loadLogs(plan); }

  const live = plan.status === "succeeded" && Boolean(plan.resource);
  byId("success-panel").hidden = !live;
  if (live) {
    setText("resource-name", plan.resource);
    setText("success-meta", `${plan.project} · ${plan.region}${plan.completed_at && plan.submitted_at ? ` · built in ${duration(plan.completed_at - plan.submitted_at)}` : ""}`);
    byId("open-cloud").href = cloudConsoleUrl(plan.project);
    setText("query-snippet", querySnippet(plan));
  }

  const active = ["submitting", "deploying"].includes(plan.status);
  setStatusMark("status-icon", active ? "busy" : plan.status === "succeeded" ? "ok" : ["failed"].includes(plan.status) ? "bad" : "warn");
  setText("status-title", statusNames[plan.status] || plan.status);
  setText("status-message", plan.message);
  byId("approval-section").hidden = plan.status !== "planned";
  byId("deploy-button").hidden = plan.status !== "planned";
  setText("operation-name", plan.resource || plan.operation);
  byId("cloud-link").href = cloudConsoleUrl(plan.project);
  byId("operation-result").hidden = !plan.operation || live;
  renderSandbox(plan.sandbox || { status: "not_run" });
  if (resetApproval) { byId("approve-deployment").checked = false; byId("confirm-project").value = ""; }
  byId("confirm-project").placeholder = plan.project;
  updateApproval();
  icons();
}

const sandboxTitles = { not_run: "Sandbox test not run", running: "Testing in a GCP sandbox…", passed: "Sandbox test passed", failed: "Sandbox test failed", error: "Sandbox test couldn't run" };
function renderSandbox(sandbox) {
  setStatusMark("sandbox-icon", sandbox.status === "running" ? "busy" : sandbox.status === "passed" ? "ok" : ["failed", "error"].includes(sandbox.status) ? "bad" : "");
  setText("sandbox-title", sandboxTitles[sandbox.status] || sandbox.status);
  const fallback = state.config?.sandbox_enabled ? "Loads this snapshot in an isolated code-execution sandbox, before you pay for a build." : "Sandbox tests are off in this console. See Environment to turn them on.";
  setText("sandbox-message", sandbox.message || fallback);
  const log = [sandbox.details?.query_output && `query() returned: ${sandbox.details.query_output}`, sandbox.stdout && `stdout:\n${sandbox.stdout}`, sandbox.stderr && `stderr:\n${sandbox.stderr}`, sandbox.details?.traceback].filter(Boolean).join("\n\n");
  byId("sandbox-output").hidden = !log;
  setText("sandbox-log", log);
  byId("sandbox-section").hidden = state.plan?.status !== "planned" && sandbox.status === "not_run";
}

/* ---------- Status changes → toasts ---------- */
function announceChanges(plans) {
  for (const plan of plans) {
    const key = plan.id;
    const before = state.seen.get(key);
    const now = `${plan.status}/${plan.sandbox?.status || ""}`;
    state.seen.set(key, now);
    if (!before || before === now) continue;
    const [oldStatus, oldSandbox] = before.split("/");
    if (plan.status === "succeeded" && oldStatus !== "succeeded") {
      toast({ title: `${plan.display_name} is live`, text: plan.completed_at && plan.submitted_at ? `Built in ${duration(plan.completed_at - plan.submitted_at)}. Go say hello.` : "Go say hello.", kind: "success", confetti: true,
        action: { label: "Chat with it", run: () => openTester(agentFromPlan(plan)) } });
      garden.loaded = false;
    } else if (plan.status === "failed" && oldStatus !== "failed") {
      toast({ title: `${plan.display_name} didn't make it`, text: "The review card shows why, with the container logs.", kind: "error",
        action: { label: "See why", run: () => { showPlan(plan, true); location.hash = "#deploy"; } } });
    } else if (plan.sandbox?.status !== oldSandbox && ["passed", "failed", "error"].includes(plan.sandbox?.status)) {
      toast({ title: plan.sandbox.status === "passed" ? "Sandbox test passed" : "Sandbox test failed", text: plan.sandbox.message || "", kind: plan.sandbox.status === "passed" ? "success" : "error" });
    }
  }
}

/* ---------- Deployments ---------- */
function agentFromPlan(plan) {
  return { name: plan.resource, id: plan.resource.split("/").pop(), display_name: plan.display_name, framework: plan.framework,
    methods: plan.framework === "google-adk" ? ["async_create_session", "async_stream_query"] : ["query"] };
}
function openPlan(plan) { if (state.busy) return; showPlan(plan, true); location.hash = "#deploy"; }

function renderHistory() {
  const plans = state.plans;
  setText("history-count", String(plans.length));
  setText("stat-total", String(plans.length));
  setText("stat-succeeded", String(plans.filter((plan) => plan.status === "succeeded").length));
  setText("stat-active", String(plans.filter((plan) => ["submitting", "deploying"].includes(plan.status)).length));
  setText("stat-failed", String(plans.filter((plan) => ["failed", "unknown"].includes(plan.status)).length));
  if (!plans.length) {
    const empty = el("div", "empty");
    const go = el("a", "btn primary press", "Ship your first agent"); go.href = "#deploy";
    empty.append(el("strong", "", "Nothing shipped yet this session"), el("span", "serif", "Reviewed snapshots and their deployments will gather here."), go);
    byId("history-body").replaceChildren(empty); return;
  }
  byId("history-body").replaceChildren(...plans.map((plan) => {
    const failed = ["failed", "unknown"].includes(plan.status);
    const item = el("article", `item${failed ? " attention" : ""}`);
    const body = el("div", "item-body");
    const title = el("div", "item-title"); title.append(el("h3", "", plan.display_name));
    const stateClass = plan.status === "succeeded" ? "live" : failed ? "bad" : ["submitting", "deploying"].includes(plan.status) ? "busy" : "wait";
    const label = plan.status === "succeeded" && plan.completed_at && plan.submitted_at ? `Live in ${duration(plan.completed_at - plan.submitted_at)}` : statusNames[plan.status];
    title.append(el("span", `state ${stateClass}`, label));
    body.append(title);
    if (["submitting", "deploying"].includes(plan.status)) {
      const row = el("div", "row"); const bar = el("span", "progress"); bar.append(el("span")); row.append(bar);
      const elapsed = el("span", "meta", plan.submitted_at ? clock(Date.now() / 1000 - plan.submitted_at) : ""); elapsed.dataset.elapsedFrom = plan.submitted_at || "";
      row.append(elapsed); body.append(row);
    } else if (failed) body.append(el("p", "item-desc", plan.message));
    body.append(metaLine([sourceNames[plan.source_kind], [el("code", "", plan.digest.slice(0, 8))], relativeTime(plan.created_at), plan.resource ? `engine ${plan.resource.split("/").pop()}` : ""]));
    const actions = el("div", "item-actions");
    if (plan.status === "succeeded" && plan.resource) { const chat = el("button", "btn primary small press", "Chat"); chat.type = "button"; chat.addEventListener("click", () => openTester(agentFromPlan(plan))); actions.append(chat); }
    const open = el("button", `btn ${failed ? "danger" : "soft"} small press`, failed ? "See why" : plan.status === "planned" ? "Continue" : "Open"); open.type = "button";
    open.addEventListener("click", () => openPlan(plan)); actions.append(open);
    item.append(avatar(plan.display_name), body, actions);
    return item;
  }));
  icons();
}

async function refreshHistory() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    const data = await api("/api/plans");
    const plans = data.plans;
    for (let index = 0; index < plans.length; index += 1) {
      if (plans[index].status === "deploying" || plans[index].sandbox?.status === "running") plans[index] = await api(`/api/plans/${plans[index].id}`);
    }
    state.plans = plans;
    announceChanges(plans);
    const current = plans.find((plan) => plan.id === state.plan?.id);
    if (current) showPlan(current); else if (state.plan) invalidatePlan();
    renderHistory();
  } catch (error) { showError(error.message); }
  finally { state.refreshing = false; }
}

/* ---------- Agent garden ---------- */
const garden = { agents: [], loaded: false, loading: false, fetchedAt: 0, frameworks: new Set() };
function isFresh(agent) { return Date.now() - Date.parse(agent.create_time) < 15 * 60 * 1000; }

function renderFrameworkFilters() {
  const counts = new Map();
  for (const agent of garden.agents) counts.set(agent.framework || "", (counts.get(agent.framework || "") || 0) + 1);
  byId("framework-filters").replaceChildren(...[...counts.entries()].sort((a, b) => b[1] - a[1]).map(([framework, count]) => {
    const label = el("label", "filter");
    const box = el("input"); box.type = "checkbox"; box.checked = garden.frameworks.has(framework);
    box.addEventListener("change", () => { if (box.checked) garden.frameworks.add(framework); else garden.frameworks.delete(framework); renderGarden(); });
    label.append(box, swatch(framework), el("span", "", frameworkNames[framework] || framework || "Not set"), el("span", "n", String(count)));
    return label;
  }));
}

function agentItem(agent) {
  const fresh = isFresh(agent);
  const item = el("article", `item${fresh ? " fresh" : ""}`);
  const body = el("div", "item-body");
  const title = el("div", "item-title"); title.append(el("h3", "", agent.display_name));
  const hasCode = agent.deployment !== "none";
  title.append(el("span", `state ${hasCode ? "live" : "idle"}`, hasCode ? "Deployed" : "No code"));
  if (fresh) title.append(el("span", "tag", "new"));
  if (agent.managed_by_console) title.append(el("span", "tag", "from this console"));
  body.append(title);
  const desc = el("p", "item-desc"); if (agent.description) desc.textContent = agent.description; else desc.append(el("span", "serif", hasCode ? "No description yet." : "An empty runtime, often used to host sandboxes."));
  body.append(desc);
  const framework = [swatch(agent.framework), document.createTextNode(frameworkNames[agent.framework] || agent.framework || "Framework not set")];
  body.append(metaLine([`created ${relativeTime(agent.create_time)}`, framework, agent.service_account ? [el("code", "", agent.service_account.split("@")[0] + "@…")] : "service agent", agent.private_network ? "private network" : "public network", [el("code", "", agent.id)]]));
  const actions = el("div", "item-actions");
  const chat = el("button", `btn ${fresh ? "primary" : "soft"} small press`, "Chat"); chat.type = "button";
  chat.disabled = !agent.methods.length; if (!agent.methods.length) chat.title = "This agent exposes no methods to call";
  chat.addEventListener("click", () => openTester(agent));
  const copy = el("button", "icon-button press"); copy.type = "button"; copy.title = "Copy resource name"; copy.setAttribute("aria-label", `Copy resource name of ${agent.display_name}`); copy.append(icon("copy"));
  copy.addEventListener("click", () => copyText(agent.name, "Resource name copied"));
  actions.append(chat, copy);
  item.append(avatar(agent.display_name, true), body, actions);
  return item;
}

function renderGarden() {
  const query = byId("garden-filter").value.trim().toLowerCase();
  const mine = byId("filter-mine").checked;
  const sort = byId("garden-sort").value;
  let shown = garden.agents.filter((agent) => (!query || [agent.display_name, agent.id, agent.framework, agent.description].some((value) => (value || "").toLowerCase().includes(query)))
    && (!garden.frameworks.size || garden.frameworks.has(agent.framework || "")) && (!mine || agent.managed_by_console));
  shown = [...shown].sort((a, b) => sort === "name" ? a.display_name.localeCompare(b.display_name) : sort === "updated" ? Date.parse(b.update_time) - Date.parse(a.update_time) : Date.parse(b.create_time) - Date.parse(a.create_time));
  const total = garden.agents.length;
  setText("garden-total", String(total));
  setText("garden-live", String(garden.agents.filter((agent) => agent.deployment !== "none").length));
  setText("garden-attention", String(garden.agents.filter((agent) => agent.managed_by_console).length));
  setText("garden-nav-count", String(total));
  const age = garden.fetchedAt ? Math.max(0, Math.round((Date.now() - garden.fetchedAt) / 1000)) : 0;
  setText("garden-count", `Showing ${shown.length} of ${total} · fresh as of ${age < 5 ? "just now" : `${age}s ago`}`);
  if (!total) {
    const empty = el("div", "empty"); const go = el("a", "btn primary press", "Ship an agent"); go.href = "#deploy";
    empty.append(el("strong", "", "The garden is empty"), el("span", "serif", "Nothing is deployed in this project and region yet. Plant the first one."), go);
    byId("garden-grid").replaceChildren(empty);
  } else if (!shown.length) {
    const empty = el("div", "empty"); empty.append(el("strong", "", "No agents match"), el("span", "serif", "Try a different search or clear the filters."));
    byId("garden-grid").replaceChildren(empty);
  } else byId("garden-grid").replaceChildren(...shown.map(agentItem));
  icons();
}

async function loadGarden(force = false) {
  if (garden.loading) return;
  garden.loading = true;
  byId("garden-refresh").disabled = true;
  if (!garden.loaded) byId("garden-grid").replaceChildren(...Array.from({ length: 3 }, () => el("div", "skeleton")));
  byId("garden-status").replaceChildren();
  try {
    const data = await api(`/api/agents${force === true ? "?refresh=1" : ""}`);
    garden.agents = data.agents; garden.loaded = true;
    garden.fetchedAt = Date.now() - (data.age_seconds || 0) * 1000;
    setText("garden-scope", `${data.project} / ${data.region}`);
    renderFrameworkFilters();
  } catch (error) {
    byId("garden-status").replaceChildren(alertBox("error", error.message));
    if (!garden.loaded) byId("garden-grid").replaceChildren();
  } finally { garden.loading = false; byId("garden-refresh").disabled = false; if (garden.loaded) renderGarden(); icons(); }
}

/* ---------- Test drawer ---------- */
const tester = { agent: null, session: "", busy: false, userId: "console-tester", turns: [] };
// Agents whose method takes a `history` list get the conversation so far (role/content turns).
const takesHistory = () => byId("test-mode").value === "query" && tester.agent?.method_schemas?.[byId("test-method").value]?.properties?.history !== undefined;
function testDefaults(agent) {
  const methods = agent.methods || [];
  const textKey = ["langchain", "langgraph", "llama_index"].includes(agent.framework) ? "input" : "message";
  if (methods.includes("async_stream_query")) return { method: "async_stream_query", mode: "stream", input: { user_id: tester.userId, message: "{message}" } };
  if (methods.includes("stream_query") && agent.framework === "google-adk") return { method: "stream_query", mode: "stream", input: { user_id: tester.userId, message: "{message}" } };
  if (methods.includes("query")) return { method: "query", mode: "query", input: { [textKey]: "{message}" } };
  if (methods.includes("stream_query")) return { method: "stream_query", mode: "stream", input: { [textKey]: "{message}" } };
  return { method: methods[0] || "query", mode: "query", input: { message: "{message}" } };
}
const textKeys = ["message", "input", "query", "prompt", "text", "question"];
// Build the method's input from the JSON schema the agent published for it.
function inputFromSchema(schema) {
  const props = schema?.properties || {};
  const keys = Object.keys(props);
  if (!keys.length) return null;
  const required = schema.required || [];
  const textKey = textKeys.find((key) => key in props)
    || required.find((key) => props[key]?.type === "string" && key !== "user_id")
    || keys.find((key) => props[key]?.type === "string" && key !== "user_id");
  const input = {};
  for (const key of keys) {
    const type = props[key]?.type;
    if (key === "user_id") input[key] = tester.userId;
    else if (key === textKey) input[key] = "{message}";
    else if (required.includes(key)) input[key] = props[key]?.enum?.[0] ?? (type === "number" || type === "integer" ? 0 : type === "boolean" ? false : type === "array" ? [] : type === "object" ? {} : "");
  }
  return { input, textKey, enumValues: textKey && props[textKey]?.enum ? props[textKey].enum : null };
}
function applyMethod(method) {
  const agent = tester.agent;
  const schema = agent.method_schemas?.[method];
  const built = inputFromSchema(schema);
  const fallback = testDefaults(agent);
  tester.baseInput = built ? built.input : fallback.input;
  byId("test-input").value = JSON.stringify(tester.baseInput, null, 2);
  byId("test-schema").textContent = schema && Object.keys(schema).length ? JSON.stringify(schema, null, 2) : "This agent didn't publish a schema for this method.";
  const natural = !built || textKeys.includes(built.textKey);
  setSuggestions(built?.enumValues?.slice(0, 4) || (natural ? ["Hi! What can you do?", "Show me an example of what you're good at"] : []));
  byId("test-message").placeholder = natural ? "Message the agent…" : `Value for "${built.textKey}"…`;
  testInfo();
}

function fillMessage(value, message) {
  if (typeof value === "string") return value.replaceAll("{message}", message);
  if (Array.isArray(value)) return value.map((item) => fillMessage(item, message));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, fillMessage(item, message)]));
  return value;
}
function testInfo() {
  const parts = [frameworkNames[tester.agent.framework] || tester.agent.framework || "Agent", byId("test-method").value];
  if (tester.session) parts.push(`session ${tester.session.slice(0, 8)}…`);
  setText("test-info", parts.join(" · "));
}
function bubble(kind, text) {
  const item = el("div", `bubble ${kind}`);
  if (text !== undefined) item.append(el("p", "", text));
  byId("test-transcript").append(item); byId("test-transcript").scrollTop = byId("test-transcript").scrollHeight;
  return item;
}
// A suggestion is either text to send, or { label, input, send } that replaces the input JSON.
function setSuggestions(prompts) {
  byId("test-suggestions").replaceChildren(...prompts.map((prompt) => {
    const label = typeof prompt === "string" ? prompt : prompt.label;
    const chip = el("button", "press", label); chip.type = "button";
    chip.addEventListener("click", () => {
      if (typeof prompt === "string") { byId("test-message").value = prompt; byId("test-form").requestSubmit(); return; }
      if (prompt.message !== undefined) {
        // The agent takes the command as text in its message field: put it in the chat box.
        byId("test-input").value = JSON.stringify(tester.baseInput, null, 2);
        byId("test-message").value = prompt.message; autosize();
        if (prompt.send) { byId("test-form").requestSubmit(); return; }
        bubble("system", prompt.note || "Edit the command in the message box, then press Send.");
        byId("test-message").focus(); return;
      }
      byId("test-input").value = JSON.stringify(prompt.input, null, 2);
      byId("test-message").value = label;
      if (prompt.send) { byId("test-form").requestSubmit(); return; }
      byId("test-form").querySelector(".advanced").open = true;
      bubble("system", prompt.note || "Fill in the values in the input JSON, then press Send.");
      byId("test-input").focus();
    });
    return chip;
  }));
}

// Pull example JSON objects out of an agent's error text, e.g. 'Send JSON: {"action":"list_tools"}'.
function examplesFromError(message) {
  const text = message.replaceAll('\\"', '"');
  const found = [];
  for (let start = text.indexOf("{"); start !== -1 && found.length < 4; start = text.indexOf("{", start + 1)) {
    let depth = 0;
    for (let end = start; end < text.length; end += 1) {
      if (text[end] === "{") depth += 1;
      else if (text[end] === "}" && --depth === 0) {
        try {
          const value = JSON.parse(text.slice(start, end + 1));
          if (value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length && !("detail" in value) && !found.some((item) => JSON.stringify(item) === JSON.stringify(value))) found.push(value);
        } catch { /* not JSON */ }
        break;
      }
    }
  }
  return found;
}
const looksLikePlaceholder = (value) => typeof value === "string" && /^[A-Z][A-Z0-9_]{2,}$/.test(value);
// The input key that carries the typed text ("{message}"), if the agent's input has one.
function textField() { return Object.keys(tester.baseInput || {}).find((key) => tester.baseInput[key] === "{message}"); }
// A command chip: agents with a text field get the command as JSON text in the chat box,
// others get it as the whole input.
function commandChip(command, label, send, note) {
  if (textField()) return { label, message: JSON.stringify(command), send, note: note.replace("input JSON", "message box") };
  return { label, input: command, send, note };
}
function exampleChip(example) {
  const values = Object.values(example);
  const needsEditing = values.some(looksLikePlaceholder) || values.some((value) => value && typeof value === "object" && !Object.keys(value).length);
  const label = Object.values(example).find((value) => typeof value === "string" && !looksLikePlaceholder(value)) || JSON.stringify(example);
  return commandChip(example, needsEditing ? `${label} …` : label, !needsEditing,
    "Replace the placeholder values (like TOOL_NAME) in the input JSON, then press Send.");
}

// Tool servers answer list_tools with a list of tools: offer a ready-made call for each.
function toolChips(output) {
  const tools = [output?.tools, output?.result?.tools, output?.result?.result?.tools, output].find(Array.isArray) || null;
  if (!tools) return [];
  return tools.filter((tool) => tool && typeof tool.name === "string").slice(0, 6).map((tool) => {
    const schema = tool.inputSchema || tool.input_schema || tool.parameters || {};
    const args = Object.fromEntries(Object.keys(schema.properties || {}).map((key) => [key, ""]));
    return commandChip({ action: "call_tool", name: tool.name, arguments: args }, `Call ${tool.name}`, false,
      Object.keys(args).length ? `Fill in the arguments for ${tool.name} (${Object.keys(args).join(", ")}) in the input JSON, then press Send.` : `Add any arguments ${tool.name} needs in the input JSON, then press Send.`);
  });
}
function openTester(agent) {
  tester.agent = agent; tester.session = ""; tester.turns = [];
  paintAvatar(byId("test-avatar"), agent.display_name || agent.id);
  setText("test-title", agent.display_name || agent.id); setText("test-resource", agent.name);
  const defaults = testDefaults(agent);
  const methods = agent.methods?.length ? agent.methods : [defaults.method];
  byId("test-method").replaceChildren(...methods.map((name) => Object.assign(document.createElement("option"), { value: name, textContent: name })));
  byId("test-method").value = defaults.method; byId("test-mode").value = defaults.mode;
  byId("test-form").querySelector(".advanced").open = false;
  byId("test-transcript").replaceChildren();
  bubble("system", "You're talking to the deployed agent in Google Cloud. It may call models and tools, and the first reply can take a moment while it warms up.");
  applyMethod(defaults.method);
  byId("test-drawer").showModal(); icons();
  byId("test-message").focus();
}
async function ensureSession() {
  const agent = tester.agent;
  const input = (() => { try { return JSON.parse(byId("test-input").value); } catch { return {}; } })();
  if (tester.session || !agent.methods?.includes("async_create_session") || byId("test-mode").value !== "stream" || !("user_id" in input)) return;
  const data = await api("/api/agents/test", { method: "POST", body: JSON.stringify({ name: agent.name, method: "async_create_session", mode: "query", input: { user_id: input.user_id } }) });
  tester.session = data.output?.id || "";
  testInfo();
}
function autosize() { const box = byId("test-message"); box.style.height = "auto"; box.style.height = `${Math.min(box.scrollHeight, 160)}px`; }

/* ---------- Command palette ---------- */
const palette = { items: [], index: 0 };
function paletteItems(query) {
  const views = [["deploy", "Ship an agent", "upload"], ["history", "Deployments", "list"], ["agents", "Agent garden", "sprout"], ["environment", "Environment", "settings-2"]]
    .map(([view, label, iconName]) => ({ label, hint: "Page", iconName, run: () => { location.hash = `#${view}`; } }));
  const agents = garden.agents.map((agent) => ({ label: agent.display_name, hint: frameworkNames[agent.framework] || "Agent", agent,
    run: () => { if (agent.methods.length) openTester(agent); else { location.hash = "#agents"; byId("garden-filter").value = agent.display_name; renderGarden(); } } }));
  const all = [...views, ...agents];
  const needle = query.trim().toLowerCase();
  return (needle ? all.filter((item) => item.label.toLowerCase().includes(needle)) : all).slice(0, 12);
}
function renderPalette() {
  palette.items = paletteItems(byId("palette-query").value);
  palette.index = Math.min(palette.index, Math.max(0, palette.items.length - 1));
  byId("palette-results").replaceChildren(...palette.items.map((item, index) => {
    const row = el("li"); row.setAttribute("role", "option"); row.setAttribute("aria-selected", String(index === palette.index));
    row.append(item.agent ? avatar(item.agent.display_name) : icon(item.iconName), el("span", "", item.label), el("small", "", item.agent ? `Chat · ${item.hint}` : item.hint));
    row.addEventListener("click", () => runPalette(index));
    return row;
  }));
  if (!palette.items.length) byId("palette-results").replaceChildren(el("li", "meta", "Nothing matches."));
  icons();
}
function openPalette() { if (byId("palette").open) return; byId("palette-query").value = ""; palette.index = 0; renderPalette(); byId("palette").showModal(); byId("palette-query").focus(); }
function runPalette(index) { const item = palette.items[index]; if (!item) return; byId("palette").close(); item.run(); }

/* ---------- Environment ---------- */
function renderEnvironment(config) {
  setText("target-project", config.project); setText("target-region", config.region);
  setText("target-identity", config.runtime_account || "Agent Engine service agent");
  setText("target-network", config.network_attachment ? `Private · ${config.network_attachment.split("/").pop()}` : "Public (no PSC attachment)");
  const deploys = el("span", "", config.deployment_enabled ? "Unlocked" : "Locked"); byId("submission-state").replaceChildren(el("span", `dot ${config.deployment_enabled ? "" : "warn"}`), deploys);
  const switches = [
    ["blue-soft", "rocket", "Deploys unlocked", "CONSOLE_ALLOW_DEPLOY=1 and CONSOLE_SECURITY_REVIEW_APPROVED=1", config.deployment_enabled],
    ["violet-soft", "flask-conical", "Sandbox tests", config.sandbox_engine ? `CONSOLE_ALLOW_SANDBOX_TEST=1 · host ${config.sandbox_engine.split("/").pop()}` : "CONSOLE_SANDBOX_ENGINE and CONSOLE_ALLOW_SANDBOX_TEST=1", config.sandbox_enabled],
    ["red-soft", "lock", "Require a passing sandbox test", "CONSOLE_REQUIRE_SANDBOX_TEST=1", config.sandbox_required],
    ["amber-soft", "user-round", "Agents run as", `CONSOLE_RUNTIME_ACCOUNT=${config.runtime_account || "default"}`, null, config.runtime_account ? "Dedicated account" : "Service agent"],
    ["green-soft", "network", "Network", `CONSOLE_NETWORK_ATTACHMENT=${config.network_attachment || "none"}`, null, config.network_attachment ? "Private (PSC)" : "Public"],
  ];
  byId("switch-list").replaceChildren(...switches.map(([tone, iconName, title, code, on, value]) => {
    const row = el("li"); const tile = el("span", `tile ${tone}`); tile.append(icon(iconName));
    const text = el("span", "text"); text.append(el("strong", "", title), el("code", "", code));
    row.append(tile, text);
    if (on === null) row.append(el("span", "pill neutral", value));
    else { const toggle = el("span", `toggle${on ? " on" : ""}`); toggle.setAttribute("role", "img"); toggle.setAttribute("aria-label", on ? "On" : "Off"); row.append(toggle); }
    return row;
  }));
  const details = { ...(config.hosted ? { "Hosted on": "Cloud Run, behind Identity-Aware Proxy", "Signed in as": config.user } : {}), "Project": config.project, "Region": config.region, ...(config.local_sources_enabled ? { "Allowed source folder": config.source_root } : {}), "Runtime service account": config.runtime_account || "Agent Engine default service agent", "PSC attachment": config.network_attachment || "None (public networking)", "Sandbox host engine": config.sandbox_engine || "Not configured" };
  byId("environment-details").replaceChildren(...Object.entries(details).map(([label, value]) => { const row = el("div"); row.append(el("dt", "", label), el("dd", "", value)); return row; }));
  icons();
}

async function runHealthCheck() {
  const button = byId("check-cloud"); const list = byId("preflight-result");
  button.disabled = true; button.textContent = "Checking…"; list.hidden = false;
  list.replaceChildren(...Array.from({ length: 3 }, () => el("li", "skeleton")));
  const tiles = { passed: ["green", "check"], skipped: ["grey", "minus"], blocked: ["red", "x"], missing: ["red", "x"], unknown: ["amber", "circle-help"] };
  try {
    const report = await api("/api/preflight");
    list.replaceChildren(...report.checks.map((check) => {
      const row = el("li", check.status); const [tone, iconName] = tiles[check.status] || tiles.unknown;
      const tile = el("span", `tile ${tone}`); tile.append(icon(iconName));
      const text = el("div"); text.append(el("strong", "", checkNames[check.id] || check.id), el("span", "msg", check.message));
      row.append(tile, text); return row;
    }), el("li", "note", report.note));
    const passed = report.checks.filter((check) => check.status === "passed").length;
    toast({ title: "Health check finished", text: `${passed} of ${report.checks.length} checks passed.`, kind: passed === report.checks.length ? "success" : "info" });
    icons();
  } catch (error) { list.replaceChildren(el("li", "note", error.message)); }
  finally { button.disabled = false; button.textContent = "Run a health check"; }
}

/* ---------- Events ---------- */
byId("deployment-form").addEventListener("submit", async (event) => {
  event.preventDefault(); clearError();
  if (!state.config) { showError("The console couldn't load its settings. Refresh the page."); return; }
  if (state.kind === "zip" && !state.file) { showError("Choose a ZIP archive first."); return; }
  if (!byId("agent-name").value.trim()) { showError("Give the agent a name."); byId("agent-name").focus(); return; }
  const body = { source_kind: state.kind, location: state.kind === "local" ? byId("local-path").value.trim() : byId("github-url").value.trim(), revision: byId("github-sha").value.trim(), subdirectory: byId("subdirectory").value.trim(), display_name: byId("agent-name").value.trim(), module: byId("entry-module").value.trim(), object_name: byId("entry-object").value.trim(), requirements: byId("requirements").value.trim(), framework: byId("framework").value };
  setBusy(true);
  try {
    if (state.kind === "zip") {
      if (!state.uploadId) state.uploadId = (await api("/api/uploads", { method: "POST", body: state.file })).id;
      body.location = state.uploadId;
    }
    const plan = await api("/api/plans", { method: "POST", body: JSON.stringify(body) });
    if (state.kind === "zip") state.uploadId = null;
    state.seen.set(plan.id, `${plan.status}/${plan.sandbox?.status || ""}`);
    showPlan(plan, true);
    await refreshHistory();
  } catch (error) {
    if (error.message.startsWith("Upload expired")) state.uploadId = null;
    showError(error.message);
  } finally { setBusy(false); selectRequiredFields(); }
});

byId("deploy-button").addEventListener("click", async () => {
  if (byId("deploy-button").disabled || !state.plan) return;
  clearError(); setBusy(true, "Submitting…");
  const plan = state.plan;
  try {
    const result = await api(`/api/plans/${plan.id}/deploy`, { method: "POST", body: JSON.stringify({ digest: plan.digest, project: byId("confirm-project").value, approve_cost: byId("approve-deployment").checked, approve_code: byId("approve-deployment").checked }) });
    state.seen.set(result.id, `${result.status}/${result.sandbox?.status || ""}`);
    showPlan(result);
    if (result.status === "deploying") toast({ title: "Off it goes", text: "Agent Engine is building it now. We'll tell you when it's live." });
    await refreshHistory();
  } catch (error) { showError(error.message); }
  finally { setBusy(false); selectRequiredFields(); }
});

byId("sandbox-button").addEventListener("click", async () => {
  if (byId("sandbox-button").disabled || !state.plan) return;
  clearError();
  try { showPlan(await api(`/api/plans/${state.plan.id}/sandbox-test`, { method: "POST" })); await refreshHistory(); }
  catch (error) { showError(error.message); }
});

byId("deployment-form").addEventListener("input", (event) => { if (event.target.name !== "source-kind") invalidatePlan(); });
document.querySelectorAll('input[name="source-kind"]').forEach((input) => input.addEventListener("change", () => selectSource(input.value)));
byId("approve-deployment").addEventListener("change", updateApproval);
byId("confirm-project").addEventListener("input", updateApproval);
byId("zip-file").addEventListener("change", (event) => selectFile(event.target.files[0]));
byId("upload-zone").addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); byId("zip-file").click(); } });
byId("upload-zone").addEventListener("dragover", (event) => { event.preventDefault(); event.currentTarget.classList.add("drag-over"); });
byId("upload-zone").addEventListener("dragleave", (event) => event.currentTarget.classList.remove("drag-over"));
byId("upload-zone").addEventListener("drop", (event) => { event.preventDefault(); event.currentTarget.classList.remove("drag-over"); if (!state.busy) selectFile(event.dataTransfer.files[0]); });
byId("clear-uploads").addEventListener("click", async () => {
  if (state.busy || !globalThis.confirm("Discard all staged ZIP uploads? Reviewed snapshots and deployments are not affected.")) return;
  try { await api("/api/uploads", { method: "DELETE" }); state.uploadId = null; toast({ title: "Staged uploads cleared" }); }
  catch (error) { showError(error.message); }
});
byId("refresh-history").addEventListener("click", refreshHistory);
byId("new-button").addEventListener("click", () => {
  byId("deployment-form").reset(); state.file = null; state.uploadId = null;
  const kind = state.config?.local_sources_enabled === false ? "github" : "local";
  document.querySelector(`input[name="source-kind"][value="${kind}"]`).checked = true; selectSource(kind);
  setText("upload-name", "Drop a ZIP here, or click to choose"); setText("upload-size", "Up to 8 MiB");
  setText("framework-label", "Custom agent"); clearError();
  (kind === "local" ? byId("local-path") : byId("github-url")).focus();
});
byId("framework").addEventListener("change", () => { setText("framework-label", byId("framework").value === "google-adk" ? "Google ADK" : "Custom agent"); invalidatePlan(); });
byId("check-cloud").addEventListener("click", runHealthCheck);
byId("copy-resource").addEventListener("click", () => copyText(byId("resource-name").textContent, "Resource name copied"));
byId("copy-snippet").addEventListener("click", () => copyText(byId("query-snippet").textContent, "Snippet copied"));
byId("copy-digest").addEventListener("click", () => { if (state.plan) copyText(state.plan.digest, "SHA-256 copied"); });
byId("download-plan").addEventListener("click", () => {
  if (!state.plan) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(state.plan, null, 2)], { type: "application/json" }));
  const link = el("a"); link.href = url; link.download = `deployment-${state.plan.id}.json`; link.click(); URL.revokeObjectURL(url);
});
byId("logs-refresh").addEventListener("click", () => { if (state.plan) loadLogs(state.plan); });
byId("test-deployed").addEventListener("click", () => { if (state.plan?.resource) openTester(agentFromPlan(state.plan)); });

byId("garden-refresh").addEventListener("click", () => loadGarden(true));
byId("garden-filter").addEventListener("input", renderGarden);
byId("garden-sort").addEventListener("change", renderGarden);
byId("filter-mine").addEventListener("change", renderGarden);

byId("test-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = byId("test-message").value.trim();
  if (!message || tester.busy) return;
  let template;
  try { template = JSON.parse(byId("test-input").value || "{}"); } catch { bubble("error", "The input JSON under “Method & input” isn't valid JSON."); return; }
  tester.busy = true; byId("test-send").disabled = true;
  bubble("user", message); byId("test-message").value = ""; autosize();
  const typing = el("div", "typing"); typing.setAttribute("aria-label", "The agent is responding"); typing.append(el("i"), el("i"), el("i"));
  byId("test-transcript").append(typing); byId("test-transcript").scrollTop = byId("test-transcript").scrollHeight;
  try {
    await ensureSession();
    const input = fillMessage(template, message);
    if (takesHistory() && !("history" in input) && tester.turns.length) input.history = tester.turns.slice(-10);
    if (tester.session && byId("test-mode").value === "stream" && !input.session_id) input.session_id = tester.session;
    const started = performance.now();
    const data = await api("/api/agents/test", { method: "POST", body: JSON.stringify({ name: tester.agent.name, method: byId("test-method").value, mode: byId("test-mode").value, input }) });
    typing.remove();
    const reply = bubble("agent");
    const calls = (data.tools || []).filter((tool) => tool.kind === "call");
    for (const call of calls) {
      const result = (data.tools || []).find((tool) => tool.kind === "result" && tool.name === call.name);
      const card = el("details", "tool"); card.open = true;
      const summary = el("summary"); summary.append(icon("wrench"), document.createTextNode("Called "), el("strong", "", call.name));
      card.append(summary, el("pre", "", `${call.data}${result ? `\n→ ${result.data}` : ""}`));
      reply.append(card);
    }
    // MCP tool results carry their answer as text content blocks: show that text first.
    const mcpText = [data.output?.result?.content, data.output?.content].find(Array.isArray)?.filter((block) => block?.type === "text" && block.text).map((block) => block.text).join("\n\n");
    if (!data.reply && mcpText) data.reply = mcpText;
    const structured = !data.reply && data.output !== undefined && data.output !== null && typeof data.output === "object";
    const silent = data.events ? (data.tools?.length ? "The agent used its tools but didn't write a reply. Its tool results are above, and the raw events are below." : "The agent didn't send any text back. Open the raw response below to see what it returned.") : "(no text in the response)";
    if (data.reply && takesHistory()) tester.turns.push({ role: "user", content: message }, { role: "assistant", content: data.reply });
    if (data.output?.tools_unavailable) reply.append(el("div", "bubble error", `The agent couldn't reach its data tools: ${data.output.tools_unavailable}`));
    if (data.reply || data.output !== undefined || !data.errors?.length) reply.append(el("div", structured ? "text json" : "text", data.reply || (data.output !== undefined ? JSON.stringify(data.output, null, 2) : silent)));
    for (const problem of data.errors || []) reply.append(el("div", "bubble error", `The agent reported an error: ${problem}`));
    const chips = toolChips(data.output);
    setSuggestions(chips);
    const footer = el("div", "footer"); const raw = el("details", "raw");
    raw.append(el("summary", "", `${((performance.now() - started) / 1000).toFixed(1)}s${data.event_count ? ` · ${data.event_count} events` : ""} · raw response`), el("pre", "", JSON.stringify(data.events || data.output, null, 2)));
    footer.append(raw); reply.append(footer); icons();
  } catch (error) {
    typing.remove(); bubble("error", error.message);
    const examples = examplesFromError(error.message);
    if (examples.length) {
      setSuggestions(examples.map(exampleChip));
      bubble("system", "This agent expects structured input, not a chat message. Pick one of its own examples below to try it.");
    }
    if (!examples.length && /Send JSON|INVALID_ARGUMENT|missing|required|unexpected keyword|argument/i.test(error.message)) {
      byId("test-form").querySelector(".advanced").open = true;
      bubble("system", "The agent didn't accept that input. Its expected input is under “Method & input”: adjust the JSON (your text replaces {message}) and send again.");
    }
  }
  finally { tester.busy = false; byId("test-send").disabled = false; byId("test-transcript").scrollTop = byId("test-transcript").scrollHeight; byId("test-message").focus(); }
});
byId("test-message").addEventListener("input", autosize);
byId("test-message").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); byId("test-form").requestSubmit(); } });
byId("test-new-session").addEventListener("click", () => { if (!tester.agent) return; tester.session = ""; tester.turns = []; byId("test-transcript").replaceChildren(); bubble("system", "Fresh conversation. The agent won't remember the last one."); testInfo(); });
byId("test-method").addEventListener("change", () => applyMethod(byId("test-method").value));
byId("test-mode").addEventListener("change", testInfo);
byId("test-close").addEventListener("click", () => byId("test-drawer").close());
byId("test-drawer").addEventListener("click", (event) => { if (event.target === byId("test-drawer")) byId("test-drawer").close(); });

byId("palette-open").addEventListener("click", openPalette);
byId("palette-query").addEventListener("input", () => { palette.index = 0; renderPalette(); });
byId("palette-query").addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown") { event.preventDefault(); palette.index = Math.min(palette.index + 1, palette.items.length - 1); renderPalette(); }
  else if (event.key === "ArrowUp") { event.preventDefault(); palette.index = Math.max(palette.index - 1, 0); renderPalette(); }
  else if (event.key === "Enter") { event.preventDefault(); runPalette(palette.index); }
});
byId("palette").addEventListener("click", (event) => { if (event.target === byId("palette")) byId("palette").close(); });
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openPalette(); }
  else if ((event.metaKey || event.ctrlKey) && event.key === "Enter" && !byId("view-deploy").hidden && !byId("test-drawer").open) { event.preventDefault(); byId("deployment-form").requestSubmit(); }
});

/* ---------- Start ---------- */
async function initialize() {
  showView(); selectSource("local"); icons();
  try {
    const config = state.config = await api("/api/config");
    setText("summary-project", config.project); setText("summary-region", config.region);
    byId("summary-submission").replaceChildren(el("span", `dot ${config.deployment_enabled ? "" : "warn"}`));
    byId("summary-submission").title = config.deployment_enabled ? "Deploys unlocked" : "Deploys locked";
    byId("history-cloud-link").href = cloudConsoleUrl(config.project);
    byId("runtimes-link").href = cloudConsoleUrl(config.project);
    setText("runtime-account", config.runtime_account ? config.runtime_account.split("@")[0] : "the service agent");
    setText("network-label", config.network_attachment ? "private network" : "public network");
    setText("allowed-root", config.source_root);
    byId("local-path").placeholder = config.source_root || "C:\\projects\\my-agent";
    byId("sandbox-tip").hidden = !config.sandbox_enabled;
    if (!config.local_sources_enabled) {
      document.querySelector('input[name="source-kind"][value="local"]').closest("label").hidden = true;
      document.querySelector('input[name="source-kind"][value="github"]').checked = true;
      selectSource("github");
    }
    renderEnvironment(config);
    updateApproval();
    if (!garden.loaded && !garden.loading) loadGarden();
    await refreshHistory();
  } catch (error) { showError(error.message); }
}

initialize();
setInterval(() => { if (!document.hidden && !state.busy) refreshHistory(); }, 6000);
setInterval(() => {
  if (state.plan?.submitted_at && ["submitting", "deploying"].includes(state.plan.status) && byId("build-elapsed")) setText("build-elapsed", clock(Date.now() / 1000 - state.plan.submitted_at));
  document.querySelectorAll("[data-elapsed-from]").forEach((node) => { if (node.dataset.elapsedFrom) node.textContent = clock(Date.now() / 1000 - Number(node.dataset.elapsedFrom)); });
}, 1000);
setInterval(() => { if (garden.loaded && !document.hidden && location.hash === "#agents") renderGarden(); }, 15000);
