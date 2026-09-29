"""感知层 API：真实数据的总入口。

- POST /api/events（Bearer token）：Xclaw / 连接器 / 任何 HTTP 客户端的通用事件推送
- 连接器控制：文件收件箱（Xclaw 落表格）、外部 API 轮询、IMAP 邮箱
- 未解析队列：抽取失败/订单不明的内容等人工处理——宁可漏一条，不可错一条
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from backend.config import INGEST, LLM
from backend.connectors.manager import get_manager
from backend.domain import Event, now_str
from backend.perception.extractor import extract
from backend.store import get_store

router = APIRouter(prefix="/api")


def _check_token(authorization: str = ""):
    expected = "Bearer %s" % INGEST.api_token
    if (authorization or "").strip() != expected:
        raise HTTPException(401, "token 缺失或错误（应为 Authorization: Bearer <SENTINEL_API_TOKEN>）")


class EventBody(BaseModel):
    order_id: str = ""
    type: str
    source: str = "API"
    content: str
    payload: dict = {}


class BatchBody(BaseModel):
    events: list[EventBody]


@router.post("/events", status_code=202)
def push_event(body: EventBody, authorization: str = Header(default="")):
    _check_token(authorization)
    store = get_store()
    if not body.content.strip():
        raise HTTPException(400, "content 不能为空")
    ev = Event(id=store.next_event_id(), ts=now_str(), order_id=body.order_id.strip(),
               type=body.type, source=body.source or "API", content=body.content,
               payload=body.payload, consumed=False)
    store.add_event(ev, source="API推送")
    return {"ok": True, "event_id": ev.id, "pending": len(store.pending_events()),
            "note": "事件已入收件箱，运行一轮扫描后生效"}


@router.post("/events/batch", status_code=202)
def push_events(body: BatchBody, authorization: str = Header(default="")):
    _check_token(authorization)
    store = get_store()
    ids = []
    for b in body.events:
        ev = Event(id=store.next_event_id(), ts=now_str(), order_id=b.order_id.strip(),
                   type=b.type, source=b.source or "API", content=b.content,
                   payload=b.payload, consumed=False)
        store.add_event(ev, source="API推送")
        ids.append(ev.id)
    return {"ok": True, "count": len(ids), "event_ids": ids}


@router.post("/events/extract")
def push_raw_text(body: dict, authorization: str = Header(default="")):
    """自由文本入口：先抽取（LLM/正则）再入库；抽不出可信事件 → 未解析队列。"""
    _check_token(authorization)
    store = get_store()
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text 不能为空")
    r = extract(text)
    made = []
    for ev0 in r["events"]:
        ev = Event(id=store.next_event_id(), ts=now_str(), order_id=ev0["order_id"],
                   type=ev0["type"], source=body.get("source") or "自由文本",
                   content=text[:300], payload=ev0["payload"], consumed=False)
        store.add_event(ev, source="自由文本")
        made.append(ev.id)
    if not made:
        store.add_unparsed({"source": body.get("source") or "自由文本", "content": text[:300],
                            "reason": "抽取不到可信事件（订单号/字段缺失）", "via": r["via"]})
    return {"ok": True, "extracted": len(made), "event_ids": made,
            "via": r["via"], "unparsed": not made}


# ─────────────────────── 感知中心 ───────────────────────
@router.get("/hub")
def hub():
    store = get_store()
    mgr = get_manager(store)
    sources = {}
    for e in store.events:
        sources[e.source] = sources.get(e.source, 0) + 1
    return {
        "token": INGEST.api_token,
        "token_note": "推送事件时携带：Authorization: Bearer <token>（可用环境变量 SENTINEL_API_TOKEN 更换）",
        "inbox_dir": mgr.inbox_dir(),
        "connectors": mgr.all_status(),
        "ingest_stats": store.ingest_stats,
        "event_sources": sorted(sources.items(), key=lambda kv: -kv[1]),
        "unparsed": store.unparsed[:20],
        "unparsed_total": len(store.unparsed),
        "pending_events": len(store.pending_events()),
        "llm_enabled": LLM.enabled,
        "orders_count": len(store.orders),
    }


@router.post("/connectors/{name}/scan")
def connector_scan(name: str):
    conn = get_manager(get_store()).get(name)
    if not conn:
        raise HTTPException(404, "连接器不存在")
    n = conn.scan_safe()
    if conn.last_error:
        raise HTTPException(500, "采集失败：%s" % conn.last_error)
    return {"ok": True, "new_events": n, "total": conn.total_events}


@router.post("/connectors/{name}/start")
def connector_start(name: str):
    conn = get_manager(get_store()).get(name)
    if not conn:
        raise HTTPException(404, "连接器不存在")
    if not conn.configured():
        raise HTTPException(400, "该连接器未配置（检查 SENTINEL_* 环境变量）")
    interval = {"inbox": INGEST.inbox_interval, "api_poll": INGEST.poll_interval,
                "imap": INGEST.imap_interval}.get(name, 60)
    if interval <= 0:
        raise HTTPException(400, "该连接器的自动周期被设为 0（SENTINEL_*_INTERVAL=0），只能手动扫描")
    conn.start_loop(interval)
    return {"ok": True, "status": conn.status()}


@router.post("/connectors/{name}/stop")
def connector_stop(name: str):
    conn = get_manager(get_store()).get(name)
    if not conn:
        raise HTTPException(404, "连接器不存在")
    conn.stop_loop()
    return {"ok": True, "status": conn.status()}


@router.post("/inbox/sample")
def inbox_sample():
    conn = get_manager(get_store()).get("inbox")
    path = conn.write_sample()
    return {"ok": True, "file": path}


class ResolveBody(BaseModel):
    order_id: str
    type: str = "订单变更"
    payload: dict = {}


@router.post("/unparsed/{uid}/resolve")
def unparsed_resolve(uid: str, body: ResolveBody):
    """人工把未解析内容挂到指定订单 → 转为正式事件。"""
    store = get_store()
    item = next((u for u in store.unparsed if u.get("id") == uid), None)
    if not item:
        raise HTTPException(404, "未解析记录不存在")
    if body.order_id not in store.orders:
        raise HTTPException(400, "订单 %s 不存在" % body.order_id)
    ev = Event(id=store.next_event_id(), ts=now_str(), order_id=body.order_id,
               type=body.type, source=item.get("source") or "人工确认",
               content=item.get("content", ""), payload=body.payload, consumed=False)
    store.add_event(ev, source="人工确认")
    store.unparsed = [u for u in store.unparsed if u.get("id") != uid]
    store.save()
    return {"ok": True, "event_id": ev.id}


@router.post("/unparsed/{uid}/ignore")
def unparsed_ignore(uid: str):
    store = get_store()
    store.unparsed = [u for u in store.unparsed if u.get("id") != uid]
    store.save()
    return {"ok": True}
