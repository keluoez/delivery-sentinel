/* 交期哨兵 前端 SPA（零构建）—— 仿 Dify：左导航 + 页面路由 + 智能体画布 + 抽屉 + 对话 */
"use strict";

/* ───────────── 工具 ───────────── */
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const resp = await fetch("/api" + path, {
    headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || data.message || ("HTTP " + resp.status));
  return data;
}

function toast(msg, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  $("#toast-box").appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

const LV = {
  red: { label: "严重", cls: "red" }, orange: { label: "警告", cls: "orange" },
  yellow: { label: "提醒", cls: "yellow" }, green: { label: "正常", cls: "green" },
};
const badge = lv => `<span class="badge ${LV[lv]?.cls || "gray"}">${LV[lv]?.label || lv}</span>`;
const dshort = s => (s || "").slice(5).replace("-", "/");
const tshort = s => (s || "").slice(11, 16);
const pct = v => Math.round((v || 0) * 100);

/* ───────────── 全局状态 ───────────── */
const S = {
  page: "dashboard", ordersFilter: "", ordersQ: "", alertsTab: "open",
  dash: null, graph: null, run: null, runTimer: null, chat: [],
};

/* ───────────── 页面注册 ───────────── */
const PAGES = {
  dashboard: { title: "工作台", desc: "订单履约风险全景 · 事件流实时接入", render: renderDashboard },
  orders: { title: "订单风险", desc: "订单-工序-物料状态图 · 实际 vs 计划进度", render: renderOrders },
  hub: { title: "感知中心", desc: "真实数据接入：事件 API / 文件收件箱 / API 轮询 / 邮箱 · 未解析队列", render: renderHub },
  alerts: { title: "预警中心", desc: "每条预警带证据链、调查报告与 AI 催办草稿 · 人工确认后发送", render: renderAlerts },
  agent: { title: "智能体运行", desc: "LangGraph 工作流 · 逐节点可观测", render: renderAgent },
  copilot: { title: "智能助手", desc: "自主查证（ReAct）· 追问预警原因 · 查进度与物料缺口", render: renderCopilot },
  settings: { title: "设置", desc: "LLM Provider（可选）· 风险阈值", render: renderSettings },
};

function go(page) {
  S.page = page;
  $$("#nav a").forEach(a => a.classList.toggle("active", a.dataset.page === page));
  const p = PAGES[page];
  $("#page-title").textContent = p.title;
  $("#page-desc").textContent = p.desc;
  p.render().catch(e => { $("#content").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; });
}

window.addEventListener("hashchange", () => {
  const page = (location.hash || "#/dashboard").slice(2) || "dashboard";
  if (PAGES[page]) go(page);
});

/* ───────────── 数据刷新 ───────────── */
async function refreshDash() {
  S.dash = await api("/dashboard");
  $("#pending-chip").hidden = !S.dash.pending_events;
  if (S.dash.pending_events) $("#pending-chip").textContent = `📥 ${S.dash.pending_events} 条事件待消费`;
  setLLM(S.dash.llm_enabled);
  $(".logo-sub").textContent = "Delivery Sentinel v2.0";
  const b = $("#nav-alert-badge");
  b.hidden = !S.dash.kpis.pending_alerts; b.textContent = S.dash.kpis.pending_alerts;
  const ub = $("#nav-unparsed-badge");
  ub.hidden = !S.dash.unparsed_total; ub.textContent = S.dash.unparsed_total;
}
async function refreshAlertBadge() {
  await refreshDash();
}
function setLLM(on) {
  $("#llm-dot").className = "dot " + (on ? "on" : "off");
  $("#llm-status").textContent = on ? "LLM：已接入" : "LLM：模板引擎（未配置 Key）";
}

/* ───────────── 工作台 ───────────── */
function donutSVG(dist) {
  const total = dist.reduce((s, d) => s + d.count, 0) || 1;
  const C = 2 * Math.PI * 54, colors = { red: "#dc2626", orange: "#ea580c", yellow: "#ca8a04", green: "#16a34a" };
  let acc = 0, segs = "";
  for (const d of dist) {
    if (!d.count) continue;
    const frac = d.count / total;
    segs += `<circle r="54" cx="70" cy="70" fill="none" stroke="${colors[d.level]}" stroke-width="15"
      stroke-dasharray="${(frac * C).toFixed(1)} ${(C - frac * C).toFixed(1)}"
      stroke-dashoffset="${(-acc * C).toFixed(1)}" transform="rotate(-90 70 70)"/>`;
    acc += frac;
  }
  return `<svg width="140" height="140" viewBox="0 0 140 140">${segs}
    <text x="70" y="68" text-anchor="middle" font-size="24" font-weight="700" fill="#1f2329">${total}</text>
    <text x="70" y="88" text-anchor="middle" font-size="11" fill="#8a919f">在制订单</text></svg>`;
}

async function renderDashboard() {
  await refreshDash();
  const d = S.dash;
  const kpi = (lbl, num, cls = "") => `<div class="card kpi"><div class="lbl">${lbl}</div><div class="num ${cls}">${num}</div></div>`;
  $("#content").innerHTML = `
    <div class="kpis">
      ${kpi("在制订单", d.kpis.total, "primary")}
      ${kpi("风险订单", d.kpis.at_risk, d.kpis.at_risk ? "orange" : "")}
      ${kpi("红+橙预警", d.kpis.red_orange, d.kpis.red_orange ? "red" : "")}
      ${kpi("待处理预警", d.kpis.pending_alerts)}
    </div>
    <div class="grid-3">
      <div class="card card-pad">
        <div class="card-title">风险分布</div>
        <div style="display:flex;justify-content:center">${donutSVG(d.distribution)}</div>
        <div style="margin-top:10px">${d.distribution.map(x =>
          `<span class="chip" style="margin:2px">${badge(x.level)} ${x.count} 单</span>`).join("")}</div>
      </div>
      <div class="card card-pad">
        <div class="card-title">TOP 风险订单 <button class="btn btn-sm" data-go="orders">全部 →</button></div>
        ${d.top_risks.length ? d.top_risks.map(r => `
          <div class="ev" style="cursor:pointer" data-oid="${r.order_id}">
            <div style="flex:none;padding-top:2px">${badge(r.level)}</div>
            <div style="flex:1;min-width:0">
              <div><span class="oid">${r.order_id}</span> ${esc(r.product)} <span class="muted">· ${esc(r.customer)}</span></div>
              <div class="muted" style="font-size:12px;margin-top:2px">进度 ${pct(r.progress)}% / 计划 ${pct(r.expected)}% · 距交期 ${r.days_to_due} 天</div>
            </div>
            <div style="flex:none;font-weight:700;color:var(--orange)">${r.score.toFixed(0)}<span class="muted" style="font-size:11px"> 分</span></div>
          </div>`).join("") : `<div class="empty">暂无风险订单 🎉</div>`}
      </div>
      <div class="card card-pad">
        <div class="card-title">实时事件流</div>
        ${d.recent_events.map(evEvent).join("") || `<div class="empty">暂无事件</div>`}
      </div>
    </div>
    <div class="card card-pad" style="margin-top:14px">
      <div class="card-title">模拟数据源（演示「感知 → 决策 → 执行」闭环）</div>
      <div class="toolbar" style="margin:0">
        <select id="sim-scenario" class="form" style="width:auto">${(await api("/scenarios")).data.map(s =>
          `<option value="${s.key}">${s.label} — ${s.desc}</option>`).join("")}</select>
        <select id="sim-order" style="border:1px solid var(--border);border-radius:8px;padding:8px 12px">
          ${(await api("/scenarios")).orders.map(o => `<option value="${o.id}">${o.label}</option>`).join("")}</select>
        <button class="btn" id="btn-inject">📨 注入事件</button>
        <button class="btn" id="btn-inject-run">📨 注入并立即扫描</button>
        <span class="muted" style="font-size:12px">注入的事件进入队列，由智能体「事件接入」节点消费</span>
      </div>
    </div>`;
}

function evEvent(e) {
  return `<div class="ev"><div class="t">${tshort(e.ts)}</div>
    <div style="flex:1;min-width:0"><div style="font-size:12px"><span class="badge gray">${esc(e.type)} · ${esc(e.source)}</span> <span class="muted">${e.order_id || "—"}</span></div>
    <div style="margin-top:2px">${esc(e.content)}</div></div></div>`;
}

/* ───────────── 订单风险 ───────────── */
async function renderOrders() {
  const q = S.ordersQ ? "&q=" + encodeURIComponent(S.ordersQ) : "";
  const r = await api("/orders?level=" + S.ordersFilter + q);
  const chip = (v, lbl) => `<button class="fchip ${S.ordersFilter === v ? "on" : ""}" data-lv="${v}">${lbl}</button>`;
  $("#content").innerHTML = `
    <div class="toolbar">
      ${chip("", "全部")}${chip("red", "🔴 严重")}${chip("orange", "🟠 警告")}${chip("yellow", "🟡 提醒")}${chip("green", "🟢 正常")}
      <input class="search" id="ord-q" placeholder="搜索订单号 / 客户 / 产品…" value="${esc(S.ordersQ)}">
    </div>
    <div class="card" style="overflow-x:auto"><table class="tbl"><thead><tr>
      <th>订单</th><th>客户</th><th>产品 × 数量</th><th>交期</th><th style="min-width:160px">进度（实际 vs 计划）</th><th>物料</th><th>风险</th></tr></thead>
      <tbody>${r.data.map(({ order: o, level, score }) => `
        <tr class="click" data-oid="${o.id}">
          <td><span class="oid">${o.id}</span><div class="muted" style="font-size:12px">${o.priority}</div></td>
          <td>${esc(o.customer)}</td>
          <td>${esc(o.product)}<div class="muted" style="font-size:12px">× ${o.qty} 台 · ${o.workshop}</div></td>
          <td>${dshort(o.due_date)}<div class="muted" style="font-size:12px">${o.overdue ? "已超期 " + (-o.days_to_due) + " 天" : "剩 " + o.days_to_due + " 天"}</div></td>
          <td><div style="display:flex;align-items:center;gap:8px">
            <div class="progress" style="flex:1"><i style="width:${pct(o.progress)}%"></i><u style="left:${pct(o.expected_progress)}%"></u></div>
            <span style="font-size:12px;width:70px">${pct(o.progress)}% / ${pct(o.expected_progress)}%</span></div></td>
          <td>${o.max_shortage_days > 0 ? `<span class="badge orange">缺口 ${o.max_shortage_days} 天</span>` : `<span class="badge green">齐套</span>`}</td>
          <td>${badge(level)}<div class="muted" style="font-size:12px">${level === "green" ? "—" : score.toFixed(0) + " 分"}</div></td>
        </tr>`).join("")}</tbody></table></div>`;
  $("#ord-q").addEventListener("keydown", e => { if (e.key === "Enter") { S.ordersQ = e.target.value; renderOrders(); } });
}

/* ───────────── 感知中心 ───────────── */
async function renderHub() {
  const h = await api("/hub");
  const scenarioOrders = (await api("/scenarios")).orders;
  const connCard = c => `
    <div class="card card-pad" style="margin-bottom:14px">
      <div class="card-title">${esc(c.label)}
        <span>
          ${c.configured ? `<span class="badge green">已配置</span>` : `<span class="badge gray">未配置</span>`}
          ${c.auto_running ? `<span class="badge blue">自动运行中</span>` : ""}
        </span>
      </div>
      <p class="muted" style="font-size:12.5px">${esc(c.desc)}</p>
      <div class="muted" style="font-size:12px;margin:6px 0">
        累计摄入 ${c.total_events} 条 · 最近运行 ${esc(c.last_run_at || "—")}
        ${c.last_error ? ` · <span style="color:var(--red)">${esc(c.last_error)}</span>` : ""}
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-sm" data-hub-act="scan" data-name="${c.name}">▶ 立即扫描</button>
        ${c.auto_running
          ? `<button class="btn btn-sm" data-hub-act="stop" data-name="${c.name}">⏹ 停止自动</button>`
          : `<button class="btn btn-sm" data-hub-act="start" data-name="${c.name}">↻ 启动自动</button>`}
        ${c.name === "inbox" ? `<button class="btn btn-sm" data-hub-act="sample">📄 生成示例表格</button>` : ""}
      </div>
    </div>`;
  const curlTxt = `curl -X POST http://127.0.0.1:8765/api/events \\
  -H "Authorization: Bearer ${h.token}" \\
  -H "Content-Type: application/json" \\
  -d '{"order_id":"MO-2609-01","type":"报工","source":"Xclaw","content":"OP30 本班完成 120 台","payload":{"step_hint":"整机组装","done_qty":120}}'`;
  $("#content").innerHTML = `
    <div class="grid-2">
      <div>
        <div class="card card-pad" style="margin-bottom:14px">
          <div class="card-title">通用事件推送（Xclaw / 任意 HTTP 客户端）</div>
          <p class="muted" style="font-size:12.5px;margin-bottom:8px">
            所有真实数据都从 <b>POST /api/events</b> 进入收件箱（Bearer token 鉴权）；
            自由文本走 <b>/api/events/extract</b>（LLM/正则抽取），抽不出就进未解析队列。
          </p>
          <div class="draft-box" id="curl-box">${esc(curlTxt)}</div>
          <button class="btn btn-sm" style="margin-top:6px" data-hub-act="copy">复制 curl</button>
        </div>
        <div class="card card-pad">
          <div class="card-title">摄入统计 <span class="muted" style="font-size:12px">收件箱待消费 ${h.pending_events} 条</span></div>
          ${Object.entries(h.ingest_stats).length
            ? Object.entries(h.ingest_stats).sort((a, b) => b[1] - a[1]).map(([k, v]) =>
              `<span class="chip" style="margin:2px">${esc(k)} · ${v} 条</span>`).join("")
            : `<span class="muted">还没有真实摄入记录——点上方"生成示例表格"再"立即扫描"试试</span>`}
          <div class="muted" style="font-size:12px;margin-top:8px">事件来源分布：${h.event_sources.map(([k, v]) => `${esc(k)} ${v}`).join(" · ") || "—"}</div>
        </div>
      </div>
      <div>${h.connectors.map(connCard).join("")}</div>
    </div>
    <div class="card card-pad" style="margin-top:4px">
      <div class="card-title">未解析队列（${h.unparsed_total}）<span class="muted" style="font-size:12px">抽取失败/订单不明的内容——宁可漏一条，不可错一条</span></div>
      ${h.unparsed.length ? `
        <table class="tbl"><thead><tr><th>来源</th><th style="min-width:240px">内容</th><th>原因</th><th style="min-width:220px">人工处理</th></tr></thead><tbody>
        ${h.unparsed.map(u => `<tr>
          <td>${esc(u.source || "—")}<div class="muted" style="font-size:11px">${esc((u.ts || "").slice(5, 16))}</div></td>
          <td style="font-size:12.5px">${esc(u.content)}</td>
          <td><span class="badge orange">${esc(u.reason || "未知")}</span></td>
          <td>
            <div style="display:flex;gap:6px;flex-wrap:wrap">
              <select data-uid="${u.id}" class="up-oid" style="border:1px solid var(--border);border-radius:6px;padding:4px;font-size:12px">
                ${scenarioOrders.map(o => `<option value="${o.id}">${o.id}</option>`).join("")}
              </select>
              <select data-uid="${u.id}" class="up-type" style="border:1px solid var(--border);border-radius:6px;padding:4px;font-size:12px">
                ${["订单变更", "报工", "供应商反馈", "设备事件"].map(t => `<option>${t}</option>`).join("")}
              </select>
              <button class="btn btn-sm" data-hub-act="resolve" data-uid="${u.id}">转正式事件</button>
              <button class="btn btn-sm" data-hub-act="ignore" data-uid="${u.id}">忽略</button>
            </div>
          </td></tr>`).join("")}</tbody></table>`
        : `<div class="empty">队列为空 🎉 所有感知层输入都成功解析为事件</div>`}
    </div>`;
}
/* ───────────── 感知中心动作 ───────────── */
async function handleHubAct(el) {
  const act = el.dataset.hubAct;
  try {
    if (act === "copy") { await navigator.clipboard.writeText($("#curl-box").textContent); toast("已复制 curl 命令"); return; }
    if (act === "sample") { await api("/inbox/sample", { method: "POST", body: {} }); toast("示例表格已生成 → 点「立即扫描」消费"); return; }
    if (act === "resolve") {
      const uid = el.dataset.uid;
      const oid = document.querySelector(`select.up-oid[data-uid="${uid}"]`).value;
      const type = document.querySelector(`select.up-type[data-uid="${uid}"]`).value;
      const r = await api(`/unparsed/${uid}/resolve`, { method: "POST", body: { order_id: oid, type } });
      toast(`已转为正式事件 ${r.event_id}，运行扫描后生效`);
      renderHub(); return;
    }
    if (act === "ignore") { await api(`/unparsed/${el.dataset.uid}/ignore`, { method: "POST", body: {} }); toast("已忽略"); renderHub(); return; }
    const r = await api(`/connectors/${el.dataset.name}/${act}`, { method: "POST", body: {} });
    toast(act === "scan" ? `扫描完成：新增 ${r.new_events} 条事件` : "连接器状态已更新");
    renderHub();
  } catch (e) { toast(e.message, true); }
}

/* ───────────── 预警中心 ───────────── */
function alertCard(a, withActions = true) {
  return `<div class="card alert-card ${a.level}">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <div>${badge(a.level)} <b>${esc(a.title)}</b></div>
      <div class="muted" style="font-size:12px">评分 ${a.score?.toFixed?.(0) ?? a.score} · ${tshort(a.created_at)} ${a.run_id ? "· " + a.run_id : ""}</div>
    </div>
    <p style="margin:8px 0 4px">${esc(a.summary)}</p>
    ${a.reasons.map(r => `
      <div class="reason"><span class="kind">【${esc(r.kind)}】</span>${esc(r.text)}
        <div>${(r.evidence || []).map(e => `<span class="evi"><b>${esc(e.source)}</b> · ${esc(e.detail)}</span>`).join("")}</div>
      </div>`).join("")}
    ${a.report ? `<div class="sec" style="margin-top:10px"><h3>🔍 调查报告（${esc(a.investigation?.via || "")}${a.investigation?.steps?.length ? ` · 自主查询 ${a.investigation.steps.length} 步` : ""}）</h3>
      <div class="draft-box" style="background:#f8fafc;color:#334155;border:1px solid var(--border)">${esc(a.report)}</div></div>` : ""}
    <p style="margin:8px 0"><b>建议动作：</b>${esc(a.suggestion)}</p>
    <div class="sec" style="margin-top:8px"><h3>AI 起草的催办消息（${esc(a.draft.channel)} → ${esc(a.draft.to)}）</h3>
      <div class="draft-box">${esc(a.draft.text)}</div></div>
    ${withActions && a.status === "待处理" ? `
      <div style="display:flex;gap:8px;margin-top:6px">
        <button class="btn btn-primary btn-sm" data-action="send" data-aid="${a.id}">✓ 确认无误，发送</button>
        <button class="btn btn-sm" data-action="ignore" data-aid="${a.id}">忽略</button>
      </div>` : `<div style="margin-top:6px"><span class="badge ${a.status === "已发送" ? "green" : "gray"}">${a.status}</span></div>`}
  </div>`;
}

async function renderAlerts() {
  const r = await api("/alerts?status=" + encodeURIComponent(S.alertsTab));
  const tab = (v, lbl) => `<button class="fchip ${S.alertsTab === v ? "on" : ""}" data-tab="${v}">${lbl}</button>`;
  $("#content").innerHTML = `
    <div class="toolbar">${tab("open", "进行中（待处理/已发送）")}${tab("待处理", "仅待处理")}${tab("all", "全部记录")}</div>
    ${r.data.length ? r.data.map(a => alertCard(a)).join("") : `<div class="card card-pad"><div class="empty">暂无预警。到「智能体运行」页跑一轮扫描试试 →</div></div>`}`;
}

/* ───────────── 智能体运行（画布） ───────────── */
const NODE_POS = {
  ingest: [18, 36], update: [218, 36], rule_scan: [418, 36], risk_score: [618, 36], dedup: [818, 36],
  attribute: [18, 262], investigate: [218, 262], draft: [418, 262], gate: [618, 262], finalize: [818, 262],
};

function nodeState(key, run) {
  if (!run || run.status === "idle") return "idle";
  const doneKeys = run.nodes.map(n => n.node);
  const idx = doneKeys.indexOf(key);
  if (idx >= 0) return "done";
  if (run.status === "done") return "skipped";
  // 运行中：最后一个已完成的下一节点是 running
  const order = S.graph.nodes.map(n => n.key);
  const nextKey = order[order.indexOf(doneKeys[doneKeys.length - 1]) + 1];
  return nextKey === key ? "running" : "idle";
}

const STATE_TXT = { idle: "待运行", running: "运行中", done: "完成", skipped: "跳过" };

function agentCanvas(run) {
  const pos = (k, o) => `style="left:${NODE_POS[k][0]}px;top:${NODE_POS[k][1]}px"`;
  const nodes = S.graph.nodes.map(n => {
    const st = nodeState(n.key, run);
    return `<div class="node ${st === "idle" ? "" : st}" ${pos(n.key)}
      title="${esc(n.sub)}"><span class="n-state">${STATE_TXT[st]}</span>
      <div class="n-ico">${n.icon}</div><div class="n-title">${n.title}</div>
      <div class="n-sub">${n.sub}</div></div>`;
  }).join("");
  const edge = (a, b, cond = false, label = "") => {
    const [ax, ay] = NODE_POS[a], [bx, by] = NODE_POS[b];
    const lit = run && ["done", "skipped"].includes(nodeState(a, run)) && nodeState(b, run) !== "idle";
    const cls = "edge-line" + (cond ? " cond" : "") + (lit ? " lit" : "");
    let d;
    if (ay === by) d = `M ${ax + 152} ${ay + 46} L ${bx - 6} ${by + 46}`;
    else d = `M ${ax + 76} ${ay + 100} C ${ax + 76} ${ay + 190}, ${bx + 76} ${by - 60}, ${bx + 76} ${by - 4}`;
    return `<path d="${d}" class="${cls}"/>` + (label ? `<text class="edge-label" x="${(ax + bx) / 2 + 40}" y="${(ay + by) / 2 + 40}">${label}</text>` : "");
  };
  const E = S.graph.edges;
  return `<div class="canvas"><svg style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none">
    <defs><marker id="arw" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#c3c9d4"/></marker></defs>
    ${E.map(e => edge(e.from, e.to, e.type === "cond", e.label || "")).join("")}
  </svg>${nodes}</div>`;
}

async function renderAgent() {
  S.graph = S.graph || await api("/graph");
  const runs = await api("/agent/runs");
  const lastRun = S.run || (runs.data[0] ? null : null);
  $("#content").innerHTML = `
    <div class="toolbar">
      <button class="btn btn-primary" id="btn-run">▶ 运行一轮扫描</button>
      <select id="run-history" style="border:1px solid var(--border);border-radius:8px;padding:8px 12px">
        <option value="">— 历史运行 —</option>
        ${runs.data.map(r => `<option value="${r.id}">${r.id} · ${r.status}${r.stats ? ` · 新预警 ${r.stats.new_alerts}` : ""}</option>`).join("")}
      </select>
      <span class="muted" style="font-size:12px">归因引擎：${S.dash ? (S.dash.llm_enabled ? "LLM" : "模板引擎（未配置 LLM Key）") : "…"}</span>
    </div>
    <div class="card canvas-wrap" style="padding:18px 0" id="canvas-box">
      ${agentCanvas(lastRun)}
    </div>
    <div class="card card-pad" style="margin-top:14px" id="log-box">
      <div class="card-title">节点日志</div>
      ${lastRun ? runLog(lastRun) : `<div class="empty">点击「运行一轮扫描」，观察事件如何变成带证据链的预警</div>`}
    </div>`;
}

function runLog(run) {
  if (run.status === "error") return `<div class="log"><div class="log-item err"><span class="lk">ERROR</span>${esc(run.error || "")}</div></div>`;
  if (!run.nodes.length) return `<div class="empty">运行中，等待第一个节点产出…</div>`;
  return `<div class="log">${run.nodes.map(n => `
    <div class="log-item"><span class="lk">${esc(n.title || n.node)}</span>${esc(n.summary)}
      <span class="ms">· ${n.ms}ms</span>
      ${n.detail && Object.keys(n.detail).length ? `<details><summary style="cursor:pointer;font-size:12px;color:var(--muted)">明细</summary>
        <div class="log-detail">${esc(JSON.stringify(n.detail, null, 1))}</div></details>` : ""}
    </div>`).join("")}</div>
  ${run.stats ? `<div style="margin-top:10px;color:var(--muted);font-size:12px">统计：扫描 ${run.stats.scanned} 单 · 信号 ${run.stats.signals} · 新预警 ${run.stats.new_alerts} · 升级 ${run.stats.upgrades} · 归因引擎 ${esc(run.stats.engine)}</div>` : ""}`;
}

/* ───────────── 智能助手 ───────────── */
async function renderCopilot() {
  const sugg = (await api("/copilot/suggestions")).data;
  $("#content").innerHTML = `<div class="card chat">
    <div class="chat-body" id="chat-body">${S.chat.length ? S.chat.map(chatMsg).join("") :
      `<div class="msg"><div class="avatar">🛰️</div><div class="bubble">你好，我是交期哨兵助手。可以问我：\n· 某订单为什么预警\n· 订单当前进度\n· 风险 / 物料缺口清单</div></div>`}</div>
    <div class="chat-foot">
      <div class="sugg">${sugg.map(s => `<button data-sugg="${esc(s)}">${esc(s)}</button>`).join("")}</div>
      <div class="chat-input"><input id="chat-in" placeholder="例如：MO-2609-01 为什么预警？"><button class="btn btn-primary" id="chat-send">发送</button></div>
    </div></div>`;
  const body = $("#chat-body"); body.scrollTop = body.scrollHeight;
}

function chatMsg(m) {
  return `<div class="msg ${m.role}"><div class="avatar">${m.role === "user" ? "👤" : "🛰️"}</div>
    <div><div class="bubble">${esc(m.text)}</div>
    ${m.role === "assistant" ? `<div class="via">${esc(m.via || "")}${m.refs?.length ? " · 涉及 " + m.refs.join("、") : ""}</div>` : ""}</div></div>`;
}

async function sendChat(text) {
  if (!text.trim()) return;
  S.chat.push({ role: "user", text });
  renderChatAppend(chatMsg(S.chat[S.chat.length - 1]));
  const thinking = { role: "assistant", text: "思考中…" };
  S.chat.push(thinking);
  renderChatAppend(chatMsg(thinking));
  try {
    const r = await api("/copilot", { method: "POST", body: { message: text } });
    thinking.text = r.reply; thinking.via = "via " + r.via; thinking.refs = r.refs;
  } catch (e) { thinking.text = "出错了：" + e.message; }
  renderCopilotRefresh();
}
function renderChatAppend(html) { const b = $("#chat-body"); b.insertAdjacentHTML("beforeend", html); b.scrollTop = b.scrollHeight; }
function renderCopilotRefresh() {
  const b = $("#chat-body");
  if (b) { b.innerHTML = S.chat.map(chatMsg).join(""); b.scrollTop = b.scrollHeight; }
}

/* ───────────── 设置 ───────────── */
async function renderSettings() {
  const s = await api("/settings/llm");
  const risk = await api("/settings/risk");
  const preset = s.presets[s.provider] || { base_url: "", model: "" };
  $("#content").innerHTML = `
    <div class="grid-2">
      <div class="card card-pad">
        <div class="card-title">LLM Provider（可选，OpenAI 兼容协议）</div>
        <div class="form">
          <div><label>Provider</label><select id="llm-provider">
            ${["none", "openai", "dashscope", "zhipu", "deepseek", "custom"].map(p =>
              `<option value="${p}" ${s.provider === p ? "selected" : ""}>${p === "none" ? "不使用（模板引擎）" : p}</option>`).join("")}
          </select></div>
          <div><label>Base URL</label><input id="llm-base" value="${esc(s.base_url || preset.base_url)}" placeholder="https://…/v1"></div>
          <div><label>Model</label><input id="llm-model" value="${esc(s.model || preset.model)}" placeholder="gpt-4o-mini / qwen-plus / glm-4-flash"></div>
          <div><label>API Key</label><input id="llm-key" type="password" value="" placeholder="${s.api_key_masked || "未设置"}">
            <div class="hint">当前：${esc(s.api_key_masked || "未配置")}。留空表示保留现有 Key。</div></div>
          <div style="display:flex;gap:8px">
            <button class="btn btn-primary" id="llm-save">保存</button>
            <button class="btn" id="llm-test">测试连接</button>
            <span id="llm-test-result" style="align-self:center;font-size:13px"></span>
          </div>
          <div class="hint">不配置 LLM 时，归因与助手使用内置确定性模板引擎，全部功能可用（内网部署友好）。配置后「归因生成」与「智能助手」自动升级为 LLM 输出。</div>
        </div>
      </div>
      <div>
        <div class="card card-pad" style="margin-bottom:14px">
          <div class="card-title">风险阈值（v1.0 内置）</div>
          <table class="tbl"><tbody>
            <tr><td>L1 进度偏差</td><td>红 ≥${(risk.rules.dev_red * 100).toFixed(0)}% · 橙 ≥${(risk.rules.dev_orange * 100).toFixed(0)}% · 黄 ≥${(risk.rules.dev_yellow * 100).toFixed(0)}%</td></tr>
            <tr><td>L1 物料缺口</td><td>橙 ≥${risk.rules.mat_orange_days} 天 · 黄 ≥${risk.rules.mat_yellow_days} 天</td></tr>
            <tr><td>L2 评分权重</td><td>${Object.entries(risk.score.weights).map(([k, v]) => `${k} ${(v * 100).toFixed(0)}%`).join(" · ")}</td></tr>
            <tr><td>L2 分级</td><td>红 ≥${risk.score.levels.red} · 橙 ≥${risk.score.levels.orange} · 黄 ≥${risk.score.levels.yellow}</td></tr>
            <tr><td>最终等级</td><td>max(规则等级, 评分等级)</td></tr>
          </tbody></table>
        </div>
        <div class="card card-pad">
          <div class="card-title">数据</div>
          <p class="muted" style="font-size:13px;margin-bottom:10px">演示数据基于相对"今天"的日期生成；重置后将恢复 12 张种子订单与历史事件。</p>
          <button class="btn btn-danger" id="btn-reseed">↺ 重置为种子数据</button>
        </div>
      </div>
    </div>`;
  $("#llm-provider").addEventListener("change", e => {
    const p = s.presets[e.target.value];
    if (p) { $("#llm-base").value = p.base_url; $("#llm-model").value = p.model; }
  });
}

/* ───────────── 订单抽屉 ───────────── */
async function openOrder(oid) {
  const d = await api("/orders/" + oid);
  const o = d.order;
  $("#drawer").hidden = false; $("#drawer-mask").hidden = false;
  $("#drawer").innerHTML = `
    <button class="close" id="drawer-close">✕</button>
    <h2>${o.id} <span style="font-weight:400">${esc(o.product)}</span></h2>
    <p class="muted">${esc(o.customer)} · × ${o.qty} 台 · ${o.workshop} · 优先级 ${o.priority}</p>
    <div style="margin:10px 0">${badge(d.level)} ${d.alert ? `<span class="chip" style="background:var(--primary-weak);color:var(--primary)">评分 ${d.alert.score.toFixed(0)}</span>` : ""}</div>
    <div class="sec"><h3>关键指标</h3>
      <div class="info-grid">
        <span class="k">交期</span><span>${o.due_date} ${o.overdue ? "（已超期）" : `（剩 ${o.days_to_due} 天）`}</span>
        <span class="k">实际进度</span><span>${pct(o.progress)}%</span>
        <span class="k">计划应达</span><span>${pct(o.expected_progress)}%</span>
        <span class="k">进度偏差</span><span>${o.deviation_pct > 0 ? `落后 ${o.deviation_pct} 个百分点` : "正常"}</span>
        <span class="k">订单变更</span><span>${o.change_count} 次</span>
      </div>
      <div class="progress" style="margin-top:10px;height:10px"><i style="width:${pct(o.progress)}%"></i><u style="left:${pct(o.expected_progress)}%"></u></div>
    </div>
    ${d.alert ? `<div class="sec"><h3>当前预警</h3>${alertCard(d.alert)}</div>` : ""}
    <div class="sec"><h3>工序（${o.steps.length}）</h3>
      ${o.steps.map(s => `<div class="step"><div class="row1"><b>${s.seq}. ${esc(s.name)}</b>
        <span class="muted">${esc(s.workshop)} · ${dshort(s.planned_start)}~${dshort(s.planned_end)} · ${s.status}</span></div>
        <div class="progress"><i style="width:${pct(s.actual_progress)}%"></i></div>
        ${s.device_note ? `<span class="warn-note">⚠ ${esc(s.device_note)}</span>` : ""}</div>`).join("")}
    </div>
    <div class="sec"><h3>物料</h3>
      ${o.materials_status.length ? o.materials_status.map(m => `<div class="step">
        <div class="row1"><b>${esc(m.name)}</b>${m.shortage_days > 0 ? `<span class="badge orange">缺口 ${m.shortage_days} 天</span>` : `<span class="badge ${m.arrived ? "green" : "blue"}">${m.status}</span>`}</div>
        <span class="muted" style="font-size:12px">${esc(m.spec)} × ${esc(m.qty_text)} · 需求 ${dshort(m.required_date)} · 承诺到货 ${dshort(m.eta)}${m.change_count ? `（已推迟 ${m.change_count} 次）` : ""}</span>
      </div>`).join("") : `<div class="muted">无外购物料</div>`}
    </div>
    <div class="sec"><h3>事件历史</h3>${d.events.map(evEvent).join("") || `<div class="muted">暂无事件</div>`}</div>`;
  $("#drawer-close").onclick = closeDrawer;
}
function closeDrawer() { $("#drawer").hidden = true; $("#drawer-mask").hidden = true; }

/* ───────────── 运行扫描 + 轮询 ───────────── */
async function runScan() {
  try {
    const r = await api("/agent/run", { method: "POST" });
    toast("扫描已启动：" + r.run_id);
    if (S.page !== "agent") location.hash = "#/agent";
    pollRun(r.run_id);
  } catch (e) { toast(e.message, true); }
}

function pollRun(runId) {
  clearInterval(S.runTimer);
  $("#btn-run-global").disabled = true;
  const t = setInterval(async () => {
    try {
      const run = await api("/agent/runs/" + runId);
      S.run = run;
      if (S.page === "agent") {
        const box = $("#canvas-box"); if (box) box.innerHTML = agentCanvas(run);
        const log = $("#log-box");
        if (log) log.innerHTML = `<div class="card-title">节点日志 · ${run.id}</div>` + runLog(run);
      }
      if (run.status !== "running") {
        clearInterval(t); $("#btn-run-global").disabled = false;
        const st = run.stats;
        if (run.status === "done") {
          toast(`扫描完成：新预警 ${st?.new_alerts ?? 0} 条 · 升级 ${st?.upgrades ?? 0} · 归因引擎 ${st?.engine ?? ""}`);
          await Promise.all([refreshDash(), refreshAlertBadge()]);
          if (S.page === "alerts") renderAlerts();
          if (S.page === "dashboard") renderDashboard();
        } else toast("扫描失败：" + (run.error || "未知错误"), true);
      }
    } catch (e) { clearInterval(t); $("#btn-run-global").disabled = false; toast(e.message, true); }
  }, 650);
}

/* ───────────── 全局事件委托 ───────────── */
document.addEventListener("click", async e => {
  const t = e.target;
  const hit = sel => t.closest(sel);
  let el;
  if (el = hit("[data-oid]")) return openOrder(el.dataset.oid);
  if (el = hit("[data-go]")) { location.hash = "#/" + el.dataset.go; return; }
  if (el = hit("[data-hub-act]")) { handleHubAct(el); return; }
  if (el = hit("#btn-run-global") || hit("#btn-run")) { runScan(); return; }
  if (el = hit("[data-action=send]")) {
    try { const r = await api(`/alerts/${el.dataset.aid}/send`, { method: "POST", body: {} }); toast(r.message); renderAlerts(); refreshAlertBadge(); }
    catch (err) { toast(err.message, true); } return;
  }
  if (el = hit("[data-action=ignore]")) {
    try { await api(`/alerts/${el.dataset.aid}/ignore`, { method: "POST", body: {} }); toast("已忽略"); renderAlerts(); refreshAlertBadge(); }
    catch (err) { toast(err.message, true); } return;
  }
  if (el = hit("[data-lv]")) { S.ordersFilter = el.dataset.lv; renderOrders(); return; }
  if (el = hit("[data-tab]")) { S.alertsTab = el.dataset.tab; renderAlerts(); return; }
  if (el = hit("[data-sugg]")) { sendChat(el.dataset.sugg); return; }
  if (el = hit("#chat-send")) { sendChat($("#chat-in").value); $("#chat-in").value = ""; return; }
  if (el = hit("#btn-inject") || hit("#btn-inject-run")) {
    try {
      const scenario = $("#sim-scenario").value, oid = $("#sim-order").value;
      const r = await api("/simulator/inject", { method: "POST", body: { scenario, order_id: oid } });
      toast(`已注入「${r.scenario.label}」→ ${r.order_id}；事件等待智能体消费`);
      await refreshDash();
      if (el.id === "btn-inject-run") runScan();
      else if (S.page === "dashboard") renderDashboard();
    } catch (err) { toast(err.message, true); } return;
  }
  if (el = hit("#llm-save")) {
    try {
      await api("/settings/llm", { method: "POST", body: {
        provider: $("#llm-provider").value, base_url: $("#llm-base").value.trim(),
        model: $("#llm-model").value.trim(), api_key: $("#llm-key").value.trim() } });
      toast("已保存 LLM 设置"); await refreshDash();
    } catch (err) { toast(err.message, true); } return;
  }
  if (el = hit("#llm-test")) {
    const out = $("#llm-test-result");
    out.textContent = "测试中…";
    try {
      await api("/settings/llm", { method: "POST", body: {
        provider: $("#llm-provider").value, base_url: $("#llm-base").value.trim(),
        model: $("#llm-model").value.trim(), api_key: $("#llm-key").value.trim() } });
      const r = await api("/settings/llm/test", { method: "POST", body: {} });
      out.textContent = r.message; out.style.color = r.ok ? "var(--green)" : "var(--red)";
      await refreshDash();
    } catch (err) { out.textContent = err.message; out.style.color = "var(--red)"; }
    return;
  }
  if (el = hit("#btn-reseed")) {
    try { await api("/admin/reseed", { method: "POST", body: {} }); toast("已重置"); S.run = null; S.chat = [];
      await Promise.all([refreshDash(), refreshAlertBadge()]); go(S.page); }
    catch (err) { toast(err.message, true); } return;
  }
  if (el = hit("#run-history")) return; // change 事件处理
  if (t.id === "drawer-close" || t.id === "drawer-mask") closeDrawer();
});
document.addEventListener("change", e => {
  if (e.target.id === "run-history" && e.target.value) {
    api("/agent/runs/" + e.target.value).then(run => { S.run = run;
      $("#canvas-box").innerHTML = agentCanvas(run);
      $("#log-box").innerHTML = `<div class="card-title">节点日志 · ${run.id}</div>` + runLog(run); });
  }
});
document.addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.id === "chat-in") { sendChat(e.target.value); e.target.value = ""; }
  if (e.key === "Escape") closeDrawer();
});

/* ───────────── 启动 ───────────── */
(async function init() {
  try { await refreshDash(); await refreshAlertBadge(); } catch (e) { toast("后端连接失败：" + e.message, true); }
  const page = (location.hash || "#/dashboard").slice(2) || "dashboard";
  go(PAGES[page] ? page : "dashboard");
})();
