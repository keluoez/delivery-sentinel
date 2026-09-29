"""LangGraph 智能体工作流：交期哨兵的"感知-决策-执行"主干。

图结构（画布按此渲染）：
  START → 事件接入 → 状态更新 → 规则扫描 → 风险评分 → 预警去重
            └─(有新预警)→ 归因生成 → 动作起草 → 人工闸门 → 汇总收尾 → END
            └─(无新预警)──────────────────────────────┘

工程要点：
- graph.stream(stream_mode="updates") 逐节点产出，运行器把每个节点的
  trace 渐进写入 run record，前端据此"逐节点点亮"。
- 风险引擎(L1/L2)是纯函数；本图负责编排与副作用（事件消费、预警落库）。
"""
from __future__ import annotations

import operator
import time
from datetime import date, timedelta
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.agents import attribution as attr
from backend.config import LLM
from backend.domain import LEVEL_LABEL, LEVEL_ORDER, RiskAlert
from backend.engine.rules import rule_level, scan_order
from backend.engine.scoring import final_level, score_order
from backend.store import Store

# 画布节点元数据（前端渲染节点卡：图标/标题/副标题）
NODE_META = [
    {"key": "ingest", "icon": "📡", "title": "事件接入", "sub": "ERP·MES·邮件·微信 → 事件队列"},
    {"key": "update", "icon": "🔄", "title": "状态更新", "sub": "事件落地到订单-工序-物料图"},
    {"key": "rule_scan", "icon": "📏", "title": "规则扫描", "sub": "L1：超期/进度偏差/物料缺口"},
    {"key": "risk_score", "icon": "🧮", "title": "风险评分", "sub": "L2：四因子加权 0-100"},
    {"key": "dedup", "icon": "🧹", "title": "预警去重", "sub": "同级抑制 · 恶化升级"},
    {"key": "attribute", "icon": "🧠", "title": "归因生成", "sub": "L3：证据链解释 + 建议（LLM/模板）"},
    {"key": "investigate", "icon": "🔍", "title": "风险调查", "sub": "Agent 自主查证：时间线/同类单/定性"},
    {"key": "draft", "icon": "✍️", "title": "动作起草", "sub": "AI 催办消息草稿"},
    {"key": "gate", "icon": "🚦", "title": "人工闸门", "sub": "human-in-the-loop，人审后才发送"},
    {"key": "finalize", "icon": "📦", "title": "汇总收尾", "sub": "统计与运行记录"},
]


class SentinelState(TypedDict, total=False):
    today: date
    run_id: str
    events: list[dict]
    touched: list[str]
    signals: dict            # order_id -> [signal...]
    rule_levels: dict        # order_id -> level
    scores: dict             # order_id -> score dict
    candidates: list[dict]   # {order_id, level, score, signals, upgrade}
    skipped: list[dict]
    alerts: list[dict]       # 新建预警（dict 形态）
    stats: dict
    traces: Annotated[list[dict], operator.add]
    error: str


def build_graph(store: Store):
    """编译工作流。节点闭包 store，副作用加锁落库。"""

    # ── 1. 事件接入 ─────────────────────────────────────────────
    def ingest(state: SentinelState) -> dict:
        pending = store.pending_events()
        for e in pending:
            e.consumed = True
        store.save()
        evs = [e.to_dict() for e in pending]
        return {"events": evs,
                "traces": [{"node": "ingest", "summary": "拉取 %d 条新事件（已标记消费）" % len(evs),
                            "detail": {"events": [e["content"] for e in evs]}}]}

    # ── 2. 状态更新：事件落地 ────────────────────────────────────
    def update_state(state: SentinelState) -> dict:
        touched: list[str] = []
        applied = []
        with store.lock:
            for ev in state["events"]:
                p = ev.get("payload", {}) or {}
                order = store.orders.get(ev["order_id"]) if ev["order_id"] else None

                # 订单登记：主数据由事件驱动注册（感知层/API/表格均可推）
                if ev["type"] == "订单登记":
                    try:
                        from backend.seed_data import make_order_from
                        o = make_order_from(p)
                        store.orders[o.id] = o
                        if o.id not in touched:
                            touched.append(o.id)
                        applied.append(ev["id"])
                        continue
                    except Exception as exc:
                        store.add_unparsed({"source": ev.get("source"), "content": ev.get("content", ""),
                                            "reason": "订单登记失败：%s" % exc})
                        applied.append(ev["id"])
                        continue

                if order is None:
                    store.add_unparsed({"source": ev.get("source"), "content": ev.get("content", ""),
                                        "reason": "订单 %s 不存在" % (ev["order_id"] or "未知"),
                                        "suggestion": "先推送「订单登记」事件，或在感知中心人工指定订单"})
                    applied.append(ev["id"])
                    continue

                if ev["type"] == "报工":
                    step = None
                    if p.get("step"):
                        step = next((s for s in order.steps if s.name == p["step"]), None)
                    elif p.get("step_hint"):
                        step = next((s for s in order.steps if p["step_hint"] in s.name), None)
                    if step is None:  # 未指明工序 → 落到第一道未完工工序
                        step = next((s for s in order.steps if s.actual_progress < 0.999), None)
                    if step:
                        if p.get("delta"):
                            step.actual_progress = min(1.0, step.actual_progress + p["delta"])
                        elif p.get("done_qty") is not None and order.qty:
                            # done_qty 口径 = 本次完成数量 → 折算为该工序进度增量
                            delta = min(p["done_qty"] / order.qty, 1.0 - step.actual_progress)
                            if delta > 0:
                                step.actual_progress = min(1.0, step.actual_progress + delta)
                elif ev["type"] == "订单变更" and p.get("qty_delta"):
                    order.qty += int(p["qty_delta"])
                    order.change_count += 1
                elif ev["type"] == "供应商反馈":
                    m = None
                    if p.get("material"):
                        m = next((x for x in order.materials if x.name == p["material"]), None)
                    elif p.get("material_hint"):
                        hint = str(p["material_hint"])
                        m = next((x for x in order.materials
                                  if hint in x.name or x.name in hint), None)
                    if m:
                        if p.get("new_eta"):
                            from datetime import date as _d
                            m.eta = _d.fromisoformat(str(p["new_eta"])[:10])
                            m.change_count += 1
                        elif p.get("eta_shift_days"):
                            m.eta += timedelta(days=int(p["eta_shift_days"]))
                            m.change_count += 1
                    else:
                        store.add_unparsed({"source": ev.get("source"), "content": ev.get("content", ""),
                                            "reason": "订单 %s 未匹配到物料 %s" % (
                                                order.id, p.get("material") or p.get("material_hint"))})
                elif ev["type"] == "设备事件" and p.get("step"):
                    step = next((s for s in order.steps if s.name == p["step"]), None)
                    if step:
                        step.device_note = ev["content"]
                if order.id not in touched:
                    touched.append(order.id)
                applied.append(ev["id"])
            store.save()
        return {"touched": touched,
                "traces": [{"node": "update",
                            "summary": "落地 %d 条事件 → 更新 %d 张订单的状态图" % (
                                len(state["events"]), len(touched)),
                            "detail": {"orders": touched}}]}

    # ── 3. 规则扫描（L1） ───────────────────────────────────────
    def rule_scan(state: SentinelState) -> dict:
        today = state["today"]
        signals: dict[str, list] = {}
        for order in store.orders.values():
            sigs = scan_order(order, today)
            if sigs:
                signals[order.id] = sigs
        levels = {oid: rule_level(sigs) for oid, sigs in signals.items()}
        cnt = {"red": 0, "orange": 0, "yellow": 0}
        for lv in levels.values():
            if lv in cnt:
                cnt[lv] += 1
        return {"signals": signals, "rule_levels": levels,
                "traces": [{"node": "rule_scan",
                            "summary": "扫描 %d 单 → %d 条规则信号（红 %d · 橙 %d · 黄 %d）" % (
                                len(store.orders), sum(len(s) for s in signals.values()),
                                cnt["red"], cnt["orange"], cnt["yellow"]),
                            "detail": {"orders_with_signals": len(signals)}}]}

    # ── 4. 风险评分（L2） + 综合分级 ─────────────────────────────
    def risk_score(state: SentinelState) -> dict:
        today = state["today"]
        scores = {}
        candidates = []
        for order in store.orders.values():
            sc = score_order(order, today)
            scores[order.id] = sc
            lv = final_level(state["rule_levels"].get(order.id, "green"), sc["level"])
            if lv != "green":
                candidates.append({"order_id": order.id, "level": lv,
                                   "score": sc["score"],
                                   "signals": state["signals"].get(order.id, []),
                                   "upgrade": False})
        candidates.sort(key=lambda c: (LEVEL_ORDER[c["level"]], c["score"]), reverse=True)
        cnt = {}
        for c in candidates:
            cnt[c["level"]] = cnt.get(c["level"], 0) + 1
        return {"scores": scores, "candidates": candidates,
                "traces": [{"node": "risk_score",
                            "summary": "综合判定：%s｜绿灯 %d 单进入静默监控" % (
                                " · ".join("%s %d" % (LEVEL_LABEL[k], v) for k, v in cnt.items())
                                if cnt else "无风险订单",
                                len(store.orders) - len(candidates)),
                            "detail": {"candidates": [c["order_id"] + ":" + c["level"] for c in candidates]}}]}

    # ── 5. 预警去重（同级抑制 / 恶化升级） ────────────────────────
    def dedup(state: SentinelState) -> dict:
        keep, skipped, upgrades = [], [], 0
        with store.lock:
            for cand in state["candidates"]:
                open_a = store.open_alert_for(cand["order_id"])
                if open_a is None:
                    keep.append(cand)
                elif LEVEL_ORDER[cand["level"]] > LEVEL_ORDER[open_a.level]:
                    open_a.status = "已升级"
                    from backend.domain import now_str
                    open_a.updated_at = now_str()
                    cand["upgrade"] = True
                    cand["upgraded_from"] = open_a.id
                    keep.append(cand)
                    upgrades += 1
                else:
                    skipped.append({"order_id": cand["order_id"],
                                    "reason": "已有同级预警 %s（%s）" % (open_a.id, open_a.status)})
            store.save()
        return {"candidates": keep, "skipped": skipped,
                "traces": [{"node": "dedup",
                            "summary": "新增 %d · 升级 %d · 同级抑制 %d" % (
                                len(keep) - upgrades, upgrades, len(skipped)),
                            "detail": {"skipped": skipped}}]}

    def has_candidates(state: SentinelState) -> str:
        return "attribute" if state.get("candidates") else "finalize"

    # ── 6. 归因生成（L3，LLM 可插拔） ────────────────────────────
    def attribute(state: SentinelState) -> dict:
        today = state["today"]
        out = []
        via = "模板引擎" if not LLM.enabled else "LLM(%s)" % LLM.model
        for cand in state["candidates"]:
            order = store.orders[cand["order_id"]]
            reasons = attr.build_reasons(order, cand["signals"], state["scores"][cand["order_id"]])
            summary = attr.template_summary(order, today)
            suggestion = attr.template_suggestion(order, cand["signals"], today)
            llm_draft = None
            if LLM.enabled:
                r = attr.attribute_via_llm(order, cand["signals"],
                                           state["scores"][cand["order_id"]], today)
                if r:
                    summary, suggestion = r["summary"], r["suggestion"]
                    llm_draft = r["draft"]
                    via = "LLM(%s)" % LLM.model
            out.append({**cand, "reasons": reasons, "summary": summary,
                        "suggestion": suggestion, "llm_draft": llm_draft})
        return {"candidates": out,
                "traces": [{"node": "attribute",
                            "summary": "归因引擎：%s · %d 条预警完成证据链组装" % (via, len(out)),
                            "detail": {"engine": via}}]}

    # ── 7. 风险调查：橙/红预警并行调查（事实引擎组装 + LLM 解释） ──
    def investigate(state: SentinelState) -> dict:
        import time as _t
        from backend.agents import investigator
        targets = [c for c in state["candidates"] if c["level"] in ("orange", "red")]
        t0 = _t.time()
        done = investigator.investigate_many(store, targets)
        tool_calls = sum(len(c["investigation"].get("steps", [])) for c in done)
        llm_reports = sum(1 for c in done if c["investigation"].get("via", "").startswith("LLM"))
        by_id = {c["order_id"]: c for c in done}
        out = [by_id.get(c["order_id"], c) if c["level"] in ("orange", "red") else c
               for c in state["candidates"]]
        return {"candidates": out,
                "traces": [{"node": "investigate",
                            "summary": "调查员完成 %d 份报告（LLM %d / 模板 %d · 自主查询 %d 次 · %.1fs）" % (
                                len(done), llm_reports, len(done) - llm_reports,
                                tool_calls, _t.time() - t0)}]}

    # ── 8. 动作起草 ────────────────────────────────────────────
    def draft(state: SentinelState) -> dict:
        today = state["today"]
        for cand in state["candidates"]:
            order = store.orders[cand["order_id"]]
            if cand.get("llm_draft"):
                base = attr.build_draft(order, cand["level"], cand["signals"], today)
                base["text"] = cand["llm_draft"]["text"]
                base["to"] = cand["llm_draft"].get("to", base["to"])
                cand["draft"] = base
            else:
                cand["draft"] = attr.build_draft(order, cand["level"], cand["signals"], today)
        return {"candidates": state["candidates"],
                "traces": [{"node": "draft",
                            "summary": "生成 %d 条催办草稿（待人工确认后发送）" % len(state["candidates"])}]}

    # ── 9. 人工闸门：落库为"待处理" ───────────────────────────────
    def gate(state: SentinelState) -> dict:
        made = []
        with store.lock:
            for cand in state["candidates"]:
                order = store.orders[cand["order_id"]]
                alert = RiskAlert(
                    id=store.next_alert_id(), order_id=order.id, level=cand["level"],
                    score=cand["score"],
                    title="%s｜%s" % (order.id, order.product),
                    summary=cand["summary"], reasons=cand["reasons"],
                    suggestion=cand["suggestion"], draft=cand["draft"],
                    report=cand.get("report", ""),
                    investigation=cand.get("investigation", {}),
                    run_id=state.get("run_id", ""))
                store.alerts[alert.id] = alert
                made.append(alert.to_dict())
            store.save()
        return {"alerts": made,
                "traces": [{"node": "gate",
                            "summary": "%d 条预警进入人工闸门——确认草稿后才会推送企微" % len(made)}]}

    # ── 10. 汇总收尾 ────────────────────────────────────────────
    def finalize(state: SentinelState) -> dict:
        stats = {
            "scanned": len(store.orders),
            "events": len(state.get("events", [])),
            "signals": sum(len(v) for v in state.get("signals", {}).values()),
            "new_alerts": len(state.get("alerts", [])),
            "upgrades": sum(1 for c in state.get("candidates", []) if c.get("upgrade")),
            "skipped": len(state.get("skipped", [])),
            "reports": sum(1 for c in state.get("candidates", []) if c.get("report")),
            "engine": ("LLM:%s" % LLM.model) if LLM.enabled else "模板引擎",
        }
        return {"stats": stats,
                "traces": [{"node": "finalize",
                            "summary": "本轮完成：扫描 %d 单 · 新预警 %d · 升级 %d · 调查报告 %d" % (
                                stats["scanned"], stats["new_alerts"], stats["upgrades"],
                                stats["reports"])}]}

    g = StateGraph(SentinelState)
    g.add_node("ingest", ingest)
    g.add_node("update", update_state)
    g.add_node("rule_scan", rule_scan)
    g.add_node("risk_score", risk_score)
    g.add_node("dedup", dedup)
    g.add_node("attribute", attribute)
    g.add_node("investigate", investigate)
    g.add_node("draft", draft)
    g.add_node("gate", gate)
    g.add_node("finalize", finalize)
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "update")
    g.add_edge("update", "rule_scan")
    g.add_edge("rule_scan", "risk_score")
    g.add_edge("risk_score", "dedup")
    g.add_conditional_edges("dedup", has_candidates, {"attribute": "attribute", "finalize": "finalize"})
    g.add_edge("attribute", "investigate")
    g.add_edge("investigate", "draft")
    g.add_edge("draft", "gate")
    g.add_edge("gate", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


def run_sync(store: Store, run_record: dict, compiled=None):
    """同步执行一轮扫描：逐节点产出 trace，渐进写入 run_record（供前端轮询点亮）。"""
    compiled = compiled or build_graph(store)
    run_record["status"] = "running"
    init: SentinelState = {"today": store.today(), "run_id": run_record["id"], "traces": []}
    try:
        for chunk in compiled.stream(init, stream_mode="updates"):
            for node, update in chunk.items():
                if not update:
                    continue
                t0 = time.time()
                for tr in update.get("traces", []):
                    run_record["nodes"].append({
                        "node": node, "title": next(m["title"] for m in NODE_META if m["key"] == node),
                        "summary": tr.get("summary", ""), "detail": tr.get("detail", {}),
                        "ms": int((time.time() - t0) * 1000) + 40,  # 演示动画最低时长
                    })
                if "stats" in update:
                    run_record["stats"] = update["stats"]
        run_record["status"] = "done"
    except Exception as exc:  # 任何节点异常：记录并结束，产品不崩
        run_record["status"] = "error"
        run_record["error"] = str(exc)
    run_record["finished_at"] = time.strftime("%H:%M:%S")
    store.save()
    return run_record
