/* 合同付款信息提取前端脚本 */

const $ = (sel) => document.querySelector(sel);

const els = {
  form: $("#upload-form"),
  file: $("#file"),
  submitBtn: $("#submit-btn"),
  progressCard: $("#progress-card"),
  progressItems: document.querySelectorAll(".progress-list li"),
  statusMsg: $("#status-msg"),
  errorCard: $("#error-card"),
  errorBody: $("#error-body"),
  step1Card: $("#step1-card"),
  step1Meta: $("#step1-meta"),
  step1Tbody: document.querySelector("#step1-table tbody"),
  step2Card: $("#step2-card"),
  step2Meta: $("#step2-meta"),
  paymentTbody: document.querySelector("#payment-table tbody"),
  warrantyTbody: document.querySelector("#warranty-table tbody"),
};

let currentEventSource = null;
let step1Paragraphs = [];  // 供 Step 2 按 clause 匹配 clause_context

// ===== 耗时统计 =====
const timers = {
  step1Start: 0,
  step2Start: 0,
  step1Elapsed: null,  // 秒
  step2Elapsed: null,
  tickHandle: null,
};

function fmtSec(s) {
  if (s == null) return "-";
  return s >= 10 ? s.toFixed(1) + "s" : s.toFixed(2) + "s";
}

function updateTimerDisplay() {
  const now = performance.now();
  const liveStep1 = timers.step1Start && timers.step1Elapsed == null
    ? (now - timers.step1Start) / 1000 : null;
  const liveStep2 = timers.step2Start && timers.step2Elapsed == null
    ? (now - timers.step2Start) / 1000 : null;

  const s1 = timers.step1Elapsed != null ? fmtSec(timers.step1Elapsed)
           : liveStep1 != null ? fmtSec(liveStep1) + " …"
           : "-";
  const s2 = timers.step2Elapsed != null ? fmtSec(timers.step2Elapsed)
           : liveStep2 != null ? fmtSec(liveStep2) + " …"
           : "-";

  const el = document.getElementById("timer-display");
  if (el) {
    el.innerHTML =
      `Service 1: <strong>${s1}</strong> &nbsp;|&nbsp; ` +
      `Service 2: <strong>${s2}</strong>`;
  }
}

function startTimerTicker() {
  if (timers.tickHandle) return;
  timers.tickHandle = setInterval(updateTimerDisplay, 200);
}

function stopTimerTicker() {
  if (timers.tickHandle) {
    clearInterval(timers.tickHandle);
    timers.tickHandle = null;
  }
  updateTimerDisplay();
}

function resetTimers() {
  timers.step1Start = 0;
  timers.step2Start = 0;
  timers.step1Elapsed = null;
  timers.step2Elapsed = null;
  stopTimerTicker();
  const el = document.getElementById("timer-display");
  if (el) el.innerHTML = "";
}

// ===== 工具 =====
function esc(s) {
  if (s == null) return "";
  return String(s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function resetUI() {
  els.errorCard.hidden = true;
  els.errorBody.textContent = "";
  els.step1Card.hidden = true;
  els.step2Card.hidden = true;
  els.step1Tbody.innerHTML = "";
  els.paymentTbody.innerHTML = "";
  els.warrantyTbody.innerHTML = "";
  els.progressItems.forEach((li) => li.classList.remove("active", "done"));
  els.statusMsg.textContent = "";
  resetTimers();
}

function setStage(stage, state) {
  els.progressItems.forEach((li) => {
    if (li.dataset.stage === stage) {
      li.classList.remove("active", "done");
      li.classList.add(state);
    }
  });
}

function markStageDone(stage) { setStage(stage, "done"); }
function markStageActive(stage) { setStage(stage, "active"); }

function showError(stage, message, detail) {
  els.errorCard.hidden = false;
  let text = `[${stage}] ${message}`;
  if (detail) text += `\n\n详情: ${typeof detail === "string" ? detail : JSON.stringify(detail, null, 2)}`;
  els.errorBody.textContent = text;
}

function renderCtxCell(text) {
  if (!text) return '<span style="color:#9ca3af">—</span>';
  const full = esc(text);
  const preview = esc(text.length > 160 ? text.slice(0, 160) + "…" : text);
  const needsToggle = text.length > 160;
  if (!needsToggle) return `<span class="context-preview">${preview}</span>`;
  return `
    <div class="context-cell">
      <span class="context-preview">${preview}</span>
      <span class="context-full">${full}</span>
      <span class="toggle-ctx" onclick="this.parentNode.classList.toggle('expanded'); this.textContent = this.parentNode.classList.contains('expanded') ? '[收起]' : '[展开]';">[展开]</span>
    </div>`;
}

// ===== Step 1 渲染 =====
function renderStep1(payload) {
  const { paragraphs, count, contract_type, filename } = payload;
  step1Paragraphs = paragraphs || [];

  els.step1Card.hidden = false;
  const typeLabel = {
    installation: "安装合同（→ 安装付款条款）",
    equipment: "设备合同（→ 设备付款条款）",
    mixed: "混签合同（→ 混签付款条款）",
  }[contract_type] || contract_type;

  const allCount = payload.all_clauses_count || (payload.all_clauses || []).length;
  els.step1Meta.innerHTML = `
    文件: <strong>${esc(filename || "")}</strong> &nbsp;|&nbsp;
    合同类型: <strong>${esc(typeLabel)}</strong> &nbsp;|&nbsp;
    付款/质保期条款: <strong>${count || 0}</strong> &nbsp;|&nbsp;
    全部条款: <strong>${allCount || 0}</strong>
  `;

  if (!paragraphs || paragraphs.length === 0) {
    els.step1Tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:#9ca3af">未筛选到付款/质保期相关条款</td></tr>`;
  } else {
    // 排序：付款条款优先，质保期条款后置；组内保持原顺序
    const isWarrantyCls = (p) => (p.clause_class || []).some((c) => String(c).includes("质保期"));
    const sorted = paragraphs
      .map((p, origIdx) => ({ p, origIdx, warranty: isWarrantyCls(p) }))
      .sort((a, b) => (a.warranty - b.warranty) || (a.origIdx - b.origIdx));

    const rows = sorted.map((entry, i) => {
      const p = entry.p;
      const cls = (p.clause_class || []).join(", ");
      const origCls = (p.original_classes || []).join(", ") || "-";
      const tagClass = entry.warranty ? "tag warranty" : "tag";
      return `
        <tr>
          <td class="idx">${i + 1}</td>
          <td><span class="${tagClass}">${esc(cls)}</span></td>
          <td><code class="orig-tag">${esc(origCls)}</code></td>
          <td class="clause-text">${esc(p.clause || "")}</td>
          <td>${renderCtxCell(p.clause_context || "")}</td>
        </tr>`;
    });
    els.step1Tbody.innerHTML = rows.join("");
  }

  // 渲染全部条款展开区
  renderAllClauses(payload.all_clauses || []);
}

// ===== Step 1 全部条款（含其他类别） =====
let allClausesCache = [];
let allClausesActiveFilter = "__all__";

function renderAllClauses(list) {
  allClausesCache = list || [];
  allClausesActiveFilter = "__all__";

  const block = document.getElementById("all-clauses-block");
  const countEl = document.getElementById("all-clauses-count");
  if (countEl) countEl.textContent = `（共 ${allClausesCache.length} 条）`;

  // 按类别分组统计
  const counter = new Map();
  for (const c of allClausesCache) {
    const classes = (c.clause_class && c.clause_class.length) ? c.clause_class : ["(未分类)"];
    for (const k of classes) counter.set(k, (counter.get(k) || 0) + 1);
  }

  // 生成过滤按钮
  const filtersEl = document.getElementById("all-clauses-filters");
  const sortedKeys = Array.from(counter.keys()).sort(
    (a, b) => counter.get(b) - counter.get(a)
  );
  const btns = [`<button type="button" class="filter-btn active" data-filter="__all__">全部 (${allClausesCache.length})</button>`]
    .concat(sortedKeys.map((k) =>
      `<button type="button" class="filter-btn" data-filter="${esc(k)}">${esc(k)} (${counter.get(k)})</button>`
    ));
  filtersEl.innerHTML = btns.join("");
  filtersEl.querySelectorAll(".filter-btn").forEach((b) => {
    b.addEventListener("click", () => {
      allClausesActiveFilter = b.dataset.filter;
      filtersEl.querySelectorAll(".filter-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      renderAllClausesTable();
    });
  });

  renderAllClausesTable();

  if (block) block.style.display = allClausesCache.length ? "" : "none";
}

function renderAllClausesTable() {
  const tbody = document.querySelector("#all-clauses-table tbody");
  const filtered = allClausesActiveFilter === "__all__"
    ? allClausesCache
    : allClausesCache.filter((c) => {
        const classes = (c.clause_class && c.clause_class.length) ? c.clause_class : ["(未分类)"];
        return classes.includes(allClausesActiveFilter);
      });

  if (!filtered.length) {
    tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;color:#9ca3af">无该类别条款</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered.map((c, i) => {
    const classes = (c.clause_class && c.clause_class.length) ? c.clause_class.join(", ") : "(未分类)";
    const conf = c.confidence != null ? Number(c.confidence).toFixed(2) : "-";
    return `
      <tr>
        <td class="idx">${i + 1}</td>
        <td><code class="orig-tag">${esc(classes)}</code></td>
        <td>${esc(conf)}</td>
        <td class="clause-text">${esc(c.text || "")}</td>
      </tr>`;
  }).join("");
}

// ===== Step 2 渲染 =====
// 计算两字符串的最长公共子串长度（用于打分）
function longestCommonSubstringLen(a, b) {
  if (!a || !b) return 0;
  // 空间优化：只保留前一行 DP
  const m = a.length, n = b.length;
  if (m === 0 || n === 0) return 0;
  let prev = new Array(n + 1).fill(0);
  let best = 0;
  for (let i = 1; i <= m; i++) {
    const curr = new Array(n + 1).fill(0);
    for (let j = 1; j <= n; j++) {
      if (a[i - 1] === b[j - 1]) {
        curr[j] = prev[j - 1] + 1;
        if (curr[j] > best) best = curr[j];
      }
    }
    prev = curr;
  }
  return best;
}

// 从 step1Paragraphs 中为 Step 2 的 payment_clause 找最匹配的上下文。
// 采用「最长公共子串长度 / 较短者长度」的重叠比作为评分，避免多段包含同一短语时误匹配。
function findContextByClause(clauseText) {
  if (!clauseText || !step1Paragraphs.length) return "";

  const text = String(clauseText).trim();
  if (!text) return "";

  // 1) 精确相等优先
  for (const p of step1Paragraphs) {
    if ((p.clause || "").trim() === text) return p.clause_context || "";
  }

  // 2) 完整包含优先（避免计算重叠）：挑选包含 text 的最短 clause
  let bestWrap = null;
  let bestWrapLen = Infinity;
  for (const p of step1Paragraphs) {
    const pc = p.clause || "";
    if (pc.includes(text) && pc.length < bestWrapLen) {
      bestWrap = p;
      bestWrapLen = pc.length;
    }
  }
  if (bestWrap) return bestWrap.clause_context || "";

  // 3) 反向包含：text 完整包含 clause，则 text 较大，挑选 clause 最长的（重叠最充分）
  let bestContained = null;
  let bestContainedLen = 0;
  for (const p of step1Paragraphs) {
    const pc = p.clause || "";
    if (pc && text.includes(pc) && pc.length > bestContainedLen) {
      bestContained = p;
      bestContainedLen = pc.length;
    }
  }
  if (bestContained) return bestContained.clause_context || "";

  // 4) 最长公共子串打分：重叠比例 = LCS / min(len)
  let bestScore = 0;
  let bestParagraph = null;
  const MIN_RATIO = 0.6;    // 至少 60% 重叠才视为同一条款
  const MIN_ABS = 10;       // 绝对重叠字符数下限，避免短语误中
  for (const p of step1Paragraphs) {
    const pc = p.clause || "";
    if (!pc) continue;
    const lcs = longestCommonSubstringLen(text, pc);
    if (lcs < MIN_ABS) continue;
    const ratio = lcs / Math.min(text.length, pc.length);
    if (ratio >= MIN_RATIO && ratio > bestScore) {
      bestScore = ratio;
      bestParagraph = p;
    }
  }
  return bestParagraph ? (bestParagraph.clause_context || "") : "";
}

function renderStep2(result) {
  els.step2Card.hidden = false;

  const msg = result.message || "success";
  const elapsed = result._elapsed_seconds;
  els.step2Meta.innerHTML = `
    状态: <strong>${esc(msg)}</strong>
    ${elapsed != null ? `&nbsp;|&nbsp; 耗时: <strong>${elapsed}s</strong>` : ""}
  `;

  const extraction = result.extraction_result || [];
  const paymentItems = extraction.filter(
    (r) => r.payment_ratio != null || r.payment_clause
  );
  const warrantyItems = extraction.filter((r) => r.warranty != null);

  // 付款
  if (paymentItems.length === 0) {
    els.paymentTbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:#9ca3af">无付款节点</td></tr>`;
  } else {
    els.paymentTbody.innerHTML = paymentItems.map((item, i) => {
      const clause = item.payment_clause || "";
      const ctx = findContextByClause(clause) || item.payment_context || "";
      return `
        <tr>
          <td class="idx">${i + 1}</td>
          <td><span class="tag">${esc(item.clause_category || "-")}</span></td>
          <td>${esc(item.payment_type || "-")}</td>
          <td>${esc(item.payment_ratio ?? "-")}</td>
          <td>${esc(item.payment_amount ?? "-")}</td>
          <td class="clause-text">${esc(clause)}</td>
          <td>${renderCtxCell(ctx)}</td>
        </tr>`;
    }).join("");
  }

  // 质保期
  if (warrantyItems.length === 0) {
    els.warrantyTbody.innerHTML = `<tr><td colspan="3" style="text-align:center;color:#9ca3af">无质保期信息</td></tr>`;
  } else {
    els.warrantyTbody.innerHTML = warrantyItems.map((item, i) => `
      <tr>
        <td class="idx">${i + 1}</td>
        <td><span class="tag warranty">${esc(item.warranty || "-")}</span></td>
        <td class="clause-text">${esc(item.warranty_clause || "-")}</td>
      </tr>`).join("");
  }
}

// ===== SSE 处理 =====
function startProcess(sessionId) {
  if (currentEventSource) currentEventSource.close();
  currentEventSource = new EventSource(`/api/process?session_id=${encodeURIComponent(sessionId)}`);

  currentEventSource.addEventListener("status", (evt) => {
    const { stage, message } = JSON.parse(evt.data);
    els.statusMsg.textContent = message || "";
    if (stage === "step1_running") {
      markStageActive("step1");
      timers.step1Start = performance.now();
      startTimerTicker();
    }
    if (stage === "step2_running") {
      markStageDone("step1");
      markStageActive("step2");
      if (timers.step1Start && timers.step1Elapsed == null) {
        timers.step1Elapsed = (performance.now() - timers.step1Start) / 1000;
      }
      timers.step2Start = performance.now();
      startTimerTicker();
    }
    if (stage === "step2_skipped") {
      markStageDone("step1");
      if (timers.step1Start && timers.step1Elapsed == null) {
        timers.step1Elapsed = (performance.now() - timers.step1Start) / 1000;
      }
    }
    updateTimerDisplay();
  });

  currentEventSource.addEventListener("step1_progress", (evt) => {
    try {
      const { done, total, percent } = JSON.parse(evt.data);
      const pct = (percent != null ? percent : (total ? (done * 100 / total) : 0)).toFixed(1);
      els.statusMsg.textContent =
        `Service 1 处理中... 已处理 ${done}/${total} 块（${pct}%）`;
    } catch (_) {}
  });

  currentEventSource.addEventListener("step1", (evt) => {
    const payload = JSON.parse(evt.data);
    if (timers.step1Start && timers.step1Elapsed == null) {
      timers.step1Elapsed = (performance.now() - timers.step1Start) / 1000;
    }
    renderStep1(payload);
    markStageDone("step1");
    updateTimerDisplay();
  });

  currentEventSource.addEventListener("step2", (evt) => {
    const payload = JSON.parse(evt.data);
    if (timers.step2Start && timers.step2Elapsed == null) {
      timers.step2Elapsed = (performance.now() - timers.step2Start) / 1000;
    }
    // 若服务端提供 _elapsed_seconds，优先使用（更精确，不含网络往返）
    if (typeof payload._elapsed_seconds === "number") {
      timers.step2Elapsed = payload._elapsed_seconds;
    }
    renderStep2(payload);
    markStageDone("step2");
    updateTimerDisplay();
    // 自动滚动到 Step 2 卡片，提升查找便利性
    els.step2Card.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  currentEventSource.addEventListener("error", (evt) => {
    // 浏览器 SSE 内建 error 事件无 data 字段，区分服务端推送
    if (evt.data) {
      try {
        const { stage, message, detail } = JSON.parse(evt.data);
        showError(stage, message, detail);
      } catch (_) {}
    }
  });

  currentEventSource.addEventListener("done", () => {
    currentEventSource.close();
    currentEventSource = null;
    els.submitBtn.disabled = false;
    els.statusMsg.textContent = "处理完成";
    stopTimerTicker();
  });

  currentEventSource.onerror = () => {
    // 连接断开
    if (currentEventSource && currentEventSource.readyState === EventSource.CLOSED) {
      els.submitBtn.disabled = false;
      stopTimerTicker();
    }
  };
}

// ===== 表单提交 =====
els.form.addEventListener("submit", async (e) => {
  e.preventDefault();
  resetUI();

  const file = els.file.files[0];
  const contractType = els.form.querySelector('input[name="contract_type"]:checked');
  if (!file) { alert("请选择文件"); return; }
  if (!contractType) { alert("请选择合同类型"); return; }

  els.submitBtn.disabled = true;
  els.progressCard.hidden = false;
  markStageActive("upload");
  els.statusMsg.textContent = "上传中...";

  const fd = new FormData();
  fd.append("file", file);
  fd.append("contract_type", contractType.value);

  const linesInput = document.getElementById("lines_per_chunk");
  const maxCharsInput = document.getElementById("max_chars");
  const skipService2 = document.getElementById("skip_service2");
  const taskIdInput = document.getElementById("task_id");
  if (linesInput && linesInput.value.trim() !== "") {
    fd.append("lines_per_chunk", linesInput.value.trim());
  }
  if (maxCharsInput && maxCharsInput.value.trim() !== "") {
    fd.append("max_chars", maxCharsInput.value.trim());
  }
  if (skipService2 && skipService2.checked) {
    fd.append("skip_service2", "1");
  }
  if (taskIdInput && taskIdInput.value.trim() !== "") {
    fd.append("task_id", taskIdInput.value.trim());
  }

  try {
    const resp = await fetch("/api/upload", { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) {
      showError("upload", data.error || `HTTP ${resp.status}`);
      els.submitBtn.disabled = false;
      return;
    }
    markStageDone("upload");
    startProcess(data.session_id);
  } catch (err) {
    showError("upload", String(err));
    els.submitBtn.disabled = false;
  }
});
