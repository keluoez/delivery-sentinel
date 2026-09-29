"""种子数据：12 张订单跨 3 车间，日期相对"今天"生成，保证任何一天启动都有真实分布。

设计目标分布：红 3（超期/严重偏差/物料断供）、橙 2、黄 4、绿 3。
事件为历史（consumed=True），效果已烘焙进进度/日期；模拟器注入的新事件才由工作流消费。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from backend.domain import Event, Material, Order, ProcessStep

_TODAY = date.today()


def _d(offset: int) -> date:
    return _TODAY + timedelta(days=offset)


def _ts_ago(hours: float) -> str:
    return (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")


def _order(oid, customer, product, qty, priority, workshop, start_off, due_off,
           progress, materials, changes=0, n_steps=5):
    """构造订单：把总进度目标分配到工序（整完成 + 一个部分完成），计划窗口均分。"""
    start, due = _d(start_off), _d(due_off)
    span = max(1, (due - start).days)
    window = span / n_steps
    steps = []
    done_full = int(progress * n_steps)
    for i in range(n_steps):
        ps = start + timedelta(days=round(i * window))
        pe = start + timedelta(days=round((i + 1) * window))
        if i < done_full:
            ap = 1.0
        elif i == done_full and progress * n_steps > done_full:
            ap = round(progress * n_steps - done_full, 3)
        else:
            ap = 0.0
        steps.append(ProcessStep(seq=i + 1, name=_STEP_NAMES[workshop][i % len(_STEP_NAMES[workshop])],
                                 workshop=workshop, planned_start=ps, planned_end=pe, actual_progress=ap))
    mats = [Material(name=n, spec=sp, qty_text=q, required_date=_d(rd), eta=_d(et), arrived=arr,
                     change_count=cc) for (n, sp, q, rd, et, arr, cc) in materials]
    return Order(id=oid, customer=customer, product=product, qty=qty, priority=priority,
                 workshop=workshop, start_date=start, due_date=due, steps=steps,
                 materials=mats, change_count=changes)


_STEP_NAMES = {
    "SMT车间": ["来料检验", "SMT贴片", "AOI检测", "整机组装", "老化测试"],
    "总装车间": ["来料检验", "部件装配", "整机组装", "功能测试", "包装入库"],
    "机加车间": ["下料", "粗加工", "精加工", "热处理", "终检"],
}


def build_orders() -> list[Order]:
    return [
        # ── 红：进度严重落后(0.366) + 连接器缺口，加急单 ──
        _order("MO-2609-01", "华宏电子", "工业路由器 R-3200", 800, "加急", "SMT车间",
               -14, 3, 0.52,
               [("连接器", "J5-48P", "800 pcs", -2, 1, False, 1),
                ("PCB主板", "R3-V2", "800 pcs", -12, -13, True, 0)]),
        # ── 黄：进度落后 0.17 + 客户追单 ──
        _order("MO-2609-02", "蓝驰设备", "伺服驱动器 S-200", 350, "紧急", "总装车间",
               -18, 6, 0.62,
               [("铝型材机壳", "S200-A", "350 pcs", -8, -10, True, 0)], changes=1),
        # ── 绿：进度超前 ──
        _order("MO-2609-03", "恒泰自动化", "变频器模块 V-450", 500, "常规", "SMT车间",
               -18, 9, 0.70,
               [("PCB板A4", "V4-2L", "500 pcs", 2, -1, False, 0)]),
        # ── 橙：偏差 0.28 + 外壳缺口 4 天 ──
        _order("MO-2609-04", "明迅科技", "智能电表 M-80", 2000, "常规", "总装车间",
               -12, 12, 0.36,
               [("电表外壳", "M80-ABS", "2000 pcs", 2, 6, False, 1),
                ("计量芯片", "RN8209", "2000 pcs", -6, -8, True, 0)]),
        # ── 黄：临近交期 + 包材缺口 1 天 ──
        _order("MO-2609-05", "中科光联", "液晶模组 L-156", 600, "加急", "总装车间",
               -13, 1, 0.84,
               [("彩盒包材", "L156-C", "600 套", 0, 1, False, 0)]),
        # ── 红：超期 2 天未完工 ──
        _order("MO-2609-06", "宏远电力", "充电桩控制板 C-50", 400, "常规", "SMT车间",
               -12, -2, 0.85,
               [("贴片元件", "C50-KIT", "400 套", -10, -11, True, 0)], n_steps=4),
        # ── 黄：偏差 0.16 + 锻件缺口 3 天 ──
        _order("MO-2609-07", "北辰重工", "齿轮箱总成 G-88", 80, "常规", "机加车间",
               -15, 15, 0.42,
               [("锻件坯料", "42CrMo", "80 件", 5, 8, False, 0)]),
        # ── 绿 ──
        _order("MO-2609-08", "迅捷医疗", "检测仪外壳组件 H-12", 250, "加急", "机加车间",
               -8, 8, 0.50,
               [("亚克力面板", "H12-PMMA", "250 pcs", -2, -4, True, 0)]),
        # ── 橙：偏差 0.25 + 回流焊设备异常 ──
        _order("MO-2609-09", "瑞丰仪器", "数据采集卡 D-21", 700, "常规", "SMT车间",
               -20, 5, 0.60,
               [("高速ADC芯片", "AD9226", "700 pcs", -3, -5, True, 0)]),
        # ── 绿：进度超前 ──
        _order("MO-2609-10", "泰达光电", "光纤跳线 F-9", 5000, "常规", "总装车间",
               -15, 10, 0.80, []),
        # ── 黄：偏差 0.13 + 两次变更 ──
        _order("MO-2609-11", "华宏电子", "工业路由器 R-3200", 600, "常规", "SMT车间",
               -14, 7, 0.58, [], changes=2),
        # ── 红：开工即严重落后(0.78) + PCB 缺口 10 天 ──
        _order("MO-2609-12", "蓝驰设备", "伺服驱动器 S-200", 200, "常规", "机加车间",
               -7, 12, 0.08,
               [("PCB板B2", "S2-4L", "200 pcs", 2, 12, False, 1)]),
    ]


def build_events() -> list[Event]:
    """历史事件（consumed=True）：效果已烘焙进种子数据，仅作证据链与展示。"""
    e = []

    def ev(eid, hours_ago, oid, etype, source, content, payload=None):
        e.append(Event(id=eid, ts=_ts_ago(hours_ago), order_id=oid, type=etype,
                       source=source, content=content, payload=payload or {}, consumed=True))

    ev("E-1001", 70, "MO-2609-01", "报工", "MES", "OP30 整机组装：本班完成 120 台，累计 416/800 台。")
    ev("E-1002", 46, "MO-2609-01", "供应商反馈", "邮件",
       "【精连科技】贵司 J5-48P 连接器因上游晶圆缺货，交期被迫推迟，预计 9 月 8 日前可发货。",
       {"material": "连接器", "eta_shift_days": 2})
    ev("E-1003", 24, "MO-2609-02", "订单变更", "邮件",
       "客户蓝驰设备来函：S-200 订单追加 50 台（300→350），交期不变。",
       {"qty_delta": 50})
    ev("E-1004", 30, "MO-2609-04", "供应商反馈", "微信",
       "外壳供应商业务员：M80-ABS 模具修复延期，最快 6 天后到货。",
       {"material": "电表外壳", "eta_shift_days": 3})
    ev("E-1005", 8, "MO-2609-09", "设备事件", "传感器",
       "回流焊 RF-02 炉温异常报警，停机检修 2 小时，恢复生产。",
       {"step": "SMT贴片", "downtime_hours": 2})
    ev("E-1006", 54, "MO-2609-12", "供应商反馈", "邮件",
       "【华芯板业】B2 四层板排产紧张，承诺到货调整至 12 天后。",
       {"material": "PCB板B2", "eta_shift_days": 4})
    ev("E-1007", 20, "MO-2609-11", "订单变更", "ERP",
       "销售部确认：R-3200 新订单包装要求变更为出口木箱。", {"note": "变更1"})
    ev("E-1008", 6, "MO-2609-11", "订单变更", "ERP",
       "华宏电子追加 100 台 R-3200 并入本订单（500→600）。", {"qty_delta": 100, "note": "变更2"})
    ev("E-1009", 12, "MO-2609-06", "报工", "MES",
       "OP40 老化测试：本班完成 40 台，累计 340/400 台。插单导致产能被分摊。")
    ev("E-1010", 16, "MO-2609-05", "报工", "MES",
       "OP40 功能测试：累计 504/600 台，良率 99.2%。")
    ev("E-1011", 2, "MO-2609-07", "供应商反馈", "电话记录",
       "锻造厂：42CrMo 锻件坯料在途，预计 8 天后到厂。", {"material": "锻件坯料"})
    ev("E-1012", 1, "", "系统", "调度器",
       "每日 07:30 自动拉取 ERP 订单 / MES 报工 / 供应商承诺快照，共 12 单。")
    return e


def make_order_from(p: dict) -> Order:
    """「订单登记」事件 → 订单实体。p 来自感知层推送的订单主数据（日期为 iso 字符串）。

    必填：order_id / product / qty / due_date；可选 customer、priority、workshop、
    start_date（缺省=今天）、materials: [{name, spec?, qty_text?, required_date, eta, arrived?}]。
    工序按车间默认工序表均分排布。
    """
    from datetime import date as _d

    def pd(v, default):
        if not v:
            return default
        return v if isinstance(v, _d) else _d.fromisoformat(str(v)[:10])

    start = pd(p.get("start_date"), _TODAY)
    due = pd(p.get("due_date"), None)
    if due is None or due <= start:
        raise ValueError("due_date 必须晚于 start_date")
    qty = int(p.get("qty") or 0)
    if qty <= 0:
        raise ValueError("qty 必须为正整数")
    workshop = p.get("workshop") or "总装车间"
    names = _STEP_NAMES.get(workshop, ["来料检验", "加工", "装配", "测试", "包装入库"])
    n = len(names)
    span = max(1, (due - start).days)
    window = span / n
    steps = [ProcessStep(seq=i + 1, name=names[i], workshop=workshop,
                         planned_start=start + timedelta(days=round(i * window)),
                         planned_end=start + timedelta(days=round((i + 1) * window)))
             for i in range(n)]
    mats = []
    for m in (p.get("materials") or []):
        mats.append(Material(name=m["name"], spec=m.get("spec", "-"),
                             qty_text=m.get("qty_text", "-"),
                             required_date=pd(m.get("required_date"), due),
                             eta=pd(m.get("eta"), due), arrived=bool(m.get("arrived", False))))
    return Order(id=str(p["order_id"]).strip(), customer=p.get("customer") or "-",
                 product=p["product"], qty=qty, priority=p.get("priority") or "常规",
                 workshop=workshop, start_date=start, due_date=due,
                 steps=steps, materials=mats, change_count=0)


SCENARIOS = {
    "supplier_delay": {
        "label": "供应商延期",
        "desc": "把所选订单的一种未到货物料承诺到货再推 3 天（注入供应商邮件事件）",
    },
    "order_change": {
        "label": "客户改单",
        "desc": "订单数量 +10%，交期不变（注入客户邮件事件）",
    },
    "machine_down": {
        "label": "设备故障",
        "desc": "所选订单当前工序设备停机 4 小时（注入传感器事件）",
    },
    "normal_report": {
        "label": "正常报工",
        "desc": "当前工序推进 6% 进度（注入 MES 报工事件）",
    },
}
