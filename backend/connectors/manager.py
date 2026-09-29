"""连接器管理器：注册/启停/状态汇总（单例）。"""
from __future__ import annotations

import threading

from backend.config import INGEST
from backend.connectors.base import BaseConnector
from backend.connectors.feeds import ImapConnector, RestPollConnector
from backend.connectors.inbox_file import FileInboxConnector
from backend.store import Store


class ConnectorManager:
    def __init__(self, store: Store):
        self.lock = threading.Lock()
        self._items: dict[str, BaseConnector] = {}
        self.register(FileInboxConnector(store))
        self.register(RestPollConnector(store))
        self.register(ImapConnector(store))

    def register(self, conn: BaseConnector):
        with self.lock:
            self._items[conn.name] = conn

    def get(self, name: str) -> BaseConnector | None:
        return self._items.get(name)

    def all_status(self) -> list[dict]:
        with self.lock:
            return [c.status() for c in self._items.values()]

    def inbox_dir(self) -> str:
        c = self._items.get("inbox")
        return str(c.dir) if c else INGEST.inbox_dir


_MANAGER: ConnectorManager | None = None


def get_manager(store: Store | None = None) -> ConnectorManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ConnectorManager(store or get_store())
    return _MANAGER


def get_store():
    from backend.store import get_store as _g
    return _g()
