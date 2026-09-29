"""工作流集成测试：LangGraph 全流程 + 去重抑制 + 预警升级 + 事件落地效果。"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from datetime import timedelta

from backend.agents.graph import build_graph, run_sync
from backend.engine.rules import SIGNAL_MAT
from backend.simulator import inject
from backend.store import Store


def fresh_run(store, rid):
    rec = {"id": rid, "status": "running", "started_at": "", "finished_at": "",
           "nodes": [], "stats": None, "error": None}
    return run_sync(store, rec, compiled=build_graph(store))


def test_first_run_produces_alerts_with_full_pipeline():
    store = Store(persist=False)
    rec = fresh_run(store, "R-001")
    assert rec["status"] == "done", rec.get("error")
    # 10 个节点全部执行（首跑必有红/橙预警，走完整链路）
    assert {n["node"] for n in rec["nodes"]} == {
        "ingest", "update", "rule_scan", "risk_score", "dedup",
        "attribute", "investigate", "draft", "gate", "finalize"}
    st = rec["stats"]
    assert st["scanned"] == 12 and st["new_alerts"] >= 8
    assert st["reports"] >= 2  # 红/橙预警都应带调查报告
    assert st["engine"] == "模板引擎"  # 测试环境无 LLM Key
    # 预警落库且带证据链
    alerts = list(store.alerts.values())
    assert len(alerts) == st["new_alerts"]
    for a in alerts:
        assert a.reasons and a.draft["text"] and a.suggestion
        assert a.status == "待处理"  # 人工闸门：不自动发送


def test_second_run_same_level_is_suppressed():
    """同级抑制：第二跑无新事件 → 0 新预警（防预警疲劳）。"""
    store = Store(persist=False)
    fresh_run(store, "R-001")
    n1 = len(store.alerts)
    rec2 = fresh_run(store, "R-002")
    assert rec2["stats"]["new_alerts"] == 0
    assert rec2["stats"]["skipped"] >= 8
    assert len(store.alerts) == n1
    # 条件路由：本轮没有 candidates，attribute/draft/gate 应被跳过
    executed = {n["node"] for n in rec2["nodes"]}
    assert "dedup" in executed and "gate" not in executed


def test_supplier_delay_event_updates_material_and_escalates():
    """注入供应商延期 → 状态更新节点落地 → 黄色预警升级为橙色（带供应商证据）。"""
    store = Store(persist=False)
    fresh_run(store, "R-001")
    base = store.open_alert_for("MO-2609-07")   # 种子里为黄色（锻件缺口 3 天）
    assert base is not None and base.level == "yellow"

    r = inject(store, "supplier_delay", "MO-2609-07")
    assert r["order_id"] == "MO-2609-07"
    rec = fresh_run(store, "R-002")
    assert rec["status"] == "done"
    assert rec["stats"]["upgrades"] == 1

    # 物料 eta 被「状态更新」节点真实推移 3 天：3 → 6 天，触发橙色规则
    m = store.orders["MO-2609-07"].materials[0]
    assert m.shortage_days == 6 and m.change_count == 1
    new_alert = store.open_alert_for("MO-2609-07")
    assert new_alert.id != base.id and new_alert.level == "orange"
    assert base.status == "已升级"
    kinds = {rr["kind"] for rr in new_alert.reasons}
    assert any("物料" in k for k in kinds)
    assert any(e["source"] == "供应商反馈" for rr in new_alert.reasons for e in rr["evidence"])


def test_event_landing_changes_progress_and_qty():
    """报工/改单事件在「状态更新」节点真实改变领域状态。"""
    store = Store(persist=False)
    before_qty = store.orders["MO-2609-03"].qty
    before_prog = store.orders["MO-2609-03"].progress
    inject(store, "normal_report", "MO-2609-03")
    inject(store, "order_change", "MO-2609-03")
    fresh_run(store, "R-001")
    o = store.orders["MO-2609-03"]
    assert o.qty == before_qty + max(10, int(before_qty * 0.10))
    assert o.change_count == 1
    assert o.progress > before_prog


def test_events_marked_consumed_and_pending_empty():
    store = Store(persist=False)
    inject(store, "machine_down", "MO-2609-09")
    assert len(store.pending_events()) == 1
    fresh_run(store, "R-001")
    assert len(store.pending_events()) == 0
    # 设备事件写到工序备注（归因素材）
    o = store.orders["MO-2609-09"]
    assert any(s.device_note for s in o.steps)


def test_overdue_order_becomes_red():
    store = Store(persist=False)
    fresh_run(store, "R-001")
    a = store.open_alert_for("MO-2609-06")  # 种子里的超期单
    assert a is not None and a.level == "red"
    assert any("超过交期" in r["text"] for r in a.reasons)
