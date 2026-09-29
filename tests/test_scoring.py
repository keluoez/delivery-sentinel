"""L2 评分引擎单测：因子分解、分级、与 L1 的融合规则。"""
from datetime import date, timedelta

from backend.domain import Material, Order, ProcessStep
from backend.engine.scoring import final_level, score_order

T = date.today()


def mk_order(progress=0.5, due_off=10, shortage=0, priority="常规", changes=0):
    start, due = T - timedelta(days=10), T + timedelta(days=due_off)
    steps = [ProcessStep(seq=1, name="OP1", workshop="W", planned_start=start,
                         planned_end=due, actual_progress=progress)]
    mats = [Material(name="M", spec="S", qty_text="1", required_date=T + timedelta(days=2),
                     eta=T + timedelta(days=2 + shortage), arrived=(shortage <= 0))]
    return Order(id="MO-T-02", customer="C", product="P", qty=10, priority=priority,
                 workshop="W", start_date=start, due_date=due, steps=steps,
                 materials=mats, change_count=changes)


def test_all_clear_scores_low_and_green():
    r = score_order(mk_order(progress=0.5, due_off=10), T)
    assert r["level"] == "green" and r["score"] < 35
    assert r["parts"]["material"]["value"] == 0


def test_weights_sum_to_one():
    from backend.config import RISK
    assert abs(RISK.w_dev + RISK.w_mat + RISK.w_urgency + RISK.w_volatility - 1.0) < 1e-9


def test_each_factor_contributes():
    r = score_order(mk_order(progress=0.5, due_off=2, shortage=6, priority="紧急", changes=3), T)
    p = r["parts"]
    assert p["deviation"]["value"] > 0 or r["score"] > 60  # 多因子叠加必然高分
    assert r["level"] in ("red", "orange")
    # 紧迫因子封顶为 1（加急×1.2 不越界）
    assert p["urgency"]["value"] <= 1.0 + 1e-9


def test_score_level_thresholds_monotonic():
    """风险单调性：其它条件不变，缺口越大分数不减。"""
    a = score_order(mk_order(shortage=1), T)["score"]
    b = score_order(mk_order(shortage=4), T)["score"]
    c = score_order(mk_order(shortage=9), T)["score"]
    assert a <= b <= c


def test_final_level_is_worse_of_two():
    assert final_level("green", "orange") == "orange"
    assert final_level("red", "yellow") == "red"
    assert final_level("yellow", "yellow") == "yellow"
