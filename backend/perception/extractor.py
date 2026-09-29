"""感知层抽取器：把非结构化文本（邮件/聊天/变更函）翻译成结构化事件。

双路径：
- LLM 路径（配置了 Provider 时）：JSON 模式抽取 type/order_id/payload
- 正则路径（无 Key 或 LLM 失败时）：关键词分类 + 日期/数量正则，确定性兜底

纪律：抽取结果永远携带原文（证据链）；抽不出订单号或抽不出任何字段 → 进"未解析队列"，
绝不编造。LLM 在这里只做"翻译官"，做不了"搬运工"。
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from backend.config import LLM
from backend.llm import provider as llm_provider

KNOWN_TYPES = {"订单登记", "报工", "订单变更", "供应商反馈", "设备事件"}


def _normalize(ev: dict) -> dict | None:
    """语义校验：事件类型必须与字段形状匹配，否则修正或丢弃（返回 None）。

    实测教训：LLM 会把"供应商推迟交期"抽成 订单变更+new_eta（无数量）——
    该组合在落地时是静默空操作。这里强制：字段决定类型，形状不对就改判或进未解析。
    """
    t = ev["type"]
    p = dict(ev.get("payload") or {})
    has_material = bool(p.get("material_hint") or p.get("material"))
    has_eta = bool(p.get("new_eta") or p.get("eta_shift_days"))
    has_qty = bool(p.get("qty_delta"))
    if t == "订单变更":
        if not has_qty and (has_material or has_eta):
            ev["type"] = "供应商反馈"   # 交期/物料语义 → 改判
        elif not has_qty:
            return None                 # 说改单却没说改多少 → 不可信
    if t == "供应商反馈":
        if not has_eta:
            return None                 # 没有到货信息，无法落地
        p.pop("qty_delta", None)
    if t == "报工" and p.get("done_qty") is None and not p.get("delta"):
        return None
    ev["payload"] = p
    return ev

_TYPE_HINTS = [
    ("供应商反馈", ("推迟", "延期", "晚于", "到货", "发货", "缺货", "物料", "交期推迟")),
    ("订单变更", ("追加", "加单", "改单", "变更", "增加", "改为", "取消")),
    ("报工", ("报工", "完工", "完成", "本班", "累计", "良率")),
    ("设备事件", ("停机", "报警", "故障", "检修", "异常")),
]

_LLM_SYSTEM = (
    "你是制造业供应链事件的抽取器。从给定文本中抽取结构化事件，输出严格 JSON："
    '{"type": "报工|订单变更|供应商反馈|设备事件|订单登记", '
    '"order_id": "MO-XXXX-XX 或 null", '
    '"material_hint": "提到的物料名或 null", '
    '"new_eta": "YYYY-MM-DD 或 null", '
    '"qty_delta": 整数或 null, '
    '"done_qty": 整数或 null, '
    '"step_hint": "工序名或 null", '
    '"summary": "不超过40字的摘要"}'
    "。只输出 JSON。日期按今天是 %s 推算（如'下周三'、'3天后'）。文本里没有的信息一律填 null，禁止编造。"
)


def _relative_date(text: str, today: date) -> date | None:
    # 负向断言：避免把订单号里的 "09-12" 当成日期
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", text)
    if m:
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            return d if d >= today - timedelta(days=1) else date(today.year + 1, d.month, d.day)
        except ValueError:
            pass
    m = re.search(r"(?<![\d/-])(\d{1,2})[/-](\d{1,2})[日号]?(?![\d/-])", text)
    if m:
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            return d if d >= today - timedelta(days=1) else date(today.year + 1, d.month, d.day)
        except ValueError:
            pass
    m = re.search(r"(\d+)\s*天[后後]", text)
    if m:
        return today + timedelta(days=int(m.group(1)))
    if "明天" in text:
        return today + timedelta(days=1)
    if "下周" in text:
        return today + timedelta(days=7)
    return None


def _qty_delta(text: str) -> int | None:
    m = re.search(r"(?:追加|增加|加单|改为|变更为)\s*(\d+)\s*(?:台|件|套|个|pcs)?", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*(?:→|->|至|到)\s*(\d+)\s*(?:台|件|套|个|pcs)", text)
    if m:
        return int(m.group(2)) - int(m.group(1))
    return None


def _done_qty(text: str) -> int | None:
    m = re.search(r"(?:完成|完工)\s*(\d+)\s*(?:台|件|套|个|pcs)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"累计\s*(\d+)\s*/\s*(\d+)", text)
    if m:
        return int(m.group(1))
    return None


def _classify(text: str) -> str:
    for t, kws in _TYPE_HINTS:
        if any(k in text for k in kws):
            return t
    return "订单变更"


def _order_id(text: str) -> str | None:
    m = re.search(r"MO[-\s]?(\d{4})[-\s]?(\d{2})", text.upper())
    if m:
        return "MO-%s-%s" % (m.group(1), m.group(2))
    m = re.search(r"(\d{1,2})\s*号(?:订单|单)", text)
    if m:
        return "MO-2609-%02d" % int(m.group(1))
    return None


def extract_regex(text: str, today: date | None = None) -> dict:
    """正则兜底抽取：零 token、确定性，LLM 不可用时的保底。"""
    today = today or date.today()
    etype = _classify(text)
    payload: dict = {}
    new_eta = _relative_date(text, today)
    qty = _qty_delta(text)
    done = _done_qty(text)
    if etype == "供应商反馈" and new_eta:
        payload["new_eta"] = new_eta.isoformat()
    if etype == "订单变更" and qty:
        payload["qty_delta"] = qty
    if etype == "报工" and done:
        payload["done_qty"] = done
    m = re.search(r"(SMT贴片|整机组装|部件装配|功能测试|老化测试|精加工|粗加工|终检|包装入库|来料检验)", text)
    if m:
        payload["step_hint"] = m.group(1)
    m = re.search(r"(连接器|PCB[\w\u4e00-\u9fa5]{0,4}|锻件[\w\u4e00-\u9fa5]{0,2}|机壳|外壳|包材|彩盒|芯片|面板|坯料|元件)", text)
    if m:
        payload["material_hint"] = m.group(1)
    oid = _order_id(text)
    ev = ({
        "type": etype, "order_id": oid, "payload": payload,
        "content": text, "via": "regex",
    } if (oid and payload) else None)
    ev = _normalize(ev) if ev else None
    return {"events": ([ev] if ev else []),
            "via": "regex", "summary": text[:40]}


def extract_llm(text: str) -> dict | None:
    """LLM 抽取；失败返回 None（上层回落正则）。"""
    import json as _json
    r = llm_provider.chat_json(_LLM_SYSTEM % date.today().isoformat(), text)
    if not r or not r.get("type"):
        return None
    etype = r["type"]
    if etype not in KNOWN_TYPES:
        return None
    payload = {}
    for k in ("material_hint", "new_eta", "qty_delta", "done_qty", "step_hint"):
        if r.get(k) not in (None, "", "null"):
            payload[k] = r[k]
    payload["summary"] = str(r.get("summary") or text[:40])
    oid = r.get("order_id") or _order_id(text)
    ev = ({
        "type": etype, "order_id": oid, "payload": payload,
        "content": text, "via": "llm",
    } if (oid and payload) else None)
    ev = _normalize(ev) if ev else None
    return {"events": ([ev] if ev else []),
            "via": "llm", "summary": payload.get("summary", "")}


def extract(text: str, today: date | None = None) -> dict:
    """总入口：LLM 优先，正则兜底。返回 {events, via, summary}。

    events 为空表示"抽取不出可信事件"——调用方应把它送进未解析队列，而不是硬造。
    """
    text = (text or "").strip()
    if not text:
        return {"events": [], "via": "none", "summary": ""}
    if LLM.enabled:
        try:
            r = extract_llm(text)
            if r and r["events"]:
                return r
        except Exception:
            pass
    return extract_regex(text, today)
