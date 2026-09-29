"""ReAct 引擎与预警调查员测试（LLM 用脚本桩模拟，验证工具真的被执行）。"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

import pytest

from backend.agents import react
from backend.agents.graph import build_graph, run_sync
from backend.agents.investigator import investigate
from backend.agents.react import build_tools, run_react
from backend.store import Store


def run_graph(store, rid="R"):
    rec = {"id": rid, "status": "running", "started_at": "", "finished_at": "",
           "nodes": [], "stats": None, "error": None}
    return run_sync(store, rec, compiled=build_graph(store))


# ─────────────────────────── 工具集 ───────────────────────────
def test_tools_return_real_data():
    store = Store(persist=False)
    t = build_tools(store)
    o = t["query_order"]["fn"]("MO-2609-01")
    assert "MO-2609-01" in o and "progress" in o
    assert "订单不存在" in t["query_order"]["fn"]("MO-XXXX-XX")
    shortages = t["list_material_shortages"]["fn"]("")
    assert "连接器" in shortages or "PCB" in shortages


# ─────────────────────────── ReAct 引擎 ───────────────────────────
class ScriptedLLM:
    """脚本桩：按序返回预设回复，模拟一次"查订单 → 给结论"的 ReAct 过程。"""

    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def __call__(self, system, prompt):
        self.calls += 1
        return self.script[min(self.calls, len(self.script)) - 1]


@pytest.fixture()
def fake_llm(monkeypatch):
    from backend.llm import provider
    p = provider.__dict__
    def _install(script):
        llm = ScriptedLLM(script)
        monkeypatch.setattr(react.llm_provider, "chat", llm)
        return llm
    return _install


def test_react_executes_tool_and_answers(fake_llm):
    fake_llm([
        "Thought: 需要先查订单\nAction: query_order\nAction Input: MO-2609-01",
        "Thought: 信息足够\nFinal Answer: MO-2609-01 进度 52%，落后计划，建议催料。",
    ])
    store = Store(persist=False)
    r = run_react(store, "sys", "MO-2609-01 情况如何", ("query_order",))
    assert r["answer"] and "建议催料" in r["answer"]
    assert len(r["steps"]) == 1 and r["steps"][0]["tool"] == "query_order"


def test_react_invalid_tool_gets_feedback_and_recovers(fake_llm):
    fake_llm([
        "Thought: 查一下\nAction: 不存在的工具\nAction Input: x",
        "Thought: 换正确工具\nAction: list_alerts\nAction Input: red",
        "Final Answer: 共 3 条红色预警。",
    ])
    store = Store(persist=False)
    r = run_react(store, "sys", "有哪些红警", ("list_alerts",))
    assert r["answer"] and r["steps"][0]["tool"] == "list_alerts"


def test_react_max_steps_forces_final(fake_llm):
    fake_llm(["Thought: 再查\nAction: query_order\nAction Input: MO-2609-01"] * 9)
    store = Store(persist=False)
    r = run_react(store, "sys", "问题", ("query_order",), max_steps=3)
    assert r["answer"]  # 强制收束给出回答


def test_react_without_llm_returns_none():
    store = Store(persist=False)  # 测试环境无 Key
    r = run_react(store, "sys", "问题", ("query_order",))
    assert r["answer"] is None and r["via"] == "react"


# ─────────────────────────── 预警调查员 ───────────────────────────
def test_investigator_template_report_structure():
    store = Store(persist=False)
    r = investigate(store, "MO-2609-12", "red", "进度落后且物料缺口", [], score=67.4)
    assert "模板引擎" in r["report"]
    for sec in ("现状", "时间线", "同类对照", "定性", "建议"):
        assert sec in r["report"]
    assert "67" in r["report"]  # 评分必须出现（来自引擎，不是编的）


def test_graph_investigate_node_runs_and_attaches_reports():
    store = Store(persist=False)
    rec = run_graph(store, "R-001")
    assert rec["status"] == "done"
    nodes = [n["node"] for n in rec["nodes"]]
    assert "investigate" in nodes and nodes.index("attribute") < nodes.index("investigate")
    reds = [a for a in store.alerts.values() if a.level in ("red", "orange")]
    assert reds and all(a.report for a in reds)
    yellows = [a for a in store.alerts.values() if a.level == "yellow"]
    assert yellows and all(not a.report for a in yellows)  # 黄灯不触发调查
    assert rec["stats"]["reports"] == len(reds)


def test_investigator_uses_llm_path_when_available(fake_llm, monkeypatch):
    fake_llm([
        "Thought: 查现状\nAction: query_order\nAction Input: MO-2609-12",
        "Thought: 查时间线\nAction: get_events\nAction Input: MO-2609-12",
        "Final Answer: ■ 现状：严重落后\n■ 定性：供应商反复延期，建议立即换供应商。",
    ])
    monkeypatch.setattr("backend.agents.investigator.LLM", type(
        "X", (), {"enabled": True, "model": "glm-4.5-air"}))
    store = Store(persist=False)
    r = investigate(store, "MO-2609-12", "red", "落后", [], score=60)
    assert "glm-4.5-air" in r["report"] and "供应商" in r["report"]
    assert len(r["investigation"]["steps"]) == 2
