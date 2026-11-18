"use strict";

const byId = (id) => document.getElementById(id);
const state = { config: null, plan: null, file: null, uploadId: null, kind: "local", busy: false, refreshing: false, plans: [] };
const token = document.querySelector('meta[name="console-token"]').content;
const statusNames = { planned: "Awaiting approval", submitting: "Submitting", deploying: "Deploying", succeeded: "Deployed", failed: "Failed", unknown: "Needs verification" };
const sourceNames = { local: "Local folder", github: "GitHub", zip: "ZIP upload" };
const sourceIcons = { local: "folder", github: "github", zip: "file-archive" };
const sandboxNames = { not_run: "GCP sandbox test not run", running: "Testing in GCP sandbox", passed: "Sandbox test passed", failed: "Sandbox test failed", error: "Sandbox test could not run" };

function cloudConsoleUrl(project) { return `https://console.cloud.google.com/vertex-ai/agents/agent-engines?project=${encodeURIComponent(project)}`; }
function duration(seconds) { const s = Math.max(0, Math.round(seconds)); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`; }
function alertBox(kind, text) {
  const box = document.createElement("div"); box.className = `alert ${kind}`;
  const message = document.createElement("span"); message.textContent = text;
  box.append(icon(kind === "error" ? "circle-alert" : kind === "warning" ? "triangle-alert" : "info"), message); return box;
}

function progressSteps(plan) {
  const sandbox = plan.sandbox?.status || "not_run";
  const sandboxState = sandbox === "passed" ? "done" : ["failed", "error"].includes(sandbox) ? "failed" : sandbox === "running" ? "current" : plan.status === "planned" ? "" : "skipped";
  const deployFailed = ["failed", "unknown"].includes(plan.status);
  return [
    ["Snapshot", "done"],
    ["Sandbox test", sandboxState],
    ["Approve", plan.status === "planned" ? (sandbox === "running" ? "" : "current") : "done"],
    ["Deploy", plan.status === "planned" ? "" : ["submitting", "deploying"].includes(plan.status) ? "current" : deployFailed ? "failed" : "done"],
    ["Live", plan.status === "succeeded" ? "done" : ""],
  ];
}

function querySnippet(plan) {
  const head = `import vertexai\n\nclient = vertexai.Client(project="${plan.project}", location="${plan.region}")\nagent = client.agent_engines.get(name="${plan.resource}")\n`;
  if (plan.framework === "google-adk") {
    return `import asyncio\n${head}\nasync def main():\n    async for event in agent.async_stream_query(user_id="demo", message="Hello"):\n        print(event)\n\nasyncio.run(main())`;
  }
  return `${head}print(agent.query(message="Hello"))`;
}

function icons() { if (globalThis.lucide) globalThis.lucide.createIcons(); }
function setText(id, value) { byId(id).textContent = value; }
function readableBytes(value) { return value < 1024 ? `${value} B` : value < 1048576 ? `${(value / 1024).toFixed(1)} KiB` : `${(value / 1048576).toFixed(2)} MiB`; }
function showError(message) { setText("notice", message); byId("notice").hidden = false; }
function clearError() { byId("notice").hidden = true; }
function icon(name, spinning = false) { const element = document.createElement("i"); element.dataset.lucide = name; if (spinning) element.classList.add("spinning"); return element; }

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

function setBusy(busy, label = "Reviewing source...") {
  state.busy = busy;
  byId("deployment-form").querySelectorAll("input, button, select").forEach((input) => { input.disabled = busy; });
  const button = byId("review-button");
  button.replaceChildren(icon(busy ? "loader-circle" : "scan-line", busy), document.createTextNode(busy ? label : "Review deployment"));
  byId("new-button").disabled = busy;
  updateApproval();
  icons();
}

function invalidatePlan() {
  if (state.busy) return;
  state.plan = null;
  byId("review-content").hidden = true;
  byId("review-empty").hidden = false;
  setText("review-badge", "No snapshot");
  byId("review-badge").className = "badge neutral";
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
  byId("local-path").disabled = kind !== "local";
  byId("github-url").disabled = kind !== "github";
  byId("github-sha").disabled = kind !== "github";
  invalidatePlan();
}

function selectFile(file) {
  if (!file) return;
  state.uploadId = null;
  if (!file.name.toLowerCase().endsWith(".zip") || file.size > 8 * 1024 * 1024) {
    state.file = null;
    byId("zip-file").value = "";
    setText("upload-name", "Choose ZIP archive");
    setText("upload-size", "8 MiB maximum");
    showError("Choose a ZIP archive no larger than 8 MiB.");
    invalidatePlan();
    return;
  }
  state.file = file;
  setText("upload-name", file.name);
  setText("upload-size", readableBytes(file.size));
  clearError();
  invalidatePlan();
}

function updateApproval() {
  const sandboxStatus = state.plan?.sandbox?.status;
  const sandboxOk = !state.config?.sandbox_required || sandboxStatus === "passed";
  const canDeploy = state.config?.deployment_enabled && state.plan?.status === "planned" && !state.busy && sandboxOk
    && byId("approve-deployment").checked && byId("confirm-project").value === state.config.project;
  byId("sandbox-button").disabled = !state.config?.sandbox_enabled || !state.plan || state.busy || sandboxStatus === "running";
  byId("deploy-button").disabled = !canDeploy;
  byId("lock-state").hidden = Boolean(state.config?.deployment_enabled);
}

function showPlan(plan, resetApproval = false) {
  state.plan = plan;
  byId("review-empty").hidden = true;
  byId("review-content").hidden = false;
  setText("review-badge", statusNames[plan.status]);
  byId("review-badge").className = `badge ${plan.status}`;
  setText("snapshot-name", plan.display_name);
  setText("review-project", plan.project);
  setText("review-entrypoint", plan.entrypoint);
  setText("review-dependencies", plan.dependency_format === "uv" ? "uv.lock (locked export)" : (plan.requirements_file || "requirements.txt"));
  setText("review-size", `${readableBytes(plan.total_bytes)} / ${plan.files.length} files`);
  setText("review-digest", `${plan.digest.slice(0, 12)}...${plan.digest.slice(-8)}`);
  byId("review-digest").title = plan.digest;
  setText("files-summary", `Source files (${plan.files.length})`);
  byId("file-list").replaceChildren(...plan.files.map((file) => {
    const row = document.createElement("li");
    const name = document.createElement("span"); name.textContent = file.path;
    const size = document.createElement("span"); size.textContent = readableBytes(file.bytes);
    row.append(name, size); return row;
  }));
  byId("progress-steps").replaceChildren(...progressSteps(plan).map(([label, stepState]) => { const item = document.createElement("li"); item.textContent = label; if (stepState) item.className = stepState; return item; }));
  const alerts = [];
  if (plan.status === "failed") alerts.push(alertBox("error", plan.message));
  else if (plan.status === "unknown") alerts.push(alertBox("warning", plan.message));
  if (plan.status_check) alerts.push(alertBox("warning", `${plan.status_check}. The deployment may still be running in Google Cloud.`));
  for (const warning of plan.warnings || []) alerts.push(alertBox("warning", warning));
  byId("plan-alerts").replaceChildren(...alerts);
  const live = plan.status === "succeeded" && Boolean(plan.resource);
  byId("success-panel").hidden = !live;
  if (live) {
    setText("resource-name", plan.resource);
    setText("success-meta", `${plan.display_name} in ${plan.project} / ${plan.region}${plan.completed_at && plan.submitted_at ? ` - built in ${duration(plan.completed_at - plan.submitted_at)}` : ""}`);
    byId("open-cloud").href = cloudConsoleUrl(plan.project);
    setText("query-snippet", querySnippet(plan));
  }
  const active = ["submitting", "deploying"].includes(plan.status);
  byId("status-icon").replaceChildren(icon(active ? "loader-circle" : plan.status === "failed" ? "circle-alert" : "circle-check", active));
  setText("status-title", statusNames[plan.status]);
  setText("status-message", plan.status === "deploying" && plan.submitted_at ? `${plan.message} (${duration(Date.now() / 1000 - plan.submitted_at)} elapsed, usually a few minutes)` : plan.message);
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

function renderSandbox(sandbox) {
  const running = sandbox.status === "running";
  const iconName = running ? "loader-circle" : sandbox.status === "passed" ? "circle-check" : ["failed", "error"].includes(sandbox.status) ? "circle-alert" : "flask-conical";
  byId("sandbox-icon").replaceChildren(icon(iconName, running));
  setText("sandbox-title", sandboxNames[sandbox.status] || sandbox.status);
  const fallback = state.config?.sandbox_enabled ? "Runs this snapshot in an isolated Agent Engine code-execution sandbox before deployment." : "Sandbox testing is disabled. Set CONSOLE_SANDBOX_ENGINE and CONSOLE_ALLOW_SANDBOX_TEST=1.";
  setText("sandbox-message", sandbox.message || fallback);
  const log = [sandbox.details?.query_output && `query() returned: ${sandbox.details.query_output}`, sandbox.stdout && `stdout:\n${sandbox.stdout}`, sandbox.stderr && `stderr:\n${sandbox.stderr}`, sandbox.details?.traceback].filter(Boolean).join("\n\n");
  byId("sandbox-output").hidden = !log;
  setText("sandbox-log", log);
  byId("sandbox-section").hidden = state.plan?.status !== "planned" && sandbox.status === "not_run";
}

const viewTitles = { deploy: "Deploy agent", history: "Deployment history", environment: "Environment" };
function showView() {
  const requested = location.hash.slice(1);
  const view = viewTitles[requested] ? requested : "deploy";
  document.querySelectorAll("section.view").forEach((section) => { section.hidden = section.dataset.view !== view; });
  document.querySelectorAll(".nav-link[data-view]").forEach((link) => {
    const active = link.dataset.view === view;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
  setText("breadcrumb-current", viewTitles[view]);
  document.title = `${viewTitles[view]} | Agent Platform`;
  if (requested && requested !== view) return;
  window.scrollTo({ top: 0 });
}
window.addEventListener("hashchange", showView);

function renderHistory() {
  setText("history-count", String(state.plans.length));
  setText("stat-total", String(state.plans.length));
  setText("stat-succeeded", String(state.plans.filter((plan) => plan.status === "succeeded").length));
  setText("stat-active", String(state.plans.filter((plan) => ["submitting", "deploying"].includes(plan.status)).length));
  setText("stat-failed", String(state.plans.filter((plan) => ["failed", "unknown"].includes(plan.status)).length));
  if (!state.plans.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td"); cell.colSpan = 6; cell.className = "empty-history";
    const label = document.createElement("strong"); label.textContent = "No deployments yet";
    cell.append(icon("inbox"), label); row.append(cell); byId("history-body").replaceChildren(row); icons();
    return;
  }
  byId("history-body").replaceChildren(...state.plans.map((plan) => {
    const row = document.createElement("tr");
    const name = document.createElement("td"); name.textContent = plan.display_name;
    const source = document.createElement("td"); const sourceLabel = document.createElement("span");
    sourceLabel.className = "history-source"; sourceLabel.append(icon(sourceIcons[plan.source_kind]), document.createTextNode(sourceNames[plan.source_kind])); source.append(sourceLabel);
    const time = document.createElement("td"); time.textContent = new Date(plan.created_at * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const status = document.createElement("td"); const badge = document.createElement("span"); badge.className = `badge ${plan.status}`; badge.textContent = statusNames[plan.status]; status.append(badge);
    const cloud = document.createElement("td"); cloud.className = "resource-cell";
    if (plan.resource) { const link = document.createElement("a"); link.className = "history-link"; link.href = cloudConsoleUrl(plan.project); link.target = "_blank"; link.rel = "noreferrer"; link.title = plan.resource; link.append(document.createTextNode(`Engine ${plan.resource.split("/").pop()}`), icon("external-link")); cloud.append(link); }
    else cloud.textContent = plan.operation ? "Building..." : "-";
    const action = document.createElement("td"); const button = document.createElement("button"); button.type = "button"; button.className = "icon-button"; button.title = "Open deployment"; button.setAttribute("aria-label", `Open ${plan.display_name}`); button.append(icon("arrow-up-right"));
    button.addEventListener("click", () => { if (!state.busy) { showPlan(plan, true); location.hash = "#deploy"; requestAnimationFrame(() => byId("review-title").scrollIntoView({ behavior: "smooth", block: "start" })); } }); action.append(button);
    row.append(name, source, time, status, cloud, action); return row;
  }));
  icons();
}

async function refreshHistory() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    const data = await api("/api/plans");
    state.plans = data.plans;
    for (let index = 0; index < state.plans.length; index += 1) {
      if (state.plans[index].status === "deploying" || state.plans[index].sandbox?.status === "running") state.plans[index] = await api(`/api/plans/${state.plans[index].id}`);
    }
    const current = state.plans.find((plan) => plan.id === state.plan?.id);
    if (current) showPlan(current);
    else if (state.plan) invalidatePlan();
    renderHistory();
  } catch (error) { showError(error.message); }
  finally { state.refreshing = false; }
}

byId("deployment-form").addEventListener("submit", async (event) => {
  event.preventDefault(); clearError();
  if (!state.config) { showError("Environment is unavailable. Refresh the page."); return; }
  if (state.kind === "zip" && !state.file) { showError("Choose a ZIP archive."); return; }
  const body = { source_kind: state.kind, location: state.kind === "local" ? byId("local-path").value.trim() : byId("github-url").value.trim(), revision: byId("github-sha").value.trim(), subdirectory: byId("subdirectory").value.trim(), display_name: byId("agent-name").value.trim(), module: byId("entry-module").value.trim(), object_name: byId("entry-object").value.trim(), requirements: byId("requirements").value.trim(), framework: byId("framework").value };
  setBusy(true);
  try {
    if (state.kind === "zip") {
      if (!state.uploadId) {
        const upload = await api("/api/uploads", { method: "POST", body: state.file });
        state.uploadId = upload.id;
      }
      body.location = state.uploadId;
    }
    const plan = await api("/api/plans", { method: "POST", body: JSON.stringify(body) });
    if (state.kind === "zip") state.uploadId = null;
    showPlan(plan, true);
    await refreshHistory();
  } catch (error) {
    if (error.message.startsWith("Upload expired")) state.uploadId = null;
    showError(error.message);
  }
  finally { setBusy(false); selectRequiredFields(); }
});

function selectRequiredFields() {
  byId("local-path").disabled = state.kind !== "local";
  byId("github-url").disabled = state.kind !== "github";
  byId("github-sha").disabled = state.kind !== "github";
}

byId("deploy-button").addEventListener("click", async () => {
  if (byId("deploy-button").disabled || !state.plan) return;
  clearError(); setBusy(true, "Submitting deployment...");
  const plan = state.plan;
  try {
    const result = await api(`/api/plans/${plan.id}/deploy`, { method: "POST", body: JSON.stringify({ digest: plan.digest, project: byId("confirm-project").value, approve_cost: byId("approve-deployment").checked, approve_code: byId("approve-deployment").checked }) });
    showPlan(result);
    await refreshHistory();
  } catch (error) { showError(error.message); }
  finally { setBusy(false); selectRequiredFields(); }
});

byId("sandbox-button").addEventListener("click", async () => {
  if (byId("sandbox-button").disabled || !state.plan) return;
  clearError();
  try {
    showPlan(await api(`/api/plans/${state.plan.id}/sandbox-test`, { method: "POST" }));
    await refreshHistory();
  } catch (error) { showError(error.message); }
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
  if (state.busy || !globalThis.confirm("Discard all staged ZIP uploads? Reviewed plans and GCP deployments are not affected.")) return;
  clearError(); setBusy(true, "Clearing staged uploads...");
  try {
    await api("/api/uploads", { method: "DELETE" });
    state.uploadId = null;
  } catch (error) { showError(error.message); }
  finally { setBusy(false); selectRequiredFields(); }
});
byId("refresh-history").addEventListener("click", refreshHistory);
byId("new-button").addEventListener("click", () => { if (location.hash && location.hash !== "#deploy") location.hash = "#deploy"; byId("deployment-form").reset(); state.file = null; state.uploadId = null; const kind = state.config?.local_sources_enabled === false ? "github" : "local"; document.querySelector(`input[name="source-kind"][value="${kind}"]`).checked = true; selectSource(kind); setText("upload-name", "Choose ZIP archive"); setText("upload-size", "8 MiB maximum"); clearError(); byId("new-deployment").scrollIntoView({ behavior: "smooth" }); byId("local-path").focus({ preventScroll: true }); });
byId("framework").addEventListener("change", () => { setText("framework-label", byId("framework").value === "google-adk" ? "Google ADK" : "Custom agent"); invalidatePlan(); });
byId("check-cloud").addEventListener("click", async () => {
  const button = byId("check-cloud");
  const list = byId("preflight-result");
  button.disabled = true; list.hidden = false;
  const pending = document.createElement("li"); pending.className = "note"; pending.textContent = "Checking Google Cloud prerequisites...";
  list.replaceChildren(pending);
  const checkIcons = { passed: "circle-check", skipped: "circle-minus", blocked: "circle-x", missing: "circle-x", unknown: "circle-help" };
  const checkNames = { submission: "Cloud submission", billing: "Billing", vertex_api: "Vertex AI API", runtime_account: "Runtime service account", psc_attachment: "PSC network attachment", sandbox_engine: "Sandbox host engine", credentials: "Google credentials" };
  try {
    const report = await api("/api/preflight");
    const rows = report.checks.map((check) => {
      const row = document.createElement("li"); row.className = check.status;
      const body = document.createElement("div");
      const heading = document.createElement("strong"); heading.textContent = `${checkNames[check.id] || check.id} - ${check.status}`;
      const message = document.createElement("span"); message.textContent = check.message;
      body.append(heading, message); row.append(icon(checkIcons[check.status] || "circle-help"), body); return row;
    });
    const note = document.createElement("li"); note.className = "note"; note.textContent = report.note;
    list.replaceChildren(...rows, note); icons();
  } catch (error) { pending.textContent = error.message; }
  finally { button.disabled = false; }
});
async function copyText(text, button) { try { await navigator.clipboard.writeText(text); button.title = "Copied"; } catch { showError("Clipboard unavailable."); } }
byId("copy-resource").addEventListener("click", () => copyText(byId("resource-name").textContent, byId("copy-resource")));
byId("copy-snippet").addEventListener("click", () => copyText(byId("query-snippet").textContent, byId("copy-snippet")));
byId("copy-digest").addEventListener("click", async () => { try { await navigator.clipboard.writeText(state.plan.digest); byId("copy-digest").title = "Digest copied"; } catch { showError("Clipboard unavailable."); } });
byId("download-plan").addEventListener("click", () => {
  if (!state.plan) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(state.plan, null, 2)], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = `deployment-${state.plan.id}.json`; link.click(); URL.revokeObjectURL(url);
});

async function initialize() {
  showView(); icons(); selectSource("local");
  try {
    state.config = await api("/api/config");
    const config = state.config;
    setText("target-project", config.project); setText("target-region", config.region);
    setText("summary-project", config.project); setText("summary-region", config.region);
    byId("summary-submission").replaceChildren(Object.assign(document.createElement("span"), { className: `status-dot${config.deployment_enabled ? "" : " warning"}` }), document.createTextNode(config.deployment_enabled ? "Cloud submission enabled" : "Cloud submission locked"));
    byId("history-cloud-link").href = cloudConsoleUrl(config.project);
    setText("target-identity", config.runtime_account || "Agent Engine default service agent");
    setText("target-network", config.network_attachment ? `PSC: ${config.network_attachment.split("/").pop()}` : "Public (no PSC attachment)"); setText("allowed-root", config.source_root); setText("runtime-account", config.runtime_account || "Agent Engine default service agent");
    byId("local-path").placeholder = config.source_root;
    if (!config.local_sources_enabled) {
      const local = document.querySelector('input[name="source-kind"][value="local"]');
      local.closest("label").hidden = true;
      document.querySelector('input[name="source-kind"][value="github"]').checked = true;
      selectSource("github");
    }
    byId("submission-state").replaceChildren(Object.assign(document.createElement("span"), { className: `status-dot${config.deployment_enabled ? "" : " warning"}` }), document.createTextNode(config.deployment_enabled ? "Enabled" : "Locked"));
    const details = { ...(config.hosted ? { "Hosted on": "Cloud Run (Identity-Aware Proxy)", "Signed in as": config.user } : {}), "Project": config.project, "Region": config.region, ...(config.local_sources_enabled ? { "Allowed source root": config.source_root } : {}), "Runtime service account": config.runtime_account || "Agent Engine default service agent", "PSC attachment": config.network_attachment || "None (public Agent Engine networking)", "Cloud submission": config.deployment_enabled ? "Enabled" : "Locked", "Sandbox host engine": config.sandbox_engine || "Not configured", "Sandbox testing": config.sandbox_enabled ? (config.sandbox_required ? "Required before deploy" : "Enabled") : "Disabled" };
    byId("environment-details").replaceChildren(...Object.entries(details).map(([label, value]) => { const row = document.createElement("div"); const term = document.createElement("dt"); term.textContent = label; const description = document.createElement("dd"); description.textContent = value; row.append(term, description); return row; }));
    updateApproval();
    await refreshHistory();
  } catch (error) { showError(error.message); setText("submission-state", "Unavailable"); }
}

initialize();
setInterval(() => { if (!document.hidden && !state.busy) refreshHistory(); }, 6000);
setInterval(() => { if (state.plan?.status === "deploying" && state.plan.submitted_at) setText("status-message", `${state.plan.message} (${duration(Date.now() / 1000 - state.plan.submitted_at)} elapsed, usually a few minutes)`); }, 1000);