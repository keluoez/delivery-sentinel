"""事件模拟器：模拟 ERP/MES/邮件/微信 的实时事件流，注入 pending inbox，
由 LangGraph 工作流的「事件接入/状态更新」节点消费——形成感知-决策-执行闭环的演示。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from backend import seed_data
from backend.domain import Event
from backend.store import Store


def inject(store: Store, scenario: str, order_id: str | None = None) -> dict:
    """按场景注入一条新事件（仅入 inbox，效果由工作流的状态更新节点落地）。"""
    if scenario not in seed_data.SCENARIOS:
        raise ValueError("未知场景: %s" % scenario)
    orders = store.orders
    if order_id and order_id in orders:
        order = orders[order_id]
    else:
        # 未指定订单：选一个"最有戏剧性"的——未到货物料缺口最大的未完成订单
        alive = [o for o in orders.values() if o.progress < 0.999]
        order = max(alive, key=lambda o: o.to_dict()["max_shortage_days"])

    now = datetime.now().isoformat(timespec="seconds")
    if scenario == "supplier_delay":
        m = next((x for x in order.materials if not x.arrived), None)
        if m is None:
            m = order.materials[0] if order.materials else None
        if m is None:
            content = "供应商例行沟通：暂无物料波动。"
            payload = {}
        else:
            content = ("【%s】贵司 %s 因上游原材料紧张，承诺到货需再推迟 3 天，敬请谅解。"
                       % (m.spec, m.name))
            payload = {"material": m.name, "eta_shift_days": 3}
        ev = Event(id=store.next_event_id(), ts=now, order_id=order.id, type="供应商反馈",
                   source="邮件", content=content, payload=payload)
    elif scenario == "order_change":
        delta = max(10, int(order.qty * 0.10))
        ev = Event(id=store.next_event_id(), ts=now, order_id=order.id, type="订单变更",
                   source="邮件",
                   content="客户 %s 来函：订单追加 %d 台（%d→%d），交期不变。"
                           % (order.customer, delta, order.qty, order.qty + delta),
                   payload={"qty_delta": delta})
    elif scenario == "machine_down":
        step = next((s for s in order.steps if s.actual_progress < 0.999), order.steps[0])
        ev = Event(id=store.next_event_id(), ts=now, order_id=order.id, type="设备事件",
                   source="传感器",
                   content="%s %s 设备主轴报警，停机检修，预计 4 小时恢复。" % (
                       order.workshop, step.name),
                   payload={"step": step.name, "downtime_hours": 4})
    elif scenario == "normal_report":
        step = next((s for s in order.steps if 0.001 < s.actual_progress < 0.999),
                    next((s for s in order.steps if s.actual_progress < 0.999), order.steps[0]))
        ev = Event(id=store.next_event_id(), ts=now, order_id=order.id, type="报工",
                   source="MES",
                   content="%s：本班完成 %d 件，进度推进 6%%。" % (step.name, max(1, int(order.qty * 0.06))),
                   payload={"step": step.name, "delta": 0.06})
    else:
        raise ValueError(scenario)

    store.add_event(ev)
    return {"event": ev.to_dict(), "order_id": order.id,
            "scenario": seed_data.SCENARIOS[scenario]}
