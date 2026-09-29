"""感知层测试：抽取器（正则兜底）、文件收件箱、API 轮询、IMAP 桩、事件 API 鉴权与落地。"""
import json
import sys
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.stdout.reconfigure(encoding="utf-8")

from backend.config import INGEST
from backend.connectors.feeds import ImapConnector
from backend.connectors.inbox_file import FileInboxConnector
from backend.perception.extractor import extract, extract_regex

# ─────────────────────────── 抽取器 ───────────────────────────
def test_regex_extracts_relative_eta_and_material():
    r = extract_regex("供应商说 PCB板B2 要推迟，大概 9 月 21 日才能发货，MO-2609-12 的交期要小心",
                      date(2026, 9, 7))
    assert r["via"] == "regex" and r["events"]
    ev = r["events"][0]
    assert ev["type"] == "供应商反馈" and ev["order_id"] == "MO-2609-12"
    assert ev["payload"]["new_eta"] == "2026-09-21"
    assert ev["payload"]["material_hint"] == "PCB板B2"


def test_regex_does_not_confuse_order_id_with_date():
    """回归：MO-2609-12 里的 09-12 不能被当成 9月12日。"""
    r = extract_regex("MO-2609-12 的锻件坯料 3 天后到货", date(2026, 9, 7))
    assert r["events"][0]["payload"]["new_eta"] == "2026-09-10"


def test_regex_extracts_qty_and_done():
    r = extract_regex("客户 MO-2609-02 追加 50 台", date(2026, 9, 7))
    assert r["events"][0]["payload"]["qty_delta"] == 50
    r2 = extract_regex("MO-2609-06 老化测试完成 40 台", date(2026, 9, 7))
    assert r2["events"][0]["payload"]["done_qty"] == 40
    assert r2["events"][0]["type"] == "报工"


def test_extract_garbage_yields_no_events():
    r = extract("今天天气不错，大家午饭吃什么", date(2026, 9, 7))
    assert r["events"] == []  # 宁可漏一条，不可错一条


def test_normalize_reclassifies_llm_misclassification(monkeypatch):
    """回归（实测发现的真 bug）：LLM 把供应商延期邮件抽成 订单变更+new_eta（无数量），
    落地时是静默空操作。归一化必须改判为 供应商反馈。"""
    from backend.perception import extractor
    bad = {"type": "订单变更", "order_id": "MO-2609-01",
           "material_hint": "J5-48P 连接器", "new_eta": "2026-09-24",
           "summary": "交付延期"}
    monkeypatch.setattr(extractor.llm_provider, "chat_json", lambda s, u: bad)
    r = extractor.extract_llm("供应商邮件：连接器延至9月24日，订单号 MO-2609-01")
    assert r["events"] and r["events"][0]["type"] == "供应商反馈"
    assert "qty_delta" not in r["events"][0]["payload"]


def test_normalize_drops_unshapeable_events():
    """说改单却没说改多少 → 不可信，宁可漏掉。"""
    r = extract_regex("MO-2609-02 客户要求调整", date(2026, 9, 7))
    assert r["events"] == []


# ─────────────────────────── 文件收件箱 ───────────────────────────
@pytest.fixture()
def inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(INGEST, "inbox_dir", str(tmp_path))
    s = __import__("backend.store", fromlist=["Store"]).Store(persist=False)
    return FileInboxConnector(s), s


def test_inbox_parses_csv_to_events(inbox):
    conn, store = inbox
    f = conn.dir / "报工.csv"
    f.write_text("订单号,工序,完工数量,内容\nMO-2609-09,SMT贴片,80,本班加班\n",
                 encoding="utf-8-sig")
    assert conn.scan() == 1
    pend = store.pending_events()
    assert len(pend) == 1 and pend[0].type == "报工"
    assert pend[0].payload["done_qty"] == 80
    assert list(conn.processed.iterdir())  # 已归档，防重复消费
    assert conn.scan() == 0


def test_inbox_unknown_order_goes_to_unparsed(inbox):
    conn, store = inbox
    (conn.dir / "x.csv").write_text("订单号,工序,完工数量\nMO-9999-99,SMT贴片,10\n", encoding="utf-8")
    assert conn.scan() == 0
    assert len(store.unparsed) == 1
    assert "不存在" in store.unparsed[0]["reason"]


def test_inbox_reimport_deduplicated(inbox):
    """真实 ERP/MES 导出包含全部历史行：同内容再导入必须去重（行指纹）。"""
    conn, store = inbox
    body = "订单号,工序,完工数量,内容\nMO-2609-09,SMT贴片,80,本班加班\n"
    (conn.dir / "a.csv").write_text(body, encoding="utf-8-sig")
    assert conn.scan() == 1
    # 同内容换文件名再导（模拟每日全量导出）
    (conn.dir / "b.csv").write_text(body, encoding="utf-8-sig")
    assert conn.scan() == 0
    # 行内容有变化（新进度）→ 应作为新事件
    (conn.dir / "c.csv").write_text(
        "订单号,工序,完工数量,内容\nMO-2609-09,SMT贴片,120,本班继续赶工\n", encoding="utf-8-sig")
    assert conn.scan() == 1


def test_inbox_fuzzy_headers_and_formats(inbox):
    """真实表头变体（最新承诺到货/变化说明/千分位数量/斜杠日期）必须可解析。"""
    conn, store = inbox
    (conn.dir / "p.csv").write_text(
        "订单号,物料名称,需求数量,最新承诺到货,变化说明\n"
        "MO-2609-01,连接器,\"1,800\",2026/9/16,铜料短缺再延两天\n",
        encoding="utf-8-sig")
    assert conn.scan() == 1
    ev = store.pending_events()[0]
    assert ev.type == "供应商反馈"
    assert ev.payload["material_hint"] == "连接器"
    assert ev.payload["new_eta"] == f"{__import__('datetime').date.today().year}-09-16"


def test_inbox_registers_new_order(inbox):
    conn, store = inbox
    due = (date.today() + timedelta(days=10)).isoformat()
    (conn.dir / "reg.csv").write_text(
        "订单号,产品,数量,交期,客户\nMO-2610-01,变频器模块 V-450,300,%s,新客户\n" % due,
        encoding="utf-8-sig")
    # 登记要经工作流的「状态更新」节点落地
    assert conn.scan() == 1
    from backend.agents.graph import build_graph, run_sync
    run_sync(store, {"id": "R", "status": "running", "started_at": "", "finished_at": "",
                     "nodes": [], "stats": None, "error": None}, compiled=build_graph(store))
    assert "MO-2610-01" in store.orders
    assert store.orders["MO-2610-01"].qty == 300


# ─────────────────────────── API 轮询 ───────────────────────────
def test_rest_poll_connector(tmp_path, monkeypatch):
    rows = [{"order_id": "MO-2609-02", "type": "订单变更", "content": "客户追加 20 台",
             "payload": {"qty_delta": 20}}]

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(rows).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(INGEST, "poll_url", "http://127.0.0.1:%d/data" % srv.server_port)
    monkeypatch.setattr(INGEST, "poll_token", "t0ken")
    from backend.connectors.feeds import RestPollConnector
    s = __import__("backend.store", fromlist=["Store"]).Store(persist=False)
    conn = RestPollConnector(s)
    assert conn.configured() and conn.scan() == 1
    assert s.pending_events()[0].payload["qty_delta"] == 20
    srv.shutdown()


# ─────────────────────────── IMAP（桩） ───────────────────────────
class FakeMailSource:
    def __init__(self, messages):
        self.messages, self.seen = messages, []

    def fetch_unseen(self):
        return self.messages

    def mark_seen(self, uid):
        self.seen.append(uid)


def test_imap_connector_with_fake_source():
    s = __import__("backend.store", fromlist=["Store"]).Store(persist=False)
    msgs = [
        {"uid": "1", "from": "supplier@x.com", "subject": "交期更新",
         "body": "MO-2609-12 的 PCB板B2 需要推迟，大概 12 天后才能发货"},
        {"uid": "2", "from": "hr@x.com", "subject": "团建通知", "body": "下周三团建，请报名"},
    ]
    conn = ImapConnector(s, source=FakeMailSource(msgs))
    assert conn.configured() and conn.scan() == 1
    ev = s.pending_events()[0]
    assert ev.order_id == "MO-2609-12" and ev.type == "供应商反馈"
    assert len(s.unparsed) == 1 and s.unparsed[0]["source"] == "IMAP邮箱"


# ─────────────────────────── 事件 API ───────────────────────────
@pytest.fixture()
def client(monkeypatch):
    import backend.store as store_mod
    store_mod._STORE = store_mod.Store(persist=False)
    from fastapi.testclient import TestClient
    from backend.main import app
    return TestClient(app), store_mod._STORE


def test_events_api_token_and_flow(client):
    client, store = client
    # 无 token → 401
    r = client.post("/api/events", json={"order_id": "MO-2609-01", "type": "报工",
                                         "content": "完成 100 台", "payload": {"done_qty": 100}})
    assert r.status_code == 401
    # 错 token → 401
    r = client.post("/api/events", json={"order_id": "MO-2609-01", "type": "报工", "content": "x"},
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    # 正确 token → 202 且进收件箱
    tok = INGEST.api_token
    r = client.post("/api/events", json={"order_id": "MO-2609-01", "type": "报工",
                                         "content": "完成 100 台", "payload": {"done_qty": 100}},
                    headers={"Authorization": "Bearer %s" % tok})
    assert r.status_code == 202 and r.json()["pending"] == 1
    # 工作流消费后真实推进进度
    from backend.agents.graph import build_graph, run_sync
    run_sync(store, {"id": "R", "status": "running", "started_at": "", "finished_at": "",
                     "nodes": [], "stats": None, "error": None}, compiled=build_graph(store))
    assert store.orders["MO-2609-01"].progress > 0.52


def test_events_extract_endpoint_creates_or_queues(client):
    client, store = client
    tok = {"Authorization": "Bearer %s" % INGEST.api_token}
    r = client.post("/api/events/extract",
                    json={"text": "MO-2609-11 客户要求追加 30 台，交期不变"}, headers=tok)
    assert r.status_code == 200 and r.json()["extracted"] == 1
    r2 = client.post("/api/events/extract", json={"text": "中午吃什么呢"}, headers=tok)
    assert r2.json()["unparsed"] is True and store.unparsed


def test_unparsed_resolve_flow(client):
    client, store = client
    tok = {"Authorization": "Bearer %s" % INGEST.api_token}
    client.post("/api/events/extract", json={"text": "她说再晚两天吧"}, headers=tok)
    uid = store.unparsed[0]["id"]
    r = client.post("/api/unparsed/%s/resolve" % uid,
                    json={"order_id": "MO-2609-05", "type": "订单变更", "payload": {"qty_delta": 10}},
                    headers=tok)
    assert r.status_code == 202 or r.json().get("ok")
    assert not store.unparsed and len(store.pending_events()) == 1
