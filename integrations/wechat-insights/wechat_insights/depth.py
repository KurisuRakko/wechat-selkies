"""深度维度的可替换实现。

v1 只用词法代理（平均长度、疑问句占比、长消息占比），完全离线。
v2 计划在这里加一个接 OpenAI 兼容 API 的策略：抽样对话交给 LLM 打「对话深度」
分，把结果作为一个新的 raw metric 返回，`components()` 里给它一个权重即可，
scoring 和存储层都不需要改动。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from .metrics import Metrics


@dataclass(frozen=True, slots=True)
class Component:
    """一个维度的组成项：某个原始指标 + 权重 + 方向。

    原始值单位各不相同，所以先各自在联系人群体里取百分位，再按权重加权，
    维度分因此永远是 0–100 的相对值。
    """

    metric: str
    weight: float
    higher_is_better: bool


class DepthStrategy(Protocol):
    """深度维度策略接口。"""

    name: str

    def raw_metrics(self, window: Metrics) -> dict[str, float | None]:
        """从窗口指标里算出本策略需要的原始值。"""

    def components(self) -> tuple[Component, ...]:
        """声明本策略的组成项及权重。"""


def _rate(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


class LexicalDepth:
    """v1 默认策略：用文本的长度与句式做深度代理。

    分母统一用 TA 的文字消息条数——图片、语音这些没有可比的文本长度。
    """

    name = "lexical"

    def raw_metrics(self, window: Metrics) -> dict[str, float | None]:
        texts = window.get("kind_text_them")
        return {
            "avg_len_them": _rate(window.get("chars_them"), texts),
            "question_rate_them": _rate(window.get("questions_them"), texts),
            "long_msg_rate_them": _rate(window.get("long_msgs_them"), texts),
        }

    def components(self) -> tuple[Component, ...]:
        return (
            Component("avg_len_them", 0.4, True),
            Component("question_rate_them", 0.3, True),
            Component("long_msg_rate_them", 0.3, True),
        )


_STRATEGIES: dict[str, type] = {LexicalDepth.name: LexicalDepth}


def get_depth_strategy(name: str | None = None) -> DepthStrategy:
    """按名字取深度策略，未知名字回退到词法策略。"""

    key = (name or os.environ.get("INSIGHTS_DEPTH_STRATEGY") or LexicalDepth.name).strip()
    return _STRATEGIES.get(key, LexicalDepth)()
