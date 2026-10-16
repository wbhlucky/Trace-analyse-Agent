"use strict";

const state = {
  cases: [],
  activeId: null,
  bundle: null,
  query: "",
  runningJobs: [],
  jobEvents: {},
  preflight: null,
  view: "case",
  memory: null,
  memoryTrust: "all",
  memoryQuery: "",
  memoryFlash: null,
  memoryProbeOpen: false,
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
  analyzeError: document.getElementById("analyze-error"),
  jobsList: document.getElementById("jobs-list"),
  jobsTitle: document.getElementById("jobs-title"),
  tabs: document.getElementById("tabs"),
  panelOverview: document.getElementById("panel-overview"),
  panelFindings: document.getElementById("panel-findings"),
  panelEvidence: document.getElementById("panel-evidence"),
  panelRun: document.getElementById("panel-run"),
  panelJson: document.getElementById("panel-json"),
  emptyBody: document.querySelector("#empty-state"),
  memoryOpen: document.getElementById("memory-open"),
  memoryView: document.getElementById("memory-view"),
  memoryCrumbs: document.getElementById("memory-crumbs"),
  memorySubtitle: document.getElementById("memory-subtitle"),
  memoryBody: document.getElementById("memory-body"),
  memorySearch: document.getElementById("memory-search"),
  memoryTrustFilter: document.getElementById("memory-trust-filter"),
  memoryMaintain: document.getElementById("memory-maintain"),
  memoryProbe: document.getElementById("memory-probe"),
  memoryProbeToggle: document.getElementById("memory-probe-toggle"),
  memoryProbeQuery: document.getElementById("memory-probe-query"),
  memoryProbeScenario: document.getElementById("memory-probe-scenario"),
  memoryProbeRun: document.getElementById("memory-probe-run"),
  memoryProbeResult: document.getElementById("memory-probe-result"),
  memoryBack: document.getElementById("memory-back"),
  preflightBox: null,
  scenarioType: null,
  scenario: null,
  symptom: null,
  problemDuration: null,
  tracePath: null,
  fields: {},
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
const STEP_LABELS = {
  "prepare.trace": "准备 Trace",
  "prepare": "准备",
  "discover": "发现候选",
  "analyze": "性能分析",
  "root-cause": "根因归因",
  "evidence": "证据核验",
  "report": "报告生成",
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
  if (status === "done") return "completed";
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

const CHECK_STATUS_LABEL = {
  ok: "\u5c31\u7eea",
  warn: "\u9700\u6ce8\u610f",
  error: "\u672a\u914d\u7f6e",
};

async function loadPreflight() {
  try {
    state.preflight = await fetchJson("/api/preflight");
  } catch (err) {
    state.preflight = null;
  }
  renderPreflight();
  renderEmptyState();
}

function renderPreflight() {
  const data = state.preflight;
  const containers = [
    els.preflightBox,
    document.getElementById("modal-preflight"),
  ].filter(Boolean);
  if (!containers.length) return;
  if (!data || !data.checks) {
    containers.forEach((container) => { container.innerHTML = ""; });
    return;
  }
  const rows = data.checks.map((check) => `
    <li class="preflight-item ${check.status}">
      <span class="preflight-dot"></span>
      <div class="preflight-copy">
        <div class="preflight-label">${escapeHtml(check.label)}</div>
        <div class="preflight-detail">${escapeHtml(check.detail)}</div>
        ${check.action ? `<div class="preflight-action">${escapeHtml(check.action)}</div>` : ""}
      </div>
      <span class="pill status-badge ${check.status === "ok" ? "completed" : check.status === "warn" ? "running" : "failed"}">${escapeHtml(CHECK_STATUS_LABEL[check.status] || check.status)}</span>
    </li>`).join("");
  const html = `
    <div class="preflight-head">
      <span>环境自检</span>
      <span class="preflight-summary">${escapeHtml(data.summary || "")}</span>
    </div>
    <ul class="preflight-list">${rows}</ul>`;
  containers.forEach((container) => { container.innerHTML = html; });
}

function renderEmptyState() {
  const el = els.emptyBody;
  if (!el) return;
  const data = state.preflight;
  const checks = data && data.checks ? data.checks : [];
  const ready = data ? data.ready : false;
  const badge = !data
    ? '<span class="pill status-badge failed">\u65e0\u6cd5\u81ea\u68c0</span>'
    : ready
      ? '<span class="pill status-badge completed">\u73af\u5883\u5c31\u7eea</span>'
      : '<span class="pill status-badge running">\u90e8\u5206\u7f3a\u5931</span>';
  el.innerHTML = `
    <div class="empty-glow">\u8c03</div>
    <h1>${checks.some((c) => c.status === "error") ? "\u5148\u8865\u5168\u914d\u7f6e\uff0c\u518d\u5f00\u59cb\u5206\u6790" : "\u9009\u62e9\u6216\u53d1\u8d77\u4e00\u4e2a\u5206\u6790\u7528\u4f8b"}</h1>
    <p>\u5de6\u4fa7\u5217\u51fa <code>results/</code> \u4e0b\u7684\u5206\u6790\u7ed3\u679c\uff1b\u70b9\u51fb\u201c\u53d1\u8d77\u5206\u6790\u201d\u63d0\u4ea4\u65b0\u7684 Agent \u4efb\u52a1\u3002</p>
    <div class="preflight" id="preflight-box">${badge}</div>`;
  els.preflightBox = document.getElementById("preflight-box");
  renderPreflight();
}

async function loadCases() {
  setServerStatus("", "加载中");
  try {
    const data = await fetchJson("/api/cases");
    state.cases = data.cases || [];
    renderList();
    setServerStatus("ok", `${state.cases.length} 个用例`);
    // The memory view is corpus-level, so a background case refresh must not
    // yank the operator back into a per-case screen they left on purpose.
    if (state.view === "memory") return;
    if (state.activeId) {
      const exists = state.cases.some((c) => c.id === state.activeId);
      if (!exists) selectCase(null);
    } else if (state.cases.length) {
      selectCase(state.cases[0].id);
    }
  } catch (err) {
    setServerStatus("err", "连接失败");
    const emptyEl = els.emptyBody;
    if (emptyEl) {
      emptyEl.querySelector("h1").textContent = "无法连接到面板服务";
      emptyEl.querySelector("p").textContent = "请确认已运行 serve 命令，然后刷新页面。";
    }
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
      <button class="case-card ${c.id === state.activeId && state.view === "case" ? "active" : ""}" data-id="${escapeHtml(c.id)}">
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
  hideMemoryView();
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
  attachFindingLinks(els.panelFindings);
}

function findingCard(f) {
  const sev = f.severity || "low";
  const conf = f.confidence != null ? Math.round(Number(f.confidence) * 100) : null;
  const evidence = (f.evidence_ids || []).map((id) => `<button type="button" class="chip link" data-jump-evidence="${escapeHtml(id)}">${escapeHtml(id)} \u2192</button>`).join("");
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

function jumpToEvidence(id) {
  switchTab("evidence");
  if (!state.bundle) return;
  els.panelEvidence.querySelectorAll("[data-evidence-id]").forEach((node) => {
    const match = node.dataset.evidenceId === String(id);
    node.classList.toggle("flash", match);
    if (match) {
      setTimeout(() => node.scrollIntoView({ behavior: "smooth", block: "nearest" }), 30);
      setTimeout(() => node.classList.remove("flash"), 1600);
    }
  });
}

function attachFindingLinks(root) {
  root.querySelectorAll("[data-jump-evidence]").forEach((btn) => {
    btn.addEventListener("click", () => jumpToEvidence(btn.dataset.jumpEvidence));
  });
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
    <div class="card evidence-card" data-evidence-id="${escapeHtml(ev.evidence_id || "\u2014")}">
      <div class="evidence-head">
        <span class="evidence-identity">${escapeHtml(ev.evidence_id || "—")}</span>
        <span class="evidence-tool">${escapeHtml(ev.tool || "")}</span>
      </div>
      <p class="evidence-summary">${escapeHtml(ev.summary || "")}</p>
      <pre class="json">${escapeHtml(pretty(ev.data))}</pre>
    </div>`).join("");
}

const STEP_STATUS_LABEL = {
  done: "\u5df2\u5b8c\u6210",
  running: "\u8fd0\u884c\u4e2d",
  failed: "\u5931\u8d25",
  pending: "\u7b49\u5f85\u4e2d",
  skipped: "\u5df2\u8df3\u8fc7",
};

function renderActivityTimeline(run) {
  const steps = Array.isArray(run.step_states) ? run.step_states : [];
  const phases = steps.filter((st) => st && st.step);
  if (!phases.length) return "";
  const labelOf = (st) => {
    const step = String(st.step || "");
    return STEP_LABELS[step] || step;
  };
  const elapsed = (st) => {
    const a = parseIso(st.started_at);
    const b = parseIso(st.completed_at);
    if (a && b) return formatDuration(Math.max(0, (b.getTime() - a.getTime()) / 1000));
    return st.started_at ? "\u8fdb\u884c\u4e2d" : "";
  };
  const items = phases.map((st) => {
    const status = st.status || "pending";
    const dur = elapsed(st);
    return `
    <li class="timeline-item ${status}">
      <span class="timeline-marker"></span>
      <div class="timeline-copy">
        <div class="timeline-head">
          <span class="timeline-step">${escapeHtml(labelOf(st))}</span>
          <span class="pill status-badge ${statusClass(status)}">${escapeHtml(STEP_STATUS_LABEL[status] || status)}</span>
          ${dur ? `<span class="timeline-duration">${escapeHtml(dur)}</span>` : ""}
        </div>
        ${st.error ? `<div class="timeline-error">${escapeHtml(st.error)}</div>` : ""}
      </div>
    </li>`;
  }).join("");
  return `
    <div class="card wide">
      <h3>Agent \u6d3b\u52a8\u65f6\u95f4\u7ebf</h3>
      <ul class="timeline">${items}</ul>
    </div>`;
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

  const timeline = renderActivityTimeline(run);
  els.panelRun.innerHTML = `${timeline}<div class="grid"><div class="card wide">${body}</div></div>${extra}`;
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

els.refresh.addEventListener("click", () => {
  loadCases();
  if (state.view === "memory") loadMemory();
});

// --- 发起新的分析 / 任务状态 ---
let jobsTimer = null;
const eventSources = new Map();

function isModalOpen() {
  return !els.analyzeModal.classList.contains("hidden");
}

function openAnalyzeModal() {
  els.analyzeModal.classList.remove("hidden");
  loadJobs();
  if (!jobsTimer) jobsTimer = setInterval(loadJobs, 2000);
}

function closeAllEventSources() {
  for (const source of eventSources.values()) source.close();
  eventSources.clear();
}

function closeAnalyzeModal() {
  els.analyzeModal.classList.add("hidden");
  if (jobsTimer) {
    clearInterval(jobsTimer);
    jobsTimer = null;
  }
  closeAllEventSources();
}

function newestRunningJob() {
  return [...state.runningJobs].reverse().find(
    (j) => j.status === "queued" || j.status === "running"
  );
}

async function cancelNewestRunningJob() {
  const job = newestRunningJob();
  if (!job) {
    closeAnalyzeModal();
    return;
  }
  try {
    const res = await fetch(`/runs/${encodeURIComponent(job.id)}/cancel`, {
      method: "POST",
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (err) {
    /* cancellation is advisory; keep the modal open for polling fallback */
  }
  closeAnalyzeModal();
}

els.analyzeOpen.addEventListener("click", openAnalyzeModal);
els.analyzeClose.addEventListener("click", closeAnalyzeModal);
els.analyzeCancel.addEventListener("click", cancelNewestRunningJob);
els.analyzeModal.addEventListener("click", (event) => {
  if (event.target === els.analyzeModal) closeAnalyzeModal();
});

async function loadJobs() {
  try {
    const data = await fetchJson("/api/analyze/jobs");
    state.runningJobs = data.jobs || [];
    renderJobs(state.runningJobs);
    syncEventSources(state.runningJobs);
  } catch (err) {
    /* keep the previous job list on transient failure */
  }
}

function openJobStream(job) {
  if (eventSources.has(job.id)) return;
  if (!state.jobEvents[job.id]) {
    state.jobEvents[job.id] = {
      phase: null,
      phaseMessage: null,
      modelText: "",
      tools: [],
      sequence: 0,
    };
  }
  const source = new EventSource(
    `/api/analyze/jobs/${encodeURIComponent(job.id)}/events`
  );
  eventSources.set(job.id, source);

  source.onmessage = (message) => {
    let event = null;
    try {
      event = JSON.parse(message.data);
    } catch (err) {
      return;
    }
    applyJobEvent(job.id, event, message.lastEventId);
  };
  source.onerror = () => {
    source.close();
    eventSources.delete(job.id);
    loadJobs();
  };
}

function syncEventSources(jobs) {
  const active = new Set(
    jobs.filter((j) => j.status === "queued" || j.status === "running").map((j) => j.id)
  );
  for (const [jobId, source] of eventSources) {
    if (!active.has(jobId)) {
      source.close();
      eventSources.delete(jobId);
    }
  }
  for (const job of jobs) {
    if (active.has(job.id)) openJobStream(job);
  }
}

function applyJobEvent(jobId, event, lastEventId) {
  const live = state.jobEvents[jobId] || {
    phase: null,
    phaseMessage: null,
    modelText: "",
    tools: [],
    sequence: 0,
  };
  if (event.seq != null) live.sequence = Number(event.seq);
  const data = event.data || {};

  if (event.type === "phase.started") {
    live.phase = event.phase || live.phase;
    live.phaseMessage = data.message || live.phaseMessage;
    live.modelText = "";
    live.tools = [];
  } else if (event.type === "phase.completed") {
    live.phase = event.phase || live.phase;
  } else if (event.type === "tool.started" && event.tool_name) {
    live.tools.push({
      name: event.tool_name,
      args: data,
      status: "running",
      result: data,
    });
  } else if (event.type === "tool.completed") {
    const tool = live.tools.reverse().find((item) => item.name === event.tool_name && item.status === "running");
    if (tool) {
      tool.status = "completed";
      tool.result = data;
    }
    live.tools.reverse();
  } else if (event.type === "tool.failed") {
    const tool = live.tools.reverse().find((item) => item.name === event.tool_name && item.status === "running");
    if (tool) {
      tool.status = "failed";
      tool.result = data;
    }
    live.tools.reverse();
  } else if (event.type === "model.message.delta") {
    live.modelText = (live.modelText + (data.delta || "")).slice(-12000);
  } else if (event.type === "run.started" || event.type === "phase.started") {
    live.phase = event.phase || live.phase;
  }

  state.jobEvents[jobId] = live;
  renderJobLive(jobId);
  if (
    event.type === "run.completed" ||
    event.type === "run.failed" ||
    event.type === "run.interrupted"
  ) {
    const source = eventSources.get(jobId);
    if (source) {
      source.close();
      eventSources.delete(jobId);
    }
    loadJobs();
  }
}

function jobStatusClass(status) {
  return status === "failed" ? "failed" : status === "running" ? "running" : status === "interrupted" ? "failed" : "completed";
}

function jobStatusLabel(status) {
  return { queued: "\u6392\u961f\u4e2d", running: "\u8fd0\u884c\u4e2d", completed: "\u5df2\u5b8c\u6210", failed: "\u5931\u8d25", interrupted: "\u5df2\u53d6\u6d88" }[status] || status;
}

function jobErrorBlock(j) {
  const err = j.error;
  if (!err) return `<div class="job-meta">${escapeHtml(j.output_dir || "")}</div>`;
  return `
    <details class="job-error">
      <summary>
        <span class="job-meta error-copy">${escapeHtml(err.user_message || "????")}</span>
      </summary>
      <dl class="kv job-error-detail">
        <dt>????</dt><dd>${escapeHtml(err.user_message || "?")}</dd>
        <dt>????</dt><dd>${escapeHtml(err.suggested_action || "?")}</dd>
        <dt>TraceId</dt><dd>${escapeHtml(err.trace_id || j.case_name || "?")}</dd>
        <dt>????</dt><dd><button type="button" class="ghost" data-diagnostics="${escapeHtml(j.id)}">????</button><pre class="json diagnostics-output" data-diagnostics-output="${escapeHtml(j.id)}"></pre></dd>
      </dl>
    </details>`;
}

async function loadJobDiagnostics(jobId) {
  const output = document.querySelector(`[data-diagnostics-output="${CSS.escape(jobId)}"]`);
  if (!output) return;
  try {
    const data = await fetchJson(`/api/analyze/jobs/${encodeURIComponent(jobId)}/diagnostics`);
    output.textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    output.textContent = `?????????${err.message}`;
  }
}

function renderJobs(jobs) {
  if (!jobs.length) {
    els.jobsList.innerHTML = `<div class="empty-note">????</div>`;
    return;
  }
  els.jobsList.innerHTML = jobs.map((j) => `
    <div class="job-item">
      <div class="job-main">
        <div class="job-case">${escapeHtml(j.case_name || (j.params && j.params.trace_path) || j.id)}</div>
        ${jobErrorBlock(j)}
        <div class="job-live" data-job-live="${escapeHtml(j.id)}"></div>
      </div>
      <div class="job-actions">
        ${j.status === "queued" || j.status === "running" ? `<button type="button" class="ghost" data-cancel="${escapeHtml(j.id)}">Cancel</button>` : ""}
        <span class="job-duration">${escapeHtml(j.duration_display || "?")}</span>
        <span class="pill status-badge ${jobStatusClass(j.status)}">${escapeHtml(jobStatusLabel(j.status))}</span>
      </div>
    </div>`).join("");

  els.jobsList.querySelectorAll("[data-diagnostics]").forEach((button) => {
    button.addEventListener("click", () => loadJobDiagnostics(button.dataset.diagnostics));
  });
  els.jobsList.querySelectorAll("[data-cancel]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await fetch(`/runs/${encodeURIComponent(button.dataset.cancel)}/cancel`, {
          method: "POST",
        });
      } catch (err) {
        /* keep polling fallback */
      }
      loadJobs();
    });
  });
  jobs.forEach((j) => renderJobLive(j.id));
}


function toolSummary(tool) {
  const data = tool.result || {};
  const parts = [];
  if (data.arguments && typeof data.arguments === "object" && Object.keys(data.arguments).length) {
    try {
      parts.push(`args=${JSON.stringify(data.arguments)}`);
    } catch (err) {
      parts.push("args=<non-serializable>");
    }
  }
  if (data.activity) parts.push(String(data.activity));
  if (data.evidence_id) parts.push(`evidence=${data.evidence_id}`);
  if (data.returned_rows != null) parts.push(`${data.returned_rows} rows`);
  if (data.error) parts.push(`error=${data.error}`);
  return parts.join(" ? ");
}

function renderJobLive(jobId) {
  const node = document.querySelector(`[data-job-live="${CSS.escape(jobId)}"]`);
  if (!node) return;
  const live = state.jobEvents[jobId];
  if (!live) {
    node.innerHTML = "";
    return;
  }
  const phase = live.phase
    ? `<div class="job-live-phase">phase: ${escapeHtml(live.phase || "")}${live.phaseMessage ? ` \u00b7 ${escapeHtml(live.phaseMessage)}` : ""}</div>`
    : "";
  const latestTool = live.tools.length ? live.tools[live.tools.length - 1] : null;
  const tools = latestTool
    ? `<div class="job-live-tools"><div class="job-tool-chip status-${escapeHtml(latestTool.status)}"><b>${escapeHtml(latestTool.name)}</b>${toolSummary(latestTool) ? ` · ${escapeHtml(toolSummary(latestTool))}` : ""}</div></div>`
    : "";
  const model = live.modelText
    ? `<pre class="job-live-model">${escapeHtml(live.modelText)}</pre>`
    : "";
  node.innerHTML = `${phase}${tools}${model}`;
}


els.analyzeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!validateAnalyzeForm()) return;
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
  els.analyzeSubmit.textContent = "\u63d0\u4ea4\u4e2d\u2026";
  clearAnalyzeError();
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
    showAnalyzeError(err);
  } finally {
    els.analyzeSubmit.disabled = false;
    els.analyzeSubmit.textContent = "\u63d0\u4ea4\u4e2d\u2026";
  }
});

function clearAnalyzeError() {
  if (!els.analyzeError) return;
  els.analyzeError.classList.add("hidden");
  els.analyzeError.textContent = "";
}

function showAnalyzeError(err) {
  if (!els.analyzeError) {
    console.error(err);
    return;
  }
  const message = String((err && err.message) || err || "\u53d1\u751f\u672a\u77e5\u9519\u8bef");
  els.analyzeError.textContent = message;
  els.analyzeError.classList.remove("hidden");
}


function bindFormFields() {
  els.analyzeForm.querySelectorAll("input, select").forEach((node) => {
    if (node.name) els.fields[node.name] = node;
  });
  els.scenarioType = els.fields.scenario_type;
  els.scenario = els.fields.scenario;
  els.symptom = els.fields.symptom;
  els.problemDuration = els.fields.problem_duration_ms;
  els.tracePath = els.fields.trace_path;
}

function fillScenarioTemplate(type) {
  const tpl = state.preflight && state.preflight.templates
    ? state.preflight.templates[type]
    : null;
  if (!tpl) return;
  if (els.scenario && !els.scenario.value) els.scenario.value = tpl.scenario || "";
  if (els.symptom && !els.symptom.value) els.symptom.value = tpl.symptom || "";
  if (els.problemDuration && !els.problemDuration.value && tpl.problem_duration_ms) {
    els.problemDuration.value = tpl.problem_duration_ms;
  }
}

function validateAnalyzeForm() {
  let valid = true;
  let firstInvalid = null;
  const required = ["trace_path", "scenario_type", "scenario", "symptom"];
  for (const name of required) {
    const field = els.fields[name];
    if (!field) continue;
    const value = String(field.value || "").trim();
    field.classList.toggle("invalid", !value);
    const hint = field.parentElement?.querySelector(".field-hint");
    if (hint) {
      hint.textContent = value
        ? ""
        : (field === els.tracePath ? "\u8bf7\u586b\u5199 Trace \u6587\u4ef6\u8def\u5f84" : "\u5fc5\u586b\u9879\u4e0d\u80fd\u4e3a\u7a7a");
    }
    if (!value) {
      valid = false;
      if (!firstInvalid) firstInvalid = field;
    }
  }
  const dur = els.problemDuration;
  if (dur) {
    const v = String(dur.value || "").trim();
    let invalid = false;
    let hint = "";
    if (v) {
      const num = Number(v);
      if (Number.isNaN(num) || num <= 0 || num > 3600000) {
        invalid = true;
        hint = "\u5e94\u4e3a 0\u301c3600000 \u4e4b\u95f4\u7684\u6b63\u6570";
      }
    }
    dur.classList.toggle("invalid", invalid);
    const dh = dur.parentElement?.querySelector(".field-hint");
    if (dh) dh.textContent = hint;
    if (invalid) {
      valid = false;
      if (!firstInvalid) firstInvalid = dur;
    }
  }
  if (firstInvalid) firstInvalid.focus();
  return valid;
}

function setupFormValidation() {
  if (els.scenarioType) {
    els.scenarioType.addEventListener("change", () => {
      fillScenarioTemplate(els.scenarioType.value);
    });
  }
}

// --- 记忆库：语料级视图，不属于任何单个用例 ---
// 信任级别是这一屏的主角。方案第七节要求"agent 刚猜出来的东西"和
// "多案例验证过的规律"在数据上和界面上都不能混为一谈，所以列表默认按
// trust 过滤，且每条都带上 status_reason —— 没有理由的状态就是不可审计的状态。
const TRUST_LABEL = {
  verified: "已验证",
  consolidated: "已验证",
  active: "生效",
  candidate: "候选",
  archived: "已归档",
  superseded: "已归档",
};
const TRUST_BUCKETS = ["verified", "active", "candidate", "archived"];
const TYPE_LABEL = {
  semantic: "语义",
  episodic: "案例",
  procedural: "流程",
  profile: "画像",
  project: "项目",
  summary: "摘要",
};
const KIND_LABEL = {
  pattern: "规律",
  fact: "事实",
  procedure: "步骤",
  "anti-pattern": "反模式",
  profile: "画像",
  project: "项目",
  summary: "摘要",
};
const ACTOR_LABEL = {
  "quality-gate": "质量闸门",
  "conflict-detector": "冲突检测",
  retention: "时效治理",
  "dreaming:summarize": "会话摘要",
  "dreaming:project": "项目摘要",
  consolidate: "整合",
};
const RELATION_LABEL = {
  contradicts: "矛盾",
  "duplicate-of": "重复归并",
  supersedes: "取代",
  supports: "支持",
  "derived-from": "派生",
};
const JOB_LABEL = {
  embed: "向量回填",
  episode: "候选生产",
  gate_review: "信任复核",
  deeppromote: "跨案例归纳",
  conflicts: "冲突扫描",
  decay: "时效衰减",
  summarize: "会话摘要",
  project: "项目摘要",
};

function trustBucket(trust) {
  if (trust === "consolidated") return "verified";
  if (trust === "superseded") return "archived";
  return TRUST_BUCKETS.includes(trust) ? trust : "candidate";
}

function trustPill(trust) {
  return `<span class="pill trust ${trustBucket(trust)}">${escapeHtml(TRUST_LABEL[trust] || trust || "—")}</span>`;
}

function labelOf(map, key) {
  return map[key] || key || "—";
}

function memoryTime(value) {
  const date = parseIso(value);
  if (!date) return "—";
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function trustCounts(stats) {
  const counts = { verified: 0, active: 0, candidate: 0, archived: 0 };
  Object.entries((stats && stats.by_trust) || {}).forEach(([key, value]) => {
    counts[trustBucket(key)] += Number(value || 0);
  });
  return counts;
}

function memoryMetricCards(stats) {
  const cards = [
    ["记忆条目", stats.memories],
    ["历史案例", stats.episodes],
    ["会话", stats.sessions],
    ["矛盾对", stats.contradictions],
    ["版本记录", stats.versions],
    ["关系链接", stats.links],
    ["已过期", stats.expired],
  ];
  return `<div class="grid metrics">${cards.map(([label, value]) => `
    <div class="card">
      <div class="metric-value">${escapeHtml(String(value ?? 0))}</div>
      <div class="metric-label">${escapeHtml(label)}</div>
    </div>`).join("")}</div>`;
}

function distChips(map, labels) {
  const entries = Object.entries(map || {}).filter(([, value]) => Number(value) > 0);
  if (!entries.length) return "";
  return entries
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .map(([key, value]) => `<span class="chip">${escapeHtml(labelOf(labels, key))}<b class="chip-count">${escapeHtml(String(value))}</b></span>`)
    .join("");
}

function distBlock(title, chips) {
  if (!chips) return "";
  return `<div class="dist-block"><div class="dist-title">${escapeHtml(title)}</div><div class="dist-row">${chips}</div></div>`;
}

function memoryTrustCard(stats) {
  const counts = trustCounts(stats);
  const total = TRUST_BUCKETS.reduce((sum, name) => sum + counts[name], 0);
  const bar = total
    ? TRUST_BUCKETS
        .filter((name) => counts[name] > 0)
        .map((name) => `<i class="${name}" style="width:${((counts[name] / total) * 100).toFixed(2)}%" title="${escapeHtml(TRUST_LABEL[name])} ${counts[name]}"></i>`)
        .join("")
    : "";
  const legend = TRUST_BUCKETS.map((name) => `
    <span><i class="${name}"></i>${escapeHtml(TRUST_LABEL[name])}<b>${counts[name]}</b></span>`).join("");
  return `
    <div class="card wide">
      <h3>信任分布</h3>
      <div class="trust-bar">${bar}</div>
      <div class="dist-legend">${legend}</div>
      ${distBlock("记忆类型", distChips(stats.by_type, TYPE_LABEL))}
      ${distBlock("内容形态", distChips(stats.by_kind, KIND_LABEL))}
    </div>`;
}

function renderMemoryTrustFilter(stats) {
  const counts = trustCounts(stats);
  const total = TRUST_BUCKETS.reduce((sum, name) => sum + counts[name], 0);
  const options = [["all", "全部", total]].concat(
    TRUST_BUCKETS.map((name) => [name, TRUST_LABEL[name], counts[name]])
  );
  els.memoryTrustFilter.innerHTML = options.map(([value, label, count]) => `
    <button type="button" data-trust="${escapeHtml(value)}" class="${state.memoryTrust === value ? "active" : ""}">
      ${escapeHtml(label)}<span class="seg-count">${escapeHtml(String(count))}</span>
    </button>`).join("");
  els.memoryTrustFilter.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => {
      state.memoryTrust = button.dataset.trust;
      renderMemoryTrustFilter((state.memory || {}).stats);
      renderMemoryBody();
    });
  });
}

function filteredMemories() {
  const items = (state.memory && state.memory.recent) || [];
  const q = state.memoryQuery.trim().toLowerCase();
  return items.filter((m) => {
    if (state.memoryTrust !== "all" && trustBucket(m.trust) !== state.memoryTrust) return false;
    if (!q) return true;
    return [m.topic, m.content, m.scenario_type, m.memory_id, m.kind, m.type, m.status_reason]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(q));
  });
}

function memoryTable(memories) {
  if (!memories.length) {
    return `<div class="card wide"><div class="empty-note">没有匹配的记忆条目</div></div>`;
  }
  const rows = memories.map((m) => `
    <tr>
      <td class="memory-main">
        <div class="memory-topic">${escapeHtml(m.topic || "(无主题)")}</div>
        <div class="memory-content">${escapeHtml(m.content || "")}</div>
        ${m.status_reason ? `<div class="memory-reason">${escapeHtml(m.status_reason)}</div>` : ""}
      </td>
      <td><div class="memory-tags">
        <span class="chip">${escapeHtml(labelOf(TYPE_LABEL, m.type))}</span>
        <span class="chip">${escapeHtml(labelOf(KIND_LABEL, m.kind))}</span>
        ${m.scenario_type ? `<span class="chip">${escapeHtml(m.scenario_type)}</span>` : ""}
      </div></td>
      <td>${trustPill(m.trust)}</td>
      <td class="memory-num">${escapeHtml(String(m.confidence ?? 0))}</td>
      <td class="memory-num" title="独立 run 数 / 累计出现次数">${escapeHtml(String(m.distinct_runs ?? 0))} / ${escapeHtml(String(m.occurrences ?? 0))}</td>
      <td class="memory-time">${escapeHtml(memoryTime(m.updated_at))}${m.expires_at ? `<div title="过期时间">→ ${escapeHtml(memoryTime(m.expires_at))}</div>` : ""}</td>
      <td class="memory-id">${escapeHtml(m.memory_id || "")}</td>
    </tr>`).join("");
  return `
    <div class="table-wrap">
      <table class="memory-table">
        <thead><tr>
          <th>记忆</th><th>类型</th><th>信任</th><th>置信</th><th>Run / 出现</th><th>更新</th><th>ID</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function govCard(title, body) {
  return `<div class="card"><h3>${escapeHtml(title)}</h3>${body}</div>`;
}

function versionList(versions) {
  if (!versions || !versions.length) return `<div class="gov-empty">暂无信任跃迁记录</div>`;
  const items = versions.map((v) => `
    <li class="gov-item">
      <div class="gov-head">
        <span class="gov-title">${escapeHtml(v.memory_id || "")}</span>
        <span class="gov-meta">v${escapeHtml(String(v.version ?? 1))} · ${escapeHtml(memoryTime(v.created_at))}</span>
      </div>
      <div class="gov-detail">${trustPill(v.trust)}<span class="gov-conf">置信 ${escapeHtml(String(v.confidence ?? 0))}</span></div>
      <div class="gov-note">${escapeHtml(v.reason || "")}${v.actor ? ` · ${escapeHtml(labelOf(ACTOR_LABEL, v.actor))}` : ""}</div>
    </li>`).join("");
  return `<ul class="gov-list">${items}</ul>`;
}

function linkList(links, emptyText) {
  if (!links || !links.length) return `<div class="gov-empty">${escapeHtml(emptyText)}</div>`;
  const items = links.map((link) => `
    <li class="gov-item${link.relation === "contradicts" ? " warn" : ""}">
      <div class="gov-head">
        <span class="gov-title">${escapeHtml(labelOf(RELATION_LABEL, link.relation))}</span>
        <span class="gov-meta">${escapeHtml(memoryTime(link.created_at))}</span>
      </div>
      <div class="gov-detail memory-id">${escapeHtml(link.memory_id || "")} → ${escapeHtml(link.related_memory_id || "")}</div>
      ${link.evidence ? `<div class="gov-note">${escapeHtml(String(link.evidence))}</div>` : ""}
    </li>`).join("");
  return `<ul class="gov-list">${items}</ul>`;
}

function queueList(queue) {
  if (!queue || !queue.length) return `<div class="gov-empty">队列为空，没有待处理的治理任务</div>`;
  const items = queue.map((job) => {
    const target = job.episode_id || job.session_id || job.scenario_type || "";
    return `
    <li class="gov-item">
      <div class="gov-head">
        <span class="gov-title">${escapeHtml(labelOf(JOB_LABEL, job.kind))}</span>
        <span class="gov-meta">${escapeHtml(String(job.kind || ""))}</span>
      </div>
      ${target ? `<div class="gov-detail memory-id">${escapeHtml(String(target))}</div>` : ""}
    </li>`;
  }).join("");
  return `<ul class="gov-list">${items}</ul>`;
}

function batchSummary(batch) {
  const rows = [
    ["向量回填", batch.embedded],
    ["处理案例", batch.processed],
    ["新建记忆", (batch.created || []).length],
    ["信任跃迁", (batch.trust_changed || []).length],
    ["矛盾对", batch.conflicts],
    ["重复归并", batch.duplicates],
    ["交叉佐证", batch.supports],
    ["取代", (batch.superseded || []).length],
    ["归档", (batch.archived || []).length],
    ["过期清理", batch.expired],
    ["时效标记", batch.stamped],
    ["会话摘要", (batch.summarized || []).length],
    ["项目摘要", (batch.projects || []).length],
    ["错误", batch.errors],
  ].filter(([, value]) => Number(value || 0) > 0);
  if (!rows.length) return "本轮没有需要处理的变更，记忆库已处于稳定状态。";
  return `本轮治理完成：<ul>${rows.map(([label, value]) => `<li>${escapeHtml(label)} ${escapeHtml(String(value))}</li>`).join("")}</ul>`;
}

function coverageBar(done, total, cls) {
  const span = total > 0 ? Math.min(100, (Number(done) / Number(total)) * 100) : 0;
  const rest = Math.max(0, 100 - span);
  return `<div class="trust-bar" title="${escapeHtml(String(done))} / ${escapeHtml(String(total))}">
    <i class="${escapeHtml(cls)}" style="width:${span.toFixed(2)}%"></i>
    <i class="archived" style="width:${rest.toFixed(2)}%"></i>
  </div>`;
}

function memorySemanticCard(emb) {
  const head = "<h3>语义索引</h3>";
  if (!emb) return `<div class="card">${head}<div class="gov-empty">未提供语义覆盖数据</div></div>`;
  if (emb.error) return `<div class="card">${head}<div class="gov-empty">读取失败：${escapeHtml(String(emb.error))}</div></div>`;
  if (!emb.available) {
    const reason = emb.reason ? ` · ${escapeHtml(String(emb.reason))}` : "";
    return `<div class="card">${head}<div class="gov-empty">未启用 · 仅词法召回${reason}</div></div>`;
  }
  const rows = [
    ["提供方", emb.kind || "—"],
    ["模型", emb.model || "—"],
    ["维度", String(emb.dim ?? 0)],
    ["向量存储", emb.vector_store ? "支持" : "不支持"],
  ];
  const memDone = Number(emb.memories_embedded || 0);
  const memTotal = Number(emb.memories || 0);
  const epDone = Number(emb.episodes_embedded || 0);
  const epTotal = Number(emb.episodes || 0);
  const stale = Number(emb.stale || 0);
  const unembedded = Number(emb.unembedded || 0);
  const chips = [
    stale > 0 ? `<span class="chip">待重建<b class="chip-count">${stale}</b></span>` : "",
    unembedded > 0 ? `<span class="chip">未向量化<b class="chip-count">${unembedded}</b></span>` : "",
    stale === 0 && unembedded === 0 ? `<span class="chip">全部向量化<b class="chip-count">${memDone + epDone}</b></span>` : "",
  ].filter(Boolean).join("");
  return `<div class="card">${head}
    <dl class="kv">${rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`).join("")}</dl>
    <div class="dist-block"><div class="dist-title">记忆向量化 ${memDone} / ${memTotal}</div>${coverageBar(memDone, memTotal, "verified")}</div>
    <div class="dist-block"><div class="dist-title">案例向量化 ${epDone} / ${epTotal}</div>${coverageBar(epDone, epTotal, "active")}</div>
    <div class="dist-row">${chips}</div>
    ${emb.last_error ? `<div class="gov-note">${escapeHtml(String(emb.last_error))}</div>` : ""}
  </div>`;
}

function renderMemoryBody() {
  const data = state.memory;
  if (!data) return;
  const stats = data.stats || {};
  if (stats.error) {
    els.memoryBody.innerHTML = `<div class="card wide"><div class="empty-note">记忆库统计读取失败：${escapeHtml(stats.error)}</div></div>`;
    return;
  }
  const memories = filteredMemories();
  const flash = state.memoryFlash
    ? `<div class="memory-flash ${escapeHtml(state.memoryFlash.kind)}">${state.memoryFlash.html}</div>`
    : "";
  const merged = [].concat(data.duplicates || [], data.superseded || []);
  els.memoryBody.innerHTML = `
    ${flash}
    ${memoryMetricCards(stats)}
    <div class="grid" style="margin-top:14px">${memoryTrustCard(stats)}</div>
    <div class="section-head">记忆条目 · ${memories.length} / ${data.total ?? memories.length}</div>
    ${memoryTable(memories)}
    <div class="section-head">治理记录</div>
    <div class="memory-grid">
      ${memorySemanticCard(data.embedding)}
      ${govCard("最近信任跃迁", versionList(data.recent_versions))}
      ${govCard("矛盾对", linkList(data.contradictions, "没有互相矛盾的记忆"))}
      ${govCard("交叉佐证", linkList(data.supports, "没有跨案例相互佐证的记忆"))}
      ${govCard("重复归并与取代", linkList(merged, "没有归并或取代记录"))}
      ${govCard("待处理队列", queueList(data.queue))}
    </div>`;
}

async function loadMemory() {
  els.memorySubtitle.textContent = "加载中…";
  try {
    state.memory = await fetchJson("/api/memory/overview?limit=200");
  } catch (err) {
    state.memory = null;
    els.memorySubtitle.textContent = "记忆库加载失败，请重试。";
    els.memoryBody.innerHTML = `<div class="card wide"><div class="empty-note">无法读取 /api/memory/overview：${escapeHtml(err.message)}</div></div>`;
    return;
  }
  const stats = state.memory.stats || {};
  const namespace = stats.namespace || state.memory.namespace || "default";
  els.memoryCrumbs.textContent = `memory / ${namespace}`;
  els.memorySubtitle.textContent = [
    `${stats.store || state.memory.store || "unknown"} 存储`,
    `${stats.memories ?? 0} 条记忆`,
    `${stats.episodes ?? 0} 个案例`,
    `${stats.sessions ?? 0} 个会话`,
    state.memory.root || "",
  ].filter(Boolean).join(" · ");
  renderMemoryTrustFilter(stats);
  renderMemoryBody();
}

async function runMemoryMaintain() {
  const button = els.memoryMaintain;
  button.disabled = true;
  button.textContent = "治理中…";
  state.memoryFlash = null;
  try {
    const res = await fetch("/api/memory/maintain", { method: "POST" });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok || payload.status !== "ok") {
      throw new Error(payload.error || `HTTP ${res.status}`);
    }
    state.memoryFlash = { kind: "ok", html: batchSummary(payload.batch || {}) };
  } catch (err) {
    state.memoryFlash = { kind: "err", html: `治理失败：${escapeHtml(err.message)}` };
  } finally {
    button.disabled = false;
    button.textContent = "立即治理";
  }
  await loadMemory();
}

// ------------------------------------------------------------------ recall probe
//
// Runs a real recall against the live corpus through the same endpoint a run
// uses, so the dashboard can answer "what would an analysis actually see for
// this query" without starting one. The two numbers worth reading are the ones
// section 9 asks for and a hit list alone cannot give: how long retrieval took,
// and whether the composer dropped ranked hits because the prompt budget ran out.

function probeHitId(hit) {
  const memory = (hit || {}).memory || {};
  return String(memory.memory_id || memory.episode_id || "");
}

function probeMetaChips(result) {
  const hits = result.hits || [];
  const injected = new Set(result.injected_ids || []);
  const chars = String(result.context_prompt || "").length;
  const truncatedChip = result.truncated
    ? `<span class="chip truncated" title="排序结果超出提示词预算，末尾条目被整条丢弃">已截断</span>`
    : `<span class="chip" title="全部命中都进入了提示词">未截断</span>`;
  return [
    `<span class="chip" title="本次召回的服务端耗时，缓存命中时为查缓存的耗时">耗时<b class="chip-count">${escapeHtml(Number(result.latency_ms || 0).toFixed(2))} ms</b></span>`,
    `<span class="chip">命中<b class="chip-count">${hits.length}</b></span>`,
    `<span class="chip" title="真正渲染进提示词的条目数">注入<b class="chip-count">${injected.size}</b></span>`,
    `<span class="chip">提示词<b class="chip-count">${chars} 字</b></span>`,
    truncatedChip,
  ].join("");
}

function probeRows(hits, injected) {
  return hits.map((hit) => {
    const memory = hit.memory || {};
    const id = probeHitId(hit);
    const isEpisode = Boolean(memory.episode_id);
    const kindChip = isEpisode
      ? `<span class="chip">${escapeHtml(labelOf(TYPE_LABEL, "episodic"))}</span>`
      : `<span class="chip">${escapeHtml(labelOf(KIND_LABEL, memory.kind))}</span>`;
    const injectChip = injected.has(id)
      ? `<span class="chip" title="该条目进入了提示词，模型看到了它">已注入</span>`
      : `<span class="chip" title="被预算或类型配额丢弃，模型没有看到它">未注入</span>`;
    const trust = memory.status || (isEpisode ? "episode" : "");
    return `<tr>
      <td class="memory-main">
        <div class="memory-topic">${escapeHtml(memory.topic || memory.scenario || "(无主题)")}</div>
        <div class="memory-content">${escapeHtml(memory.content || memory.summary || "")}</div>
      </td>
      <td><div class="memory-tags">${kindChip}${injectChip}</div></td>
      <td>${trustPill(trust)}</td>
      <td class="memory-num">${escapeHtml(Number(hit.score || 0).toFixed(4))}</td>
      <td class="memory-num" title="词法相关度 / 语义余弦">${escapeHtml(Number(hit.lexical || 0).toFixed(3))} / ${escapeHtml(Number(hit.semantic || 0).toFixed(3))}</td>
      <td class="memory-id">${escapeHtml(id)}</td>
    </tr>`;
  }).join("");
}

function renderProbeResult(result) {
  const hits = result.hits || [];
  const meta = `<div class="memory-probe-meta">${probeMetaChips(result)}</div>`;
  if (!hits.length) {
    return `${meta}<div class="memory-probe-result"><div class="gov-empty">没有命中：查询与语料无关，或候选记忆未达到相关性下限。</div></div>`;
  }
  const injected = new Set(result.injected_ids || []);
  return `${meta}<div class="memory-probe-result"><div class="table-wrap">
    <table class="memory-table">
      <thead><tr><th>记忆</th><th>类型</th><th>信任</th><th>得分</th><th>词法 / 语义</th><th>ID</th></tr></thead>
      <tbody>${probeRows(hits, injected)}</tbody>
    </table>
  </div></div>`;
}

async function runMemoryProbe() {
  const query = els.memoryProbeQuery.value.trim();
  if (!query) {
    els.memoryProbeResult.innerHTML = `<div class="gov-empty">先输入一个查询。</div>`;
    els.memoryProbeQuery.focus();
    return;
  }
  const button = els.memoryProbeRun;
  button.disabled = true;
  button.textContent = "召回中…";
  els.memoryProbeResult.innerHTML = `<div class="gov-empty">召回中…</div>`;
  const scenario = els.memoryProbeScenario.value;
  const params = new URLSearchParams({ query });
  if (scenario) params.set("scenario_type", scenario);
  try {
    const result = await fetchJson(`/api/memory/recall?${params.toString()}`);
    els.memoryProbeResult.innerHTML = renderProbeResult(result || {});
  } catch (err) {
    els.memoryProbeResult.innerHTML = `<div class="gov-empty">召回失败：${escapeHtml(err.message)}</div>`;
  } finally {
    button.disabled = false;
    button.textContent = "试跑";
  }
}

function toggleMemoryProbe() {
  state.memoryProbeOpen = !state.memoryProbeOpen;
  els.memoryProbe.classList.toggle("hidden", !state.memoryProbeOpen);
  els.memoryProbeToggle.classList.toggle("ghost-btn", state.memoryProbeOpen);
  if (state.memoryProbeOpen) els.memoryProbeQuery.focus();
}

function openMemoryView() {
  state.view = "memory";
  els.detail.classList.add("hidden");
  els.empty.classList.add("hidden");
  els.memoryView.classList.remove("hidden");
  els.memoryOpen.classList.add("on");
  renderList();
  loadMemory();
}

function hideMemoryView() {
  if (state.view !== "memory") return;
  state.view = "case";
  els.memoryView.classList.add("hidden");
  els.memoryOpen.classList.remove("on");
}

function closeMemoryView() {
  hideMemoryView();
  renderList();
  if (state.activeId) els.detail.classList.remove("hidden");
  else els.empty.classList.remove("hidden");
}

els.memoryOpen.addEventListener("click", openMemoryView);
els.memoryBack.addEventListener("click", closeMemoryView);
els.memoryMaintain.addEventListener("click", runMemoryMaintain);
els.memoryProbeToggle.addEventListener("click", toggleMemoryProbe);
els.memoryProbeRun.addEventListener("click", runMemoryProbe);
els.memoryProbeQuery.addEventListener("keydown", (event) => {
  if (event.key === "Enter") runMemoryProbe();
});
els.memorySearch.addEventListener("input", () => {
  state.memoryQuery = els.memorySearch.value;
  renderMemoryBody();
});

function init() {
  bindFormFields();
  setupFormValidation();
  renderEmptyState();
  loadPreflight();
  loadCases();
}

init();

