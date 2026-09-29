"""REST API：前端唯一数据通道。约定：所有列表接口返回 dict，含 data + meta。"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.agents import copilot
from backend.agents.graph import NODE_META, build_graph, run_sync
from backend.config import LLM, PROVIDER_PRESETS
from backend import seed_data
from backend.domain import LEVEL_ORDER, LEVEL_LABEL
from backend.simulator import inject
from backend.store import get_store
from backend.llm.provider import test_connection

router = APIRouter(prefix="/api")

# 运行中标志（一轮扫描同时只允许一个）
_run_lock = threading.Lock()
_running = False
_compiled_graph = None


def _get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph(get_store())
    return _compiled_graph


# ──────────────────────────── 工作台 ────────────────────────────
@router.get("/dashboard")
def dashboard():
    store = get_store()
    today = store.today()
    dist = {"red": 0, "orange": 0, "yellow": 0, "green": 0}
    rows = []
    for o in store.orders.values():
        alert = store.open_alert_for(o.id)
        lv = alert.level if alert else "green"
        dist[lv] += 1
        od = o.to_dict(today)
        rows.append({"order": od, "level": lv, "alert_id": alert.id if alert else None,
                     "score": alert.score if alert else 0.0})
    rows.sort(key=lambda r: (-LEVEL_ORDER[r["level"]], -r["score"]))
    day_ago = (date.today() - timedelta(days=1)).isoformat()
    evs = [e for e in store.events if e.ts[:10] >= day_ago]
    return {
        "kpis": {
            "total": len(store.orders),
            "at_risk": dist["red"] + dist["orange"] + dist["yellow"],
            "red_orange": dist["red"] + dist["orange"],
            "pending_alerts": len([a for a in store.alerts.values() if a.status == "待处理"]),
            "overdue": len([o for o in store.orders.values() if o.overdue]),
            "events_24h": len(evs),
        },
        "distribution": [{"level": k, "label": LEVEL_LABEL[k], "count": v} for k, v in dist.items()],
        "top_risks": [{"order_id": r["order"]["id"], "customer": r["order"]["customer"],
                       "product": r["order"]["product"], "level": r["level"],
                       "score": r["score"], "deviation_pct": r["order"]["deviation_pct"],
                       "days_to_due": r["order"]["days_to_due"],
                       "progress": r["order"]["progress"],
                       "expected": r["order"]["expected_progress"],
                       "alert_id": r["alert_id"]} for r in rows[:6]],
        "recent_events": [{"id": e.id, "ts": e.ts, "order_id": e.order_id, "type": e.type,
                           "source": e.source, "content": e.content,
                           "consumed": e.consumed}
                          for e in sorted(store.events, key=lambda x: x.ts, reverse=True)[:8]],
        "pending_events": len(store.pending_events()),
        "unparsed_total": len(store.unparsed),
        "llm_enabled": LLM.enabled,
    }


# ──────────────────────────── 订单 ────────────────────────────
@router.get("/orders")
def orders(level: str = "", q: str = ""):
    store = get_store()
    today = store.today()
    out = []
    for o in store.orders.values():
        alert = store.open_alert_for(o.id)
        lv = alert.level if alert else "green"
        if level and lv != level:
            continue
        od = o.to_dict(today)
        if q and q not in (o.id + o.customer + o.product + o.workshop):
            continue
        out.append({"order": od, "level": lv, "alert_id": alert.id if alert else None,
                    "score": alert.score if alert else 0.0})
    out.sort(key=lambda r: (-LEVEL_ORDER[r["level"]], -r["score"]))
    return {"data": out, "count": len(out)}


@router.get("/orders/{oid}")
def order_detail(oid: str):
    store = get_store()
    o = store.orders.get(oid)
    if not o:
        raise HTTPException(404, "订单不存在")
    alert = store.open_alert_for(oid)
    evs = [e.to_dict() for e in sorted(
        [e for e in store.events if e.order_id == oid], key=lambda x: x.ts, reverse=True)[:10]]
    return {"order": o.to_dict(store.today()), "level": alert.level if alert else "green",
            "alert": alert.to_dict() if alert else None, "events": evs}


# ──────────────────────────── 预警 ────────────────────────────
@router.get("/alerts")
def alerts(status: str = "open"):
    store = get_store()
    if status == "open":
        rows = store.open_alerts()
    elif status == "all":
        rows = list(store.alerts.values())
    else:
        rows = [a for a in store.alerts.values() if a.status == status]
    rows.sort(key=lambda a: (LEVEL_ORDER[a.level], -a.score))
    return {"data": [a.to_dict() for a in rows], "count": len(rows)}


class AlertAction(BaseModel):
    note: str = ""


@router.post("/alerts/{aid}/send")
def alert_send(aid: str, body: AlertAction = None):
    store = get_store()
    a = store.alerts.get(aid)
    if not a:
        raise HTTPException(404, "预警不存在")
    if a.status != "待处理":
        raise HTTPException(400, "预警状态为 %s，不能发送" % a.status)
    a.status = "已发送"
    from backend.domain import now_str
    a.updated_at = now_str()
    store.save()
    return {"ok": True, "message": "已通过 %s 推送给 %s（模拟）" % (a.draft.get("channel"), a.draft.get("to"))}


@router.post("/alerts/{aid}/ignore")
def alert_ignore(aid: str):
    store = get_store()
    a = store.alerts.get(aid)
    if not a:
        raise HTTPException(404, "预警不存在")
    from backend.domain import now_str
    a.status = "已忽略"
    a.updated_at = now_str()
    store.save()
    return {"ok": True}


# ──────────────────────────── 智能体运行 ────────────────────────
@router.get("/graph")
def graph_meta():
    edges = [{"from": "ingest", "to": "update", "type": "normal"},
             {"from": "update", "to": "rule_scan", "type": "normal"},
             {"from": "rule_scan", "to": "risk_score", "type": "normal"},
             {"from": "risk_score", "to": "dedup", "type": "normal"},
             {"from": "dedup", "to": "attribute", "type": "normal", "label": "有新预警"},
             {"from": "dedup", "to": "finalize", "type": "cond", "label": "无新预警"},
             {"from": "attribute", "to": "investigate", "type": "normal"},
             {"from": "investigate", "to": "draft", "type": "normal"},
             {"from": "draft", "to": "gate", "type": "normal"},
             {"from": "gate", "to": "finalize", "type": "normal"}]
    return {"nodes": NODE_META, "edges": edges}


@router.post("/agent/run")
def agent_run():
    global _running
    store = get_store()
    with _run_lock:
        if _running:
            raise HTTPException(409, "已有扫描在运行中，请稍候")
        _running = True
    try:
        rid = store.next_run_id()
        record = {"id": rid, "status": "running",
                  "started_at": datetime.now().isoformat(timespec="seconds"),
                  "finished_at": "", "nodes": [], "stats": None, "error": None}
        store.runs.append(record)
        t = threading.Thread(target=_safe_run, args=(store, record), daemon=True)
        t.start()
        return {"run_id": rid}
    except Exception:
        _running = False
        raise


def _safe_run(store, record):
    global _running
    try:
        run_sync(store, record, compiled=_get_graph())
    except Exception as exc:  # 双保险
        record["status"] = "error"
        record["error"] = str(exc)
    finally:
        _running = False


@router.get("/agent/runs")
def agent_runs():
    store = get_store()
    return {"data": [{k: r[k] for k in ("id", "status", "started_at", "finished_at", "stats", "error")}
                     for r in reversed(store.runs[-10:])]}


@router.get("/agent/runs/{rid}")
def agent_run_detail(rid: str):
    store = get_store()
    for r in reversed(store.runs):
        if r["id"] == rid:
            return r
    raise HTTPException(404, "运行记录不存在")


# ──────────────────────────── 模拟器 ────────────────────────────
@router.get("/scenarios")
def scenarios():
    return {"data": [{"key": k, **v} for k, v in seed_data.SCENARIOS.items()],
            "orders": [{"id": o.id, "label": "%s %s" % (o.id, o.product)}
                       for o in get_store().orders.values()]}


@router.post("/simulator/inject")
def simulator_inject(body: dict):
    store = get_store()
    try:
        result = inject(store, body.get("scenario", ""), body.get("order_id") or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **result}


# ──────────────────────────── 智能助手 ────────────────────────────
class AskBody(BaseModel):
    message: str


@router.post("/copilot")
def copilot_ask(body: AskBody):
    if not body.message.strip():
        raise HTTPException(400, "消息为空")
    return copilot.ask(get_store(), body.message.strip())


@router.get("/copilot/suggestions")
def copilot_suggestions():
    return {"data": copilot._SUGGESTIONS}


# ──────────────────────────── 设置 ────────────────────────────
@router.get("/settings/llm")
def settings_get():
    return {"provider": LLM.provider, "base_url": LLM.base_url, "model": LLM.model,
            "api_key_masked": ("*" * 6 + LLM.api_key[-4:]) if LLM.api_key else "",
            "enabled": LLM.enabled, "presets": PROVIDER_PRESETS}


class LLMBody(BaseModel):
    provider: str = "none"
    base_url: str = ""
    model: str = ""
    api_key: str = ""


@router.post("/settings/llm")
def settings_set(body: LLMBody):
    if body.provider != "none" and not (body.base_url and body.model and body.api_key):
        raise HTTPException(400, "启用 LLM 需同时填写 base_url / model / api_key")
    LLM.provider = body.provider
    LLM.base_url = body.base_url
    LLM.model = body.model
    if body.api_key and not body.api_key.startswith("*"):  # 掩码回传不覆盖
        LLM.api_key = body.api_key
    return {"ok": True, "enabled": LLM.enabled}


@router.post("/settings/llm/test")
def settings_test():
    return test_connection(LLM)


@router.get("/settings/risk")
def settings_risk():
    from backend.config import RISK
    return {
        "rules": {"dev_red": RISK.dev_red, "dev_orange": RISK.dev_orange, "dev_yellow": RISK.dev_yellow,
                  "mat_orange_days": RISK.mat_orange_days, "mat_yellow_days": RISK.mat_yellow_days},
        "score": {"weights": {"偏差": RISK.w_dev, "物料": RISK.w_mat, "紧迫": RISK.w_urgency,
                              "波动": RISK.w_volatility},
                  "levels": {"red": RISK.score_red, "orange": RISK.score_orange,
                             "yellow": RISK.score_yellow}},
        "note": "v1.0 内置阈值（RiskConfig），后续版本开放前端调参",
    }


@router.post("/admin/reseed")
def admin_reseed():
    get_store().reseed()
    return {"ok": True, "message": "已重置为种子数据"}
