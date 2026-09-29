"""全局测试约定：
- 默认禁用 LLM（LLM.provider=none）：单元/集成测试确定性、零成本、不外呼；
- SENTINEL_LIVE=1 时不禁用，并只运行拨真测试（tests/test_live_llm.py 真实调用智谱 API）。
"""
import os

import pytest

from backend.config import LLM


@pytest.fixture(autouse=True)
def _llm_policy(monkeypatch):
    if os.getenv("SENTINEL_LIVE", "") == "1":
        yield   # 拨真模式：保留 config 里的真实 Provider
        return
    monkeypatch.setattr(LLM, "provider", "none")
    monkeypatch.setattr(LLM, "api_key", "")
    yield
