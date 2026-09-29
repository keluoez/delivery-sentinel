"""自研 ReAct 引擎 + 领域工具集。

设计取舍：
- 不依赖 langchain-openai 的模型绑定——文本协议 ReAct（Thought/Action/Observation），
  任何 OpenAI 兼容端点（GLM/Qwen/DeepSeek…）都能跑；
- 每一步工具调用都记录到 steps，前端可展示"它自己查了什么"；
- LLM 不可用或解析失败 → 返回 answer=None，调用方回落确定性路径。
"""
from __future__ import annotations

import json
import logging
import re

from backend.config import LLM
from backend.llm import provider as llm_provider
from backend.store import Store


# ──────────────────────────── 工具集 ────────────────────────────
def build_tools(store: Store) -> dict:
    """只读领域工具。fn(input:str) -> str（紧凑 JSON/文本，作为 Observation 喂回模型）。"""

    def query_order(inp: str) -> str:
        o = store.orders.get(inp.strip().upper())
        if not o:
            return json.dumps({"error": "订单不存在", "hint": "先用 list_alerts 查看现有订单号"},
                              ensure_ascii=False)
        a = store.open_alert_for(o.id)
        return json.dumps({**o.to_dict(store.today()),
                           "open_alert": {"level": a.level, "score": a.score,
                                          "summary": a.summary} if a else None},
                          ensure_ascii=False)

    def get_alert(inp: str) -> str:
        a = store.open_alert_for(inp.strip().upper())
        if not a:
            return json.dumps({"note": "该订单当前无预警"}, ensure_ascii=False)
        return json.dumps(a.to_dict(), ensure_ascii=False)

    def get_events(inp: str) -> str:
        oid = inp.strip().upper()
        evs = [e.to_dict() for e in store.events if e.order_id == oid]
        evs.sort(key=lambda e: e["ts"], reverse=True)
        return json.dumps(evs[:8], ensure_ascii=False)

    def list_same_product(inp: str) -> str:
        oid = inp.strip().upper()
        o = store.orders.get(oid)
        if not o:
            return json.dumps({"error": "订单不存在"}, ensure_ascii=False)
        rows = []
        for x in store.orders.values():
            if x.id != oid and (x.product == o.product or x.customer == o.customer):
                a = store.open_alert_for(x.id)
                od = x.to_dict(store.today())
                rows.append({"order_id": x.id, "product": x.product, "customer": x.customer,
                             "progress_pct": round(od["progress"] * 100),
                             "expected_pct": round(od["expected_progress"] * 100),
                             "days_to_due": od["days_to_due"], "level": a.level if a else "green"})
        return json.dumps(rows, ensure_ascii=False)

    def list_alerts(inp: str) -> str:
        lv = inp.strip().lower()
        alerts = store.open_alerts()
        if lv in ("red", "orange", "yellow"):
            alerts = [a for a in alerts if a.level == lv]
        alerts.sort(key=lambda a: a.score, reverse=True)
        return json.dumps([{"order_id": a.order_id, "level": a.level, "score": a.score,
                            "summary": a.summary} for a in alerts[:8]], ensure_ascii=False)

    def list_material_shortages(inp: str) -> str:
        rows = []
        for o in store.orders.values():
            for m in o.materials:
                if m.shortage_days > 0:
                    rows.append({"order_id": o.id, "material": m.name,
                                 "shortage_days": m.shortage_days, "eta": m.eta.isoformat()})
        rows.sort(key=lambda r: -r["shortage_days"])
        return json.dumps(rows, ensure_ascii=False)

    return {
        "query_order": {"desc": "查单个订单的完整状态（进度/交期/物料/当前预警）", "fn": query_order},
        "get_alert": {"desc": "查订单当前预警的完整内容（含证据链）", "fn": get_alert},
        "get_events": {"desc": "查订单最近的事件时间线", "fn": get_events},
        "list_same_product": {"desc": "查同产品/同客户的其他在制订单作对照", "fn": list_same_product},
        "list_alerts": {"desc": "按等级列当前预警（输入 red/orange/yellow 或空）", "fn": list_alerts},
        "list_material_shortages": {"desc": "列出全部物料缺口", "fn": list_material_shortages},
    }


REACT_TMPL = """{system}

你可以使用以下工具：
{tools}

回答格式（严格遵守，每次只走一步）：
Thought: <你的推理，一句话>
Action: <工具名>
Action Input: <参数>
Observation: <工具结果，由系统填入，你不要自己编>
（可以重复多轮 Thought/Action，直到信息足够）
Final Answer: <最终回答>

开始：
{history}"""


def run_react(store: Store, system: str, question: str, tool_names: tuple[str, ...],
              max_steps: int = 6, min_tools: int = 0, strict_tools: bool = False) -> dict:
    """跑一轮 ReAct。LLM 不可用 → answer=None（调用方回落确定性路径）。

    min_tools + strict_tools：要求至少 N 次工具调用才接受 Final Answer。
    strict=True 时，模型被纠错后仍不查工具 → 返回 answer=None（调用方回落确定性路径），
    绝不接受未经查证的回答（实测 glm 会编造"X-12345 短缺"这类不存在的实体）。
    """
    tools = build_tools(store)
    chosen = {k: v for k, v in tools.items() if k in tool_names}
    tools_txt = "\n".join("- %s: %s" % (k, v["desc"]) for k, v in chosen.items())
    history = "Question: %s" % question
    steps: list[dict] = []
    nudged = False

    for _ in range(max_steps):
        prompt = REACT_TMPL.format(system=system, tools=tools_txt, history=history)
        out = llm_provider.chat("你是严谨的制造业供应链分析引擎。", prompt)
        if out is None:
            return {"answer": None, "steps": steps, "via": "react"}
        if "Final Answer:" in out and (len(steps) >= min_tools or nudged):
            if len(steps) < min_tools and strict_tools:
                log = logging.getLogger("sentinel.react")
                log.warning("模型拒绝使用工具（0 步作答），按 strict 模式拒绝其回答：%s",
                            out[:80])
                return {"answer": None, "steps": steps, "via": "react"}
            answer = out.split("Final Answer:")[-1].strip()
            return {"answer": answer or "(模型未给出内容)", "steps": steps, "via": "react"}
        if "Final Answer:" in out and len(steps) < min_tools:
            # 没查就想交卷：退回，要求先调用工具
            history += ("\n%s\n[系统] 你还没有使用任何工具，不允许凭空回答。"
                        "请先输出 Thought/Action/Action Input 查询数据。" % out.strip())
            nudged = True
            continue
        m_tool = re.search(r"Action:\s*([^\s\n]+)", out)
        m_in = re.search(r"Action Input:\s*(.+)", out)
        tool = m_tool.group(1).strip() if m_tool else ""
        inp = m_in.group(1).strip().strip('"') if m_in else ""
        if tool not in chosen:
            history += "\n%s\nObservation: [系统] 无效工具 %s，可用工具：%s" % (
                out.strip(), tool, ", ".join(chosen))
            continue
        try:
            obs = chosen[tool]["fn"](inp)
        except Exception as exc:
            obs = json.dumps({"error": str(exc)}, ensure_ascii=False)
        steps.append({"tool": tool, "input": inp, "summary": obs[:80]})
        history += "\n%s\nObservation: %s" % (out.strip(), obs)

    # 步数用尽：强制收束
    out = llm_provider.chat("你是严谨的制造业供应链分析引擎。",
                            "%s\n\n已达到工具调用上限。请基于以上 Observations 直接给出 Final Answer。" % history)
    if len(steps) < min_tools and strict_tools:
        return {"answer": None, "steps": steps, "via": "react"}
    answer = out.split("Final Answer:")[-1].strip() if (out and "Final Answer:" in out) else (out or "")
    return {"answer": answer or None, "steps": steps, "via": "react"}
