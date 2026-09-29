"""存储层：内存单例 + JSON 持久化 + 线程锁。业务视图查询也集中在这里。"""
from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path

from backend import seed_data
from backend.domain import LEVEL_ORDER, Event, Order, RiskAlert, now_str

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_FILE = DATA_DIR / "state.json"


class Store:
    def __init__(self, persist: bool = True):
        self.lock = threading.RLock()
        self.persist = persist
        self.orders: dict[str, Order] = {}
        self.events: list[Event] = []
        self.alerts: dict[str, RiskAlert] = {}
        self.runs: list[dict] = []
        self.unparsed: list[dict] = []    # 感知层"未解析队列"：抽取失败/订单不明确的事件，等人工处理
        self.ingest_stats: dict[str, int] = {}  # 按来源统计摄入事件数：{"API": n, "文件-inbox": n, ...}
        self.seen_rows: list[str] = []    # 已导入表格行的指纹（防重复导入，环形保留最近 3000 条）
        self._seq_alert = 1000
        self._seq_event = 2000
        self._seq_run = 0
        if persist and STATE_FILE.exists():
            try:
                self._load()
                return
            except Exception:
                pass  # 损坏则重新播种
        self.reseed()

    # ---------- 初始化 ----------
    def reseed(self):
        with self.lock:
            self.orders = {o.id: o for o in seed_data.build_orders()}
            self.events = seed_data.build_events()
            self.alerts = {}
            self.runs = []
            self.unparsed = []
            self.ingest_stats = {}
            self.seen_rows = []   # 重置演示时允许重新导入同样的表格
            self._seq_alert, self._seq_event, self._seq_run = 1000, 2000, 0
            self._save()

    # ---------- ID ----------
    def next_alert_id(self) -> str:
        with self.lock:
            self._seq_alert += 1
            return "A-%d" % self._seq_alert

    def next_event_id(self) -> str:
        with self.lock:
            self._seq_event += 1
            return "E-%d" % self._seq_event

    def next_run_id(self) -> str:
        with self.lock:
            self._seq_run += 1
            return "R-%03d" % self._seq_run

    # ---------- 事件 ----------
    def pending_events(self) -> list[Event]:
        return [e for e in self.events if not e.consumed]

    def add_event(self, ev: Event, source: str = ""):
        """事件进入 pending 收件箱，并按来源累计摄入统计。"""
        with self.lock:
            self.events.append(ev)
            key = source or ev.source or "其他"
            self.ingest_stats[key] = self.ingest_stats.get(key, 0) + 1
            self._save()

    def add_unparsed(self, item: dict):
        """抽取失败/订单不明确的内容进入人工处理队列（宁可漏一条，不可错一条）。"""
        with self.lock:
            item = dict(item)
            item["id"] = "U-%d" % (len(self.unparsed) + 1)
            item["ts"] = item.get("ts") or now_str()
            self.unparsed.insert(0, item)
            self._save()

    def row_seen(self, fingerprint: str) -> bool:
        """表格行指纹是否已导入过（防重复导入：真实 ERP/MES 导出会包含全部历史行）。"""
        with self.lock:
            return fingerprint in self.seen_rows

    def remember_row(self, fingerprint: str):
        with self.lock:
            if fingerprint not in self.seen_rows:
                self.seen_rows.append(fingerprint)
                if len(self.seen_rows) > 3000:      # 环形保留，防止无限增长
                    self.seen_rows = self.seen_rows[-2500:]
                self._save()

    def today(self) -> date:
        return date.today()

    # ---------- 预警 ----------
    def open_alerts(self) -> list[RiskAlert]:
        """未关闭预警：待处理 / 已发送。已忽略 / 已升级不算。"""
        return [a for a in self.alerts.values() if a.status in ("待处理", "已发送")]

    def open_alert_for(self, order_id: str) -> RiskAlert | None:
        cands = [a for a in self.open_alerts() if a.order_id == order_id]
        if not cands:
            return None
        return max(cands, key=lambda a: (LEVEL_ORDER[a.level], a.created_at))

    def add_alert(self, alert: RiskAlert):
        with self.lock:
            self.alerts[alert.id] = alert
            self._save()

    def save(self):
        with self.lock:
            self._save()

    # ---------- 持久化 ----------
    def _save(self):
        if not self.persist:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "saved_at": date.today().isoformat(),
            "orders": [{**o.to_dict(), "_raw": self._raw_order(o)} for o in self.orders.values()],
            "events": [e.to_dict() for e in self.events],
            "alerts": {aid: a.to_dict() for aid, a in self.alerts.items()},
            "runs": self.runs[-20:],
            "unparsed": self.unparsed[:50],
            "ingest_stats": self.ingest_stats,
            "seen_rows": self.seen_rows[-3000:],
            "seq": {"alert": self._seq_alert, "event": self._seq_event, "run": self._seq_run},
        }
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    def _raw_order(self, o: Order) -> dict:
        """持久化原始可变字段（date 存 iso），加载时还原。"""
        return {
            "qty": o.qty,
            "change_count": o.change_count,
            "steps": [{"seq": s.seq, "actual_progress": s.actual_progress, "device_note": s.device_note} for s in o.steps],
            "materials": [{"name": m.name, "eta": m.eta.isoformat(), "arrived": m.arrived,
                           "change_count": m.change_count} for m in o.materials],
        }

    def _load(self):
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if payload.get("saved_at") != date.today().isoformat():
            raise ValueError("种子数据过期（相对日期），重新播种")
        self.orders = {od["id"]: self._rebuild(od) for od in payload["orders"]}
        self.events = [Event(**{k: v for k, v in ed.items() if k in Event.__dataclass_fields__})
                       for ed in payload["events"]]
        self.alerts = {aid: RiskAlert(**ad) for aid, ad in payload["alerts"].items()}
        self.runs = payload.get("runs", [])
        self.unparsed = payload.get("unparsed", [])
        self.ingest_stats = payload.get("ingest_stats", {})
        self.seen_rows = payload.get("seen_rows", [])
        seq = payload.get("seq", {})
        self._seq_alert = seq.get("alert", 1000)
        self._seq_event = seq.get("event", 2000)
        self._seq_run = seq.get("run", 0)

    def _rebuild(self, od: dict) -> Order:
        from datetime import date as _d
        raw = od["_raw"]
        steps = []
        for s in od["steps"]:
            from backend.domain import ProcessStep
            st = ProcessStep(seq=s["seq"], name=s["name"], workshop=s["workshop"],
                             planned_start=_d.fromisoformat(s["planned_start"]),
                             planned_end=_d.fromisoformat(s["planned_end"]),
                             actual_progress=s["actual_progress"])
            raw_step = next((r for r in raw["steps"] if r["seq"] == s["seq"]), None)
            if raw_step:
                st.device_note = raw_step.get("device_note", "")
            steps.append(st)
        from backend.domain import Material
        mats = []
        for m in od["materials_status"]:
            mt = Material(name=m["name"], spec=m["spec"], qty_text=m["qty_text"],
                          required_date=_d.fromisoformat(m["required_date"]),
                          eta=_d.fromisoformat(m["eta"]), arrived=m["arrived"])
            raw_m = next((r for r in raw["materials"] if r["name"] == m["name"]), None)
            if raw_m:
                mt.change_count = raw_m.get("change_count", 0)
            mats.append(mt)
        return Order(id=od["id"], customer=od["customer"], product=od["product"],
                     qty=raw["qty"], priority=od["priority"], workshop=od["workshop"],
                     start_date=_d.fromisoformat(od["start_date"]),
                     due_date=_d.fromisoformat(od["due_date"]),
                     steps=steps, materials=mats, change_count=raw["change_count"])


_STORE: Store | None = None


def get_store() -> Store:
    global _STORE
    if _STORE is None:
        _STORE = Store(persist=True)
    return _STORE
