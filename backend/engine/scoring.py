"""L2 评分引擎：偏差/物料/紧迫/波动四因子加权 → 0-100 分 + 分级。

科研设计：规则保证可解释下限，评分捕捉"多因子叠加"的组合风险。
因子权重与归一化常数集中在 RiskConfig，消融实验时逐项置零即可。
"""
from __future__ import annotations

from datetime import date

from backend.config import RISK
from backend.domain import LEVEL_ORDER, Order


def score_order(order: Order, today: date) -> dict:
    progress = order.progress
    expected = order.expected_progress(today)
    dev_ratio = max(0.0, (expected - progress) / expected) if expected >= 0.02 else 0.0

    # 因子1：进度偏差（归一到 dev_full_ratio 记满分）
    f_dev = min(1.0, dev_ratio / RISK.dev_full_ratio)

    # 因子2：物料（取最严重缺口）
    shortage = max((m.shortage_days for m in order.materials), default=0)
    f_mat = min(1.0, shortage / RISK.mat_full_days)

    # 因子3：交期紧迫 × 优先级（封顶 1.0）
    days_left = (order.due_date - today).days
    urgency = max(0.0, 1.0 - days_left / RISK.urgency_horizon_days) if days_left > 0 else 1.0
    f_urg = min(1.0, urgency * RISK.priority_factor.get(order.priority, 1.0))

    # 因子4：订单波动（近周期变更次数，3 次记满分）
    f_vol = min(1.0, order.change_count / 3.0)

    score = 100.0 * (
        RISK.w_dev * f_dev + RISK.w_mat * f_mat
        + RISK.w_urgency * f_urg + RISK.w_volatility * f_vol
    )

    if score >= RISK.score_red:
        level = "red"
    elif score >= RISK.score_orange:
        level = "orange"
    elif score >= RISK.score_yellow:
        level = "yellow"
    else:
        level = "green"

    return {
        "order_id": order.id,
        "score": round(score, 1),
        "level": level,
        "parts": {
            "deviation": {"value": round(f_dev, 3), "weight": RISK.w_dev,
                          "raw": "偏差 %.0f%%（计划 %.0f%% vs 实际 %.0f%%）" % (
                              dev_ratio * 100, expected * 100, progress * 100)},
            "material": {"value": round(f_mat, 3), "weight": RISK.w_mat,
                         "raw": "最大物料缺口 %d 天" % shortage if shortage else "物料无缺口"},
            "urgency": {"value": round(f_urg, 3), "weight": RISK.w_urgency,
                        "raw": "距交期 %d 天，优先级 %s" % (
                            days_left, order.priority) if days_left > 0 else "已到/超交期"},
            "volatility": {"value": round(f_vol, 3), "weight": RISK.w_volatility,
                           "raw": "近期变更 %d 次" % order.change_count},
        },
    }


def final_level(rule_lv: str, score_lv: str) -> str:
    """最终等级 = 规则与评分中更差者。"""
    return rule_lv if LEVEL_ORDER[rule_lv] >= LEVEL_ORDER[score_lv] else score_lv
