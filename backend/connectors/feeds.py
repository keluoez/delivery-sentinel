"""API 轮询连接器：对接"有接口"的 ERP/MES。

配置 SENTINEL_POLL_URL 后按周期拉取 JSON 行数组，每行形如：
  {"order_id": "MO-2609-01", "type": "报工", "content": "...", "payload": {...}}
只拉增量由调用方 URL 参数控制（如 ?since=ts）；本连接器负责转换与投递。
"""
from __future__ import annotations

from datetime import datetime

import httpx

from backend.config import INGEST
from backend.connectors.base import BaseConnector, emit


class RestPollConnector(BaseConnector):
    name = "api_poll"
    label = "外部 API 轮询"
    desc = "按周期拉取有接口系统的 JSON 数据（SENTINEL_POLL_URL 配置）"

    def configured(self) -> bool:
        return bool(INGEST.poll_url)

    def scan(self) -> int:
        headers = {"Authorization": "Bearer %s" % INGEST.poll_token} if INGEST.poll_token else {}
        with httpx.Client(timeout=15) as client:
            resp = client.get(INGEST.poll_url, headers=headers)
            resp.raise_for_status()
            rows = resp.json()
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("events") or []
        n = 0
        for row in rows:
            ev = emit(self.store, "API轮询", row.get("order_id"),
                      row.get("type") or "订单变更",
                      row.get("content") or str(row)[:200],
                      row.get("payload") or {})
            if ev is not None:
                n += 1
        return n


class MailSource:
    """邮件源抽象：真实 IMAP 与测试桩都实现这一接口。"""

    def fetch_unseen(self) -> list[dict]:
        raise NotImplementedError

    def mark_seen(self, uid: str):
        raise NotImplementedError


class ImapMailSource(MailSource):
    """真实 IMAP 实现（std lib，无额外依赖）。"""

    def __init__(self, host, port, user, password, folder, use_ssl):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.folder, self.use_ssl = folder, use_ssl

    def _connect(self):
        import imaplib
        client = imaplib.IMAP4_SSL(self.host, self.port) if self.use_ssl \
            else imaplib.IMAP4(self.host, self.port)
        client.login(self.user, self.password)
        client.select(self.folder)
        return client

    def fetch_unseen(self) -> list[dict]:
        import email
        from email.header import decode_header
        client = self._connect()
        out = []
        try:
            _, data = client.search(None, "UNSEEN")
            for num in data[0].split()[:20]:
                _, msg_data = client.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                subj = ""
                for part, enc in decode_header(msg.get("Subject", "")):
                    subj += part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part
                body = ""
                if msg.is_multipart():
                    for p in msg.walk():
                        if p.get_content_type() == "text/plain":
                            body = p.get_payload(decode=True).decode(
                                p.get_content_charset() or "utf-8", errors="replace")
                            break
                else:
                    body = msg.get_payload(decode=True).decode(
                        msg.get_content_charset() or "utf-8", errors="replace")
                out.append({"uid": num.decode(), "from": msg.get("From", ""),
                            "subject": subj, "body": body})
        finally:
            client.logout()
        return out

    def mark_seen(self, uid: str):
        pass  # fetch RFC822 默认已置 \Seen


class ImapConnector(BaseConnector):
    """邮件连接器：取未读 → LLM/正则抽取 → 事件或未解析队列。"""
    name = "imap"
    label = "邮箱接入（IMAP）"
    desc = "拉取供应商/客户邮件，LLM 抽取交期与变更（SENTINEL_IMAP_* 配置）"

    def __init__(self, store: Store, source: MailSource | None = None):
        super().__init__(store)
        self._source = source  # 测试可注入桩
        self._engine = None

    def configured(self) -> bool:
        return self._source is not None or bool(INGEST.imap_host)

    def _get_source(self) -> MailSource:
        if self._source is not None:
            return self._source
        if self._engine is None:
            self._engine = ImapMailSource(INGEST.imap_host, INGEST.imap_port,
                                          INGEST.imap_user, INGEST.imap_pass,
                                          INGEST.imap_folder, INGEST.imap_ssl)
        return self._engine

    def scan(self) -> int:
        from backend.perception.extractor import extract
        src = self._get_source()
        n = 0
        for msg in src.fetch_unseen():
            text = "%s\n%s" % (msg.get("subject", ""), msg.get("body", ""))
            r = extract(text)
            if r["events"]:
                for ev0 in r["events"]:
                    ev = emit(self.store, "IMAP邮箱", ev0["order_id"], ev0["type"],
                              "【邮件】%s" % text.strip()[:300], ev0["payload"])
                    if ev is not None:
                        n += 1
            else:
                self.store.add_unparsed({"source": "IMAP邮箱",
                                         "content": text.strip()[:300],
                                         "reason": "抽取不到可信事件（订单号/字段缺失）",
                                         "via": r["via"]})
            src.mark_seen(msg["uid"])
        return n
