"""五维原始指标、百分位归一化、趋势与异动。

绝对分没有意义：同一个人在「爱发消息的朋友圈」和「都很沉默的朋友圈」里应该得到
不同的分。所以每个原始指标先在联系人群体内取百分位，再按权重合成维度分。
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    ANOMALY_MIN_RATIO,
    ANOMALY_MIN_SAMPLES,
    MIN_REPLY_SAMPLES,
)
from .depth import Component, DepthStrategy
from .metrics import Metrics, quantile


#: 五维的固定顺序，前端雷达图也按这个顺序。
DIMENSION_NAMES = ("responsiveness", "initiative", "investment", "rhythm", "depth")


def _rate(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def reply_median(histogram: list[int], samples: int) -> float | None:
    """回复延迟中位数；样本太少时不给值，避免一两次回复决定一个维度。"""

    if samples < MIN_REPLY_SAMPLES:
        return None
    return quantile(histogram, 0.5)


def raw_metrics(
    window: Metrics, strategy: DepthStrategy, days: int
) -> dict[str, float | None]:
    """把一个窗口的累加指标换算成可比的原始值。

    绝对量（消息数、字数、加权成本）一律除以窗口天数变成日均，这样近 30 天和
    近 90 天两个不等长窗口之间也能直接比较。
    """

    span = max(1, days)
    total = window.messages_total()
    conversations = window.get("conversations")
    values: dict[str, float | None] = {
        "reply_delay_them": reply_median(
            window.reply_hist_them, window.get("replies_them")
        ),
        "reply_delay_me": reply_median(window.reply_hist_me, window.get("replies_me")),
        "fast_rate_them": _rate(
            window.get("fast_replies_them"), window.get("replies_them")
        ),
        "fast_rate_me": _rate(window.get("fast_replies_me"), window.get("replies_me")),
        "started_rate_them": _rate(window.get("conv_started_them"), conversations),
        # TA 连着发多条而我没回 = 一次追加，占 TA 全部连续块的比例。
        "followup_rate_them": _rate(
            window.get("runs_them_multi"), window.get("runs_them")
        ),
        # TA 说最后一句 = TA 不舍得结束，加分。
        "ended_rate_them": _rate(window.get("conv_ended_them"), conversations),
        "cost_them_per_day": window.cost("them") / span,
        "msgs_them_per_day": window.get("msgs_them") / span,
        "chars_them_per_day": window.get("chars_them") / span,
        "sticker_rate": _rate(
            window.get("kind_sticker_them") + window.get("kind_sticker_me"), total
        ),
        "night_rate": _rate(
            window.get("night_msgs_them") + window.get("night_msgs_me"), total
        ),
        "weekend_rate": _rate(
            window.get("weekend_msgs_them") + window.get("weekend_msgs_me"), total
        ),
        "avg_turns": _rate(window.get("turns_total"), conversations),
        "long_conv_rate": _rate(window.get("long_convs"), conversations),
    }
    values.update(strategy.raw_metrics(window))
    return values


def dimensions(strategy: DepthStrategy) -> tuple[tuple[str, tuple[Component, ...]], ...]:
    """五维的组成项定义。深度维度由策略提供，其余四维在这里固定。"""

    return (
        (
            "responsiveness",
            (
                Component("reply_delay_them", 0.6, False),
                Component("fast_rate_them", 0.4, True),
            ),
        ),
        (
            "initiative",
            (
                Component("started_rate_them", 0.4, True),
                Component("followup_rate_them", 0.3, True),
                Component("ended_rate_them", 0.3, True),
            ),
        ),
        (
            "investment",
            (
                Component("cost_them_per_day", 0.45, True),
                Component("msgs_them_per_day", 0.2, True),
                Component("chars_them_per_day", 0.2, True),
                Component("sticker_rate", 0.15, True),
            ),
        ),
        (
            "rhythm",
            (
                Component("night_rate", 0.3, True),
                Component("weekend_rate", 0.2, True),
                Component("avg_turns", 0.3, True),
                Component("long_conv_rate", 0.2, True),
            ),
        ),
        ("depth", strategy.components()),
    )


def percentile_rank(cohort: list[float], value: float) -> float:
    """value 在 cohort 中的百分位（0–100），并列取中点。"""

    if not cohort:
        return 50.0
    below = sum(1 for item in cohort if item < value)
    equal = sum(1 for item in cohort if item == value)
    return 100.0 * (below + equal / 2.0) / len(cohort)


def score_cohort(
    raws: dict[str, dict[str, float | None]], strategy: DepthStrategy
) -> dict[str, dict[str, float]]:
    """把一组联系人的原始值换算成 0–100 的五维分与综合分。

    某个组成项缺值（例如回复样本不足）时，该项的权重在同一维度内按比例分给
    其余项；整维都缺值才退回中性的 50 分。
    """

    definitions = dimensions(strategy)
    cohorts: dict[str, list[float]] = {}
    for component_metric in {
        component.metric for _, components in definitions for component in components
    }:
        cohorts[component_metric] = [
            value
            for values in raws.values()
            if (value := values.get(component_metric)) is not None
        ]

    scores: dict[str, dict[str, float]] = {}
    for session_id, values in raws.items():
        result: dict[str, float] = {}
        for name, components in definitions:
            weighted = 0.0
            weight_total = 0.0
            for component in components:
                value = values.get(component.metric)
                if value is None:
                    continue
                rank = percentile_rank(cohorts[component.metric], value)
                if not component.higher_is_better:
                    rank = 100.0 - rank
                weighted += rank * component.weight
                weight_total += component.weight
            result[name] = weighted / weight_total if weight_total else 50.0
        result["overall"] = sum(result[name] for name in DIMENSION_NAMES) / len(
            DIMENSION_NAMES
        )
        scores[session_id] = result
    return scores


def median(values: list[float]) -> float:
    if not values:
        return 50.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


# —— 近期异动 ——


@dataclass(frozen=True, slots=True)
class AnomalySpec:
    """一条异动的判定与展示方式。"""

    metric: str
    label: str
    higher_is_better: bool
    unit: str
    sample_metric: str


ANOMALY_SPECS = (
    AnomalySpec("reply_delay_them", "TA 回复延迟中位数", False, "duration", "replies_them"),
    AnomalySpec("fast_rate_them", "TA 的秒回率", True, "percent", "replies_them"),
    AnomalySpec("msgs_them_per_day", "TA 的日均消息量", True, "per_day", "msgs_them"),
    AnomalySpec("started_rate_them", "TA 主动发起对话占比", True, "percent", "conversations"),
    AnomalySpec("avg_len_them", "TA 消息平均长度", True, "chars", "kind_text_them"),
)


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


def format_value(value: float, unit: str) -> str:
    if unit == "duration":
        return format_duration(value)
    if unit == "percent":
        return f"{value * 100:.0f}%"
    if unit == "per_day":
        return f"每天 {value:.1f} 条"
    return f"{value:.0f} 字"


def detect_anomalies(
    recent: dict[str, float | None],
    baseline: dict[str, float | None],
    recent_window: Metrics,
    baseline_window: Metrics,
) -> list[dict[str, object]]:
    """对比近期与基线的原始值，挑出变化倍数超过阈值的项。"""

    anomalies: list[dict[str, object]] = []
    for spec in ANOMALY_SPECS:
        before = baseline.get(spec.metric)
        after = recent.get(spec.metric)
        if before is None or after is None or before <= 0 or after <= 0:
            continue
        if (
            recent_window.get(spec.sample_metric) < ANOMALY_MIN_SAMPLES
            or baseline_window.get(spec.sample_metric) < ANOMALY_MIN_SAMPLES
        ):
            continue
        ratio = after / before
        if 1 / ANOMALY_MIN_RATIO < ratio < ANOMALY_MIN_RATIO:
            continue
        improved = (ratio > 1) == spec.higher_is_better
        anomalies.append(
            {
                "metric": spec.metric,
                "label": spec.label,
                "direction": "better" if improved else "worse",
                "before": format_value(before, spec.unit),
                "after": format_value(after, spec.unit),
                "ratio": round(ratio, 2),
            }
        )
    # 恶化的排前面，变化越剧烈越靠前。
    anomalies.sort(
        key=lambda item: (
            item["direction"] == "better",
            -max(float(item["ratio"]), 1 / float(item["ratio"])),
        )
    )
    return anomalies
