"""终端仿真：不开浏览器也能看完整闭环。

流程：注入事件（供应商延期 + 客户改单）→ 跑 LangGraph 工作流 → 打印节点轨迹与预警报告。
用法：python scripts/simulate.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.stdout.reconfigure(encoding="utf-8")

from backend.agents.graph import build_graph, run_sync, NODE_META  # noqa: E402
from backend.domain import LEVEL_LABEL  # noqa: E402
from backend.simulator import inject  # noqa: E402
from backend.store import Store  # noqa: E402


def main():
    store = Store(persist=False)
    print("=" * 72)
    print("交期哨兵 · 终端仿真")
    print("=" * 72)

    print("\n[1] 注入演示事件")
    for sc in ("supplier_delay", "order_change"):
        r = inject(store, sc)
        print("  📨 [%s] %s → %s" % (r["scenario"]["label"], r["order_id"], r["event"]["content"]))

    print("\n[2] 运行 LangGraph 工作流")
    rec = {"id": "R-DEMO", "status": "running", "started_at": "", "finished_at": "",
           "nodes": [], "stats": None, "error": None}
    run_sync(store, rec, compiled=build_graph(store))
    executed = {n["node"] for n in rec["nodes"]}
    for m in NODE_META:
        mark = "✅" if m["key"] in executed else "⏭️ "
        trace = next((n for n in rec["nodes"] if n["node"] == m["key"]), None)
        print("  %s %-10s %s" % (mark, m["title"], trace["summary"] if trace else "（无新预警，跳过）"))
    if rec["status"] != "done":
        print("  ❌ 运行失败：", rec.get("error"))
        return

    print("\n[3] 预警报告（进入人工闸门，确认后才会推送）")
    alerts = sorted(store.alerts.values(),
                    key=lambda a: {"red": 0, "orange": 1, "yellow": 2}[a.level])
    for a in alerts:
        print("\n  ── [%s] %s（评分 %.0f）" % (LEVEL_LABEL[a.level], a.title, a.score))
        print("     结论：%s" % a.summary)
        for r in a.reasons:
            print("     · 【%s】%s" % (r["kind"], r["text"]))
            for e in r["evidence"]:
                print("        ↳ 证据[%s] %s" % (e["source"], e["detail"]))
        print("     建议：%s" % a.suggestion)
        print("     草稿 → %s/%s：" % (a.draft["channel"], a.draft["to"]))
        for line in a.draft["text"].splitlines():
            print("        | %s" % line)

    st = rec["stats"]
    print("\n" + "=" * 72)
    print("统计：扫描 %d 单 · 信号 %d · 新预警 %d · 升级 %d · 抑制 %d · 归因引擎 %s"
          % (st["scanned"], st["signals"], st["new_alerts"], st["upgrades"], st["skipped"], st["engine"]))
    print("打开可视化界面：python -m uvicorn backend.main:app --port 8765 → http://127.0.0.1:8765")


if __name__ == "__main__":
    main()
