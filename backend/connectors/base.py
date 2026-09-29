"""感知层连接器基类与事件投递助手。"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime

from backend.domain import Event
from backend.store import Store


def emit(store: Store, source: str, order_id: str | None, etype: str,
         content: str, payload: dict | None = None) -> Event | None:
    """把一条结构化事件投进收件箱；订单缺失/类型不明则转未解析队列。"""
    if not order_id or order_id not in store.orders:
        store.add_unparsed({"source": source, "content": content[:300],
                            "reason": "订单 %s 不存在，无法挂靠" % (order_id or "未知"),
                            "suggestion": "先推送「订单登记」事件，或在感知中心人工指定订单"})
        return None
    ev = Event(id=store.next_event_id(), ts=datetime.now().isoformat(timespec="seconds"),
               order_id=order_id, type=etype, source=source, content=content,
               payload=payload or {}, consumed=False)
    store.add_event(ev, source=source)
    return ev


class BaseConnector:
    """连接器 = 一次 scan() 即一轮采集；start_loop 则按周期后台跑。"""
    name = "base"
    label = "基础连接器"
    desc = ""

    def __init__(self, store: Store):
        self.store = store
        self.running = False          # 后台循环开关
        self.scanning = False
        self.total_events = 0
        self.last_run_at = ""
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # 子类实现 ---------------------------------------------------
    def configured(self) -> bool:
        return True

    def scan(self) -> int:
        raise NotImplementedError

    # 生命周期 ---------------------------------------------------
    def scan_safe(self) -> int:
        try:
            self.scanning = True
            n = self.scan()
            self.total_events += n
            self.last_run_at = datetime.now().isoformat(timespec="seconds")
            self.last_error = ""
            return n
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            traceback.print_exc()
            return 0
        finally:
            self.scanning = False

    def start_loop(self, interval: int):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.running = True

        def loop():
            while not self._stop.is_set():
                self.scan_safe()
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, daemon=True, name="conn-" + self.name)
        self._thread.start()

    def stop_loop(self):
        self._stop.set()
        self.running = False

    def status(self) -> dict:
        return {"name": self.name, "label": self.label, "desc": self.desc,
                "configured": self.configured(), "auto_running": self.running,
                "scanning": self.scanning, "total_events": self.total_events,
                "last_run_at": self.last_run_at, "last_error": self.last_error}
