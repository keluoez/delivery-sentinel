"""文件收件箱连接器：监听 data/inbox/ 下的表格（.csv/.xlsx）→ 解析 → 事件。

这是 Xclaw/人工导出 ERP/MES 表格的落地点：把下载路径指向 inbox 即完成对接，
Xclaw 侧零代码改动。解析过的文件移入 processed/ 留档，避免重复消费。
列名映射可通过 data/inbox/mapping.json 覆盖（兼容各家表头）。
"""
from __future__ import annotations

import csv
import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from backend.connectors.base import BaseConnector, emit
from backend.config import INGEST
from backend.domain import Event
from backend.store import Store

DEFAULT_COLMAP = {
    "order_id": ["订单号", "订单", "order_id", "工单号"],
    "type": ["类型", "事件类型", "type"],
    "content": ["内容", "内容描述", "备注", "说明"],
    "step": ["工序", "工序名"],
    "done_qty": ["完工数量", "完成数量", "累计", "完工"],
    "qty": ["数量", "计划数量", "订单数量"],
    "material": ["物料", "物料名", "物料名称"],
    "eta": ["承诺到货", "ETA", "到货日期", "预计到货"],
    "required_date": ["需求日期", "需用日期"],
    "product": ["产品", "品名", "产品名称"],
    "due_date": ["交期", "交货日期", "交付日期"],
    "customer": ["客户", "客户名称"],
    "priority": ["优先级"],
    "start_date": ["开工日期", "开始日期"],
    "workshop": ["车间"],
}


def _pick(row: dict, key: str, colmap: dict):
    """列名匹配：第一轮精确等值，第二轮包含匹配（真实导出的表头五花八门，
    如'最新承诺到货'应命中别名'承诺到货'）。精确匹配优先，避免误吞。"""
    aliases = colmap.get(key, [])
    for alias in aliases:                       # pass 1: 精确
        for k, v in row.items():
            if str(k).strip() == alias and v not in ("", None):
                return v
    for alias in aliases:                       # pass 2: 包含（别名出现在表头中）
        if len(alias) < 2:
            continue
        for k, v in row.items():
            h = str(k).strip()
            if v not in ("", None) and alias in h:
                return v
    return None


def _parse_date(v) -> str | None:
    if v in ("", None):
        return None
    s = str(v).strip()
    formats = [("%Y-%m-%d", False), ("%Y/%m/%d", False), ("%Y.%m.%d", False),
               ("%Y%m%d", False), ("%Y年%m月%d日", False),
               ("%m/%d", True), ("%m月%d日", True)]
    for fmt, month_day in formats:
        try:
            d = datetime.strptime(s, fmt).date()
            if month_day:  # 缺年份（09/21、9月21日）→ 按今年，跨年顺延
                year = date.today().year + (1 if d.month < date.today().month else 0)
                d = date(year, d.month, d.day)
            return d.isoformat()
        except ValueError:
            continue
    return None


def _num(v):
    """数值清洗：兼容千分位（1,200）、带单位（120 台）、空白。"""
    if v in ("", None):
        return None
    s = str(v).strip().replace(",", "").replace("，", "")
    for unit in ("台", "件", "套", "个", "pcs", "PCS"):
        s = s.replace(unit, "").strip()
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return None


def _row_fingerprint(row: dict) -> str:
    """行指纹：整行规范化后哈希。真实导出包含全部历史行，靠它去重。"""
    import hashlib
    clean = {str(k).strip(): str(v).strip() for k, v in row.items() if v not in ("", None)}
    return hashlib.sha1(json.dumps(clean, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


class FileInboxConnector(BaseConnector):
    name = "inbox"
    label = "文件收件箱"
    desc = "监听 data/inbox/ 下 ERP/MES 导出的表格（.csv/.xlsx），解析为事件"

    def __init__(self, store: Store):
        super().__init__(store)
        self.dir = Path(INGEST.inbox_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.processed = self.dir / "processed"
        self.processed.mkdir(exist_ok=True)

    def colmap(self) -> dict:
        f = self.dir / "mapping.json"
        if f.exists():
            try:
                user = json.loads(f.read_text(encoding="utf-8"))
                merged = {k: list(v) for k, v in DEFAULT_COLMAP.items()}
                for k, v in user.items():
                    merged[k] = v if isinstance(v, list) else [v]
                return merged
            except Exception:
                pass
        return DEFAULT_COLMAP

    # ---- 主流程 ------------------------------------------------
    def scan(self) -> int:
        files = sorted(p for p in self.dir.iterdir()
                       if p.suffix.lower() in (".csv", ".xlsx") and p.is_file())
        n = 0
        for f in files:
            for row in self._read_rows(f):
                fp = _row_fingerprint(row)
                if self.store.row_seen(fp):
                    continue    # 重复导入的行（真实导出会带全部历史行）→ 跳过
                if self._row_to_event(row):
                    self.store.remember_row(fp)
                    n += 1
                else:
                    # 未能转事件的行也记指纹，避免每次导出都重复进未解析队列
                    self.store.remember_row(fp)
            shutil.move(str(f), self.processed / ("%s_%s" % (datetime.now().strftime("%H%M%S%f"), f.name)))
        return n

    def _read_rows(self, f: Path) -> list[dict]:
        import pandas as pd
        if f.suffix.lower() == ".csv":
            df = None
            for enc in ("utf-8-sig", "utf-8", "gbk"):
                try:
                    df = pd.read_csv(f, encoding=enc)
                    break
                except (UnicodeDecodeError, pd.errors.ParserError):
                    continue
            if df is None:
                raise ValueError("CSV 编码无法识别: %s" % f.name)
        else:
            df = pd.read_excel(f)
        # astype(object) 先行：数值列的 NaN 用 where 直接替换不生效（pandas 已知行为）
        return df.astype(object).where(df.notna(), None).to_dict("records")

    def _row_to_event(self, row: dict) -> bool:
        cm = self.colmap()
        oid = _pick(row, "order_id", cm)
        oid = str(oid).strip() if oid not in (None, "") else None
        known = bool(oid) and oid in self.store.orders

        # ① 订单不存在：主数据齐全则登记，否则进未解析队列
        if not known:
            product, qty, due = _pick(row, "product", cm), _pick(row, "qty", cm), _pick(row, "due_date", cm)
            if not (oid and product and qty and due):
                return self._to_unparsed(row, oid, "订单不存在且主数据不全（需订单号/产品/数量/交期）")
            payload = {
                "order_id": oid,
                "customer": _pick(row, "customer", cm) or "-",
                "product": str(product),
                "qty": _num(qty) or 0,
                "priority": str(_pick(row, "priority", cm) or "常规"),
                "workshop": _pick(row, "workshop", cm) or "总装车间",
                "start_date": _parse_date(_pick(row, "start_date", cm)),
                "due_date": _parse_date(due),
            }
            if not payload["due_date"] or not payload["qty"]:
                return self._to_unparsed(row, oid, "交期或数量无法解析")
            mat, eta = _pick(row, "material", cm), _parse_date(_pick(row, "eta", cm))
            if mat and eta:
                payload["materials"] = [{"name": str(mat), "eta": eta,
                                         "required_date": _parse_date(_pick(row, "required_date", cm)) or eta}]
            # 同批文件里可能有引用该订单的报工/物料行 → 扫描时同步登记（幂等），
            # 同时保留订单登记事件供工作流留痕。
            try:
                from backend.seed_data import make_order_from
                self.store.orders[oid] = make_order_from(payload)
            except ValueError as exc:
                return self._to_unparsed(row, oid, "订单登记失败：%s" % exc)
            ev = Event(id=self.store.next_event_id(), ts=datetime.now().isoformat(timespec="seconds"),
                       order_id="", type="订单登记", source="文件-inbox",
                       content="登记订单 %s %s × %d" % (oid, payload["product"], payload["qty"]),
                       payload=payload, consumed=False)
            self.store.add_event(ev, source="文件-inbox")
            return True

        # ② 已有订单：按行内容分类为业务事件
        content = str(_pick(row, "content", cm) or "")
        etype = _pick(row, "type", cm)
        done, eta = _pick(row, "done_qty", cm), _parse_date(_pick(row, "eta", cm))
        if etype:
            etype = str(etype)
        elif done is not None:
            etype = "报工"
        elif eta:
            etype = "供应商反馈"
        else:
            return self._to_unparsed(row, oid, "无法判断事件类型（缺 类型/完工数量/到货日期）")

        payload: dict = {}
        if etype == "报工":
            payload["step_hint"] = _pick(row, "step", cm)
            payload["done_qty"] = _num(done)
        elif etype == "供应商反馈":
            if not eta:
                return self._to_unparsed(row, oid, "供应商反馈缺到货日期")
            payload["material_hint"] = _pick(row, "material", cm)
            payload["new_eta"] = eta
        elif etype == "订单变更":
            payload["qty_delta"] = _num(_pick(row, "qty", cm)) or 0
        ev = emit(self.store, "文件-inbox", oid, etype, content or "%s %s" % (etype, oid), payload)
        return ev is not None

    def _to_unparsed(self, row: dict, oid, reason: str) -> bool:
        self.store.add_unparsed({"source": "文件-inbox", "content": str(row)[:300],
                                 "reason": reason})
        return False

    # ---- 演示辅助：生成一份示例表格 -----------------------------
    def write_sample(self) -> str:
        today = date.today()
        rows = [
            {"订单号": "MO-2609-09", "工序": "SMT贴片", "完工数量": 80,
             "内容": "本班加班完成 80 件 D-21 采集卡"},
            {"订单号": "MO-2609-04", "物料": "电表外壳", "承诺到货": (today + timedelta(days=2)).isoformat(),
             "内容": "外壳模具修复提速，承诺到货提前"},
        ]
        f = self.dir / ("sample_%s.csv" % datetime.now().strftime("%H%M%S%f"))
        fields = ["订单号", "工序", "完工数量", "物料", "承诺到货", "内容"]
        with open(f, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        return str(f)
