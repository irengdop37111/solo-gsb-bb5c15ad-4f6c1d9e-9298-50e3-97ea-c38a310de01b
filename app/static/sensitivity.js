/* 偏移敏感性扫描抽屉 —— 复用 app.js 的全局工具（state / el / esc / fmtNum / toast / recompute）。 */
"use strict";

const sensState = {
  result: null,        // 最近一次扫描响应（差异相对扫描时的当前结果）
  sourceId: null,
};

const sens$ = (sel) => document.querySelector(sel);

/* ------------------------------------------------------------- 数据 */

function sensCurrentOffset(sid) {
  // 优先页面未保存调整，其次最近一次重算的生效值
  const ov = (state.overrides[sid] || {}).clock_offset;
  if (ov !== undefined && isFinite(Number(ov))) return Number(ov);
  const s = (state.result?.sources || []).find((x) => x.id === sid);
  return s ? s.effective.clock_offset : 0;
}

async function sensRun() {
  const sid = sens$("#sens-source").value;
  const lo = parseFloat(sens$("#sens-lo").value);
  const hi = parseFloat(sens$("#sens-hi").value);
  if (!sid) return toast("请选择要扫描的来源", "error");
  if (!isFinite(lo) || !isFinite(hi) || lo >= hi) {
    return toast("上下界无效：需要有限的数值且 下界 < 上界", "error");
  }
  const hint = sens$("#sens-hint");
  hint.textContent = "解析临界点并重算各区间…";
  let res;
  try {
    res = await fetch("/api/sensitivity/offset-scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_id: sid, lo, hi, overrides: state.overrides }),
    });
  } catch (err) {
    hint.textContent = "";
    return toast("扫描请求失败: " + err.message, "error");
  }
  hint.textContent = "";
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    sensState.result = null;
    sensRenderNotice(msg, true);
    return;
  }
  sensState.result = await res.json();
  sensState.sourceId = sid;
  sensRender();
}

/* ------------------------------------------------------------- 渲染 */

function sensRenderNotice(msg, isError) {
  const box = sens$("#sens-result");
  box.innerHTML = "";
  box.appendChild(el("div", "sens-notice" + (isError ? " error" : ""), esc(msg)));
}

function sensSegClass(iv) {
  if (iv.stats.impossible_edges > 0) return "bad";
  if (iv.stats.conflict_count > 0) return "warn";
  return "ok";
}

function sensRender() {
  const d = sensState.result;
  const box = sens$("#sens-result");
  box.innerHTML = "";
  if (!d) return;

  // 概要：基线 offset、相关边统计（含 offset 抵消提示）、临界点计数
  const head = el("div", "sens-summary");
  const cur = sensCurrentOffset(d.source.id);
  head.innerHTML = `
    <span>来源 <b>${esc(d.source.id)}</b> · 当前 offset <b>${fmtNum(cur)}s</b>
    （扫描基线 ${fmtNum(d.baseline.offset)}s）</span>
    <span>相关约束边 <b>${d.edges.scanned}</b> 条${d.edges.invariant
      ? ` · <span class="sens-cancel">${d.edges.invariant} 条两端同属该来源，offset 抵消</span>` : ""}</span>
    <span>临界点 <b>${d.critical_points.length}</b> 个 · 稳定区间 <b>${d.intervals.length}</b> 段</span>
    <span class="sens-baseline">当前冲突：${d.baseline.conflicts.length
      ? d.baseline.conflicts.map((c) => `<code>${esc(c.signature)}</code>`).join(" ")
      : "无"}</span>`;
  box.appendChild(head);

  if (d.message) {
    box.appendChild(el("div", "sens-notice", esc(d.message)));
    return;
  }

  box.appendChild(sensAxis(d));

  // 临界点一览（范围内）
  const cps = d.critical_points.filter((c) => c.offset > d.range.lo && c.offset < d.range.hi);
  if (cps.length) {
    const row = el("div", "sens-cps");
    row.innerHTML = `<span class="sens-cps-label">临界点（s）：</span>` + cps.map((c) =>
      `<span class="sens-cp" title="${esc(c.from)} → ${esc(c.to)}（${esc(c.kind)}）的 ${c.which} 在此穿越 0">${fmtNum(c.offset)}</span>`
    ).join("");
    box.appendChild(row);
  }

  const list = el("div", "sens-intervals");
  for (const iv of d.intervals) list.appendChild(sensIntervalCard(d, iv));
  box.appendChild(list);

  box.appendChild(el("div", "sens-footnote",
    "差异（＋新增 / －消失）相对扫描时的当前结果；点击区间把中点写入未保存 offset 后，可重新扫描刷新基线。"));
}

function sensAxis(d) {
  const { lo, hi } = d.range;
  const span = hi - lo;
  const pct = (x) => Math.max(0, Math.min(100, ((x - lo) / span) * 100));
  const outer = el("div", "sens-axis-outer");
  const axis = el("div", "sens-axis");
  for (const iv of d.intervals) {
    const seg = el("div", "sens-seg " + sensSegClass(iv));
    seg.style.left = pct(iv.lo) + "%";
    seg.style.width = Math.max(pct(iv.hi) - pct(iv.lo), 0.8) + "%";
    seg.title = `[${fmtNum(iv.lo)}, ${fmtNum(iv.hi)}] 中点 ${fmtNum(iv.representative)}s · `
      + `不可能边 ${iv.stats.impossible_edges} · 冲突 ${iv.stats.conflict_count}（点击写入中点）`;
    seg.addEventListener("click", () => sensApply(iv));
    axis.appendChild(seg);
  }
  for (const c of d.critical_points) {
    if (c.offset <= lo || c.offset >= hi) continue;
    const tick = el("div", "sens-tick");
    tick.style.left = pct(c.offset) + "%";
    axis.appendChild(tick);
  }
  const cur = sensCurrentOffset(d.source.id);
  if (cur >= lo && cur <= hi) {
    const marker = el("div", "sens-current");
    marker.style.left = pct(cur) + "%";
    marker.title = `当前 offset ${fmtNum(cur)}s`;
    axis.appendChild(marker);
  }
  const labels = el("div", "sens-axis-labels");
  labels.innerHTML = `<span>${fmtNum(lo)}s</span><span>offset（秒）</span><span>${fmtNum(hi)}s</span>`;
  outer.appendChild(axis);
  outer.appendChild(labels);
  return outer;
}

function sensIntervalCard(d, iv) {
  const cur = sensCurrentOffset(d.source.id);
  const isCur = iv.lo <= cur && cur <= iv.hi;
  const card = el("div", "sens-card " + sensSegClass(iv) + (isCur ? " current" : ""));
  card.title = "点击把该区间中点写入未保存 offset 并重算时间线";

  const sigs = (items, mark, cls, titleOf) => items.map((it) =>
    `<span class="sens-sig ${cls}" title="${esc(titleOf(it) || "")}">${mark}${esc(it.signature || (it.from + "→" + it.to))}</span>`
  ).join("");
  const conDiff = sigs(iv.conflicts_added, "＋", "added", (c) => c.explanation)
    + sigs(iv.conflicts_removed, "－", "removed", (c) => c.explanation);
  const impDiff = sigs(iv.impossible_added, "＋", "added", () => "")
    + sigs(iv.impossible_removed, "－", "removed", () => "");

  card.innerHTML = `
    <div class="sens-card-head">
      <span class="sens-range">[${fmtNum(iv.lo)}, ${fmtNum(iv.hi)}]</span>
      ${isCur ? '<span class="sens-cur-tag">当前</span>' : ""}
      <span class="sens-rep">中点 ${fmtNum(iv.representative)}s</span>
    </div>
    <div class="sens-card-stats">
      <span class="${iv.stats.impossible_edges ? "bad" : ""}">不可能边 ${iv.stats.impossible_edges}</span>
      <span class="${iv.stats.conflict_count ? "bad" : ""}">冲突链 ${iv.stats.conflict_count}</span>
      <span class="${iv.stats.ambiguity_group_count ? "warn" : ""}">歧义组 ${iv.stats.ambiguity_group_count}</span>
    </div>
    <div class="sens-diff"><span class="sens-diff-label">冲突</span>${conDiff || '<span class="sens-none">与当前一致</span>'}</div>
    <div class="sens-diff"><span class="sens-diff-label">不可能边</span>${impDiff || '<span class="sens-none">与当前一致</span>'}</div>
    <div class="sens-apply"><button class="btn tiny primary">✍ 写入中点 offset=${fmtNum(iv.representative)}s</button></div>`;
  card.addEventListener("click", () => sensApply(iv));
  return card;
}

/* ------------------------------------------------------------- 应用 */

async function sensApply(iv) {
  const sid = sensState.result.source.id;
  state.overrides[sid] = state.overrides[sid] || {};
  state.overrides[sid].clock_offset = iv.representative;  // 写入未保存 offset
  await recompute();                                      // 触发现有时间线重算
  toast(`已写入 ${sid} 的未保存 offset = ${iv.representative}s，时间线已重算（未保存，可放弃）`, "ok");
  sensRender();  // 重绘“当前”标记；差异仍相对扫描基线
}

/* ------------------------------------------------------------- 开关 */

function sensPrefill() {
  const sid = sens$("#sens-source").value;
  const cur = sensCurrentOffset(sid);
  sens$("#sens-lo").value = Math.floor(cur - 120);
  sens$("#sens-hi").value = Math.ceil(cur + 120);
}

function sensOpen() {
  if (!state.result || !state.result.sources.length) {
    return toast("暂无来源数据，请先导入事件", "error");
  }
  sens$("#sens-overlay").classList.remove("hidden");
  const sel = sens$("#sens-source");
  sel.innerHTML = state.result.sources.map((s) =>
    `<option value="${esc(s.id)}">${esc(s.name || s.id)}（${esc(s.id)}）</option>`).join("");
  if (sensState.sourceId && state.result.sources.some((s) => s.id === sensState.sourceId)) {
    sel.value = sensState.sourceId;
  }
  sensPrefill();
  if (sensState.result && sensState.result.source.id === sel.value) sensRender();
  else { sensState.result = null; sens$("#sens-result").innerHTML = ""; }
}

function sensClose() {
  sens$("#sens-overlay").classList.add("hidden");
}

sens$("#btn-sensitivity").addEventListener("click", sensOpen);
sens$("#sens-close").addEventListener("click", sensClose);
sens$("#sens-overlay").addEventListener("click", (e) => {
  if (e.target.id === "sens-overlay") sensClose();
});
sens$("#sens-run").addEventListener("click", sensRun);
sens$("#sens-source").addEventListener("change", () => { sensState.result = null; sens$("#sens-result").innerHTML = ""; sensPrefill(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !sens$("#sens-overlay").classList.contains("hidden")) sensClose();
});
