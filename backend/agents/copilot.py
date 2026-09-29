"""智能助手：自然语言查交期风险。

双引擎：
- LLM 开启 → ReAct 自主查证：模型自己决定查哪个订单、拉不拉事件线、要不要对照同类单，
  每一步工具调用都记录在案（前端可见"它自己查了什么"）；
- 无 Key / LLM 失败 → 规则意图引擎（确定性、可溯源），产品永不因 LLM 挂掉。
"""
from __future__ import annotations

import re
from datetime import date

from backend.config import LLM
from backend.domain import LEVEL_LABEL, LEVEL_ORDER
from backend.llm import provider as llm_provider
from backend.store import Store

_SUGGESTIONS = ["MO-2609-01 为什么预警？", "有哪些红色预警？", "受物料缺口影响的订单",
                "MO-2609-06 现在什么进度？", "快到交期的单子"]

COPILOT_SYSTEM = (
    "你是交期哨兵的智能助手。规则：第一步必须调用工具查询数据（如 query_order / list_alerts），"
    "禁止在没有任何工具查证的情况下回答——宁可说'请允许我先查一下'。"
    "回答用简体中文、先给结论、点出关键数字和订单号；只允许引用工具返回的事实。"
)
COPILOT_TOOLS = ("query_order", "get_alert", "get_events", "list_same_product",
                 "list_alerts", "list_material_shortages")


def _find_order(store: Store, msg: str):
    m = re.search(r"MO[-\s]?\d{4}[-\s]?\d{2}", msg.upper().replace(" ", "-"))
    if m:
        oid = m.group(0)
        # 归一化成 MO-XXXX-XX
        oid = re.sub(r"^MO-?(\d{4})-?(\d{2})$", r"MO-\1-\2", oid)
        return store.orders.get(oid)
    m = re.search(r"(\d{1,2})\s*号", msg)
    if m:
        oid = "MO-2609-%02d" % int(m.group(1))
        return store.orders.get(oid)
    return None


def _order_brief(store: Store, order, today: date) -> dict:
    od = order.to_dict(today)
    alert = store.open_alert_for(order.id)
    return {"id": order.id, "customer": order.customer, "product": order.product,
            "qty": order.qty, "priority": order.priority, "workshop": order.workshop,
            "due_date": od["due_date"], "days_to_due": od["days_to_due"],
            "overdue": od["overdue"], "progress_pct": round(od["progress"] * 100),
            "expected_pct": round(od["expected_progress"] * 100),
            "deviation_pct": od["deviation_pct"], "change_count": od["change_count"],
            "materials": [{"name": m["name"], "status": m["status"],
                           "shortage_days": m["shortage_days"]} for m in od["materials_status"]],
            "alert": {"id": alert.id, "level": alert.level, "score": alert.score,
                      "summary": alert.summary, "suggestion": alert.suggestion,
                      "reasons": [{"kind": r["kind"], "text": r["text"]} for r in alert.reasons]}
            if alert else None}


def _rule_reply(store: Store, msg: str) -> tuple[str, list] | None:
    """规则意图引擎：返回 (回复文本, 引用订单id列表)。无法识别返回 None。"""
    today = store.today()
    order = _find_order(store, msg)

    # 1) 针对具体订单的追问
    if order:
        brief = _order_brief(store, order, today)
        alert = brief["alert"]
        if any(k in msg for k in ("为什么", "原因", "预警", "风险", "红色", "橙色", "黄色")):
            if not alert:
                return ("%s 当前无预警（进度 %.0f%% / 计划 %.0f%%，距交期 %d 天，物料正常）。"
                        % (order.id, brief["progress_pct"], brief["expected_pct"],
                           brief["days_to_due"]), [order.id])
            lines = ["%s 当前为「%s」级预警（评分 %.0f）。" % (
                order.id, LEVEL_LABEL[alert["level"]], alert["score"]), "原因："]
            for r in alert["reasons"]:
                lines.append("· %s" % r["text"])
            lines.append("建议：%s" % alert["suggestion"])
            return "\n".join(lines), [order.id]
        if any(k in msg for k in ("进度", "状态", "怎么样", "情况")):
            steps = order.to_dict(today)["steps"]
            cur = next((s for s in steps if 0.001 < s["actual_progress"] < 0.999), None)
            lines = ["%s（%s × %d 台）：进度 %.0f%%，计划应达 %.0f%%，距交期 %d 天。"
                     % (order.id, order.product, order.qty, brief["progress_pct"],
                        brief["expected_pct"], brief["days_to_due"])]
            if cur:
                note = "（注意：%s）" % steps[-1].get("device_note") if any(
                    s.get("device_note") for s in steps) else ""
                lines.append("当前工序：%s，完成 %.0f%%%s" % (
                    cur["name"], cur["actual_progress"] * 100, note))
            for m in brief["materials"]:
                if m["shortage_days"] > 0:
                    lines.append("物料风险：%s 缺口 %d 天" % (m["name"], m["shortage_days"]))
            return "\n".join(lines), [order.id]

    # 2) 列表类问题
    if any(k in msg for k in ("红色", "严重", "最严重")):
        lv = "red"
    elif "橙色" in msg or "警告" in msg:
        lv = "orange"
    elif "黄色" in msg or "提醒" in msg:
        lv = "yellow"
    else:
        lv = None

    if lv or any(k in msg for k in ("预警", "风险", "延期", "交期", "物料", "缺口", "缺料")):
        if any(k in msg for k in ("物料", "缺料", "到货")):
            rows = []
            for o in store.orders.values():
                for m in o.materials:
                    if m.shortage_days > 0:
                        rows.append((o, m))
            if not rows:
                return "当前没有物料缺口订单。", []
            rows.sort(key=lambda r: -r[1].shortage_days)
            lines = ["共 %d 项物料缺口：" % len(rows)]
            for o, m in rows:
                lines.append("· %s %s：%s 缺口 %d 天（交期 %s）" % (
                    o.id, o.product, m.name, m.shortage_days, o.due_date.isoformat()))
            return "\n".join(lines), [o.id for o, _ in rows]
        alerts = [a for a in store.open_alerts() if not lv or a.level == lv]
        alerts.sort(key=lambda a: (LEVEL_ORDER[a.level], a.score), reverse=True)
        if not alerts:
            return "当前没有打开的预警。", []
        lines = ["当前 %d 条预警（按风险排序）：" % len(alerts)]
        for a in alerts[:6]:
            lines.append("· [%s] %s 评分 %.0f：%s" % (LEVEL_LABEL[a.level], a.order_id, a.score, a.summary))
        return "\n".join(lines), [a.order_id for a in alerts[:6]]

    if any(k in msg for k in ("帮助", "你能", "做什么", "怎么用")):
        return ("我是交期哨兵助手，可以：\n"
                "· 追问预警原因：如「MO-2609-01 为什么预警？」\n"
                "· 查订单进度：如「MO-2609-06 现在什么进度？」\n"
                "· 列风险清单：如「有哪些红色预警？」「受物料缺口影响的订单」", [])
    return None


_LLM_SYSTEM = (
    "你是制造业交期哨兵的智能助手。根据给定的结构化查询结果回答用户问题，"
    "用简体中文、简洁专业，事实必须来自给定数据，不得编造。不超过 8 行。"
)


def _reply_is_grounded(store: Store, reply: str) -> bool:
    """回复接地校验：提到的订单号必须真实存在（实测模型会虚构'MO-2610-04/05'这类枚举）。"""
    for oid in set(re.findall(r"MO-\d{4}-\d{2}", reply)):
        if oid not in store.orders:
            return False
    return True


def ask(store: Store, message: str) -> dict:
    # ① LLM 开启 → ReAct 自主查证（strict：模型拒绝查证则回落规则引擎）
    if LLM.enabled:
        from backend.agents.react import run_react
        try:
            r = run_react(store, COPILOT_SYSTEM, message, COPILOT_TOOLS,
                          max_steps=6, min_tools=1, strict_tools=True)
        except Exception:
            r = {"answer": None, "steps": []}
        if r["answer"] and _reply_is_grounded(store, r["answer"]):
            refs = sorted({m.upper().replace(" ", "-") for step in r["steps"]
                           for m in re.findall(r"MO[-\s]?\d{4}[-\s]?\d{2}", step.get("input", ""))})
            return {"reply": r["answer"], "refs": refs,
                    "via": "ReAct·%d步" % len(r["steps"]), "steps": r["steps"]}
        # 回答为空 / 拒绝查证 / 提到不存在的订单 → 落入规则引擎（确定性、可溯源）

    # ② 规则意图引擎（无 Key / LLM 失败时的确定性兜底）
    rule_reply = _rule_reply(store, message)
    refs: list[str] = []
    if rule_reply:
        text, refs = rule_reply
        ctx = None
    else:
        # 兜底：全局摘要交给 LLM（或模板摘要）
        today = store.today()
        rows = sorted(store.orders.values(),
                      key=lambda o: (LEVEL_ORDER.get(
                          (store.open_alert_for(o.id).level if store.open_alert_for(o.id) else "green"), 0),
                          -(o.to_dict(today)["deviation_pct"])))
        ctx = {"orders": [_order_brief(store, o, today) for o in rows[:8]]}
        text = None

    if LLM.enabled:
        import json as _json
        user = "用户问题：%s\n参考数据：%s" % (
            message, _json.dumps(ctx or rule_reply, ensure_ascii=False, default=str))
        reply = llm_provider.chat(_LLM_SYSTEM, user)
        if reply:
            return {"reply": reply.strip(), "refs": refs, "via": "llm"}

    if text is None:
        od = ctx["orders"][0] if ctx and ctx["orders"] else None
        text = ("当前最需要关注：%s（%s，评分见预警中心）。您也可以问：「有哪些红色预警？」"
                % ("%s %s" % (od["id"], od["product"]), LEVEL_LABEL[od["alert"]["level"]]
                   if od and od["alert"] else "无预警")) if od else "暂无数据。"
    return {"reply": text, "refs": refs, "via": "规则引擎"}
