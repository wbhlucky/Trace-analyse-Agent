"use strict";

const state = {
  cases: [],
  activeId: null,
  bundle: null,
  query: "",
  runningJobs: [],
  jobEvents: {},
  preflight: null,
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

els.refresh.addEventListener("click", () => loadCases());

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

function init() {
  bindFormFields();
  setupFormValidation();
  renderEmptyState();
  loadPreflight();
  loadCases();
}

init();

