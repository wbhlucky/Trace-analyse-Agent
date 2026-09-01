"use strict";

const state = {
  cases: [],
  activeId: null,
  bundle: null,
  query: "",
  runningJobs: [],
};

const els = {
  list: document.getElementById("case-list"),
  stats: document.getElementById("stats-line"),
  search: document.getElementById("search"),
  refresh: document.getElementById("refresh"),
  serverStatus: document.getElementById("server-status"),
  empty: document.getElementById("empty-state"),
  detail: document.getElementById("detail"),
  crumbs: document.getElementById("crumbs"),
  title: document.getElementById("case-title"),
  subtitle: document.getElementById("case-subtitle"),
  openReport: document.getElementById("open-report"),
  analyzeOpen: document.getElementById("analyze-open"),
  analyzeModal: document.getElementById("analyze-modal"),
  analyzeClose: document.getElementById("analyze-close"),
  analyzeCancel: document.getElementById("analyze-cancel"),
  analyzeForm: document.getElementById("analyze-form"),
  analyzeSubmit: document.getElementById("analyze-submit"),
  jobsList: document.getElementById("jobs-list"),
  jobsTitle: document.getElementById("jobs-title"),
  tabs: document.getElementById("tabs"),
  panelOverview: document.getElementById("panel-overview"),
  panelFindings: document.getElementById("panel-findings"),
  panelEvidence: document.getElementById("panel-evidence"),
  panelRun: document.getElementById("panel-run"),
  panelJson: document.getElementById("panel-json"),
};

const SEVERITY_LABEL = {
  critical: "严重",
  high: "高",
  medium: "中",
  low: "低",
};

const STATUS_LABEL = {
  completed: "已完成",
  failed: "失败",
  running: "运行中",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatNumber(value) {
  if (value == null) return "—";
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  if (Math.abs(num) >= 100) return num.toFixed(num >= 10 ? 1 : 0).replace(/\.0$/, "");
  return num.toFixed(2).replace(/\.?0+$/, "");
}

function formatNs(ns) {
  if (ns == null) return "—";
  return String(ns);
}

function formatDateTime(value) {
  return value ? escapeHtml(value) : "—";
}

function parseIso(value) {
  if (!value) return null;
  const d = new Date(String(value).replace("Z", "+00:00"));
  return Number.isNaN(d.getTime()) ? null : d;
}

function computeDurationSeconds(run) {
  const started = parseIso(run && run.started_at);
  const completed = parseIso(run && run.completed_at);
  if (started && completed) {
    return Math.max(0, (completed.getTime() - started.getTime()) / 1000);
  }
  if (started) {
    return Math.max(0, (Date.now() - started.getTime()) / 1000);
  }
  return null;
}

function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const value = Math.max(0, seconds);
  if (value >= 3600) {
    const h = Math.floor(value / 3600);
    const m = Math.floor((value % 3600) / 60);
    return `${h}h ${m}m`;
  }
  if (value >= 60) {
    const m = Math.floor(value / 60);
    const s = value % 60;
    return `${m}m ${s.toFixed(0)}s`;
  }
  return `${value.toFixed(2)}s`;
}

function sevClass(sev) {
  return ["critical", "high", "medium", "low"].includes(sev) ? sev : "none";
}

function statusClass(status) {
  return ["completed", "failed", "running"].includes(status) ? status : "none";
}

function setServerStatus(kind, text) {
  els.serverStatus.textContent = text;
  els.serverStatus.classList.toggle("ok", kind === "ok");
  els.serverStatus.classList.toggle("err", kind === "err");
}

async function fetchJson(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function loadCases() {
  setServerStatus("", "加载中");
  try {
    const data = await fetchJson("/api/cases");
    state.cases = data.cases || [];
    renderList();
    setServerStatus("ok", `${state.cases.length} 个用例`);
    if (state.activeId) {
      const exists = state.cases.some((c) => c.id === state.activeId);
      if (!exists) selectCase(null);
    } else if (state.cases.length) {
      selectCase(state.cases[0].id);
    }
  } catch (err) {
    setServerStatus("err", "连接失败");
    els.empty.querySelector("p").textContent = "无法连接到面板服务。请确认已运行 serve 命令。";
  }
}

function filteredCases() {
  const q = state.query.trim().toLowerCase();
  if (!q) return state.cases;
  return state.cases.filter((c) =>
    [c.id, c.name, c.scenario, c.symptom, c.scenario_type_label, c.model, c.status]
      .filter(Boolean)
      .some((v) => String(v).toLowerCase().includes(q))
  );
}

function renderList() {
  const items = filteredCases();
  els.stats.textContent = items.length
    ? `共 ${items.length} 个用例`
    : "未找到匹配用例";

  els.list.innerHTML = items.map((c) => {
    const metric = c.metric_value != null
      ? `<span class="case-card-metric">${escapeHtml(formatNumber(c.metric_value))} ${escapeHtml(c.metric_unit || "ms")}</span>`
      : "";
    const sev = c.severity
      ? `<span class="pill sev ${sevClass(c.severity)}">${escapeHtml(SEVERITY_LABEL[c.severity] || c.severity)}</span>`
      : "";
    const status = `<span class="pill status-badge ${statusClass(c.status)}">${escapeHtml(STATUS_LABEL[c.status] || c.status || "—")}</span>`;
    const durationChip = c.duration_display
      ? `<span class="duration-chip">${escapeHtml(c.duration_display)}</span>`
      : "";
    return `
      <button class="case-card ${c.id === state.activeId ? "active" : ""}" data-id="${escapeHtml(c.id)}">
        <div class="case-card-top">
          <span class="case-card-title">${escapeHtml(c.name || c.id)}</span>
          ${metric}
          ${durationChip}
        </div>
        <div class="case-card-top" style="margin-top:6px">
          <span class="pill type">${escapeHtml(c.scenario_type_label || c.scenario_type || "")}</span>
          ${sev}
          ${status}
        </div>
        <div class="case-card-sub">${escapeHtml(c.scenario || c.symptom || "")}</div>
      </button>`;
  }).join("");

  els.list.querySelectorAll(".case-card").forEach((node) => {
    node.addEventListener("click", () => selectCase(node.dataset.id));
  });
}

async function selectCase(id) {
  state.activeId = id;
  renderList();
  if (!id) {
    state.bundle = null;
    els.detail.classList.add("hidden");
    els.empty.classList.remove("hidden");
    return;
  }
  els.empty.classList.add("hidden");
  els.crumbs.textContent = id;
  els.title.textContent = "加载中…";
  els.subtitle.textContent = "";
  els.openReport.hidden = true;
  els.detail.classList.remove("hidden");

  try {
    state.bundle = await fetchJson(`/api/cases/${encodeURIComponent(id)}`);
    renderCase(state.bundle);
    switchTab("overview");
  } catch (err) {
    els.title.textContent = id;
    els.subtitle.textContent = "加载失败，请重试。";
    renderPanelsEmpty();
  }
}

function runFiles() {
  return state.bundle?.files || {};
}

function renderCase(bundle) {
  const files = bundle.files || {};
  const run = files.run || {};
  const findings = files.findings || {};

  els.crumbs.textContent = "results / " + bundle.name;
  els.title.textContent = run.trace_id || bundle.name;
  const meta = [
    run.scenario_type,
    run.scenario,
    run.model,
    run.started_at ? `开始于 ${run.started_at}` : null,
  ].filter(Boolean);
  els.subtitle.textContent = meta.join(" · ");

  els.openReport.hidden = !bundle.report_available;
  els.openReport.href = `/reports/${encodeURIComponent(bundle.name)}/report.html`;

  renderOverview(files);
  renderFindings(findings);
  renderEvidence(files.evidence);
  renderRun(run);
  renderJson(bundle);
}

function renderPanelsEmpty() {
  [els.panelOverview, els.panelFindings, els.panelEvidence, els.panelRun, els.panelJson]
    .forEach((p) => { p.innerHTML = `<div class="card wide"><div class="empty-note">暂无可显示内容</div></div>`; });
}

function durationCard(run) {
  const seconds = computeDurationSeconds(run);
  const running = run.status === "running";
  return `<div class="card wide">
    <h3>${running ? "实时分析时长" : "分析总耗时"}</h3>
    <div class="metric-value" id="active-duration-value">${escapeHtml(formatDuration(seconds))}</div>
    <div class="metric-label">${running ? "任务仍在进行中，数值会定时更新" : "从任务开始到完成"}</div>
  </div>`;
}

function renderOverview(files) {
  const findings = files.findings || {};
  const run = files.run || {};
  const validation = files.validation || {};

  const cards = [];
  cards.push(overviewCard("分析总结", findings.summary || "该用例暂无总结。"));
  cards.push(durationCard(run));

  const completion = findings.completion_latency;
  if (completion && typeof completion === "object") {
    cards.push(overviewCard("完成时延指标", [
      metricGrid([
        { label: "完成时延", value: completion.completion_latency_ms },
        { label: "响应时延", value: completion.response_latency_ms },
        { label: "响应后耗时", value: completion.post_response_duration_ms },
      ]),
      cpBlock("关键路径", completion.critical_path_summary),
    ]));
  }

  const cold = findings.cold_start;
  if (cold && typeof cold === "object") {
    cards.push(overviewCard("冷启动指标", [
      metricGrid([
        { label: "总耗时", value: cold.total_duration_ms },
        { label: "呈现耗时", value: cold.presentation_duration_ms },
      ]),
      cpBlock("关键路径", cold.critical_path_summary),
    ]));
  }

  const problem = findings.problem_interval;
  if (problem && typeof problem === "object") {
    cards.push(overviewCard("问题区间", intervalBlock(problem)));
  }

  if (findings.limitations && Array.isArray(findings.limitations)) {
    const items = findings.limitations.map((l) => `<li>${escapeHtml(l)}</li>`).join("");
    cards.push(overviewCard("局限性", `<ul class="limits-list">${items}</ul>`));
  }

  const v = validation;
  if (v && typeof v === "object") {
    const badge = v.valid
      ? `<span class="pill status-badge completed">校验通过</span>`
      : `<span class="pill sev critical">校验失败</span>`;
    let extra = "";
    extra += chipList("errors", v.errors, "错误");
    extra += chipList("warnings", v.warnings, "警告");
    cards.push(overviewCard("校验结果", `${badge}${extra}`));
  }

  if (run.trace_capabilities && Array.isArray(run.trace_capabilities)) {
    cards.push(overviewCard("Trace 能力", chipListPlain(run.trace_capabilities)));
  }

  els.panelOverview.innerHTML = `<div class="grid">${cards.join("")}</div>`;
}

function overviewCard(title, body) {
  return `<div class="card wide"><h3>${escapeHtml(title)}</h3>${body}</div>`;
}

function metricGrid(items) {
  return `<div class="grid metrics" style="margin-top:4px">${items.map((m) => `
    <div class="card">
      <div class="metric-value">${escapeHtml(formatNumber(m.value))}<span class="metric-unit">ms</span></div>
      <div class="metric-label">${escapeHtml(m.label)}</div>
    </div>`).join("")}</div>`;
}

function cpBlock(title, text) {
  if (!text) return "";
  return `<div style="margin-top:12px"><div class="finding-body"><div class="label">${escapeHtml(title)}</div><p>${escapeHtml(text)}</p></div></div>`;
}

function intervalBlock(problem) {
  const start = problem.start_boundary || {};
  const end = problem.end_boundary || {};
  const rows = [
    ["起点", start.name],
    ["起点时间戳", formatNs(start.timestamp_ns)],
    ["终点", end.name],
    ["终点时间戳", formatNs(end.timestamp_ns)],
    ["时长", problem.duration_ms != null ? `${formatNumber(problem.duration_ms)} ms` : null],
    ["指标定义", problem.metric_definition],
    ["选择规则", problem.selection_rule],
  ];
  return kv(rows);
}

function kv(rows) {
  const valid = rows.filter(([, v]) => v != null && v !== "");
  if (!valid.length) return `<div class="empty-note">无数据</div>`;
  return `<dl class="kv">${valid.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("")}</dl>`;
}

function chipList(key, arr, label) {
  if (!Array.isArray(arr) || !arr.length) return "";
  return `<div style="margin-top:10px"><div class="label" style="color:var(--faint);font-size:12px;margin-bottom:4px">${escapeHtml(label)}</div><div class="chip-list">${arr.map((x) => `<span class="chip">${escapeHtml(x)}</span>`).join("")}</div></div>`;
}

function chipListPlain(arr) {
  return `<div class="chip-list">${arr.map((x) => `<span class="chip">${escapeHtml(x)}</span>`).join("")}</div>`;
}

function renderFindings(findings) {
  const items = findings?.findings || [];
  if (!items.length) {
    els.panelFindings.innerHTML = `<div class="card wide"><div class="empty-note">该用例没有发现项。</div></div>`;
    return;
  }
  const sorted = [...items].sort((a, b) => {
    const rank = { critical: 4, high: 3, medium: 2, low: 1 };
    return (rank[b.severity] || 0) - (rank[a.severity] || 0);
  });
  els.panelFindings.innerHTML = `<div class="grid finding">${sorted.map(findingCard).join("")}</div>`;
}

function findingCard(f) {
  const sev = f.severity || "low";
  const conf = f.confidence != null ? Math.round(Number(f.confidence) * 100) : null;
  const evidence = (f.evidence_ids || []).map((id) => `<span class="chip">${escapeHtml(id)}</span>`).join("");
  return `
  <div class="card finding-card ${sevClass(sev)}">
    <div class="finding-head">
      <div class="finding-title">${escapeHtml(f.title)}</div>
      <span class="pill sev ${sevClass(sev)}">${escapeHtml(SEVERITY_LABEL[sev] || sev)}</span>
    </div>
    <div class="finding-meta">
      <span>状态：${escapeHtml(f.status || "—")}</span>
      ${conf != null ? `<span class="confidence">置信度 <span class="bar"><i style="width:${conf}%"></i></span> ${conf}%</span>` : ""}
    </div>
    ${field("分析", f.analysis)}
    ${field("建议", f.recommendation)}
    ${field("验证方式", f.verification)}
    ${evidence ? `<div class="finding-body"><div class="label">关联证据</div><div class="chip-list">${evidence}</div></div>` : ""}
  </div>`;
}

function field(label, value) {
  if (!value) return "";
  return `<div class="finding-body"><div class="label">${escapeHtml(label)}</div><p>${escapeHtml(value)}</p></div>`;
}

function renderEvidence(evidence) {
  const items = Array.isArray(evidence) ? evidence : [];
  if (!items.length) {
    els.panelEvidence.innerHTML = `<div class="card wide"><div class="empty-note">该用例没有证据记录。</div></div>`;
    return;
  }
  els.panelEvidence.innerHTML = items.map((ev) => `
    <div class="card evidence-card">
      <div class="evidence-head">
        <span class="evidence-identity">${escapeHtml(ev.evidence_id || "—")}</span>
        <span class="evidence-tool">${escapeHtml(ev.tool || "")}</span>
      </div>
      <p class="evidence-summary">${escapeHtml(ev.summary || "")}</p>
      <pre class="json">${escapeHtml(pretty(ev.data))}</pre>
    </div>`).join("");
}

function renderRun(run) {
  const rows = [
    ["Run ID", run.run_id],
    ["分析总耗时", formatDuration(computeDurationSeconds(run))],
    ["Trace ID", run.trace_id],
    ["状态", run.status],
    ["Agent", run.agent],
    ["Provider", run.provider],
    ["模型", run.model],
    ["场景类型", run.scenario_type],
    ["场景", run.scenario],
    ["症状", run.symptom],
    ["设备", run.device],
    ["构建版本", run.build],
    ["分析区间", run.time_range],
    ["目标进程", run.target_process],
    ["问题时长(ms)", run.problem_duration_ms],
    ["刷新率(Hz)", run.refresh_rate_hz],
    ["开始时间", run.started_at],
    ["结束时间", run.completed_at],
    ["Trace 路径", run.trace_path],
    ["数据库", run.database_path],
    ["错误", run.error],
  ];
  const body = kv(rows);

  const skills = (run.skills || []).map((s) => `${s.name}${s.fingerprint ? ` · ${s.fingerprint}` : ""}`);
  const tools = run.available_tools || [];
  const conversions = run.trace_conversions || [];

  let extra = "";
  extra += `<div class="section-head">Skills</div>${chipListPlain(skills.length ? skills : [])}`;
  extra += `<div class="section-head">可用工具</div>${chipListPlain(tools)}`;

  if (conversions.length) {
    const convHtml = conversions.map((c) => {
      const rows2 = [
        ["角色", c.role],
        ["可执行文件", c.executable],
        ["版本", c.version],
        ["数据库", c.database_path],
        ["耗时", c.duration_ms != null ? `${formatNumber(c.duration_ms)} ms` : null],
        ["返回码", c.return_code],
        ["缓存命中", c.cache_hit ? "是" : "否"],
        ["物化方式", c.materialization],
      ];
      return `<div class="card wide">${kv(rows2)}</div>`;
    }).join("");
    extra += `<div class="section-head">Trace 转换</div><div class="grid">${convHtml}</div>`;
  }

  els.panelRun.innerHTML = `<div class="grid"><div class="card wide">${body}</div></div>${extra}`;
}

function renderJson(bundle) {
  const files = bundle.files || {};
  const names = Object.keys(files)
    .filter((k) => files[k] != null)
    .sort((a, b) => a.localeCompare(b));

  els.panelJson.innerHTML = names.map((name) => `
    <details class="card evidence-card" ${name === "run" ? "open" : ""}>
      <summary class="evidence-head" style="cursor:pointer">
        <span class="evidence-identity">${escapeHtml(name)}.json</span>
        <span class="evidence-tool">${escapeHtml(typeOf(files[name]))}</span>
      </summary>
      <pre class="json">${escapeHtml(pretty(files[name]))}</pre>
    </details>`).join("");
}

function typeOf(value) {
  if (Array.isArray(value)) return `array (${value.length})`;
  if (value && typeof value === "object") return "object";
  return typeof value;
}

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function switchTab(name) {
  els.tabs.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((p) => {
    p.classList.toggle("active", p.id === `panel-${name}`);
  });
}

els.tabs.addEventListener("click", (event) => {
  const tab = event.target.closest(".tab");
  if (tab) switchTab(tab.dataset.tab);
});

els.search.addEventListener("input", () => {
  state.query = els.search.value;
  renderList();
});

els.refresh.addEventListener("click", () => loadCases());

// --- 发起新的分析 / 任务状态 ---
let jobsTimer = null;

function isModalOpen() {
  return !els.analyzeModal.classList.contains("hidden");
}

function openAnalyzeModal() {
  els.analyzeModal.classList.remove("hidden");
  loadJobs();
  if (!jobsTimer) jobsTimer = setInterval(loadJobs, 2000);
}

function closeAnalyzeModal() {
  els.analyzeModal.classList.add("hidden");
  if (jobsTimer) {
    clearInterval(jobsTimer);
    jobsTimer = null;
  }
}

els.analyzeOpen.addEventListener("click", openAnalyzeModal);
els.analyzeClose.addEventListener("click", closeAnalyzeModal);
els.analyzeCancel.addEventListener("click", closeAnalyzeModal);
els.analyzeModal.addEventListener("click", (event) => {
  if (event.target === els.analyzeModal) closeAnalyzeModal();
});

async function loadJobs() {
  try {
    const data = await fetchJson("/api/analyze/jobs");
    state.runningJobs = data.jobs || [];
    renderJobs(state.runningJobs);
  } catch (err) {
    /* keep the previous job list on transient failure */
  }
}

function jobStatusClass(status) {
  return status === "failed" ? "failed" : status === "running" ? "running" : "completed";
}

function jobStatusLabel(status) {
  return { queued: "排队中", running: "运行中", completed: "已完成", failed: "失败" }[status] || status;
}

function renderJobs(jobs) {
  if (!jobs.length) {
    els.jobsList.innerHTML = `<div class="empty-note">暂无任务</div>`;
    return;
  }
  els.jobsList.innerHTML = jobs.map((j) => `
    <div class="job-item">
      <div class="job-main">
        <div class="job-case">${escapeHtml(j.case_name || (j.params && j.params.trace_path) || j.id)}</div>
        <div class="job-meta">${escapeHtml(j.error || j.output_dir || "")}</div>
      </div>
      <span class="job-duration">${escapeHtml(j.duration_display || "—")}</span>
      <span class="pill status-badge ${jobStatusClass(j.status)}">${escapeHtml(jobStatusLabel(j.status))}</span>
    </div>`).join("");
}

els.analyzeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const formData = new FormData(els.analyzeForm);
  const payload = {};
  for (const [key, value] of formData.entries()) {
    const trimmed = String(value).trim();
    if (!trimmed) continue;
    if (key === "problem_duration_ms" || key === "refresh_rate_hz") {
      const num = Number(trimmed);
      if (!Number.isNaN(num)) payload[key] = num;
    } else {
      payload[key] = trimmed;
    }
  }
  els.analyzeSubmit.disabled = true;
  els.analyzeSubmit.textContent = "提交中…";
  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    els.analyzeForm.reset();
    await loadJobs();
    await loadCases();
  } catch (err) {
    alert(`发起分析失败：${err.message}`);
  } finally {
    els.analyzeSubmit.disabled = false;
    els.analyzeSubmit.textContent = "开始分析";
  }
});

// 周期性刷新：任务运行中或有用例正在分析时，更新时长与列表。
function hasLiveWork() {
  return (
    state.cases.some((c) => c.running) ||
    state.runningJobs.some((j) => j.status === "queued" || j.status === "running")
  );
}

setInterval(() => {
  if (hasLiveWork()) {
    loadCases();
    if (isModalOpen()) loadJobs();
    const active = state.cases.find((c) => c.id === state.activeId);
    if (active && active.running) selectCase(state.activeId);
  }
}, 3000);

loadCases();
