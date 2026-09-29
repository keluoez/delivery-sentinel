"""全局配置：风险阈值（产品可调参数）+ LLM Provider 设置（环境变量优先，可运行时覆盖）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RiskConfig:
    """L1 规则阈值与 L2 评分权重。集中定义，前端「设置」页展示，测试可覆写。"""
    # L1 规则阈值
    dev_red: float = 0.35          # 进度偏差比例 ≥35% → 红
    dev_orange: float = 0.20       # ≥20% → 橙
    dev_yellow: float = 0.10       # ≥10% → 黄
    mat_orange_days: int = 5       # 物料缺口 ≥5 天 → 橙
    mat_yellow_days: int = 1       # ≥1 天 → 黄
    # L2 评分权重（和为 1.0）
    w_dev: float = 0.40
    w_mat: float = 0.25
    w_urgency: float = 0.20
    w_volatility: float = 0.15
    # L2 分级阈值
    score_red: float = 70.0
    score_orange: float = 50.0
    score_yellow: float = 35.0
    # 归一化常数
    dev_full_ratio: float = 0.40   # 偏差达到 40% 记满分
    mat_full_days: float = 7.0     # 缺口 7 天记满分
    urgency_horizon_days: float = 14.0  # 距交期 14 天内线性收紧
    priority_factor: dict = field(default_factory=lambda: {"常规": 1.0, "加急": 1.1, "紧急": 1.2})


RISK = RiskConfig()


@dataclass
class LLMSettings:
    """LLM Provider：不配 Key 时全系统自动降级为确定性模板引擎，功能不受影响。

    配置方式（二选一）：环境变量 SENTINEL_LLM_API_KEY（可整体覆盖），或前端「设置」页。
    支持 OpenAI 兼容协议：none / openai / dashscope / zhipu / deepseek / custom。
    """
    provider: str = "zhipu"         # none / openai / dashscope / zhipu / deepseek / custom
    base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    model: str = "glm-4.5-air"
    api_key: str = ""               # 出厂不带 Key：置环境变量 SENTINEL_LLM_API_KEY 或在设置页填写
    timeout: int = 40

    @property
    def enabled(self) -> bool:
        return self.provider != "none" and bool(self.api_key) and bool(self.base_url) and bool(self.model)

    def from_env(self) -> "LLMSettings":
        g = lambda k: os.getenv(k, "").strip()  # noqa: E731
        if g("SENTINEL_LLM_PROVIDER"):
            self.provider = g("SENTINEL_LLM_PROVIDER")
        if g("SENTINEL_LLM_BASE_URL"):
            self.base_url = g("SENTINEL_LLM_BASE_URL")
        if g("SENTINEL_LLM_MODEL"):
            self.model = g("SENTINEL_LLM_MODEL")
        if g("SENTINEL_LLM_API_KEY"):
            self.api_key = g("SENTINEL_LLM_API_KEY")
        return self


LLM = LLMSettings().from_env()

# 各 provider 的默认 base_url / model（设置页快捷填充）
PROVIDER_PRESETS = {
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "dashscope": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "custom": {"base_url": "", "model": ""},
}


@dataclass
class IngestConfig:
    """感知层（真实数据接入）配置：事件 API 鉴权 + 各连接器。全部支持环境变量覆盖。"""
    # 事件推送 API 的鉴权 token（Xclaw / 连接器 / 任何 HTTP 客户端都带它）
    api_token: str = ""
    # 文件收件箱（Xclaw/人工把 ERP/MES 导出的表格放进这里）
    inbox_dir: str = ""
    inbox_interval: int = 15          # 自动扫描周期（秒），0=不自动，仅手动
    # 外部 API 轮询（有接口的系统）
    poll_url: str = ""                # 例: http://mes.local/api/workreport?since=...
    poll_token: str = ""
    poll_interval: int = 60
    # IMAP 邮箱（供应商/客户往来邮件）
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_pass: str = ""
    imap_folder: str = "INBOX"
    imap_ssl: bool = True
    imap_interval: int = 120

    def from_env(self) -> "IngestConfig":
        g = lambda k, d="": os.getenv(k, d).strip()  # noqa: E731
        self.api_token = g("SENTINEL_API_TOKEN", "sentinel-dev-token")
        self.inbox_dir = g("SENTINEL_INBOX_DIR") or str(
            (Path(__file__).resolve().parent.parent / "data" / "inbox"))
        self.inbox_interval = int(g("SENTINEL_INBOX_INTERVAL", "0") or 0)
        self.poll_url = g("SENTINEL_POLL_URL")
        self.poll_token = g("SENTINEL_POLL_TOKEN")
        self.poll_interval = int(g("SENTINEL_POLL_INTERVAL", "60") or 60)
        self.imap_host = g("SENTINEL_IMAP_HOST")
        self.imap_port = int(g("SENTINEL_IMAP_PORT", "993") or 993)
        self.imap_user = g("SENTINEL_IMAP_USER")
        self.imap_pass = g("SENTINEL_IMAP_PASS")
        self.imap_folder = g("SENTINEL_IMAP_FOLDER", "INBOX")
        self.imap_ssl = g("SENTINEL_IMAP_SSL", "1") not in ("0", "false")
        self.imap_interval = int(g("SENTINEL_IMAP_INTERVAL", "120") or 120)
        return self


INGEST = IngestConfig().from_env()
