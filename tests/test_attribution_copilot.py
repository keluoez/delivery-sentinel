"""L3 归因（模板引擎）与智能助手（规则意图）单测。"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from backend.agents.attribution import build_draft, build_reasons, template_suggestion, template_summary
from backend.agents.copilot import ask
from backend.engine.rules import scan_order
from backend.engine.scoring import score_order
from backend.store import Store


def _prepared(store, oid="MO-2609-01"):
    order = store.orders[oid]
    today = store.today()
    signals = scan_order(order, today)
    score = score_order(order, today)
    return order, signals, score, today


def test_build_reasons_contains_score_viewpoint():
    store = Store(persist=False)
    order, signals, score, today = _prepared(store)
    reasons = build_reasons(order, signals, score)
    # 每个规则信号一条 + 综合评分视角一条
    assert len(reasons) == len(signals) + 1
    assert reasons[-1]["kind"] == "综合评分"
    assert all(r["evidence"] for r in reasons)


def test_template_summary_mentions_key_facts():
    store = Store(persist=False)
    order, signals, score, today = _prepared(store, "MO-2609-12")  # 偏差 + PCB 缺口
    s = template_summary(order, today)
    assert order.id in s and "缺口" in s and "落后" in s


def test_suggestion_targets_material_team_when_material_risk():
    store = Store(persist=False)
    order, signals, score, today = _prepared(store, "MO-2609-12")
    sug = template_suggestion(order, signals, today)
    assert "采购" in sug  # 物料缺口主导 → 建议必须点到采购


def test_draft_has_recipient_and_order_ref():
    store = Store(persist=False)
    order, signals, score, today = _prepared(store, "MO-2609-12")
    d = build_draft(order, "red", signals, today)
    assert "采购" in d["to"] and order.id in d["text"] and "交期" in d["text"]
    assert d["channel"].startswith("企微")


def test_copilot_why_question_returns_reasons():
    store = Store(persist=False)
    r = ask(store, "MO-2609-01 为什么预警？")
    assert "MO-2609-01" in r["reply"] and r["refs"] == ["MO-2609-01"]
    assert r["via"] == "规则引擎"


def test_copilot_why_question_with_existing_alert_regression():
    """回归：预警已存在时，原因必须完整列出（曾因 dict/对象混用崩溃）。"""
    from backend.agents.graph import build_graph, run_sync
    store = Store(persist=False)
    run_sync(store, {"id": "R-001", "status": "running", "started_at": "", "finished_at": "",
                     "nodes": [], "stats": None, "error": None}, compiled=build_graph(store))
    r = ask(store, "MO-2609-01 为什么预警？")
    assert "原因" in r["reply"] and "建议" in r["reply"]
    assert "进度落后" in r["reply"] or "物料缺口" in r["reply"]
    assert r["refs"] == ["MO-2609-01"]


def test_copilot_progress_question():
    store = Store(persist=False)
    r = ask(store, "MO-2609-06 现在什么进度？")
    assert "MO-2609-06" in r["reply"] and "进度" in r["reply"]


def test_copilot_red_list_and_material_list():
    from backend.agents.graph import build_graph, run_sync
    store = Store(persist=False)
    run_sync(store, {"id": "R-001", "status": "running", "started_at": "", "finished_at": "",
                     "nodes": [], "stats": None, "error": None}, compiled=build_graph(store))
    r1 = ask(store, "有哪些红色预警？")
    assert "MO-2609-" in r1["reply"]
    r2 = ask(store, "受物料缺口影响的订单")
    assert "缺口" in r2["reply"]


def test_copilot_help_and_fallback_never_crash():
    store = Store(persist=False)
    assert "为什么" in ask(store, "帮助")["reply"]
    r = ask(store, "今天天气怎么样")  # 兜底路径
    assert r["reply"]


def test_copilot_order_alias_by_number():
    store = Store(persist=False)
    r = ask(store, "3号订单现在什么进度？")
    assert "MO-2609-03" in r["reply"]
