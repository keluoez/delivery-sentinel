"""LLM Provider：OpenAI 兼容 /chat/completions 协议（openai/dashscope/zhipu/deepseek/custom 通吃）。

设计原则：
- 无 Key / 调用失败 / 解析失败 → 一律返回 None，上层走确定性模板降级，产品永不因 LLM 挂掉。
- **思考模式默认关闭**：GLM-4.5 系列等推理模型默认开启思考，会吃掉 token 预算并放大延迟
  （实测：同任务 3.5s/233 tokens → 0.7s/32 tokens）；本系统任务（抽取/ReAct/归因）不需要深思考。
- **限流/超时自动重试**：429/5xx/网络错误按 1s/3s 退避重试两次。
- **JSON 模式兼容回退**：response_format 不被支持（HTTP 400）时自动去掉该参数重试。
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from backend.config import LLM

log = logging.getLogger("sentinel.llm")


def _post(s, payload: dict, headers: dict, url: str):
    """带退避重试的单次请求。返回 (content 或 None, retryable 错误或 None)。"""
    delays = (0, 1.0, 3.0)  # 首次 + 两次重试
    last_err = None
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            with httpx.Client(timeout=s.timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = "HTTP %d" % resp.status_code
                log.warning("LLM %s（第 %d 次），将重试", last_err, attempt + 1)
                continue
            if resp.status_code == 400 and payload.get("response_format"):
                # 端点不支持 JSON 模式 → 去掉该参数整体重试
                log.info("端点拒绝 response_format，回退为普通文本模式")
                payload = {k: v for k, v in payload.items() if k != "response_format"}
                last_err = "response_format"
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"], None
        except httpx.TimeoutException as exc:
            last_err = str(exc) or "timeout"
            log.warning("LLM 超时（第 %d 次），将重试", attempt + 1)
        except httpx.HTTPError as exc:
            last_err = str(exc)
            log.warning("LLM 网络错误（第 %d 次）：%s", attempt + 1, exc)
        except Exception as exc:
            return None, str(exc)
    return None, last_err


def chat(system: str, user: str, settings: LLM | None = None,
         json_mode: bool = False, thinking: bool = False) -> str | None:
    s = settings or LLM
    if not s.enabled:
        return None
    url = s.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": s.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.3,
        "max_tokens": 1500,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    # 推理模型（GLM-4.5 系列等）默认开启思考：吃 token、放大延迟；本类任务关闭。
    # 不支持该参数的端点会忽略未知字段或返回 400（400 时由 _post 回退并去掉占位不影响）。
    payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
    headers = {"Authorization": "Bearer %s" % s.api_key, "Content-Type": "application/json"}
    try:
        content, err = _post(s, payload, headers, url)
    except Exception as exc:  # 兜底：任何异常都降级
        log.warning("LLM 调用失败，降级为模板引擎: %s", exc)
        return None
    if content is None:
        log.warning("LLM 调用失败，降级为模板引擎: %s", err)
    return content


def chat_json(system: str, user: str, settings: LLM | None = None) -> dict | None:
    raw = chat(system, user, settings=settings, json_mode=True)
    if raw is None:
        return None
    try:
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as exc:
        log.warning("LLM JSON 解析失败，降级为模板引擎: %s", exc)
        return None


def test_connection(settings: LLM | None = None) -> dict:
    """设置页「测试连接」：发一句最小请求。"""
    s = settings or LLM
    if not s.enabled:
        return {"ok": False, "message": "未配置完整（provider / base_url / model / api_key）"}
    reply = chat("你是连通性测试器，只回复 OK。", "回复 OK", settings=s)
    if reply is None:
        return {"ok": False, "message": "调用失败，请检查 base_url / key / 网络（详见服务日志）"}
    return {"ok": True, "message": "连接成功，模型返回：%s" % reply[:60]}
