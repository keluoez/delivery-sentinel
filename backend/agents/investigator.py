"""预警调查员：橙/红预警触发后产出调查报告。

V2.1 重构（实测教训驱动）：
- 事实段（■现状 ■时间线 ■同类对照）由**引擎**从真实数据组装——数字永不出自模型；
- 模型（ReAct 探索，≤3 步工具调用）只产出**定性与建议**的解释段落；
- 解释段经过接地校验：出现系统中不存在的订单号即拒绝，回落模板定性；
- 多张预警并行调查（ThreadPoolExecutor），扫描墙钟时间 ÷4。
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from backend.config import LLM
from backend.domain import LEVEL_LABEL, Order
from backend.store import Store

INVESTIGATOR_SYSTEM = (
    "你是制造业交期风险调查员。调查规则：第一步必须调用 query_order 工具查询订单现状，"
    "禁止在没有工具查证的情况下直接回答。系统随后给出真实的订单现状、事件时间线与同类订单数据，"
    "你的职责是：找出根因模式（如同一供应商反复延期、单点物料依赖、插单挤占），"
    "给出定性判断与 1-2 条可执行建议。简体中文，不超过 120 字，"
    "不要复述事实数据，不要编造任何数字、日期、部件号或订单号。"
)

INVESTIGATOR_TOOLS = ("query_order", "get_events", "list_same_product", "list_material_shortages")


def _facts_sections(store: Store, order: Order, summary: str, signals: list[dict],
                    score: float) -> str:
    """事实段：全部来自引擎与真实数据，模型无权生成。"""
    today = store.today()
    od = order.to_dict(today)

    tl = [e for e in store.events if e.order_id == order.id]
    tl.sort(key=lambda e: e.ts, reverse=True)
    tl_lines = ["  %s [%s·%s] %s" % (e.ts[5:16], e.type, e.source, e.content[:60])
                for e in tl[:3]] or ["  （暂无事件记录）"]

    same = []
    for x in store.orders.values():
        if x.id != order.id and (x.product == order.product or x.customer == order.customer):
            a = store.open_alert_for(x.id)
            xd = x.to_dict(today)
            same.append("  %s %s：进度 %d%%/计划 %d%%，距交期 %d 天 [%s]" % (
                x.id, x.product, xd["progress"] * 100, xd["expected_progress"] * 100,
                xd["days_to_due"], LEVEL_LABEL[a.level] if a else "正常"))
    same_lines = "\n".join(same[:3]) if same else "  （无同类在制订单，属单点问题）"

    return (
        "■ 现状：%s %s × %d 台（%s优先级），进度 %.0f%%（计划应达 %.0f%%），"
        "距交期 %d 天，风险评分 %.0f\n"
        "■ 时间线：\n%s\n"
        "■ 同类对照：\n%s"
    ) % (order.id, order.product, order.qty, order.priority,
         od["progress"] * 100, od["expected_progress"] * 100, od["days_to_due"],
         score, "\n".join(tl_lines), same_lines)


def _template_traits(store: Store, order: Order) -> str:
    """模板定性（LLM 不可用时的兜底）。"""
    delays = sum(m.change_count for m in order.materials)
    traits = []
    if delays >= 2:
        traits.append("同一供应链接连推迟 %d 次，属供应商可靠性问题而非偶发，建议启动备选供应商" % delays)
    elif any(m.shortage_days > 0 for m in order.materials):
        traits.append("物料单点依赖，当前缺口尚未反复，需每日盯催防恶化")
    if order.overdue:
        traits.append("已超期，优先与客户沟通新交期窗口")
    if not traits:
        traits.append("多项风险因子叠加，建议人工确认根因后定向处理")
    return "；".join(traits) + "。"


def _interp_is_grounded(store: Store, order: Order, interp: str) -> bool:
    """解释段接地校验：不得出现系统中不存在的订单号；不得虚构本单不存在的物料号模式。"""
    for oid in set(re.findall(r"MO-\d{4}-\d{2}", interp)):
        if oid not in store.orders:
            return False
    # 提到的物料必须是该单真实物料（或不含具体物料编码）
    for m in re.findall(r"[A-Z]{2,}[-_]?\d{3,}[A-Z]?", interp):
        known = [x.name + x.spec for x in order.materials] + [x.name for x in order.materials]
        if not any(m in k or k in m for k in known):
            return False
    return True


def investigate(store: Store, order_id: str, level: str, summary: str,
                signals: list[dict], score: float = 0.0) -> dict:
    """总入口：事实段（引擎）+ 定性段（LLM ReAct / 模板兜底）。"""
    order = store.orders.get(order_id)
    if order is None:
        return {"report": "", "investigation": {"via": "skip", "steps": []}}

    facts = _facts_sections(store, order, summary, signals, score)
    interp, via, steps = None, "模板引擎", []
    if LLM.enabled:
        q = ("预警订单 %s（%s）。系统事实：\n%s\n\n请输出你的定性与建议（不要复述事实、不要编造数字）。"
             % (order.id, LEVEL_LABEL[level], facts))
        r = _react(store, q)
        if r["answer"] and _interp_is_grounded(store, order, r["answer"]):
            interp = r["answer"].strip()
            via = "LLM(%s·查证%d步)" % (LLM.model, len(r["steps"]))
            steps = r["steps"]
        elif r["answer"]:
            logging.getLogger("sentinel.investigator").warning(
                "调查解释未通过接地校验，回落模板：%s", r["answer"][:80])

    if interp is None:
        interp = _template_traits(store, order)

    report = "【调查报告 · %s】\n%s\n■ 定性与建议（%s）：\n%s" % (
        via.split("·")[0] if via != "模板引擎" else "模板引擎", facts, via, interp)
    return {"report": report, "investigation": {"via": via, "steps": steps}}


def investigate_many(store: Store, cands: list[dict]) -> list[dict]:
    """并行调查多张预警（互不共享状态，仅读 store → 线程安全）。"""
    if not cands:
        return []
    out = [None] * len(cands)
    with ThreadPoolExecutor(max_workers=min(4, len(cands))) as pool:
        futs = {pool.submit(investigate, store, c["order_id"], c["level"],
                            c.get("summary", ""), c["signals"],
                            score=c.get("score", 0.0)): i for i, c in enumerate(cands)}
        for fut in as_completed(futs):
            i = futs[fut]
            r = fut.result()
            out[i] = {**cands[i], "report": r["report"], "investigation": r["investigation"]}
    return out


def _react(store: Store, question: str) -> dict:
    from backend.agents.react import run_react
    try:
        return run_react(store, INVESTIGATOR_SYSTEM, question,
                         INVESTIGATOR_TOOLS, max_steps=3, min_tools=1, strict_tools=True)
    except Exception as exc:
        logging.getLogger("sentinel.investigator").warning("ReAct 调查失败，回落模板：%s", exc)
        return {"answer": None, "steps": [], "via": "react"}
