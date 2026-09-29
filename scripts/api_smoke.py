"""API 冒烟测试：对运行中的服务按真实使用时序做全流程校验。

前提：先启动服务  python -m uvicorn backend.main:app --port 8765
用法：python scripts/api_smoke.py [base_url]   （默认 http://127.0.0.1:8765）

注意：脚本按"首次启动 → 首轮扫描 → 注入事件 → 升级 → 同级抑制"的真实时序断言，
建议在刚重置过的服务上运行（重启服务或 POST /api/admin/reseed）。
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
PASSED, FAILED = [], []


def call(method, path, body=None, expect=200, headers=None, timeout=30):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code, data = resp.status, json.loads(resp.read().decode())
        assert code == expect, "HTTP %s != %s" % (code, expect)
        return data
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        assert e.code == expect, "HTTP %s != %s: %s" % (e.code, expect, detail)
        return {}


def wait_run(rid, timeout_s=180):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        rec = call("GET", "/api/agent/runs/" + rid)
        if rec["status"] != "running":
            return rec
        time.sleep(0.5)
    raise AssertionError("扫描超时未完成")


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
        print("  ✅ %s" % name)
    except AssertionError as e:
        FAILED.append((name, str(e)))
        print("  ❌ %s → %s" % (name, e or "(断言失败)"))
    except Exception as e:
        FAILED.append((name, repr(e)))
        print("  ❌ %s → %r" % (name, e))


def main():
    print("API 冒烟 → %s\n" % BASE)

    # ── 阶段 0：不依赖预警存在的基础接口 ──
    def t_index():
        with urllib.request.urlopen(BASE + "/", timeout=10) as r:
            html = r.read().decode()
        assert "交期哨兵" in html and "app.js" in html
    check("GET / 返回前端页面", t_index)

    def t_graph():
        g = call("GET", "/api/graph")
        assert len(g["nodes"]) == 10
        assert any(e["type"] == "cond" for e in g["edges"])
    check("GET /api/graph 画布元数据（10 节点 + 条件边）", t_graph)

    def t_order_detail():
        d = call("GET", "/api/orders/MO-2609-01")
        assert d["order"]["id"] == "MO-2609-01" and len(d["order"]["steps"]) == 5
        assert d["order"]["materials_status"]
    check("GET /api/orders/{id} 订单详情（工序/物料）", t_order_detail)

    def t_settings():
        s = call("GET", "/api/settings/llm")
        assert "enabled" in s and "presets" in s
        call("GET", "/api/settings/risk")
        call("GET", "/api/scenarios")
    check("GET /api/settings/* + /api/scenarios", t_settings)

    # ── 阶段 1：重置 → 首轮扫描 → 全量校验 ──
    def t_reseed():
        r = call("POST", "/api/admin/reseed", {})
        assert r["ok"]
    check("POST /api/admin/reseed 重置数据", t_reseed)

    def t_first_run():
        rec = wait_run(call("POST", "/api/agent/run")["run_id"])
        assert rec["status"] == "done", rec.get("error")
        assert rec["stats"]["scanned"] == 12
        assert rec["stats"]["new_alerts"] >= 8
        assert len(rec["nodes"]) == 10, "首轮应走满 10 节点"
        assert rec["stats"]["reports"] >= 2, "红/橙预警应有调查报告"
        assert {n["node"] for n in rec["nodes"]} >= {"attribute", "investigate", "draft", "gate"}
        assert rec["stats"]["engine"] == "模板引擎" or rec["stats"]["engine"].startswith("LLM")
    check("首轮 /agent/run 走满 10 节点并产出预警+调查报告", t_first_run)

    def t_dashboard():
        d = call("GET", "/api/dashboard")
        assert d["kpis"]["total"] == 12, d["kpis"]
        assert sum(x["count"] for x in d["distribution"]) == 12
        assert d["kpis"]["red_orange"] >= 2, d["kpis"]
        assert d["kpis"]["pending_alerts"] >= 5
    check("GET /api/dashboard KPI 与分布一致", t_dashboard)

    def t_orders():
        d = call("GET", "/api/orders")
        assert d["count"] == 12
        red = call("GET", "/api/orders?level=red")
        assert red["count"] >= 2
        assert all(r["level"] == "red" for r in red["data"])
    check("GET /api/orders 列表与风险过滤", t_orders)

    def t_alerts_flow():
        q = urllib.parse.quote("待处理")
        pending = call("GET", "/api/alerts?status=" + q)
        assert pending["count"] >= 5
        a = pending["data"][0]
        assert a["reasons"] and a["draft"]["text"] and a["suggestion"]
        assert any(rr.get("evidence") for rr in a["reasons"]), "预警必须带证据链"
        r = call("POST", "/api/alerts/%s/send" % a["id"], {})
        assert r["ok"]
        call("POST", "/api/alerts/%s/send" % a["id"], {}, expect=400)  # 重复发送被拒
    check("预警证据链 + 发送 + 重复发送拒绝", t_alerts_flow)

    def t_copilot():
        # ReAct 严格查证模式下助手可能多轮调用 LLM，放宽客户端超时
        r = call("POST", "/api/copilot", {"message": "MO-2609-01 为什么预警？"},
                 timeout=120)
        assert "MO-2609-01" in r["reply"] and r["refs"] == ["MO-2609-01"], r
        r2 = call("POST", "/api/copilot", {"message": "有哪些红色预警？"}, timeout=120)
        assert "MO-2609-" in r2["reply"], r2
        r3 = call("POST", "/api/copilot", {"message": "受物料缺口影响的订单"}, timeout=120)
        assert "缺口" in r3["reply"], r3
    check("POST /api/copilot 意图问答（原因/清单/物料）", t_copilot)

    # ── 阶段 2：无事件再跑 → 同级抑制 ──
    def t_suppression():
        rec = wait_run(call("POST", "/api/agent/run")["run_id"])
        assert rec["stats"]["new_alerts"] == 0, rec["stats"]
        assert rec["stats"]["skipped"] >= 5
        executed = {n["node"] for n in rec["nodes"]}
        assert "gate" not in executed, "无新预警时不应走到人工闸门"
    check("同级抑制：无新事件 → 0 新增 + 条件跳过", t_suppression)

    # ── 阶段 3：注入供应商延期 → 预警升级 ──
    def t_escalation():
        r = call("POST", "/api/simulator/inject", {"scenario": "supplier_delay",
                                                   "order_id": "MO-2609-07"})
        assert r["order_id"] == "MO-2609-07"
        rec = wait_run(call("POST", "/api/agent/run")["run_id"])
        assert rec["status"] == "done"
        assert rec["stats"]["upgrades"] == 1, rec["stats"]
        # 物料 eta 被真实推移 3 天
        d = call("GET", "/api/orders/MO-2609-07")
        m = d["order"]["materials_status"][0]
        assert m["shortage_days"] == 6 and m["change_count"] == 1
        # 新预警为橙色且证据含供应商反馈
        cur = d["alert"]
        assert cur and cur["level"] == "orange"
        assert any(e["source"] == "供应商反馈"
                   for rr in cur["reasons"] for e in rr["evidence"])
    check("注入供应商延期 → 状态落地 + 黄升级橙", t_escalation)

    def t_pending_chip():
        call("POST", "/api/simulator/inject", {"scenario": "machine_down",
                                               "order_id": "MO-2609-09"})
        d = call("GET", "/api/dashboard")
        assert d["pending_events"] == 1
    check("注入后 pending_events 计数", t_pending_chip)

    # ── 阶段 4：感知层 API（token 鉴权 → 推事件 → 升级） ──
    def t_events_api():
        tok = {"Authorization": "Bearer " + call("GET", "/api/hub")["token"]}
        r0 = call("POST", "/api/events", {"order_id": "MO-2609-09", "type": "报工",
                                          "content": "测试推送", "payload": {}}, expect=401)
        assert r0 == {}
        call("POST", "/api/events", {"order_id": "MO-2609-09", "type": "报工",
                                     "content": "API 推送报工 60 台", "payload": {"done_qty": 60}},
             headers=tok, expect=202)
        d = call("GET", "/api/dashboard")
        assert d["pending_events"] >= 1
        rec = wait_run(call("POST", "/api/agent/run")["run_id"])
        assert rec["status"] == "done"
    check("POST /api/events 鉴权 + 推送 + 消费", t_events_api)

    def t_extract_and_unparsed():
        tok = {"Authorization": "Bearer " + call("GET", "/api/hub")["token"]}
        r = call("POST", "/api/events/extract",
                 {"text": "MO-2609-11 客户要求追加 30 台，交期不变"}, headers=tok)
        assert r["extracted"] == 1 and r["via"] in ("regex", "llm")
        r2 = call("POST", "/api/events/extract", {"text": "中午吃什么"}, headers=tok)
        assert r2["unparsed"] is True
        hub = call("GET", "/api/hub")
        assert hub["unparsed_total"] >= 1
        uid = hub["unparsed"][0]["id"]
        call("POST", "/api/unparsed/%s/ignore" % uid, {})
    check("自由文本抽取 + 未解析队列流转", t_extract_and_unparsed)

    def t_inbox_connector():
        call("POST", "/inbox/sample", {}, expect=404)  # 旧路径应 404（已迁到 /api）
        call("POST", "/api/inbox/sample", {})
        r = call("POST", "/api/connectors/inbox/scan", {})
        assert r["new_events"] >= 1
    check("文件收件箱：示例表格 → 扫描 → 事件", t_inbox_connector)

    print("\n结果：%d 通过 / %d 失败" % (len(PASSED), len(FAILED)))
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
