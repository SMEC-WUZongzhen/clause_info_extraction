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
let lastPaymentItems = []; // 缓存最近一次 Step 2 的付款条款，用于一键复制
let lastWarrantyItems = []; // 缓存最近一次 Step 2 的质保期条款
let lastStep2Result = null; // 完整 Step 2 结果（供历史保存）
let lastStep1Payload = null; // 完整 Step 1 payload（供历史保存）
let lastTaskId = "";       // 最近一次任务 ID（用于重跑 Step 2）
let lastOperationType = "extract"; // 最近一次操作模式
let lastGtPaymentStages = null;    // 最近一次真值数据（analyze 模式）
let currentHistoryTab = "extract"; // 历史记录当前 Tab

// 合同总金额相关缓存
let lastContractPriceClause = "";   // 合同总价条款原文（多条已合并）
let lastContractPriceContext = "";  // 合同总价条款上下文
let lastContractPriceResult = null; // /compare_contract_price 响应
let lastSisContractPrice = null;    // 用户最近输入的 SIS 金额

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
  setRerunBtnEnabled(false);
  // 清空合同总金额状态
  lastContractPriceClause = "";
  lastContractPriceContext = "";
  lastContractPriceResult = null;
  const cpSec = document.getElementById("contract-price-section");
  if (cpSec) cpSec.hidden = true;
  const cpMeta = document.getElementById("contract-price-meta");
  if (cpMeta) cpMeta.innerHTML = "";
}

function setRerunBtnEnabled(enabled) {
  const btn = document.getElementById("rerun-step2-btn");
  if (!btn) return;
  btn.disabled = !enabled;
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

// ===== 条款原文截断 =====
function renderClauseCell(text) {
  if (!text) return '<span style="color:#9ca3af">—</span>';
  const safe = esc(text);
  return `<div class="clause-body">${safe}</div><span class="toggle-clause" onclick="toggleClauseCell(this)">[展开]</span>`;
}

function toggleClauseCell(el) {
  const cell = el.closest('.clause-text');
  if (!cell) return;
  cell.classList.toggle('expanded');
  el.textContent = cell.classList.contains('expanded') ? '[收起]' : '[展开]';
}

/** 隐藏不需要截断的条款原文的 toggle 按钮 */
function pruneClauseToggles(scopeEl) {
  requestAnimationFrame(() => {
    (scopeEl || document).querySelectorAll('.clause-text').forEach((cell) => {
      const body = cell.querySelector('.clause-body');
      const toggle = cell.querySelector('.toggle-clause');
      if (!body || !toggle) return;
      // 内容未溢出则隐藏 toggle
      if (body.scrollHeight <= body.clientHeight + 1) {
        toggle.style.display = 'none';
      } else {
        toggle.style.display = '';
      }
    });
  });
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
          <td class="clause-text">${renderClauseCell(p.clause || "")}</td>
          <td>${renderCtxCell(p.clause_context || "")}</td>
        </tr>`;
    });
    els.step1Tbody.innerHTML = rows.join("");
  }

  // 渲染全部条款展开区
  renderAllClauses(payload.all_clauses || []);
  pruneClauseToggles(els.step1Card);
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
        <td class="clause-text">${renderClauseCell(c.text || "")}</td>
      </tr>`;
  }).join("");
  pruneClauseToggles(document.getElementById("all-clauses-table"));
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

  // 默认隐藏对比结果区域（由 renderCompareResult 按需显示）
  const compareSection = document.getElementById("compare-result-section");
  if (compareSection) compareSection.hidden = true;

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
  lastPaymentItems = paymentItems;
  lastWarrantyItems = warrantyItems;
  lastStep2Result = result;
  // 启用导出按钮
  const exportBtn = document.getElementById("export-excel-btn");
  if (exportBtn) exportBtn.disabled = !(paymentItems.length || warrantyItems.length);

  // 付款
  if (paymentItems.length === 0) {
    els.paymentTbody.innerHTML = `<tr><td colspan="11" style="text-align:center;color:#9ca3af">无付款节点</td></tr>`;
  } else {
    els.paymentTbody.innerHTML = paymentItems.map((item, i) => {
      const clause = item.payment_clause || "";
      const ctx = findContextByClause(clause) || item.payment_context || "";
      return `
        <tr>
          <td class="idx">${i + 1}</td>
          <td><span class="tag">${esc(item.clause_category || "-")}</span></td>
          <td>${esc(item.payment_type || "-")}</td>
          <td>${esc(item.payment_code ?? "-")}</td>
          <td>${esc(item.payment_ratio ?? "-")}</td>
          <td>${esc(item.payment_amount ?? "-")}</td>
          <td>${esc(item.payment_days ?? "-")}</td>
          <td>${esc(item.latest_payment_stage ?? "-")}</td>
          <td>${esc(item.latest_payment_date ?? "-")}</td>
          <td class="clause-text">${renderClauseCell(clause)}</td>
          <td>${renderCtxCell(ctx)}</td>
        </tr>`;
    }).join("");
  }

  // 特殊条款内容（文档级汇总，取首条非空）
  const specialClauseWrap = document.getElementById("special-clause-wrap");
  const specialClauseEl = document.getElementById("special-clause-content");
  const scc = paymentItems.find((it) => it.special_clause_content)?.special_clause_content;
  if (scc && specialClauseWrap && specialClauseEl) {
    specialClauseEl.textContent = scc;
    specialClauseWrap.style.display = "block";
  } else if (specialClauseWrap) {
    specialClauseWrap.style.display = "none";
  }

  // 质保期
  if (warrantyItems.length === 0) {
    els.warrantyTbody.innerHTML = `<tr><td colspan="3" style="text-align:center;color:#9ca3af">无质保期信息</td></tr>`;
  } else {
    els.warrantyTbody.innerHTML = warrantyItems.map((item, i) => `
      <tr>
        <td class="idx">${i + 1}</td>
        <td><span class="tag warranty">${esc(item.warranty || "-")}</span></td>
        <td class="clause-text">${renderClauseCell(item.warranty_clause || "")}</td>
      </tr>`).join("");
  }
  pruneClauseToggles(els.step2Card);
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
    lastStep1Payload = payload;
    if (timers.step1Start && timers.step1Elapsed == null) {
      timers.step1Elapsed = (performance.now() - timers.step1Start) / 1000;
    }
    renderStep1(payload);
    markStageDone("step1");
    updateTimerDisplay();
    // Step 1 成功后开放"重新执行 Step 2"按钮（前提是有可用 paragraphs）
    if ((payload.paragraphs || []).length > 0) {
      setRerunBtnEnabled(true);
    }
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
    // 若包含对比数据则渲染对比结果
    if (payload.correct_payments || payload.missed_payments || payload.false_payments) {
      renderCompareResult(payload);
    }
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

  currentEventSource.addEventListener("contract_price_clause", (evt) => {
    try {
      const payload = JSON.parse(evt.data);
      lastContractPriceClause = payload.clause || "";
      lastContractPriceContext = payload.context || "";
      // 重置上一次结果；若无数据则隐藏区块
      if (!payload.has_data) {
        lastContractPriceResult = null;
        renderContractPriceSection();
      } else {
        // 显示区块（结果暂未到达，先显示原文 + 占位）
        renderContractPriceSection();
      }
    } catch (_) {}
  });

  currentEventSource.addEventListener("contract_price", (evt) => {
    try {
      lastContractPriceResult = JSON.parse(evt.data);
      renderContractPriceSection();
    } catch (_) {}
  });

  currentEventSource.addEventListener("contract_price_error", (evt) => {
    try {
      const { message } = JSON.parse(evt.data || "{}");
      const meta = document.getElementById("contract-price-meta");
      if (meta) {
        meta.innerHTML = `<span style="color:#b91c1c">合同总金额比对失败：${esc(message || "未知错误")}</span>`;
      }
      // 仍要显示区块以便看到原文
      const sec = document.getElementById("contract-price-section");
      if (sec) sec.hidden = !lastContractPriceClause;
    } catch (_) {}
  });

  currentEventSource.addEventListener("done", () => {
    currentEventSource.close();
    currentEventSource = null;
    els.submitBtn.disabled = false;
    els.statusMsg.textContent = "处理完成";
    stopTimerTicker();
    // 保存到历史记录
    saveToHistory();
  });

  currentEventSource.onerror = () => {
    // 连接断开
    if (currentEventSource && currentEventSource.readyState === EventSource.CLOSED) {
      els.submitBtn.disabled = false;
      stopTimerTicker();
    }
  };
}

// ===== 操作模式切换 & GT 真值输入 =====
document.querySelectorAll('input[name="operation_type"]').forEach((radio) => {
  radio.addEventListener("change", (e) => {
    const gtSection = document.getElementById("gt-section");
    if (gtSection) gtSection.hidden = e.target.value !== "analyze";
  });
});

function addGTRow(stage = "", category = "equipment_payment", ratio = "", amount = "") {
  const tbody = document.querySelector("#gt-table tbody");
  if (!tbody) return;
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td><input type="text" class="gt-stage" value="${esc(stage)}" placeholder="如: 预付款" /></td>
    <td>
      <select class="gt-category">
        <option value="equipment_payment"${category === "equipment_payment" ? " selected" : ""}>设备付款</option>
        <option value="installation_payment"${category === "installation_payment" ? " selected" : ""}>安装付款</option>
      </select>
    </td>
    <td><input type="text" class="gt-ratio" value="${esc(ratio)}" placeholder="如: 30 或 30%" /></td>
    <td><input type="text" class="gt-amount" value="${esc(amount)}" placeholder="如: 50000" /></td>
    <td><button type="button" class="gt-del-btn copy-btn" style="padding:2px 8px;font-size:11px;">删除</button></td>
  `;
  tr.querySelector(".gt-del-btn").addEventListener("click", () => tr.remove());
  tbody.appendChild(tr);
}

document.getElementById("gt-add-row-btn")?.addEventListener("click", () => addGTRow());

function collectGTData() {
  const rows = document.querySelectorAll("#gt-table tbody tr");
  const stages = [];
  rows.forEach((tr) => {
    const stage = tr.querySelector(".gt-stage")?.value.trim();
    const category = tr.querySelector(".gt-category")?.value || "equipment_payment";
    const ratio = tr.querySelector(".gt-ratio")?.value.trim();
    const amount = tr.querySelector(".gt-amount")?.value.trim();
    if (!stage) return; // 跳过空行
    const item = { stage, category };
    if (ratio) item.ratio = ratio;
    if (amount) item.stage_amount = amount;
    if (ratio || amount) stages.push(item);
  });
  return stages.length ? stages : null;
}

// ===== 对比结果渲染 =====
function renderCompareResult(result) {
  const section = document.getElementById("compare-result-section");
  if (!section) return;

  // 判断是否包含对比数据
  const hasCompare = result.correct_payments || result.missed_payments ||
                     result.false_payments || result.evaluation_metrics;
  if (!hasCompare) {
    section.hidden = true;
    return;
  }
  section.hidden = false;

  // 评估指标
  const metricsEl = document.getElementById("eval-metrics");
  const metrics = result.evaluation_metrics || {};
  if (metricsEl) {
    metricsEl.innerHTML = `
      <div class="metric-card"><div class="metric-label">Precision</div><div class="metric-value">${((metrics.precision || 0) * 100).toFixed(1)}%</div></div>
      <div class="metric-card"><div class="metric-label">Recall</div><div class="metric-value">${((metrics.recall || 0) * 100).toFixed(1)}%</div></div>
      <div class="metric-card"><div class="metric-label">F1 Score</div><div class="metric-value">${((metrics.f1_score || 0) * 100).toFixed(1)}%</div></div>
      <div class="metric-card"><div class="metric-label">Accuracy</div><div class="metric-value">${((metrics.accuracy || 0) * 100).toFixed(1)}%</div></div>
    `;
  }

  // 辅助函数
  const categoryLabel = (cat) => {
    if (cat === "equipment_payment") return "设备";
    if (cat === "installation_payment") return "安装";
    return "-";
  };
  // 从提取结果构建映射
  const catLookup = {};       // payment_type → clause_category（同名可能覆盖，仅兜底）
  const codeToCats = {};      // payment_code → [clause_category, ...]（共享编码可含多个）
  const extraction = (lastStep2Result && lastStep2Result.extraction_result) || [];
  extraction.forEach((it) => {
    if (it.payment_type && it.clause_category) {
      catLookup[it.payment_type] = it.clause_category;
    }
    if (it.payment_code && it.clause_category) {
      if (!codeToCats[it.payment_code]) codeToCats[it.payment_code] = [];
      if (!codeToCats[it.payment_code].includes(it.clause_category)) {
        codeToCats[it.payment_code].push(it.clause_category);
      }
    }
  });
  // 从 GT 数据构建 stage → category 映射（用于漏提取项）
  const gtCatLookup = {};
  const gtCodeToCats = {};    // GT 中 stage 对应的标准编码 → category
  (lastGtPaymentStages || []).forEach((gt) => {
    if (gt.stage && gt.category) {
      gtCatLookup[gt.stage] = gt.category;
    }
  });
  // 共享编码兜底：若编码在提取结果中只出现一种类别，则采用；多种则取 GT 中出现最多的类别
  const resolveSharedCode = (code) => {
    const cats = codeToCats[code];
    if (!cats || !cats.length) return "";
    if (cats.length === 1) return cats[0];
    // 多种类别 → 看 GT 中哪个类别多
    const gtCats = Object.values(gtCatLookup);
    const eqCount = gtCats.filter((c) => c === "equipment_payment").length;
    const instCount = gtCats.filter((c) => c === "installation_payment").length;
    if (eqCount > instCount) return "equipment_payment";
    if (instCount > eqCount) return "installation_payment";
    return cats[0]; // 默认取第一个
  };
  // payment_code 固有映射（设备独有 / 安装独有节点）
  const _EQ_ONLY = new Set(["EARNEST","DEPOSIT","WITHDRAWAL","Z001","Z002","Z021","Z022"]);
  const _INST_ONLY = new Set(["Z018","DOWNPAYMENT","Z027","Z028","Z019"]);
  const codeToCatFixed = (code) => {
    if (!code) return "";
    if (_EQ_ONLY.has(code)) return "equipment_payment";
    if (_INST_ONLY.has(code)) return "installation_payment";
    return "";
  };
  // 综合解析节点类型
  // isExtracted: 提取结果自带 clause_category，应直接使用；其余走兜底链
  const resolveCategory = (it, isExtracted) => {
    if (isExtracted && it.clause_category) return it.clause_category;
    return it.clause_category
      || catLookup[it.payment_type]
      || gtCatLookup[it.payment_type]
      || (it.payment_code && resolveSharedCode(it.payment_code))
      || codeToCatFixed(it.payment_code)
      || "";
  };

  // 构建正确匹配集合 (payment_type + "|" + payment_ratio)
  const correctSet = new Set();
  (result.correct_payments || []).forEach((cp) => {
    correctSet.add((cp.payment_type || "") + "|" + (cp.payment_ratio ?? ""));
  });

  // 提取结果：每条标注 ✔ 或 ✗
  const extractedItems = extraction
    .filter((it) => it.payment_type)
    .map((it) => {
      const key = (it.payment_type || "") + "|" + (it.payment_ratio ?? "");
      return { ...it, _correct: correctSet.has(key) };
    });

  // 漏提取项
  const missedItems = (result.missed_payments || []);

  // 渲染合并表
  const tbody = document.querySelector("#merged-compare-table tbody");
  if (!tbody) return;

  const rows = [];
  let idx = 0;

  // 提取结果行
  extractedItems.forEach((it) => {
    idx++;
    const cat = resolveCategory(it, true);
    const statusHtml = it._correct
      ? `<span class="compare-status status-correct">✔</span>`
      : `<span class="compare-status status-false">✗</span>`;
    rows.push(`
      <tr class="${it._correct ? 'row-correct' : 'row-false'}">
        <td class="idx">${idx}</td>
        <td style="text-align:center;">${statusHtml}</td>
        <td><span class="tag${cat === 'installation_payment' ? ' warranty' : ''}">${esc(categoryLabel(cat))}</span></td>
        <td>${esc(it.payment_type || "-")}</td>
        <td>${esc(it.payment_code ?? "-")}</td>
        <td>${esc(it.payment_ratio ?? "-")}</td>
        <td>${esc(it.payment_amount ?? "-")}</td>
      </tr>`);
  });

  // 漏提取行（分隔行 + 数据行）
  if (missedItems.length) {
    rows.push(`
      <tr class="row-missed-header">
        <td colspan="7">漏提取 (${missedItems.length})</td>
      </tr>`);
    missedItems.forEach((it) => {
      idx++;
      const cat = resolveCategory(it, false);
      rows.push(`
        <tr class="row-missed">
          <td class="idx">${idx}</td>
          <td style="text-align:center;"><span class="compare-status status-missed">✗</span></td>
          <td><span class="tag${cat === 'installation_payment' ? ' warranty' : ''}">${esc(categoryLabel(cat))}</span></td>
          <td>${esc(it.payment_type || "-")}</td>
          <td>${esc(it.payment_code ?? "-")}</td>
          <td>${esc(it.payment_ratio ?? "-")}</td>
          <td>${esc(it.payment_amount ?? "-")}</td>
        </tr>`);
    });
  }

  tbody.innerHTML = rows.length
    ? rows.join("")
    : `<tr><td colspan="7" style="text-align:center;color:#9ca3af">无</td></tr>`;
}

// ===== 合同总金额渲染 =====
function _fmtPrice(v) {
  if (v == null || v === "") return "-";
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  return n.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function renderContractPriceSection() {
  const sec = document.getElementById("contract-price-section");
  if (!sec) return;

  // 无原文 → 隐藏整个区块
  if (!lastContractPriceClause) {
    sec.hidden = true;
    return;
  }
  sec.hidden = false;

  // 写入原文/上下文
  const cPre = document.getElementById("contract-price-clause-pre");
  const ctxPre = document.getElementById("contract-price-context-pre");
  if (cPre) cPre.textContent = lastContractPriceClause;
  if (ctxPre) ctxPre.textContent = lastContractPriceContext || "(无)";

  const isAnalyze = lastOperationType === "analyze";

  // 列显隐控制
  sec.querySelectorAll(".cp-col-sis, .cp-col-diff, .cp-col-result").forEach((th) => {
    th.hidden = !isAnalyze;
  });

  // 比对表单（仅 analyze）
  const form = document.getElementById("contract-price-compare-form");
  if (form) form.hidden = !isAnalyze;
  const sisInput = document.getElementById("sis-contract-price-input");
  if (sisInput && isAnalyze) {
    const r = lastContractPriceResult;
    const v = (r && r.sis_contract_price != null) ? r.sis_contract_price : lastSisContractPrice;
    sisInput.value = (v != null ? v : "");
  }

  // 渲染表格行
  const tbody = document.querySelector("#contract-price-table tbody");
  if (!tbody) return;
  const r = lastContractPriceResult;
  if (!r) {
    const cols = isAnalyze ? 4 : 1;
    tbody.innerHTML = `<tr><td colspan="${cols}" style="text-align:center;color:#9ca3af">等待 Service 2 返回...</td></tr>`;
    return;
  }
  const cp = r.contract_price;
  const sis = r.sis_contract_price;
  const cmp = r.comparison_result;
  let diffStr = "-";
  let resultHtml = "-";
  if (isAnalyze) {
    if (cp != null && sis != null) {
      const diff = Number(cp) - Number(sis);
      diffStr = _fmtPrice(diff);
    }
    if (cmp === true) {
      resultHtml = `<span class="tag" style="background:#dcfce7;color:#166534">一致</span>`;
    } else if (cmp === false) {
      resultHtml = `<span class="tag" style="background:#fee2e2;color:#991b1b">不一致</span>`;
    } else {
      resultHtml = `<span style="color:#9ca3af">未比对</span>`;
    }
  }
  const row = `
    <tr>
      <td>${esc(_fmtPrice(cp))}</td>
      ${isAnalyze ? `<td>${esc(_fmtPrice(sis))}</td><td>${esc(diffStr)}</td><td>${resultHtml}</td>` : ""}
    </tr>`;
  tbody.innerHTML = row;

  // meta 状态
  const meta = document.getElementById("contract-price-meta");
  if (meta) {
    if (cp == null) {
      meta.innerHTML = `<span style="color:#b45309">未抽取到金额（条款可能不含金额或解析失败）</span>`;
    } else {
      meta.innerHTML = "";
    }
  }
}

// 重新对比按钮
document.getElementById("recompare-price-btn")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  if (!lastContractPriceClause) {
    alert("尚未识别到合同总价条款");
    return;
  }
  const sisInput = document.getElementById("sis-contract-price-input");
  const sisRaw = (sisInput && sisInput.value || "").trim();
  let sisVal = null;
  if (sisRaw !== "") {
    sisVal = Number(sisRaw);
    if (Number.isNaN(sisVal)) { alert("SIS 金额必须为数字"); return; }
  }
  lastSisContractPrice = sisVal;

  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "对比中...";
  try {
    const resp = await fetch("/api/compare-contract-price", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        clause: lastContractPriceClause,
        context: lastContractPriceContext || null,
        sis_contract_price: sisVal,
        task_id: (document.getElementById("task_id")?.value || "").trim() || undefined,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      alert(data.error || `HTTP ${resp.status}`);
      return;
    }
    lastContractPriceResult = data.result || null;
    renderContractPriceSection();
  } catch (err) {
    alert(`请求失败: ${err}`);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});


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

  // 操作模式
  const opTypeRadio = els.form.querySelector('input[name="operation_type"]:checked');
  const opType = opTypeRadio ? opTypeRadio.value : "extract";
  lastOperationType = opType;
  fd.append("operation_type", opType);
  if (opType === "analyze") {
    const gtData = collectGTData();
    if (!gtData) { alert("对比模式下请至少输入一条真值数据"); els.submitBtn.disabled = false; return; }
    lastGtPaymentStages = gtData;
    fd.append("gt_json", JSON.stringify(gtData));
    // SIS 合同总金额（可选）
    const sisInput = document.getElementById("sis_contract_price");
    const sisRaw = (sisInput && sisInput.value || "").trim();
    if (sisRaw !== "") {
      const v = Number(sisRaw);
      if (Number.isNaN(v)) { alert("SIS 合同总金额必须为数字"); els.submitBtn.disabled = false; return; }
      lastSisContractPrice = v;
      fd.append("sis_contract_price", String(v));
    } else {
      lastSisContractPrice = null;
    }
  } else {
    lastGtPaymentStages = null;
    lastSisContractPrice = null;
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

// ===== 一键复制 =====
const PAYMENT_CLASS_SET = new Set([
  "混签付款条款", "设备付款条款", "安装付款条款",
]);

function tsvEscape(s) {
  if (s == null) return "";
  // TSV: 替换制表符与换行，保证粘贴到 Excel 时不破坏列结构
  return String(s).replace(/\t/g, " ").replace(/\r?\n/g, " ");
}

function buildTSV(headers, rows) {
  return [headers.join("\t"), ...rows.map((r) => r.map(tsvEscape).join("\t"))].join("\n");
}

async function copyText(text, btn) {
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch (_) {
    // 回退：textarea + execCommand
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand("copy");
      document.body.removeChild(ta);
    } catch (_) { ok = false; }
  }
  if (btn) {
    const old = btn.dataset.label || btn.textContent;
    btn.dataset.label = old;
    btn.textContent = ok ? "已复制 ✓" : "复制失败";
    btn.disabled = true;
    setTimeout(() => {
      btn.textContent = old;
      btn.disabled = false;
    }, 1500);
  }
}

function copyStep2Payment(btn) {
  if (!lastPaymentItems.length) { alert("暂无付款条款数据"); return; }
  const headers = ["类别", "阶段类型", "节点编码", "比例", "金额", "付款天数", "最迟付款节点", "最迟付款时间(天)", "条款原文"];
  const rows = lastPaymentItems.map((it) => [
    it.clause_category ?? "",
    it.payment_type ?? "",
    it.payment_code ?? "",
    it.payment_ratio ?? "",
    it.payment_amount ?? "",
    it.payment_days ?? "",
    it.latest_payment_stage ?? "",
    it.latest_payment_date ?? "",
    it.payment_clause ?? "",
  ]);
  copyText(buildTSV(headers, rows), btn);
}

function copyStep1Payment(btn) {
  if (!step1Paragraphs.length) { alert("暂无 Step 1 数据"); return; }
  const filtered = step1Paragraphs.filter((p) =>
    (p.clause_class || []).some((c) => PAYMENT_CLASS_SET.has(String(c)))
  );
  if (!filtered.length) { alert("无符合条件的付款条款（混签/设备/安装）"); return; }
  const headers = ["归类后", "条款"];
  const rows = filtered.map((p) => [
    (p.clause_class || []).join(", "),
    p.clause ?? "",
  ]);
  copyText(buildTSV(headers, rows), btn);
}

document.getElementById("copy-step1-payment-btn")
  ?.addEventListener("click", (e) => copyStep1Payment(e.currentTarget));
document.getElementById("copy-step2-payment-btn")
  ?.addEventListener("click", (e) => copyStep2Payment(e.currentTarget));

// ===== 重新执行 Step 2 =====
document.getElementById("rerun-step2-btn")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  if (!step1Paragraphs.length) {
    alert("Step 1 结果不可用（页面刷新后需重新执行完整流程）");
    return;
  }

  btn.disabled = true;
  els.errorCard.hidden = true;
  els.errorBody.textContent = "";

  // 显示带旋转动画和计时器的执行状态
  const originalText = btn.textContent;
  const rerunStart = performance.now();
  let rerunTimerHandle = setInterval(() => {
    const elapsed = ((performance.now() - rerunStart) / 1000).toFixed(1);
    btn.innerHTML = `<span class="rerun-spinner"></span> 执行中 ${elapsed}s`;
  }, 100);
  btn.innerHTML = `<span class="rerun-spinner"></span> 执行中 0.0s`;

  // 取前端 task_id 字段，或自动生成
  const taskIdInput = document.getElementById("task_id");
  const taskId = (taskIdInput && taskIdInput.value.trim()) || "";

  // 读取操作模式和真值数据
  const opTypeRadio = document.querySelector('input[name="operation_type"]:checked');
  const rerunOpType = opTypeRadio ? opTypeRadio.value : "extract";
  lastOperationType = rerunOpType;
  const rerunBody = { paragraphs: step1Paragraphs, task_id: taskId, operation_type: rerunOpType };
  if (rerunOpType === "analyze") {
    const gtData = collectGTData();
    if (gtData) {
      lastGtPaymentStages = gtData;
      rerunBody.sis_payment_stages = gtData;
    }
    // SIS 合同金额（与上传表单同字段）
    const sisInput = document.getElementById("sis_contract_price");
    const sisRaw = (sisInput && sisInput.value || "").trim();
    if (sisRaw !== "") {
      const v = Number(sisRaw);
      if (!Number.isNaN(v)) {
        lastSisContractPrice = v;
        rerunBody.sis_contract_price = v;
      }
    }
  } else {
    lastGtPaymentStages = null;
  }
  // 合同总价条款（即便 extract 模式也允许重跑抽取，仅 analyze 模式发送 sis）
  if (lastContractPriceClause) {
    rerunBody.contract_price_clause = lastContractPriceClause;
    rerunBody.contract_price_clause_context = lastContractPriceContext || null;
  }

  try {
    const resp = await fetch("/api/rerun-step2", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(rerunBody),
    });
    const data = await resp.json();
    if (!resp.ok) {
      showError(data.stage || "step2", data.error || `HTTP ${resp.status}`, data.detail);
      return;
    }
    // 渲染新 Step 2 结果
    renderStep2(data.result || {});
    // 若包含对比数据则渲染对比结果
    if (data.result && (data.result.correct_payments || data.result.missed_payments || data.result.false_payments)) {
      renderCompareResult(data.result);
    }
    // 合同总金额结果（若服务端返回）
    if (data.contract_price) {
      lastContractPriceResult = data.contract_price;
      renderContractPriceSection();
    } else if (data.contract_price_error) {
      const meta = document.getElementById("contract-price-meta");
      if (meta) meta.innerHTML = `<span style="color:#b91c1c">合同总金额比对失败：${esc(data.contract_price_error.message || "")}</span>`;
    }
    els.step2Card.hidden = false;
    els.step2Card.scrollIntoView({ behavior: "smooth", block: "start" });
    // 重跑 Step 2 成功后自动保存到历史记录
    await saveToHistory();
  } catch (err) {
    showError("step2", `请求失败: ${err}`);
  } finally {
    clearInterval(rerunTimerHandle);
    btn.textContent = originalText;
    btn.disabled = false;
  }
});

// ===== 历史记录 (SQLite API) =====

const CONTRACT_TYPE_LABELS = {
  installation: "安装合同",
  equipment: "设备合同",
  mixed: "混签合同",
};

async function saveToHistory() {
  const s1 = lastStep1Payload;
  if (!s1) return;

  const extraction = (lastStep2Result && lastStep2Result.extraction_result) || [];
  const payItems = extraction.filter(
    (r) => r.payment_ratio != null || r.payment_clause
  );

  // 提取节点类型（中文）、比例、金额摘要
  const typeSummary = payItems
    .map((it) => it.payment_type || "-")
    .filter((v) => v !== "-")
    .join(", ");
  const ratioSummary = payItems
    .map((it) => it.payment_ratio)
    .filter((v) => v != null && v !== "")
    .join(", ");
  const amountSummary = payItems
    .map((it) => it.payment_amount)
    .filter((v) => v != null && v !== "")
    .join(", ");

  const record = {
    id: `hist-${Date.now()}`,
    createdAt: new Date().toLocaleString("zh-CN"),
    filename: s1.filename || "",
    contractType: s1.contract_type || "",
    contractTypeLabel: CONTRACT_TYPE_LABELS[s1.contract_type] || s1.contract_type || "",
    typeSummary: typeSummary || "-",
    ratioSummary: ratioSummary || "-",
    amountSummary: amountSummary || "-",
    step2Elapsed: (lastStep2Result && lastStep2Result._elapsed_seconds) || null,
    operationType: lastOperationType || "extract",
    gtData: (lastOperationType === "analyze") ? lastGtPaymentStages : null,
    compareData: (lastOperationType === "analyze" && lastStep2Result) ? {
      correct_payments: lastStep2Result.correct_payments,
      missed_payments: lastStep2Result.missed_payments,
      false_payments: lastStep2Result.false_payments,
      evaluation_metrics: lastStep2Result.evaluation_metrics,
    } : null,
    paymentItems: payItems,
    warrantyItems: extraction.filter((r) => r.warranty != null),
    step1Data: s1,
    step2Data: lastStep2Result || null,
  };

  try {
    await fetch("/api/history", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(record),
    });
  } catch (e) {
    console.warn("保存历史记录失败:", e);
  }
  await renderHistory();
}

async function renderHistory() {
  const countEl = document.getElementById("history-count");
  const tbody = document.querySelector("#history-table tbody");

  let list = [];
  try {
    const resp = await fetch(`/api/history?type=${encodeURIComponent(currentHistoryTab)}`);
    if (resp.ok) list = await resp.json();
  } catch (_) {}

  if (countEl) countEl.textContent = `（共 ${list.length} 条）`;
  if (!tbody) return;

  // 更新批量导出按钮状态
  updateBatchBtnState();

  if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="10" style="text-align:center;color:#9ca3af">暂无历史记录</td></tr>`;
    // 仍然更新 Tab 计数
    updateTabCounts();
    return;
  }

  tbody.innerHTML = list.map((r, i) => {
    // 节点列：显示 payment_type（中文），逗号分隔
    let typeStr = r.typeSummary || "-";
    // 比例列
    let ratioStr = r.ratioSummary || "-";
    // 金额列
    let amountStr = r.amountSummary || "-";
    // 处理时间（Step 2）
    const elapsed = r.step2Elapsed != null
      ? (r.step2Elapsed >= 10 ? r.step2Elapsed.toFixed(1) + "s" : r.step2Elapsed.toFixed(2) + "s")
      : "-";
    return `
    <tr>
      <td style="text-align:center;"><input type="checkbox" class="hist-check" data-id="${esc(r.id)}" /></td>
      <td class="idx">${i + 1}</td>
      <td style="white-space:nowrap;font-size:12px;">${esc(r.createdAt)}</td>
      <td title="${esc(r.filename)}">${esc(r.filename)}</td>
      <td><span class="tag">${esc(r.contractTypeLabel)}</span></td>
      <td style="font-size:12px;max-width:200px;word-break:break-word;">${esc(typeStr)}</td>
      <td style="font-size:12px;">${esc(ratioStr)}</td>
      <td style="font-size:12px;">${esc(amountStr)}</td>
      <td style="font-size:12px;white-space:nowrap;">${esc(elapsed)}</td>
      <td>
        <button type="button" class="hist-view-btn" data-id="${esc(r.id)}">查看</button>
        <button type="button" class="hist-del-btn" data-id="${esc(r.id)}">删除</button>
      </td>
    </tr>`;
  }).join("");

  // 事件绑定
  tbody.querySelectorAll(".hist-view-btn").forEach((btn) => {
    btn.addEventListener("click", () => viewHistory(btn.dataset.id));
  });
  tbody.querySelectorAll(".hist-del-btn").forEach((btn) => {
    btn.addEventListener("click", () => deleteHistory(btn.dataset.id));
  });
  // 勾选框变化时更新批量导出按钮
  tbody.querySelectorAll(".hist-check").forEach((cb) => {
    cb.addEventListener("change", updateBatchBtnState);
  });

  // 更新 Tab 计数
  updateTabCounts();
}

// 更新 Tab 标签上的记录数
async function updateTabCounts() {
  const tabs = document.querySelectorAll(".tab-btn");
  for (const btn of tabs) {
    const tab = btn.dataset.tab;
    try {
      const resp = await fetch(`/api/history?type=${encodeURIComponent(tab)}`);
      if (resp.ok) {
        const list = await resp.json();
        const baseLabel = tab === "extract" ? "提取" : "对比";
        btn.textContent = `${baseLabel}（${list.length}）`;
      }
    } catch (_) {}
  }
}

function updateBatchBtnState() {
  const batchBtn = document.getElementById("batch-export-history-btn");
  if (!batchBtn) return;
  const checks = document.querySelectorAll(".hist-check");
  const anyChecked = Array.from(checks).some((cb) => cb.checked);
  const allChecked = checks.length > 0 && Array.from(checks).every((cb) => cb.checked);
  batchBtn.disabled = !anyChecked;
  // 更新按钮文案显示选中数量
  const checkedCount = Array.from(checks).filter((cb) => cb.checked).length;
  batchBtn.textContent = checkedCount > 0
    ? `批量导出 Excel（${checkedCount}条）`
    : "批量导出 Excel";
  // 同步全选框状态
  const selectAll = document.getElementById("hist-select-all");
  if (selectAll) selectAll.checked = allChecked;
}

async function viewHistory(id) {
  let record;
  try {
    const resp = await fetch(`/api/history/${encodeURIComponent(id)}`);
    if (!resp.ok) { alert("记录不存在"); return; }
    record = await resp.json();
  } catch (e) {
    alert("加载记录失败: " + e);
    return;
  }

  // 加载 Step 1 数据
  if (record.step1Data) {
    renderStep1(record.step1Data);
    step1Paragraphs = record.step1Data.paragraphs || [];
    lastStep1Payload = record.step1Data;
    setRerunBtnEnabled((record.step1Data.paragraphs || []).length > 0);
  }

  // 加载 Step 2 数据
  if (record.step2Data) {
    renderStep2(record.step2Data);
    lastStep2Result = record.step2Data;
    // 若是对比模式记录，恢复 GT 数据并渲染对比结果
    if (record.compareData) {
      lastGtPaymentStages = record.gtData || null;
      renderCompareResult(record.compareData);
    }
  } else {
    els.step2Card.hidden = true;
  }

  // 滚动到 Step 1
  els.step1Card.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function deleteHistory(id) {
  try {
    await fetch(`/api/history/${encodeURIComponent(id)}`, { method: "DELETE" });
  } catch (_) {}
  await renderHistory();
}

// 清空全部（仅清空当前 Tab 类型）
document.getElementById("clear-history-btn")?.addEventListener("click", async () => {
  if (!confirm(`确定清空「${currentHistoryTab === "extract" ? "提取" : "对比"}」全部历史记录？`)) return;
  try {
    await fetch(`/api/history?type=${encodeURIComponent(currentHistoryTab)}`, { method: "DELETE" });
  } catch (_) {}
  await renderHistory();
});

// 历史记录 Tab 切换
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.tab === currentHistoryTab) return;
    currentHistoryTab = btn.dataset.tab;
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    renderHistory();
  });
});

// 全选/取消全选
document.getElementById("hist-select-all")?.addEventListener("change", (e) => {
  const checked = e.target.checked;
  document.querySelectorAll(".hist-check").forEach((cb) => { cb.checked = checked; });
  updateBatchBtnState();
});

// 批量导出历史记录到 Excel（通过后端 API 直接处理）
async function batchExportHistory() {
  const checks = document.querySelectorAll(".hist-check:checked");
  const ids = Array.from(checks).map((cb) => cb.dataset.id);
  if (!ids.length) { alert("请先勾选要导出的历史记录"); return; }

  const btn = document.getElementById("batch-export-history-btn");
  if (btn) { btn.disabled = true; btn.textContent = "导出中..."; }

  try {
    const resp = await fetch("/api/history/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });

    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      alert(errData.error || `导出失败: HTTP ${resp.status}`);
      return;
    }

    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `history_export_${new Date().toISOString().slice(0,10)}.xlsx`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } catch (err) {
    alert(`批量导出失败: ${err}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "批量导出 Excel"; }
  }
}

document.getElementById("batch-export-history-btn")?.addEventListener("click", batchExportHistory);

// 页面加载时：获取各 Tab 计数，自动切换到有记录的 Tab，然后渲染
(async () => {
  // 先获取两种类型的记录数
  let extractCount = 0, analyzeCount = 0;
  try {
    const [extResp, anaResp] = await Promise.all([
      fetch("/api/history?type=extract"),
      fetch("/api/history?type=analyze"),
    ]);
    if (extResp.ok) extractCount = (await extResp.json()).length;
    if (anaResp.ok) analyzeCount = (await anaResp.json()).length;
  } catch (_) {}
  // 如果默认 Tab 无记录但另一个有记录，自动切换
  if (extractCount === 0 && analyzeCount > 0) {
    currentHistoryTab = "analyze";
    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === "analyze");
    });
  }
  await renderHistory();
})();

// ===== Excel 导出 =====
const PAYMENT_EXPORT_FIELDS = [
  { key: "clause_category", label: "类别" },
  { key: "payment_type", label: "阶段类型" },
  { key: "payment_code", label: "节点编码" },
  { key: "payment_ratio", label: "比例" },
  { key: "payment_amount", label: "金额" },
  { key: "payment_days", label: "付款天数" },
  { key: "latest_payment_stage", label: "最迟付款节点" },
  { key: "latest_payment_date", label: "最迟付款时间(天)" },
  { key: "payment_clause", label: "条款原文" },
  { key: "payment_context", label: "上下文" },
];
const WARRANTY_EXPORT_FIELDS = [
  { key: "warranty", label: "质保期" },
  { key: "warranty_clause", label: "条款原文" },
];

function openExportModal() {
  const modal = document.getElementById("export-modal");
  if (!modal) return;

  // 渲染付款字段复选框
  const payBox = document.getElementById("payment-field-checkboxes");
  payBox.innerHTML = PAYMENT_EXPORT_FIELDS.map((f) => `
    <label><input type="checkbox" value="${f.key}" data-group="payment" checked /> ${esc(f.label)}</label>
  `).join("");

  // 渲染质保期字段复选框
  const warBox = document.getElementById("warranty-field-checkboxes");
  warBox.innerHTML = WARRANTY_EXPORT_FIELDS.map((f) => `
    <label><input type="checkbox" value="${f.key}" data-group="warranty" checked /> ${esc(f.label)}</label>
  `).join("");

  modal.hidden = false;
}

function closeExportModal() {
  const modal = document.getElementById("export-modal");
  if (modal) modal.hidden = true;
}

// 全选 / 全不选
document.querySelectorAll(".select-all-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const group = btn.dataset.target;
    document.querySelectorAll(`input[data-group="${group}"]`).forEach((cb) => { cb.checked = true; });
  });
});
document.querySelectorAll(".select-none-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const group = btn.dataset.target;
    document.querySelectorAll(`input[data-group="${group}"]`).forEach((cb) => { cb.checked = false; });
  });
});

// 确认导出
async function doExport() {
  const selectedPayment = Array.from(
    document.querySelectorAll('input[data-group="payment"]:checked')
  ).map((cb) => cb.value);
  const selectedWarranty = Array.from(
    document.querySelectorAll('input[data-group="warranty"]:checked')
  ).map((cb) => cb.value);

  if (!selectedPayment.length && !selectedWarranty.length) {
    alert("请至少选择一个导出字段");
    return;
  }

  // 为付款条目补充上下文字段（从 step1Paragraphs 匹配）
  const enrichedPayment = lastPaymentItems.map((it) => {
    const ctx = findContextByClause(it.payment_clause) || it.payment_context || "";
    return { ...it, payment_context: ctx };
  });

  const confirmBtn = document.getElementById("export-modal-confirm");
  if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = "生成中..."; }

  try {
    const resp = await fetch("/api/export-excel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        payment_items: enrichedPayment,
        warranty_items: lastWarrantyItems,
        payment_fields: selectedPayment,
        warranty_fields: selectedWarranty,
      }),
    });

    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      alert(errData.error || `导出失败: HTTP ${resp.status}`);
      return;
    }

    // 下载文件
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = resp.headers.get("Content-Disposition")?.split("filename=")[1]
      || `payment_export_${Date.now()}.xlsx`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    closeExportModal();
  } catch (err) {
    alert(`导出失败: ${err}`);
  } finally {
    if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = "确认导出"; }
  }
}

// 事件绑定
document.getElementById("export-excel-btn")?.addEventListener("click", openExportModal);
document.getElementById("export-modal-close")?.addEventListener("click", closeExportModal);
document.getElementById("export-modal-cancel")?.addEventListener("click", closeExportModal);
document.getElementById("export-modal-confirm")?.addEventListener("click", doExport);
// 点击蒙层关闭
document.getElementById("export-modal")?.addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeExportModal();
});
