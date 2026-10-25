"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const LS_SETTINGS = "cf.web.settings.v1";
const LS_ROOT = "cf.web.root.v1";
const DEFAULT_SETTINGS = {
  store: "",
  sensitivity: "standard",
  sameProblem: true,
  sameUser: true,
  language: "auto",
};

const state = {
  meta: null,
  scan: null,
  settings: { ...DEFAULT_SETTINGS },
  profiles: [],
  profileStore: "",
  currentUser: null,
  view: "overview",
  userSearch: "",
  overviewSearch: "",
  duplicateSearch: "",
  duplicateCrossOnly: true,
  minScore: 0.5,
  featureExpanded: false,
  featureUser: null,
  sourceCache: new Map(),
  saveTarget: undefined,
};

const PAGE_TITLES = {
  overview: "比赛总览",
  students: "学生审查",
  duplicates: "查重",
  profiles: "档案库",
  settings: "设置",
  help: "使用帮助",
};
const ATTENTION_RANK = { high: 0, medium: 1, low: 2 };
const SEVERITY_RANK = { high: 0, medium: 1, low: 2 };
const SENSITIVITY_LABELS = { strict: "严格", standard: "标准", loose: "宽松" };

// -- tiny utilities ----------------------------------------------------------

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function pct(value) {
  if (value === null || value === undefined) return "—";
  return `${Math.round(Number(value) * 100)}%`;
}

function fmtTime(seconds) {
  if (!seconds) return "—";
  const date = new Date(Number(seconds) * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

function fmtDate(seconds) {
  if (!seconds) return "—";
  const date = new Date(Number(seconds) * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString("zh-CN");
}

function initials(name) {
  const text = String(name || "?").trim();
  if (!text) return "?";
  const parts = text.split(/[\s._-]+/).filter(Boolean);
  if (parts.length > 1) return (parts[0][0] + parts[1][0]).toUpperCase();
  return text.slice(0, 2).toUpperCase();
}

function scoreClass(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  if (n >= 0.85) return "";
  if (n >= 0.7) return "warn";
  return "danger";
}

function bar(value, cls = "") {
  const n = Math.max(0, Math.min(1, Number(value) || 0));
  return `<span class="bar ${cls}"><i style="width:${Math.round(n * 100)}%"></i></span>`;
}

function scoreCell(value) {
  if (value === null || value === undefined) return `<span class="muted">—</span>`;
  return `<div class="bar-row">${bar(value, scoreClass(value))}<span class="bar-value">${pct(value)}</span></div>`;
}

function attentionBadge(level) {
  const labels = { high: "高关注", medium: "建议复核", low: "常规" };
  return `<span class="badge ${esc(level)}">${labels[level] || esc(level)}</span>`;
}

function savedBadge(user) {
  if (!user.saved_profile || !user.saved_profile.exists) return `<span class="mini-badge">未建档</span>`;
  if (user.saved_overlap) return `<span class="mini-badge amber">档案含本次 · 参考</span>`;
  return `<span class="mini-badge violet">历史 ${pct(user.saved_score)}</span>`;
}

function styleBadge(user) {
  if (user.style?.multi_style) return `<span class="mini-badge red">多套风格</span>`;
  if (user.style?.weak) return `<span class="mini-badge amber">有差异</span>`;
  return `<span class="mini-badge green">单套</span>`;
}

function warningLabel(raw) {
  const text = String(raw || "");
  const mappings = [
    ["too-short:", "短代码未参与查重："],
    ["no-kgrams:", "无法生成 k-gram："],
    ["boilerplate-filtered:", "已过滤模板指纹（数量）："],
    ["candidates-truncated:", "候选对截断（数量）："],
    ["skipped-cross-language:", "跨语言跳过："],
    ["transitive-clusters:", "传递聚类警告："],
    ["root-files-ignored:", "根目录源码未归入学生："],
    ["parse-error:", "解析告警："],
  ];
  for (const [prefix, label] of mappings) {
    if (text.startsWith(prefix)) return label + text.slice(prefix.length);
  }
  return text;
}

function sortedUsers(users) {
  return [...users].sort((a, b) => {
    const ra = ATTENTION_RANK[a.attention] ?? 9;
    const rb = ATTENTION_RANK[b.attention] ?? 9;
    if (ra !== rb) return ra - rb;
    const ma = a.style?.multi_style ? 1 : 0;
    const mb = b.style?.multi_style ? 1 : 0;
    if (ma !== mb) return mb - ma;
    if ((b.duplicate_pairs || 0) !== (a.duplicate_pairs || 0)) {
      return (b.duplicate_pairs || 0) - (a.duplicate_pairs || 0);
    }
    return String(a.user).localeCompare(String(b.user), "zh");
  });
}

function currentScan() {
  return state.scan;
}

function userByName(name) {
  return state.scan?.users?.find((u) => u.user === name) || null;
}

function fileIdByPathIn(scan, path) {
  const hit = scan?.files?.find((f) => f.path === path);
  return hit ? hit.id : "";
}

function settingsFromStorage() {
  try {
    const parsed = JSON.parse(localStorage.getItem(LS_SETTINGS) || "{}");
    return { ...DEFAULT_SETTINGS, ...parsed };
  } catch (_err) {
    return { ...DEFAULT_SETTINGS };
  }
}

function persistSettings() {
  localStorage.setItem(LS_SETTINGS, JSON.stringify(state.settings));
}

// -- API ---------------------------------------------------------------------

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data;
  try {
    data = await response.json();
  } catch (_err) {
    data = { error: `服务返回了非 JSON 响应（HTTP ${response.status}）` };
  }
  if (!response.ok) throw new Error(data.error || `请求失败（HTTP ${response.status}）`);
  return data;
}

async function loadMeta() {
  state.meta = await api("/api/meta");
  if (!state.settings.store) {
    // Keep the field blank (meaning default), but show the resolved path in the sidebar.
    state.profileStore = state.meta.store || "";
  }
}

async function loadCurrent() {
  const data = await api("/api/current");
  if (data.scan) applyScan(data.scan, { initial: true });
}

async function loadProfiles(showError = false) {
  try {
    const store = state.settings.store || state.scan?.store || "";
    const query = store ? `?store=${encodeURIComponent(store)}` : "";
    const data = await api(`/api/profiles${query}`);
    state.profiles = data.profiles || [];
    state.profileStore = data.store || state.profileStore;
    renderProfiles();
    updateStorePath();
  } catch (err) {
    if (showError) toast(err.message, "error");
  }
}

// -- scan / save -------------------------------------------------------------

function applyScan(scan, { initial = false } = {}) {
  state.scan = scan;
  state.profiles = scan.profiles || state.profiles || [];
  state.profileStore = scan.store || state.profileStore;
  state.sourceCache.clear();
  state.featureExpanded = false;
  state.featureUser = null;
  const names = new Set((scan.users || []).map((u) => u.user));
  if (state.currentUser && !names.has(state.currentUser)) state.currentUser = null;
  if (!state.currentUser && scan.users?.length && state.view === "students") {
    state.currentUser = sortedUsers(scan.users)[0].user;
  }
  $("#root-input").value = scan.root || $("#root-input").value;
  localStorage.setItem(LS_ROOT, $("#root-input").value);
  renderAll();
  if (!initial) $("#root-input").blur();
}

async function scanNow() {
  const root = $("#root-input").value.trim();
  if (!root) {
    toast("请先填写比赛文件夹路径。", "error");
    $("#root-input").focus();
    return;
  }
  setBusy(true, "正在扫描与分析…");
  try {
    const data = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({
        root,
        store: state.settings.store || null,
        language: state.settings.language,
        sensitivity: state.settings.sensitivity,
        same_problem: state.settings.sameProblem,
        include_same_user: state.settings.sameUser,
      }),
    });
    applyScan(data.scan);
    const summary = data.scan.summary;
    toast(`扫描完成：${summary.users} 位学生，${summary.submissions} 份提交，${summary.duplicate_pairs} 对重复。`, "success");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(false);
  }
}

async function saveProfiles(users, mode) {
  if (!state.scan) return;
  setBusy(true, mode === "replace" ? "正在覆盖档案…" : "正在写入档案…");
  try {
    const data = await api("/api/save-profiles", {
      method: "POST",
      body: JSON.stringify({ users, mode }),
    });
    applyScan(data.scan);
    const count = data.saved?.length || 0;
    toast(`已保存 ${count} 位学生的档案（${mode === "replace" ? "覆盖" : "合并"}）。`, "success");
    await loadProfiles();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(false);
    closeSaveModal();
  }
}

// -- render: overview --------------------------------------------------------

function statCard(label, value, note, cls = "") {
  return `<div class="stat-card ${cls}">
    <div class="stat-label">${esc(label)}</div>
    <div class="stat-value">${value}</div>
    <div class="stat-note">${esc(note || "")}</div>
  </div>`;
}

function renderOverview() {
  const scan = state.scan;
  const stats = $("#overview-stats");
  const attentionList = $("#attention-list");
  const scanMeta = $("#scan-meta");
  const usersBox = $("#overview-users");
  const heroTitle = $("#hero-title");
  const heroDesc = $("#hero-desc");
  const heroRoot = $("#hero-root");
  const heroTime = $("#hero-time");

  if (!scan) {
    stats.innerHTML = [
      statCard("学生", "0", "等待扫描"),
      statCard("提交", "0", "等待扫描"),
      statCard("查重对", "0", "等待扫描"),
      statCard("风格警报", "0", "等待扫描"),
      statCard("已存档案", `${state.profiles.length}`, "历史档案库", ""),
    ].join("");
    attentionList.className = "attention-list empty-block";
    attentionList.innerHTML = `<p class="muted">扫描后显示需要优先人工复核的学生。</p>`;
    scanMeta.className = "meta-list empty-block";
    scanMeta.innerHTML = `<p class="muted">尚未扫描。</p>`;
    usersBox.className = "table-wrap empty-block";
    usersBox.innerHTML = `<p class="muted">扫描后显示学生列表。</p>`;
    $("#attention-count").textContent = "0 人";
    heroTitle.textContent = "先扫描一场比赛，建立审查视图";
    heroDesc.textContent = "输入比赛文件夹。系统会按学生拆分提交，检查同一位学生在一场比赛中是否存在多套风格、跨用户结构重复，以及和历史档案的偏离。";
    heroRoot.textContent = "尚未扫描";
    heroTime.textContent = "—";
    return;
  }

  const s = scan.summary;
  stats.innerHTML = [
    statCard("学生", s.users, `${s.submissions} 份提交`),
    statCard("查重对", s.duplicate_pairs, `${s.cross_user_pairs} 跨用户 · ${s.same_user_pairs} 同用户`, s.duplicate_pairs ? "warn" : ""),
    statCard("风格警报", s.multi_style_users, "检测到多套风格的学生", s.multi_style_users ? "danger" : "good"),
    statCard("高关注", s.attention_high, `${s.attention_medium} 人建议复核`, s.attention_high ? "danger" : ""),
    statCard("已存档案", s.saved_profiles, "档案库中的学生数"),
  ].join("");

  heroTitle.textContent = `「${scan.root_name || "比赛"}」审查视图`;
  heroDesc.textContent = "先看高关注学生，再打开风格分组与重复证据。所有信号只用于排序和提示，不构成 AI 生成或抄袭结论。";
  heroRoot.textContent = scan.root;
  heroRoot.title = scan.root;
  heroTime.textContent = `${fmtTime(scan.scanned_at)} · 耗时 ${scan.elapsed_ms} ms`;

  const focus = sortedUsers(scan.users).filter((u) => u.attention !== "low");
  $("#attention-count").textContent = `${focus.length} 人`;
  if (!focus.length) {
    attentionList.className = "attention-list empty-block";
    attentionList.innerHTML = `<p class="muted">本场没有高/中关注学生。可以查看“全部学生”，或检查查重告警。</p>`;
  } else {
    attentionList.className = "attention-list";
    attentionList.innerHTML = focus.slice(0, 8).map((u) => {
      const reason = u.reasons?.[0] || "需要人工复核";
      return `<button class="attention-row" data-open-user="${esc(u.user)}">
        <span class="avatar">${esc(initials(u.user))}</span>
        <span class="attention-main">
          <span class="attention-name">${esc(u.user)}
            ${attentionBadge(u.attention)}
            ${u.style?.multi_style ? `<span class="mini-badge red">${u.groups.length} 组风格</span>` : ""}
            ${u.cross_user_pairs ? `<span class="mini-badge blue">${u.cross_user_pairs} 对跨用户</span>` : ""}
          </span>
          <span class="attention-reason">${esc(reason)}</span>
        </span>
        <span class="attention-badges">${savedBadge(u)}</span>
      </button>`;
    }).join("");
  }

  scanMeta.className = "meta-list";
  const metaRows = [
    ["比赛文件夹", scan.root],
    ["档案库", scan.store],
    ["查重灵敏度", SENSITIVITY_LABELS[scan.sensitivity] || scan.sensitivity],
    ["同题比较", scan.same_problem ? "是" : "否"],
    ["包含同用户", scan.include_same_user ? "是" : "否"],
    ["告警 / 跳过", `${scan.warnings.length} / ${scan.skipped.length}`],
  ];
  scanMeta.innerHTML = metaRows.map(([k, v]) => (
    `<div class="meta-row"><span>${esc(k)}</span><span title="${esc(v)}">${esc(v)}</span></div>`
  )).join("") + (scan.warnings.length
    ? `<div class="warning-item info">${esc(warningLabel(scan.warnings[0]))}</div>`
    : "");

  const query = state.overviewSearch.trim().toLowerCase();
  const all = sortedUsers(scan.users).filter((u) => !query || u.user.toLowerCase().includes(query));
  if (!all.length) {
    usersBox.className = "table-wrap empty-block";
    usersBox.innerHTML = `<p class="muted">没有匹配的学生。</p>`;
  } else {
    usersBox.className = "table-wrap";
    usersBox.innerHTML = `<table class="data">
      <thead><tr>
        <th>学生</th><th>提交</th><th>本次一致性</th><th>风格判断</th>
        <th>历史档案</th><th>重复</th><th>关注</th>
      </tr></thead>
      <tbody>${all.map((u) => `<tr class="clickable" data-open-user="${esc(u.user)}">
        <td><div class="cell-user"><span class="avatar small">${esc(initials(u.user))}</span>${esc(u.user)}</div></td>
        <td class="num">${u.files.length}</td>
        <td>${scoreCell(u.self_score)}</td>
        <td>${styleBadge(u)}</td>
        <td>${savedBadge(u)}</td>
        <td class="num">${u.cross_user_pairs} <span class="cell-sub">跨用户</span></td>
        <td>${attentionBadge(u.attention)}</td>
      </tr>`).join("")}</tbody>
    </table>`;
  }
}

// -- render: students --------------------------------------------------------

function renderUserList() {
  const scan = state.scan;
  const box = $("#user-list");
  const count = $("#student-list-count");
  if (!scan) {
    count.textContent = "0 位";
    box.className = "user-list empty-block";
    box.innerHTML = `<p class="muted">扫描后显示学生。</p>`;
    return;
  }
  const query = state.userSearch.trim().toLowerCase();
  const users = sortedUsers(scan.users).filter((u) => !query || u.user.toLowerCase().includes(query));
  count.textContent = `${users.length} / ${scan.users.length} 位`;
  if (!users.length) {
    box.className = "user-list empty-block";
    box.innerHTML = `<p class="muted">没有匹配的学生。</p>`;
    return;
  }
  box.className = "user-list";
  box.innerHTML = users.map((u) => `<button class="user-row ${u.user === state.currentUser ? "active" : ""}" data-open-user="${esc(u.user)}">
    <span class="avatar small">${esc(initials(u.user))}</span>
    <span>
      <span class="user-row-name">${esc(u.user)}</span>
      <span class="user-row-meta">${u.files.length} 份 · 本次 ${pct(u.self_score)}</span>
    </span>
    <span class="user-row-side">${attentionBadge(u.attention)}${bar(u.self_score, scoreClass(u.self_score))}</span>
  </button>`).join("");
}

function renderStudentDetail() {
  const box = $("#student-detail");
  const scan = state.scan;
  const user = state.currentUser ? userByName(state.currentUser) : null;
  if (!scan || !user) {
    box.innerHTML = `<div class="card empty-state">
      <div class="empty-mark">◉</div>
      <h2>选择一位学生</h2>
      <p>左侧列表按关注等级和风格组排序。选中后可查看风格分组、文件证据、历史档案偏离与 57 个风格轴。</p>
    </div>`;
    return;
  }

  const fileMap = new Map((scan.files || []).map((f) => [f.path, f.id]));
  const style = user.style || {};
  const groups = user.groups || [];
  const groupCount = groups.length;
  const reasonClass = user.attention === "low" ? "good" : "";
  const reasonTitle = user.attention === "low" ? "审阅提示" : "需要优先复核的原因";
  const reasons = (user.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("");

  const styleBannerClass = style.multi_style ? "multi" : (style.weak ? "weak" : "");
  const styleBanner = `<div class="style-banner ${styleBannerClass}">
    <div>
      <strong>${style.multi_style ? "存在多套可区分风格" : style.weak ? "存在一定风格差异" : "本次风格基本一致"}</strong>
      <div class="style-note">${esc(style.note || "—")}</div>
    </div>
    <div class="style-metrics">
      <span>组间差异 <b>${pct(style.separation)}</b></span>
      <span>组内 <b>${pct(style.within)}</b></span>
      <span>组间 <b>${pct(style.cross)}</b></span>
      <span>覆盖 <b>${pct(style.coverage)}</b></span>
    </div>
  </div>`;

  const groupCards = groups.length ? `<div class="group-grid">${groups.map((g, index) => {
    const members = (g.members || []).map((id) => {
      const file = (user.files || []).find((f) => f.id === id);
      return `<span class="member-chip ${g.size > 1 ? "strong" : ""}" title="${esc(id)}">${esc(file?.problem || id)}</span>`;
    }).join("");
    return `<div class="group-card ${index === 0 && groups.length > 1 ? "primary" : ""}">
      <div class="group-head"><span class="group-title">${esc(g.label || `组 ${index + 1}`)}</span><span class="group-size">${g.size} 个文件</span></div>
      <div class="group-members">${members}</div>
    </div>`;
  }).join("")}</div>` : "";

  const fileRows = (user.files || []).map((f) => `<tr>
    <td><div class="file-problem">${esc(f.problem)}</div><div class="file-id" title="${esc(f.path)}">${esc(f.file_name)}</div></td>
    <td class="num">${f.rows}<span class="cell-sub"> 行</span><div class="cell-sub">${f.tokens} tokens</div></td>
    <td>${scoreCell(f.self_score)}</td>
    <td>${f.saved_score === null ? `<span class="muted">—</span>` : scoreCell(f.saved_score)}</td>
    <td>${f.cluster_label ? `<span class="badge ${groupCount > 1 ? "violet" : "gray"}">${esc(f.cluster_label)}</span>` : `<span class="muted">—</span>`}</td>
    <td class="num">${f.duplicate_pairs || 0}${f.cross_user_pairs ? ` <span class="cell-sub">跨 ${f.cross_user_pairs}</span>` : ""}</td>
    <td>
      ${f.parse_error ? `<span class="badge medium" title="${esc(f.parse_error)}">解析告警</span>` : ""}
      <button class="mini-badge blue" data-source="${esc(f.id)}">源码</button>
    </td>
  </tr>`).join("");

  const selfFindings = [...(user.findings || [])].sort((a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9));
  const savedFindings = [...(user.saved_findings || [])].sort((a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9));
  const renderFindings = (items, emptyText, sourceLabel = "") => {
    if (!items.length) return `<p class="muted">${esc(emptyText)}</p>`;
    return `<div class="finding-list">${items.slice(0, 40).map((f) => {
      const fileId = fileMap.get(f.file) || "";
      const line = Array.isArray(f.rows) && f.rows.length ? f.rows[0] : 0;
      const rowsText = Array.isArray(f.rows) && f.rows.length
        ? `行 ${f.rows.slice(0, 5).join(", ")}${f.extra_rows ? ` +${f.extra_rows}` : ""}`
        : "";
      return `<div class="finding ${esc(f.severity || "low")}">
        <span></span>
        <div>
          <div class="finding-head">
            <span class="finding-rule">${esc(f.rule_label || f.rule)}</span>
            <span class="badge ${esc(f.severity || "low")}">${esc(f.severity_label || f.severity || "低")}</span>
            <span class="mini-badge">${esc(f.kind_label || f.kind || "")}</span>
            ${sourceLabel ? `<span class="mini-badge violet">${esc(sourceLabel)}</span>` : ""}
          </div>
          <div class="finding-values">
            <span class="value-box">${esc(f.expected_label || f.expected || "—")}</span>
            <span>→</span>
            <span class="value-box actual">${esc(f.actual_label || f.actual || "—")}</span>
            ${f.support ? `<span class="cell-sub">证据 ${f.support}/${f.total}</span>` : ""}
          </div>
          <div class="finding-msg">${esc(f.message || "")}</div>
        </div>
        <div class="finding-lines">${esc(rowsText)}
          ${fileId ? `<div><button class="mini-badge blue" data-source="${esc(fileId)}" data-line="${line}">看行</button></div>` : ""}
        </div>
      </div>`;
    }).join("")}${items.length > 40 ? `<p class="muted">仅显示前 40 条，共 ${items.length} 条。</p>` : ""}</div>`;
  };

  const allFeatures = user.style_features || [];
  const features = state.featureExpanded ? allFeatures : allFeatures.slice(0, 12);
  const featureRows = features.map((f) => `<tr class="${f.observed ? "" : "default"}">
    <td><div class="feature-label">${esc(f.label)}</div><div class="feature-key">${esc(f.key)}</div></td>
    <td><span class="feature-value">${esc(f.value_label)}</span>${f.observed ? "" : ` <span class="mini-badge">默认</span>`}</td>
    <td class="feature-conf">${f.observed ? `${bar(f.confidence, scoreClass(f.confidence))}<span class="cell-sub">${pct(f.confidence)}</span>` : `<span class="muted">—</span>`}</td>
    <td class="num">${f.observed ? `${f.support}/${f.total}` : "—"}</td>
    <td>${f.saved_observed ? `<span class="saved-value" title="历史档案：${esc(f.saved_value_label)}">${esc(f.saved_value_label)}</span>` : `<span class="muted">—</span>`}</td>
  </tr>`).join("");

  box.innerHTML = `
    <div class="card">
      <div class="student-hero">
        <div class="student-identity">
          <div class="student-avatar">${esc(initials(user.user))}</div>
          <div>
            <h2>${esc(user.user)}</h2>
            <div class="student-sub">
              <span>${esc(user.language)} / ${esc(user.family)}</span>
              <span>${user.files.length} 份提交</span>
              <span>${user.profile?.samples || 0} 个样本</span>
              ${attentionBadge(user.attention)}
              ${styleBadge(user)}
            </div>
          </div>
        </div>
        <div class="student-actions">
          <button class="btn btn-secondary" data-save-user="${esc(user.user)}">存入档案</button>
          <button class="btn btn-ghost" data-source="${esc(user.files[0]?.id || "")}">查看源码</button>
        </div>
      </div>

      <div class="detail-stats">
        <div class="detail-stat"><div class="k">本次一致性</div><div class="v">${pct(user.self_score)}</div></div>
        <div class="detail-stat"><div class="k">风格组</div><div class="v">${groupCount}<small> 组</small></div></div>
        <div class="detail-stat"><div class="k">历史档案</div><div class="v">${user.saved_profile?.exists ? pct(user.saved_score) : "未建档"}</div></div>
        <div class="detail-stat"><div class="k">重复对</div><div class="v">${user.duplicate_pairs}<small> 对</small></div></div>
      </div>

      <div class="reason-box ${reasonClass}">
        <h3>${reasonTitle}</h3>
        <ul>${reasons || "<li>未发现需要优先复核的信号。</li>"}</ul>
        ${user.saved_overlap ? `<p class="muted" style="margin:7px 0 0;font-size:11px;">已存档案包含本次 ${user.saved_overlap} 个文件，历史一致性会因自证而偏高，建议仅作参考。</p>` : ""}
      </div>

      <div class="section-title">
        <h3>风格分组</h3>
        <span class="hint">同一学生的文件按 57 轴分布比较</span>
      </div>
      ${styleBanner}
      ${groupCards}

      <div class="section-title">
        <h3>文件与证据</h3>
        <span class="hint">点击“源码”查看完整文件；行号来自查重或风格偏离</span>
      </div>
      <div class="table-wrap"><table class="data">
        <thead><tr><th>题目 / 文件</th><th>规模</th><th>本次一致性</th><th>档案一致性</th><th>风格组</th><th>重复</th><th>操作</th></tr></thead>
        <tbody>${fileRows || `<tr><td colspan="7" class="muted">没有文件。</td></tr>`}</tbody>
      </table></div>

      <div class="section-title">
        <h3>本次风格偏离</h3>
        <span class="hint">文件内混用 + 与本次聚合风格不一致</span>
      </div>
      ${renderFindings(selfFindings, "本次未发现风格偏离。")}

      ${user.saved_profile?.exists ? `
        <div class="section-title">
          <h3>与历史档案的偏离</h3>
          <span class="hint">${user.saved_overlap ? "档案含本次文件，结果仅参考" : "对照已存档案"}</span>
        </div>
        ${renderFindings(savedFindings, "未发现与历史档案的明显偏离。", "历史档案")}
      ` : ""}

      <div class="section-title">
        <h3>风格轴</h3>
        <div class="feature-tools">
          <span class="hint">${allFeatures.filter((f) => f.observed).length} / 57 个轴有本次观测</span>
          <button class="expand-link" data-toggle-features>${state.featureExpanded ? "收起" : "展开全部 57 轴"}</button>
        </div>
      </div>
      <div class="table-wrap"><table class="feature-table">
        <thead><tr><th>轴</th><th>本次</th><th>置信度</th><th>证据</th><th>历史值</th></tr></thead>
        <tbody>${featureRows}</tbody>
      </table></div>
    </div>`;
}

// -- render: duplicates ------------------------------------------------------

function renderDuplicates() {
  const scan = state.scan;
  const stats = $("#duplicate-stats");
  const table = $("#duplicate-table");
  const warnings = $("#duplicate-warnings");

  if (!scan) {
    stats.innerHTML = [
      statCard("跨用户重复", "0", "等待扫描"),
      statCard("同用户重复", "0", "等待扫描"),
      statCard("聚类", "0", "等待扫描"),
      statCard("查重告警", "0", "等待扫描"),
    ].join("");
    table.className = "table-wrap empty-block";
    table.innerHTML = `<p class="muted">扫描后显示重复对。</p>`;
    warnings.innerHTML = `<p class="muted">暂无。</p>`;
    return;
  }

  stats.innerHTML = [
    statCard("跨用户重复", scan.summary.cross_user_pairs, "最需要优先查看", scan.summary.cross_user_pairs ? "danger" : "good"),
    statCard("同用户重复", scan.summary.same_user_pairs, "本人文件之间的结构重复", scan.summary.same_user_pairs ? "warn" : ""),
    statCard("聚类", scan.summary.clusters, "传递闭包后的重复簇"),
    statCard("查重告警", scan.warnings.length, "短代码 / 跨语言 / 截断等"),
  ].join("");

  const query = state.duplicateSearch.trim().toLowerCase();
  const minScore = state.minScore;
  const pairs = (scan.duplicates?.pairs || []).filter((p) => {
    if (state.duplicateCrossOnly && !p.cross_user) return false;
    if (Math.max(p.jaccard, p.containment) < minScore) return false;
    if (!query) return true;
    const haystack = [p.user_a, p.user_b, p.problem, p.a, p.b].join(" ").toLowerCase();
    return haystack.includes(query);
  });

  if (!pairs.length) {
    table.className = "table-wrap empty-block";
    table.innerHTML = `<p class="muted">没有符合当前过滤条件的重复对。可降低最低分或取消“只看跨用户”。</p>`;
  } else {
    table.className = "table-wrap";
    table.innerHTML = `<table class="data">
      <thead><tr><th>学生 A</th><th>学生 B</th><th>题目</th><th>Jaccard</th><th>Containment</th><th>共享</th><th>聚类</th><th>操作</th></tr></thead>
      <tbody>${pairs.map((p) => `<tr class="clickable" data-pair-a="${esc(p.a)}" data-pair-b="${esc(p.b)}">
        <td><div class="cell-user">${esc(p.user_a)}<span class="cell-sub">${esc(p.a.split("/").pop())}</span></div></td>
        <td><div class="cell-user">${esc(p.user_b)}<span class="cell-sub">${esc(p.b.split("/").pop())}</span></div></td>
        <td><span class="dup-problem">${esc(p.problem || "—")}</span></td>
        <td><div class="score-cell">${bar(p.jaccard, scoreClass(p.jaccard))}<span class="cell-sub">${pct(p.jaccard)}</span></div></td>
        <td><div class="score-cell">${bar(p.containment, scoreClass(p.containment))}<span class="cell-sub">${pct(p.containment)}</span></div></td>
        <td class="num">${p.shared_ngrams}<span class="cell-sub"> / ${p.tokens_a}·${p.tokens_b}</span></td>
        <td>${p.cluster_id ? `<span class="mini-badge violet">${esc(p.cluster_id)}</span>` : `<span class="muted">—</span>`}</td>
        <td><button class="mini-badge blue" data-pair-a="${esc(p.a)}" data-pair-b="${esc(p.b)}">证据</button></td>
      </tr>`).join("")}</tbody>
    </table>`;
  }

  const allWarnings = [...(scan.warnings || []), ...(scan.duplicates?.warnings || [])];
  if (!allWarnings.length) {
    warnings.innerHTML = `<p class="muted">没有查重告警。</p>`;
  } else {
    warnings.innerHTML = `<div class="warning-list">${allWarnings.map((w) => `<div class="warning-item">${esc(warningLabel(w))}</div>`).join("")}</div>`;
  }
}

// -- render: profiles / settings --------------------------------------------

function renderProfiles() {
  const table = $("#profile-table");
  const store = state.profileStore || state.scan?.store || state.meta?.store || "—";
  $("#profiles-store-path").textContent = store;
  $("#side-store-path").textContent = store;
  const profiles = state.profiles || [];
  if (!profiles.length) {
    table.className = "table-wrap empty-block";
    table.innerHTML = `<p class="muted">档案库为空。扫描往期比赛后，在“学生审查”或“档案库”中保存学生档案。</p>`;
    $("#save-all-btn").disabled = !state.scan;
    return;
  }
  table.className = "table-wrap";
  table.innerHTML = `<table class="data">
    <thead><tr><th>学生</th><th>语言</th><th>样本</th><th>有证据的特征</th><th>行数</th><th>更新时间</th></tr></thead>
    <tbody>${profiles.map((p) => {
      const current = state.scan?.users?.some((u) => u.user === p.user);
      return `<tr>
        <td><div class="cell-user">${esc(p.user)} ${current ? `<span class="mini-badge green">本次</span>` : ""}</div></td>
        <td>${esc(p.language)} <span class="cell-sub">${esc(p.family || "")}</span></td>
        <td class="num">${p.samples}</td>
        <td class="num">${p.confident_features}</td>
        <td class="num">${p.rows}</td>
        <td>${fmtTime(p.updated)}</td>
      </tr>`;
    }).join("")}</tbody>
  </table>`;
  $("#save-all-btn").disabled = !state.scan;
}

function renderSettingsValues() {
  $("#setting-store").value = state.settings.store || "";
  $("#setting-sensitivity").value = state.settings.sensitivity;
  $("#setting-same-problem").checked = state.settings.sameProblem;
  $("#setting-same-user").checked = state.settings.sameUser;
  $("#setting-language").value = state.settings.language;
}

function updateStorePath() {
  $("#side-store-path").textContent = state.profileStore || state.scan?.store || state.meta?.store || state.settings.store || "—";
}

// -- drawer / modal ----------------------------------------------------------

function showDrawer(title, kicker, html) {
  $("#drawer-title").textContent = title;
  $("#drawer-kicker").textContent = kicker;
  $("#drawer-body").innerHTML = html;
  $("#drawer").classList.remove("hidden");
  $("#drawer").setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
}

function closeDrawer() {
  $("#drawer").classList.add("hidden");
  $("#drawer").setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
}

function showSaveModal(user) {
  state.saveTarget = user;
  const all = !user;
  $("#save-modal-title").textContent = all ? "保存本次全部学生档案" : `保存「${user}」的档案`;
  $("#save-modal-desc").textContent = all
    ? `将当前 ${state.scan?.users?.length || 0} 位学生写入档案库。建议选择“合并”，保留已有历史样本。`
    : "建议选择“合并到已有档案”：只追加档案中没有的文件，避免重复计数。";
  $("#save-modal").classList.remove("hidden");
  $("#save-modal").setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
}

function closeSaveModal() {
  $("#save-modal").classList.add("hidden");
  $("#save-modal").setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
}

// -- source / evidence -------------------------------------------------------

async function ensureSource(id) {
  if (state.sourceCache.has(id)) return state.sourceCache.get(id);
  const data = await api(`/api/source?id=${encodeURIComponent(id)}`);
  state.sourceCache.set(id, data.source);
  return data.source;
}

function codeWindowHtml(source, start, end) {
  const lines = String(source.text || "").split(/\r?\n/);
  const from = Math.max(1, Number(start || 1) - 3);
  const to = Math.min(lines.length, Number(end || start || 1) + 3);
  const rows = [];
  for (let line = from; line <= to; line += 1) {
    const highlight = line >= Number(start || 1) && line <= Number(end || start || 1);
    rows.push(`<div class="code-line ${highlight ? "highlight" : ""}">
      <span class="ln">${line}</span><span class="src">${esc(lines[line - 1] ?? "")}</span>
    </div>`);
  }
  return `<pre class="code-block">${rows.join("")}</pre>`;
}

function fullSourceHtml(source, start, end) {
  const lines = String(source.text || "").split(/\r?\n/);
  const s = Math.max(1, Number(start || 0));
  const e = Math.max(s, Number(end || s));
  const rows = lines.map((line, index) => {
    const number = index + 1;
    const highlight = number >= s && number <= e;
    return `<div class="code-line ${highlight ? "highlight" : ""}">
      <span class="ln">${number}</span><span class="src">${esc(line)}</span>
    </div>`;
  });
  return `<div class="evidence-cols" style="grid-template-columns:1fr">
    <div class="evidence-col">
      <div class="evidence-label"><span>${esc(source.id)}</span><span>${esc(source.language)} · ${source.rows} 行</span></div>
      <pre class="code-block source-full">${rows.join("")}</pre>
    </div>
  </div>`;
}

async function openSource(id, line = 0) {
  if (!id) return;
  const scan = state.scan;
  const file = scan?.files?.find((f) => f.id === id);
  showDrawer(file ? `${file.user} · ${file.problem}` : "源码", "SOURCE", `<p class="muted">正在读取源码…</p>`);
  try {
    const source = await ensureSource(id);
    $("#drawer-body").innerHTML = fullSourceHtml(source, line, line);
    if (line) {
      requestAnimationFrame(() => {
        const el = $("#drawer-body .code-line.highlight");
        if (el) el.scrollIntoView({ block: "center" });
      });
    }
  } catch (err) {
    $("#drawer-body").innerHTML = `<div class="warning-item">${esc(err.message)}</div>`;
  }
}

async function openPairDrawer(a, b) {
  const pair = state.scan?.duplicates?.pairs?.find((p) => p.a === a && p.b === b);
  if (!pair) return;
  const title = pair.cross_user
    ? `${pair.user_a} ↔ ${pair.user_b}`
    : `${pair.user_a} · 本人文件`;
  showDrawer(title, "DUPLICATE EVIDENCE", `
    <div class="metric-row">
      <div class="metric"><span>Jaccard</span><b>${pct(pair.jaccard)}</b></div>
      <div class="metric"><span>Containment</span><b>${pct(pair.containment)}</b></div>
      <div class="metric"><span>共享 k-gram</span><b>${pair.shared_ngrams}</b></div>
      <div class="metric"><span>Token</span><b>${pair.tokens_a} / ${pair.tokens_b}</b></div>
      <div class="metric"><span>题目</span><b>${esc(pair.problem || "—")}</b></div>
    </div>
    <p class="muted" style="font-size:11px">证据只给行段与 token 数，不把整份源码放进报告。下面的窗口显示行段附近上下文。</p>
    <div id="pair-evidence"><p class="muted">正在加载两端源码…</p></div>
  `);
  try {
    const [sourceA, sourceB] = await Promise.all([ensureSource(pair.a), ensureSource(pair.b)]);
    const evidence = pair.evidence || [];
    if (!evidence.length) {
      $("#pair-evidence").innerHTML = `<p class="muted">没有可定位的连续行段证据；可能是共享 k-gram 分散在文件各处。</p>`;
      return;
    }
    $("#pair-evidence").innerHTML = evidence.map((span, index) => {
      const a1 = span.a_lines?.[0] || 1;
      const a2 = span.a_lines?.[1] || a1;
      const b1 = span.b_lines?.[0] || 1;
      const b2 = span.b_lines?.[1] || b1;
      return `<div class="evidence-card">
        <div class="evidence-head">
          <span>证据 ${index + 1} · 共享 ${span.tokens || 0} tokens</span>
          <span>A ${a1}–${a2} · B ${b1}–${b2}</span>
        </div>
        <div class="evidence-cols">
          <div class="evidence-col">
            <div class="evidence-label"><span>${esc(pair.a)}</span><button class="mini-badge blue" data-source="${esc(pair.a)}" data-line="${a1}">完整源码</button></div>
            ${codeWindowHtml(sourceA, a1, a2)}
          </div>
          <div class="evidence-col">
            <div class="evidence-label"><span>${esc(pair.b)}</span><button class="mini-badge blue" data-source="${esc(pair.b)}" data-line="${b1}">完整源码</button></div>
            ${codeWindowHtml(sourceB, b1, b2)}
          </div>
        </div>
      </div>`;
    }).join("");
  } catch (err) {
    $("#pair-evidence").innerHTML = `<div class="warning-item">${esc(err.message)}</div>`;
  }
}

// -- report export -----------------------------------------------------------

function markdownEscape(value) {
  return String(value ?? "").replace(/\|/g, "\\|").replace(/\r?\n/g, " ");
}

function buildReport() {
  const scan = state.scan;
  if (!scan) return "";
  const lines = [];
  lines.push(`# AIer Checker 审查报告`);
  lines.push("");
  lines.push(`- 比赛文件夹：${scan.root}`);
  lines.push(`- 扫描时间：${fmtTime(scan.scanned_at)}`);
  lines.push(`- 学生 / 提交：${scan.summary.users} / ${scan.summary.submissions}`);
  lines.push(`- 查重对：${scan.summary.duplicate_pairs}（跨用户 ${scan.summary.cross_user_pairs}）`);
  lines.push(`- 多套风格学生：${scan.summary.multi_style_users}`);
  lines.push(`- 高关注 / 建议复核：${scan.summary.attention_high} / ${scan.summary.attention_medium}`);
  lines.push(`- 档案库：${scan.store}`);
  lines.push("");
  lines.push("## 重点关注");
  lines.push("");
  lines.push("| 学生 | 提交 | 本次一致性 | 风格组 | 历史档案 | 重复对 | 关注 | 主要原因 |");
  lines.push("|---|---:|---:|---:|---|---:|---|---|");
  for (const user of sortedUsers(scan.users)) {
    if (user.attention === "low") continue;
    lines.push(`| ${markdownEscape(user.user)} | ${user.files.length} | ${pct(user.self_score)} | ${user.groups.length} | ${user.saved_profile?.exists ? pct(user.saved_score) : "未建档"} | ${user.duplicate_pairs} | ${user.attention_label} | ${markdownEscape((user.reasons || []).join("；"))} |`);
  }
  if (!scan.users.some((u) => u.attention !== "low")) {
    lines.push("| — | — | — | — | — | — | — | 本场没有高/中关注学生 |");
  }
  lines.push("");
  lines.push("## 重复对");
  lines.push("");
  lines.push("| 学生 A | 学生 B | 题目 | Jaccard | Containment | 共享 | 聚类 |");
  lines.push("|---|---|---|---:|---:|---:|---|");
  for (const pair of (scan.duplicates?.pairs || []).slice(0, 50)) {
    lines.push(`| ${markdownEscape(pair.user_a)} | ${markdownEscape(pair.user_b)} | ${markdownEscape(pair.problem || "—")} | ${pct(pair.jaccard)} | ${pct(pair.containment)} | ${pair.shared_ngrams} | ${markdownEscape(pair.cluster_id || "—")} |`);
  }
  if (!(scan.duplicates?.pairs || []).length) lines.push("| — | — | — | — | — | — | — |");
  lines.push("");
  lines.push("## 说明");
  lines.push("");
  lines.push("本报告由 cf 内核生成，只提供风格与结构线索，不构成 AI 生成或抄袭结论。请结合过程性证据、版本记录和人工访谈判断。");
  lines.push("");
  return lines.join("\n");
}

function exportReport() {
  if (!state.scan) {
    toast("还没有可导出的扫描结果。", "error");
    return;
  }
  const blob = new Blob([buildReport()], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  const stamp = new Date().toISOString().slice(0, 10);
  const name = (state.scan.root_name || "contest").replace(/[\\/:*?"<>|]+/g, "_");
  anchor.href = url;
  anchor.download = `AIer-Checker-${name}-${stamp}.md`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
  toast("Markdown 报告已导出。", "success");
}

// -- view / boot -------------------------------------------------------------

function setView(view) {
  state.view = view;
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  $$(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));
  $("#page-title").textContent = PAGE_TITLES[view] || "比赛总览";
  if (view === "students") {
    if (!state.currentUser && state.scan?.users?.length) {
      state.currentUser = sortedUsers(state.scan.users)[0].user;
    }
    renderUserList();
    renderStudentDetail();
  } else if (view === "profiles") {
    loadProfiles();
  } else if (view === "settings") {
    renderSettingsValues();
  } else if (view === "duplicates") {
    renderDuplicates();
  } else if (view === "help") {
    // The help page is static markup; no data rendering is needed.
  } else {
    renderOverview();
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function openStudent(name) {
  state.currentUser = name;
  state.featureExpanded = false;
  state.featureUser = null;
  if (state.view !== "students") {
    setView("students");
  } else {
    renderUserList();
    renderStudentDetail();
  }
}

function setBusy(on, title) {
  $("#loading").classList.toggle("hidden", !on);
  $("#loading-title").textContent = title || "正在扫描与分析…";
  $("#scan-btn").disabled = on;
}

let toastTimer = null;
function toast(message, type = "") {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast ${type}`;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4600);
}

function renderAll() {
  const scan = state.scan;
  $("#nav-student-count").textContent = scan?.summary?.users || 0;
  $("#nav-duplicate-count").textContent = scan?.summary?.duplicate_pairs || 0;
  $("#export-btn").disabled = !scan;
  updateStorePath();
  renderOverview();
  renderUserList();
  renderStudentDetail();
  renderDuplicates();
  renderProfiles();
  renderSettingsValues();
}

function bindEvents() {
  $$(".nav-item").forEach((item) => item.addEventListener("click", () => setView(item.dataset.view)));

  $("#scan-btn").addEventListener("click", scanNow);
  $("#root-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") scanNow();
  });
  $("#export-btn").addEventListener("click", exportReport);

  $("#overview-search").addEventListener("input", (event) => {
    state.overviewSearch = event.target.value;
    renderOverview();
  });
  $("#user-search").addEventListener("input", (event) => {
    state.userSearch = event.target.value;
    renderUserList();
  });

  document.addEventListener("click", (event) => {
    const open = event.target.closest("[data-open-user]");
    if (open) {
      openStudent(open.dataset.openUser);
      return;
    }
    const saveUser = event.target.closest("[data-save-user]");
    if (saveUser) {
      showSaveModal(saveUser.dataset.saveUser);
      return;
    }
    const toggle = event.target.closest("[data-toggle-features]");
    if (toggle) {
      state.featureExpanded = !state.featureExpanded;
      renderStudentDetail();
      return;
    }
    const source = event.target.closest("[data-source]");
    if (source && source.dataset.source) {
      openSource(source.dataset.source, Number(source.dataset.line || 0));
      return;
    }
    const pair = event.target.closest("[data-pair-a][data-pair-b]");
    if (pair) {
      openPairDrawer(pair.dataset.pairA, pair.dataset.pairB);
      return;
    }
    if (event.target.closest("[data-close-drawer]")) {
      closeDrawer();
      return;
    }
    if (event.target.closest("[data-close-modal]")) {
      closeSaveModal();
      return;
    }
    const saveMode = event.target.closest("[data-save-mode]");
    if (saveMode) {
      const mode = saveMode.dataset.saveMode;
      const target = state.saveTarget;
      const users = target ? [target] : (state.scan?.users || []).map((u) => u.user);
      saveProfiles(users, mode);
    }
  });

  $("#duplicate-search").addEventListener("input", (event) => {
    state.duplicateSearch = event.target.value;
    renderDuplicates();
  });
  $("#cross-only").addEventListener("change", (event) => {
    state.duplicateCrossOnly = event.target.checked;
    renderDuplicates();
  });
  $("#min-score").addEventListener("input", (event) => {
    state.minScore = Number(event.target.value) / 100;
    $("#min-score-label").textContent = pct(state.minScore);
    renderDuplicates();
  });

  $("#refresh-profiles-btn").addEventListener("click", () => loadProfiles(true));
  $("#save-all-btn").addEventListener("click", () => {
    if (!state.scan?.users?.length) {
      toast("请先扫描比赛。", "error");
      return;
    }
    showSaveModal(null);
  });

  $("#setting-store").addEventListener("input", (event) => {
    state.settings.store = event.target.value.trim();
    persistSettings();
  });
  $("#setting-sensitivity").addEventListener("change", (event) => {
    state.settings.sensitivity = event.target.value;
    persistSettings();
  });
  $("#setting-language").addEventListener("change", (event) => {
    state.settings.language = event.target.value;
    persistSettings();
  });
  $("#setting-same-problem").addEventListener("change", (event) => {
    state.settings.sameProblem = event.target.checked;
    persistSettings();
  });
  $("#setting-same-user").addEventListener("change", (event) => {
    state.settings.sameUser = event.target.checked;
    persistSettings();
  });
  $("#settings-scan-btn").addEventListener("click", () => {
    setView("overview");
    scanNow();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (!$("#save-modal").classList.contains("hidden")) closeSaveModal();
      else if (!$("#drawer").classList.contains("hidden")) closeDrawer();
    }
  });
}

async function boot() {
  state.settings = settingsFromStorage();
  bindEvents();
  const rememberedRoot = localStorage.getItem(LS_ROOT) || "";
  if (rememberedRoot) $("#root-input").value = rememberedRoot;
  try {
    await loadMeta();
    await loadProfiles();
    await loadCurrent();
    renderAll();
  } catch (err) {
    toast(`启动失败：${err.message}`, "error");
  }
}

boot();
