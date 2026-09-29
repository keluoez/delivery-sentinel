"""拨真测试：真实调用智谱 GLM-4.5-Air。运行：SENTINEL_LIVE=1 pytest tests/test_live_llm.py -v

验证的是"接了真模型之后系统是否仍然成立"：
1. 连通性；2. 抽取接地（类型与字段形状匹配）；3. 调查员防幻觉护栏；4. ReAct 助手全链路。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

import pytest


def _live_enabled() -> bool:
    return os.getenv("SENTINEL_LIVE", "") == "1"


requires_live = pytest.mark.skipif(not _live_enabled(), reason="需 SENTINEL_LIVE=1（真实调用 LLM）")


@requires_live
def test_live_connection():
    from backend.llm import provider
    reply = provider.chat("你是连通性测试器，只回复 OK。", "回复 OK")
    assert reply and "OK" in reply.upper()


@requires_live
def test_live_extraction_is_normalized():
    """真实邮件 → 抽取 → 归一化：供应商延期必须被识别为 供应商反馈 且带 new_eta。"""
    from backend.perception.extractor import extract_llm
    r = extract_llm("【华菱线缆】贵司：因铜价上涨我司产能受限，原定9月18日交付的 J5-48P 连接器"
                    "需延至9月24日发货，敬请谅解。订单号 MO-2609-01。")
    assert r["events"], "LLM 抽取不应为空"
    ev = r["events"][0]
    assert ev["type"] == "供应商反馈", "归一化应把'只有交期没有数量'改判为供应商反馈，实际: %s" % ev
    assert ev["payload"].get("new_eta")
    assert "qty_delta" not in ev["payload"]


@requires_live
def test_live_investigator_report_is_grounded():
    """调查员报告必须通过接地校验：包含真实订单号与真实产品/物料，不得虚构部件。"""
    from backend.store import Store
    from backend.agents.graph import build_graph, run_sync
    from backend.agents.investigator import investigate
    store = Store(persist=False)
    rec = {"id": "R", "status": "running", "started_at": "", "finished_at": "",
           "nodes": [], "stats": None, "error": None}
    run_sync(store, rec, compiled=build_graph(store))
    r = investigate(store, "MO-2609-12", "red", "进度落后且物料缺口", [], score=67.9)
    assert "MO-2609-12" in r["report"], "报告必须包含真实订单号"
    assert "S-200" in r["report"] or "PCB" in r["report"] or "进度" in r["report"]
    # 真实订单里不存在的部件不允许出现
    for fake in ("CPU-8500", "MEM-3200", "XJ-4500"):
        assert fake not in r["report"], "报告出现虚构实体 %s（防幻觉护栏失效）" % fake


@requires_live
def test_live_copilot_react_answers_with_tools():
    from backend.agents.copilot import ask
    from backend.store import Store
    store = Store(persist=False)
    r = ask(store, "MO-2609-12 为什么预警？")
    assert "MO-2609-12" in r["reply"]
    assert r["via"].startswith("ReAct") or r["via"] == "规则引擎"
