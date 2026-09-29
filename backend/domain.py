"""领域模型：订单-工序-物料状态图 + 事件 + 预警。纯数据结构，无业务逻辑。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Optional


# 风险等级：值越大风险越高（用于"取更差等级"运算）
LEVEL_ORDER = {"green": 0, "yellow": 1, "orange": 2, "red": 3}
LEVEL_LABEL = {"green": "正常", "yellow": "提醒", "orange": "警告", "red": "严重"}


def worse_level(a: str, b: str) -> str:
    return a if LEVEL_ORDER[a] >= LEVEL_ORDER[b] else b


def d2s(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if isinstance(d, date) else d


def now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class ProcessStep:
    """工序。actual_progress 由报工事件推进。"""
    seq: int
    name: str
    workshop: str
    planned_start: date
    planned_end: date
    actual_progress: float = 0.0          # 0..1
    device_note: str = ""                 # 最近设备异常备注（归因用）

    @property
    def status(self) -> str:
        if self.actual_progress >= 0.999:
            return "已完成"
        if self.actual_progress > 0.001:
            return "生产中"
        return "待开工"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["planned_start"] = d2s(self.planned_start)
        d["planned_end"] = d2s(self.planned_end)
        d["status"] = self.status
        return d


@dataclass
class Material:
    """物料。eta=供应商承诺到货日，required=本厂需求日。eta 晚于 required 即缺口。"""
    name: str
    spec: str
    qty_text: str
    required_date: date
    eta: date
    arrived: bool = False
    change_count: int = 0                 # 承诺到货被推迟的次数

    @property
    def shortage_days(self) -> int:
        """缺口天数：承诺到货(eta)晚于需求日(required)的天数，>0 即缺口。已到货为 0。"""
        if self.arrived:
            return 0
        return max(0, (self.eta - self.required_date).days)

    @property
    def status(self) -> str:
        if self.arrived:
            return "已到货"
        return "缺口 %d 天" % self.shortage_days if self.shortage_days > 0 else "在途"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["required_date"] = d2s(self.required_date)
        d["eta"] = d2s(self.eta)
        d["shortage_days"] = self.shortage_days
        d["status"] = self.status
        return d


@dataclass
class Order:
    id: str
    customer: str
    product: str
    qty: int
    priority: str                         # 常规/加急/紧急
    workshop: str
    start_date: date
    due_date: date
    steps: list[ProcessStep] = field(default_factory=list)
    materials: list[Material] = field(default_factory=list)
    change_count: int = 0                 # 订单变更次数（波动性因子）

    @property
    def progress(self) -> float:
        """订单实际进度 = 各工序等权平均。"""
        if not self.steps:
            return 0.0
        return sum(s.actual_progress for s in self.steps) / len(self.steps)

    def expected_progress(self, today: date) -> float:
        """线性计划下今日应完成比例 = 已排产天数 / 总天数。"""
        total = (self.due_date - self.start_date).days
        elapsed = (today - self.start_date).days
        if total <= 0:
            return 1.0
        return max(0.0, min(1.0, elapsed / total))

    @property
    def overdue(self) -> bool:
        return date.today() > self.due_date and self.progress < 0.999

    def to_dict(self, today: Optional[date] = None) -> dict:
        today = today or date.today()
        expected = self.expected_progress(today)
        dev = max(0.0, expected - self.progress)
        return {
            "id": self.id,
            "customer": self.customer,
            "product": self.product,
            "qty": self.qty,
            "priority": self.priority,
            "workshop": self.workshop,
            "start_date": d2s(self.start_date),
            "due_date": d2s(self.due_date),
            "days_to_due": (self.due_date - today).days,
            "overdue": self.overdue,
            "progress": round(self.progress, 4),
            "expected_progress": round(expected, 4),
            "deviation_pct": round(dev * 100, 1),      # 偏差（ pct 点）
            "change_count": self.change_count,
            "materials_status": [m.to_dict() for m in self.materials],
            "max_shortage_days": max((m.shortage_days for m in self.materials), default=0),
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class Event:
    """事件流：来自 MES/ERP/邮件/微信 的原始信号，由工作流「状态更新」节点消费。"""
    id: str
    ts: str
    order_id: str
    type: str            # 报工 / 订单变更 / 供应商反馈 / 设备事件 / 系统
    source: str          # MES / ERP / 邮件 / 微信 / 传感器
    content: str         # 人类可读原文（模拟邮件/聊天文本）
    payload: dict = field(default_factory=dict)   # 结构化字段
    consumed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RiskAlert:
    """预警：多级归因 + 证据链 + AI 起草动作，人工闸门后才算"已发送"。"""
    id: str
    order_id: str
    level: str
    score: float
    title: str
    summary: str                          # L3 归因：一句话风险解释
    reasons: list[dict]                   # [{kind, text, evidence:[{source, detail}]}]
    suggestion: str
    draft: dict                           # {channel, to, text}
    report: str = ""                      # 调查员 Agent 的调查报告（红/橙预警才有）
    investigation: dict = field(default_factory=dict)  # {via, steps:[{tool,input,summary}]}
    status: str = "待处理"                 # 待处理/已发送/已忽略/已升级
    created_at: str = field(default_factory=now_str)
    updated_at: str = field(default_factory=now_str)
    run_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
