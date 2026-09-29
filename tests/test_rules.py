"""L1 规则引擎单测：阈值边界、信号结构、证据链完整性。"""
from datetime import date, timedelta

from backend.domain import Material, Order, ProcessStep
from backend.engine.rules import SIGNAL_DEV, SIGNAL_MAT, SIGNAL_OVERDUE, rule_level, scan_order

T = date.today()


def mk_order(progress, due_off, materials=(), steps=5, start_off=-10):
    """构造测试订单：due/start 相对今天，保证确定性。"""
    start, due = T + timedelta(days=start_off), T + timedelta(days=due_off)
    done_full = int(progress * steps)
    sts = []
    for i in range(steps):
        ap = 1.0 if i < done_full else (round(progress * steps - done_full, 3)
                                        if i == done_full and progress * steps > done_full else 0.0)
        sts.append(ProcessStep(seq=i + 1, name="OP%d" % (i + 1), workshop="W",
                               planned_start=start + timedelta(days=i * (due - start).days // steps),
                               planned_end=start + timedelta(days=(i + 1) * (due - start).days // steps),
                               actual_progress=ap))
    mats = [Material(name=n, spec="SP", qty_text="1", required_date=T + timedelta(days=rd),
                     eta=T + timedelta(days=et), arrived=arr) for (n, rd, et, arr) in materials]
    return Order(id="MO-T-01", customer="C", product="P", qty=100, priority="常规", workshop="W",
                 start_date=start, due_date=due, steps=sts, materials=mats)


def sig_of(order):
    return {s["rule"]: s for s in scan_order(order, T)}


def test_healthy_order_has_no_signals():
    """进度正常 + 物料齐套 → 无信号（绿灯）。"""
    o = mk_order(progress=0.5, due_off=10)  # 刚好赶上计划
    assert scan_order(o, T) == []


def test_overdue_is_red_with_evidence():
    o = mk_order(progress=0.85, due_off=-2)
    sigs = sig_of(o)
    assert sigs[SIGNAL_OVERDUE]["level"] == "red"
    assert "超过交期" in sigs[SIGNAL_OVERDUE]["text"]
    assert sigs[SIGNAL_OVERDUE]["evidence"][0]["source"] == "ERP 订单"


def test_deviation_thresholds():
    """偏差比例 0.4→红 / 0.24→橙 / 0.14→黄 / 0.04→无。"""
    # expected=0.5；progress 0.3 → ratio 0.4 红
    assert sig_of(mk_order(0.30, 10))[SIGNAL_DEV]["level"] == "red"
    # progress 0.38 → ratio 0.24 橙
    assert sig_of(mk_order(0.38, 10))[SIGNAL_DEV]["level"] == "orange"
    # progress 0.43 → ratio 0.14 黄
    assert sig_of(mk_order(0.43, 10))[SIGNAL_DEV]["level"] == "yellow"
    # progress 0.48 → ratio 0.04 无信号
    assert SIGNAL_DEV not in sig_of(mk_order(0.48, 10))


def test_material_shortage_levels():
    """缺口 10 天→橙 / 2 天→黄 / 提前到货→无。"""
    assert sig_of(mk_order(0.5, 10, [("钣金件", 2, 12, False)]))[SIGNAL_MAT]["level"] == "orange"
    assert sig_of(mk_order(0.5, 10, [("钣金件", 2, 4, False)]))[SIGNAL_MAT]["level"] == "yellow"
    assert SIGNAL_MAT not in sig_of(mk_order(0.5, 10, [("钣金件", 2, 1, False)]))
    # 已到货不算缺口
    assert SIGNAL_MAT not in sig_of(mk_order(0.5, 10, [("钣金件", 2, 99, True)]))


def test_rule_level_takes_worst():
    o = mk_order(0.2, -1, [("x", 2, 20, False)])
    sigs = scan_order(o, T)
    assert rule_level(sigs) == "red"  # 超期 + 偏差，取最差


def test_evidence_chain_is_complete():
    """每条信号必须有 text 和至少一条 evidence（可解释性要求）。"""
    o = mk_order(0.2, -1, [("y", 1, 9, False)])
    for s in scan_order(o, T):
        assert s["text"] and len(s["evidence"]) >= 1
        for e in s["evidence"]:
            assert e["source"] and e["detail"]
