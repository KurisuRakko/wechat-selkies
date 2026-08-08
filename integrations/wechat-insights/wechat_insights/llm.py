"""OpenAI 兼容 chat 客户端（纯 stdlib，不引入新依赖）。

调用方必须保证 user 内容已经过 masking.mask()——本模块只负责把请求发出
去，不负责脱敏。请求体（里面是聊天内容）绝不允许打印进日志。

失败（网络异常 / 非 200 / 响应里解析不出内容）记 WARNING 并返回 None，
自动重试 1 次（至多 2 次请求）；解析不出内容的失败重试大概率也失败，
但按成本上限由调用方兜底，这里统一简单重试。
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

from .constants import (
    INSIGHTS_LLM_API_KEY,
    INSIGHTS_LLM_BASE_URL,
    INSIGHTS_LLM_MODEL,
    INSIGHTS_LLM_TIMEOUT_SECONDS,
)

LOG = logging.getLogger("wechat-insights")


def _request(messages: list[dict[str, str]]) -> str | None:
    """发一次请求并取出 assistant 文本；失败返回 None。"""

    payload = json.dumps(
        {
            "model": INSIGHTS_LLM_MODEL,
            "messages": messages,
            "temperature": 0,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{INSIGHTS_LLM_BASE_URL.rstrip('/')}/chat/completions",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {INSIGHTS_LLM_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=INSIGHTS_LLM_TIMEOUT_SECONDS
        ) as response:
            body: Any = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        # 只记异常类型与摘要，绝不打请求体——里面是聊天原文。
        LOG.warning("LLM 请求失败（%s: %s）", type(exc).__name__, exc)
        return None
    try:
        return str(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        LOG.warning("LLM 响应里解析不出 choices[0].message.content")
        return None


def chat(system: str, user: str) -> str | None:
    """调用配置的模型打一轮对话，返回 assistant 的文本回复。

    失败时自动重试 1 次（共至多 2 次请求），仍失败返回 None。user 必须是
    masking.mask() 处理过的文本，否则聊天原文会离开容器。
    """

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    for _ in range(2):
        content = _request(messages)
        if content is not None:
            return content
    return None
