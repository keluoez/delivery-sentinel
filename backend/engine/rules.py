"""L1 规则引擎：超期 / 进度偏差 / 物料缺口 → 结构化信号（含证据链）。

纯函数、可单测。每条信号自带证据（来源系统 + 明细），供 L3 归因引用。
"""
from __future__ import annotations

from datetime import date

from backend.config import RISK
from backend.domain import Order

SIGNAL_OVERDUE = "OVERDUE"
SIGNAL_DEV = "DEV"
SIGNAL_MAT = "MAT"


def scan_order(order: Order, today: date) -> list[dict]:
    """对单个订单跑全部规则，返回信号列表（可能为空 = 绿灯）。"""
    signals: list[dict] = []
    progress = order.progress
    expected = order.expected_progress(today)

    # 规则1：超期（最硬的红线）
    if order.overdue:
        signals.append({
            "order_id": order.id,
            "rule": SIGNAL_OVERDUE,
            "level": "red",
            "text": "订单已超过交期 %d 天，仍未完工（进度 %.0f%%）" % (
                (today - order.due_date).days, progress * 100),
            "evidence": [{
                "source": "ERP 订单",
                "detail": "交期 %s，当前进度 %.0f%%" % (order.due_date.isoformat(), progress * 100),
            }],
        })

    # 规则2：进度偏差（计划应完成 vs 实际完成）
    if expected >= 0.02:
        dev_ratio = (expected - progress) / expected
        if dev_ratio >= RISK.dev_yellow:
            if dev_ratio >= RISK.dev_red:
                level = "red"
            elif dev_ratio >= RISK.dev_orange:
                level = "orange"
            else:
                level = "yellow"
            worst = max(order.steps, key=lambda s: abs(
                _step_expected(s, order, today) - s.actual_progress))
            signals.append({
                "order_id": order.id,
                "rule": SIGNAL_DEV,
                "level": level,
                "text": "进度落后：计划应完成 %.0f%%，实际 %.0f%%，偏差 %.0f%%" % (
                    expected * 100, progress * 100, dev_ratio * 100),
                "evidence": [{
                    "source": "MES 报工",
                    "detail": "%s（第%d道工序）实际 %.0f%%，最拖后腿" % (
                        worst.name, worst.seq, worst.actual_progress * 100),
                }, {
                    "source": "ERP 计划",
                    "line": True,
                    "detail": "订单 %s：%s × %d 台，优先级 %s，交期 %s" % (
                        order.id, order.product, order.qty, order.priority,
                        order.due_date.isoformat()),
                }],
            })

    # 规则3：物料缺口（承诺到货晚于需求日）
    for m in order.materials:
        gap = m.shortage_days
        if gap >= RISK.mat_yellow_days:
            level = "orange" if gap >= RISK.mat_orange_days else "yellow"
            signals.append({
                "order_id": order.id,
                "rule": SIGNAL_MAT,
                "level": level,
                "text": "物料缺口：%s 承诺到货 %s，晚于需求日 %s，缺口 %d 天" % (
                    m.name, m.eta.isoformat(), m.required_date.isoformat(), gap),
                "evidence": [{
                    "source": "供应商反馈" if m.change_count else "ERP 物料",
                    "detail": "%s %s × %s，承诺到货 %s（被推迟 %d 次），需求 %s" % (
                        m.name, m.spec, m.qty_text, m.eta.isoformat(),
                        m.change_count, m.required_date.isoformat()),
                }],
            })

    return signals


def rule_level(signals: list[dict]) -> str:
    """一组信号中的最高风险等级。"""
    from backend.domain import LEVEL_ORDER
    level = "green"
    for s in signals:
        if LEVEL_ORDER[s["level"]] > LEVEL_ORDER[level]:
            level = s["level"]
    return level


def _step_expected(step, order: Order, today: date) -> float:
    """工序线性计划下的期望进度（用于定位拖后腿工序）。"""
    total = (step.planned_end - step.planned_start).days or 1
    elapsed = (today - step.planned_start).days
    return max(0.0, min(1.0, elapsed / total))
