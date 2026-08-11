"""深度维度的可替换实现。

v1 只用词法代理（平均长度、疑问句占比、长消息占比），完全离线。
v2 加了一个接 OpenAI 兼容 API 的策略：抽样对话交给 LLM 打「对话深度」分，
LLM 分数由 analyzer 经 extras 机制注入 raw 值，scoring 和存储层都不用改动。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol

from .constants import INSIGHTS_LLM_BASE_URL
from .metrics import Metrics


LOG = logging.getLogger("wechat-insights")


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


class LLMDepth:
    """v2 可选策略：词法三项保留，另加 LLM 的时段深度分与亲密度分。

    包装 LexicalDepth：raw_metrics 仍只回词法三项，LLM 分数不从这里算，由
    analyzer 经 extras 注入——分数不是「当前时刻的一个缓存」，而是时段分
    （llm_period 表）按 as-of 聚合出来的历史可重放值（recent/baseline
    窗口不注入）。某联系人还没有 LLM 分（没采样过 / API 挂了 / 该月样本
    不足）时，缺值权重按 score_cohort 的现有机制回流给词法三项，深度维度
    自动退化成纯词法，不需要任何特判。
    """

    name = "llm"

    def __init__(self) -> None:
        self._lexical = LexicalDepth()

    def raw_metrics(self, window: Metrics) -> dict[str, float | None]:
        return self._lexical.raw_metrics(window)

    def components(self) -> tuple[Component, ...]:
        return (
            # 词法三项权重各 ×0.3（0.4/0.3/0.3 缩到 0.30 总权重，内部仍是
            # 4:3:3）：词法代理在「高频短消息型亲密」上系统性误判，只留
            # LLM 不可用时的回退份额。LLM 两项等权，占 0.70。
            Component("avg_len_them", 0.4 * 0.3, True),
            Component("question_rate_them", 0.3 * 0.3, True),
            Component("long_msg_rate_them", 0.3 * 0.3, True),
            Component("llm_depth_score", 0.35, True),
            Component("llm_warmth_score", 0.35, True),
        )


_STRATEGIES: dict[str, type] = {
    LexicalDepth.name: LexicalDepth,
    LLMDepth.name: LLMDepth,
}


def get_depth_strategy(name: str | None = None) -> DepthStrategy:
    """按名字取深度策略，未知名字回退到词法策略。

    选了 llm 策略但没配置 LLM 端点时，接不上任何服务，回退到纯词法并告警。
    """

    key = (name or os.environ.get("INSIGHTS_DEPTH_STRATEGY") or LexicalDepth.name).strip()
    if key == LLMDepth.name and not INSIGHTS_LLM_BASE_URL:
        LOG.warning("深度策略选了 llm 但 INSIGHTS_LLM_BASE_URL 为空，回退词法策略")
        key = LexicalDepth.name
    return _STRATEGIES.get(key, LexicalDepth)()
