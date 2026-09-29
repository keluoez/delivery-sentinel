"""L3 归因层：把结构化信号翻译成"风险解释 + 建议动作 + 催办草稿"。

双路径：
- template_*：确定性模板引擎（零依赖、可单测、制造业内网可用的兜底）
- attribute_via_llm：配置了 LLM Provider 时走生成式归因，输出 JSON；失败自动回落模板

所有路径产出的 reasons 均保留 L1/L2 的原始证据（来源系统 + 明细），保证可解释、可追溯。
"""
from __future__ import annotations

from datetime import date

from backend.config import LLM
from backend.domain import LEVEL_LABEL, Order
from backend.engine.rules import SIGNAL_DEV, SIGNAL_MAT, SIGNAL_OVERDUE
from backend.llm import provider as llm_provider

_KIND_LABEL = {SIGNAL_OVERDUE: "交期超限", SIGNAL_DEV: "进度偏差", SIGNAL_MAT: "物料缺口",
               "score": "综合评分"}
_PART_LABEL = {"deviation": "进度偏差", "material": "物料", "urgency": "交期紧迫",
               "volatility": "订单波动"}


def build_reasons(order: Order, signals: list[dict], score: dict) -> list[dict]:
    """把信号 + 评分因子组合成归因链（每条含证据）。"""
    reasons = []
    for s in signals:
        reasons.append({"kind": _KIND_LABEL.get(s["rule"], s["rule"]),
                        "text": s["text"], "evidence": s["evidence"]})
    # 评分视角补充一句（说明为什么分级是这个颜色：多因子叠加）
    top_key, top_part = max(score["parts"].items(), key=lambda kv: kv[1]["value"] * kv[1]["weight"])
    reasons.append({
        "kind": "综合评分",
        "text": "四因子加权评分 %.1f 分（%s 为主要贡献：%.0f 分），叠加判定为「%s」"
                % (score["score"], _PART_LABEL.get(top_key, top_key),
                   top_part["value"] * top_part["weight"] * 100, LEVEL_LABEL[score["level"]]),
        "evidence": [{"source": "评分引擎", "detail": "%s：%s" % (_PART_LABEL.get(k, k), part["raw"])}
                     for k, part in
                     sorted(score["parts"].items(), key=lambda kv: -kv[1]["value"] * kv[1]["weight"])],
    })
    return reasons


def template_summary(order: Order, today: date) -> str:
    parts = []
    od = order.to_dict(today)
    if od["overdue"]:
        parts.append("订单已超期 %d 天" % (-od["days_to_due"]))
    elif od["deviation_pct"] > 0:
        parts.append("进度落后计划 %.0f 个百分点" % od["deviation_pct"])
    if od["max_shortage_days"] > 0:
        worst = max(order.materials, key=lambda m: m.shortage_days)
        parts.append("%s 缺口 %d 天" % (worst.name, worst.shortage_days))
    if order.change_count:
        parts.append("近期 %d 次订单变更" % order.change_count)
    if not parts:
        parts.append("多项风险因子叠加")
    return "%s：%s，需立即关注。" % (order.id, "，".join(parts))


def template_suggestion(order: Order, signals: list[dict], today: date) -> str:
    """按主导风险给建议动作（跟单员可直接执行）。"""
    kinds = {s["rule"] for s in signals}
    acts = []
    if order.overdue or SIGNAL_OVERDUE in kinds:
        acts.append("与客户沟通延期方案并同步违约风险评估")
    if SIGNAL_MAT in kinds:
        acts.append("采购部今日跟进缺口物料到货，必要时启动备选供应商")
    if SIGNAL_DEV in kinds or order.overdue:
        acts.append("车间评估加班/增开班次追赶计划，明天早会同步追赶方案")
    if order.change_count >= 2:
        acts.append("与销售确认变更冻结，避免进度反复")
    if not acts:
        acts.append("保持日度监控")
    return "；".join(acts) + "。"


_RECIPIENT = {
    SIGNAL_MAT: ("企微·采购协调群", "采购部"),
    SIGNAL_OVERDUE: ("企微·生产协调群", "生产经理"),
    SIGNAL_DEV: ("企微·生产协调群", "车间主任/计划员"),
}


def build_draft(order: Order, level: str, signals: list[dict], today: date) -> dict:
    """AI 起草的催办消息（发送前需人工确认——产品原则）。"""
    od = order.to_dict(today)
    if any(s["rule"] == SIGNAL_MAT for s in signals):
        key = SIGNAL_MAT
    elif order.overdue or any(s["rule"] == SIGNAL_OVERDUE for s in signals):
        key = SIGNAL_OVERDUE
    else:
        key = SIGNAL_DEV
    channel, to = _RECIPIENT[key]
    due_txt = "%s（剩 %d 天）" % (od["due_date"], od["days_to_due"]) if od["days_to_due"] >= 0 \
        else "%s（已超期 %d 天）" % (od["due_date"], -od["days_to_due"])
    lines = ["【交期预警-%s】%s %s × %d 台" % (LEVEL_LABEL[level], order.id, order.product, order.qty),
             "交期：%s，当前进度 %.0f%%（计划应达 %.0f%%）" % (due_txt, od["progress"] * 100,
                                                            od["expected_progress"] * 100)]
    for s in signals:
        lines.append("· %s" % s["text"])
    lines.append("请 %s 今日跟进，明早会同步处理结果。" % to)
    return {"channel": channel, "to": to, "text": "\n".join(lines)}


_LLM_SYSTEM = (
    "你是制造业交期风险管理专家。根据给定的订单结构化信号，输出严格 JSON："
    '{"summary": "一句话风险解释(<=80字，点出主要矛盾)", '
    '"suggestion": "给跟单员的1-2条可执行建议(<=60字)", '
    '"draft_to": "消息接收方如 采购部/车间主任/生产经理", '
    '"draft_text": "可直接发到企业微信群的催办消息(<=120字，含订单号/交期/关键风险/明确请求)"}'
    "。只输出 JSON，不要多余文字。所有事实必须来自输入信号，不得编造数据。"
)


def attribute_via_llm(order: Order, signals: list[dict], score: dict, today: date) -> dict | None:
    """LLM 归因；失败返回 None（上层回落模板）。json_mode 保证可解析。"""
    od = order.to_dict(today)
    payload = {
        "order": {"id": order.id, "customer": order.customer, "product": order.product,
                  "qty": order.qty, "priority": order.priority,
                  "due_date": od["due_date"], "days_to_due": od["days_to_due"],
                  "progress_pct": round(od["progress"] * 100),
                  "expected_pct": round(od["expected_progress"] * 100)},
        "signals": [{k: s[k] for k in ("rule", "level", "text", "evidence")} for s in signals],
        "score": score["score"],
    }
    import json as _json
    result = llm_provider.chat_json(_LLM_SYSTEM, _json.dumps(payload, ensure_ascii=False))
    if not result or not all(k in result for k in ("summary", "suggestion", "draft_text")):
        return None
    return {
        "summary": str(result["summary"])[:120],
        "suggestion": str(result["suggestion"])[:120],
        "draft": {"channel": "企微·生产协调群",
                  "to": str(result.get("draft_to", "生产协调群"))[:30],
                  "text": str(result["draft_text"])[:300]},
        "via": "llm",
    }
