"""按天聚合的指标累加器与回复延迟直方图。

指标全部是可加的：跨天、跨窗口合成只需要逐项相加，所以「按天存储、展示时滚动
合成」不需要任何额外的中间表。回复延迟这种需要分位数的量存成对数直方图，
同样可以逐桶相加，代价是分位数带一点桶内插值误差。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime

from .constants import (
    COST_WEIGHTS,
    FAST_REPLY_SECONDS,
    HISTOGRAM_BUCKETS,
    LATE_NIGHT_MAX_HOUR,
    METRIC_COLUMNS,
    NIGHT_HOURS,
    TEXT_CHARS_PER_COST_UNIT,
)
from .conversation import THEM, ConversationShape, Message, describe
from .lexical import analyze_text


_METRIC_SET = frozenset(METRIC_COLUMNS)


def day_key(timestamp: int) -> str:
    """Unix 秒 → 容器本地时区的 YYYY-MM-DD。"""

    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def day_span(start_day: str, end_day: str) -> int:
    """闭区间 [start_day, end_day] 覆盖的天数，含首尾两天。

    存储层的窗口查询是两端闭区间，日均的除数必须等于实际加载的日键个数，
    否则「近 30 天」的 31 个日键会被除以 30，近期与基线之间凭空多出
    31/30 的比值偏差。
    """

    return (date.fromisoformat(end_day) - date.fromisoformat(start_day)).days + 1


def side_of(direction: str) -> str:
    """方向 → 指标列后缀。"""

    return "them" if direction == THEM else "me"


# —— 对数直方图 ——


def bucket_of(delay: int) -> int:
    """延迟秒数 → 桶号；桶 i 覆盖 [2^i, 2^(i+1)) 秒。"""

    if delay < 1:
        return 0
    return min(HISTOGRAM_BUCKETS - 1, int(math.log2(delay)))


def empty_histogram() -> list[int]:
    return [0] * HISTOGRAM_BUCKETS


def parse_histogram(value: object) -> list[int]:
    """把存储里的 JSON 文本还原成直方图；损坏或缺失一律当空处理。"""

    if not value:
        return empty_histogram()
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return empty_histogram()
    if not isinstance(parsed, list):
        return empty_histogram()
    histogram = empty_histogram()
    for index, count in enumerate(parsed[:HISTOGRAM_BUCKETS]):
        try:
            histogram[index] = max(0, int(count))
        except (TypeError, ValueError):
            return empty_histogram()
    return histogram


def dump_histogram(histogram: list[int]) -> str:
    return json.dumps(histogram, separators=(",", ":"))


def quantile(histogram: list[int], fraction: float) -> float | None:
    """在对数直方图上估算分位数（秒），桶内按几何插值。"""

    total = sum(histogram)
    if total <= 0:
        return None
    target = total * fraction
    cumulative = 0
    for index, count in enumerate(histogram):
        if count <= 0:
            continue
        if cumulative + count >= target:
            # 桶覆盖 [2^index, 2^(index+1))，按落在桶内的比例做几何插值。
            inside = (target - cumulative) / count
            return float(2 ** (index + min(1.0, max(0.0, inside))))
        cumulative += count
    return float(2 ** HISTOGRAM_BUCKETS)


# —— 打分窗口的指数衰减 ——


def decayed_weight(age_days: int, half_life: int) -> float:
    """窗口内一个天桶的权重：0.5^(天龄/半衰期)。

    age_days 是距离「今天」的天数（今天为 0），半衰期处权重正好 0.5。
    """

    return 0.5 ** (age_days / half_life)


def decayed_span(span_days: int, half_life: int) -> float:
    """打分窗口的「等效天数」：Σ_{age=0..span-1} 0.5^(age/half_life)。

    作为日均的除数替代 span 本身：均匀活跃的人加权后的日均与未加权相同，
    所以「两年衰减窗口」里的日均仍然和近 30 天窗口可直接比较。
    """

    return sum(decayed_weight(age, half_life) for age in range(span_days))


# —— 指标累加器 ——


@dataclass(slots=True)
class Metrics:
    """一个（联系人, 时间桶）下的全部可加指标。

    counts 与直方图写成 float：加权合成（merge_weighted）只存在于内存打分
    路径；写库路径仍走整数（merge），dump_histogram 不受影响。
    """

    counts: dict[str, float] = field(default_factory=dict)
    reply_hist_them: list[float] = field(default_factory=empty_histogram)
    reply_hist_me: list[float] = field(default_factory=empty_histogram)

    def add(self, name: str, value: int = 1) -> None:
        if name not in _METRIC_SET:
            raise KeyError(f"unknown metric column: {name}")
        self.counts[name] = self.counts.get(name, 0) + value

    def get(self, name: str) -> float:
        return self.counts.get(name, 0)

    def add_reply(self, direction: str, delay: int) -> None:
        histogram = (
            self.reply_hist_them if direction == THEM else self.reply_hist_me
        )
        histogram[bucket_of(delay)] += 1
        side = side_of(direction)
        self.add(f"replies_{side}")
        if delay < FAST_REPLY_SECONDS:
            self.add(f"fast_replies_{side}")

    def merge(self, other: Metrics) -> None:
        for name, value in other.counts.items():
            self.counts[name] = self.counts.get(name, 0) + value
        for index in range(HISTOGRAM_BUCKETS):
            self.reply_hist_them[index] += other.reply_hist_them[index]
            self.reply_hist_me[index] += other.reply_hist_me[index]

    def merge_weighted(self, other: Metrics, weight: float) -> None:
        """按指数衰减权重并入另一个桶：所有计数与直方图逐项乘 weight 再相加。

        与 merge 一样只进内存，绝不落库：加权后的浮点值只有在打分窗口合成时
        才会出现，写库路径（_write_daily 的 merge）永远是整数。
        """

        for name, value in other.counts.items():
            self.counts[name] = self.counts.get(name, 0) + value * weight
        for index in range(HISTOGRAM_BUCKETS):
            self.reply_hist_them[index] += other.reply_hist_them[index] * weight
            self.reply_hist_me[index] += other.reply_hist_me[index] * weight

    def messages_total(self) -> float:
        return self.get("msgs_them") + self.get("msgs_me")

    def cost(self, side: str) -> float:
        """按类型加权的「投入成本」，文字按字数折算。"""

        total = self.get(f"chars_{side}") / TEXT_CHARS_PER_COST_UNIT
        for kind, weight in COST_WEIGHTS.items():
            total += self.get(f"kind_{kind}_{side}") * weight
        return total


# —— 从消息序列聚合 ——


def _accumulate_message(bucket: Metrics, message: Message) -> int:
    """把一条消息计入所在天的桶，返回它的「哈」连击长度。"""

    side = side_of(message.direction)
    bucket.add(f"msgs_{side}")

    kind = message.kind if f"kind_{message.kind}_them" in _METRIC_SET else "unknown"
    bucket.add(f"kind_{kind}_{side}")

    moment = datetime.fromtimestamp(message.timestamp)
    if moment.hour in NIGHT_HOURS:
        bucket.add(f"night_msgs_{side}")
    if moment.weekday() >= 5:
        bucket.add(f"weekend_msgs_{side}")

    if message.kind != "text":
        return 0

    features = analyze_text(message.text)
    bucket.add(f"chars_{side}", features.chars)
    if features.is_question:
        bucket.add(f"questions_{side}")
    if features.is_long:
        bucket.add(f"long_msgs_{side}")
    return features.laugh_run


def _accumulate_shape(bucket: Metrics, shape: ConversationShape) -> None:
    """把一段对话的结构指标计入它开始那天的桶。"""

    bucket.add("conversations")
    bucket.add(f"conv_started_{side_of(shape.starter)}")
    bucket.add(f"conv_ended_{side_of(shape.ender)}")
    bucket.add("turns_total", shape.turns)
    if shape.is_long:
        bucket.add("long_convs")
    for run in shape.runs:
        side = side_of(run.direction)
        bucket.add(f"runs_{side}")
        if run.count > 1:
            # 连着发多条而对方没插话 = 一次「追加」。
            bucket.add(f"runs_{side}_multi")
    for reply in shape.replies:
        bucket.add_reply(reply.direction, reply.delay)


@dataclass(slots=True)
class Aggregation:
    """一次聚合的产物：按天分桶的指标 + 需要写回联系人行的里程碑增量。"""

    buckets: dict[str, Metrics] = field(default_factory=dict)
    max_laugh_run: int = 0

    def bucket(self, timestamp: int) -> Metrics:
        key = day_key(timestamp)
        existing = self.buckets.get(key)
        if existing is None:
            existing = Metrics()
            self.buckets[key] = existing
        return existing


def aggregate(conversations: list[list[Message]]) -> Aggregation:
    """把若干段完整对话聚合成按天分桶的指标。

    单条消息计入它自己所在的那天；对话级指标（发起/结束/轮次/回复延迟）整体计入
    对话开始那天，这样一段跨零点的夜聊不会被拆成两段半截对话。
    """

    result = Aggregation()
    for messages in conversations:
        if not messages:
            continue
        for message in messages:
            laugh = _accumulate_message(result.bucket(message.timestamp), message)
            result.max_laugh_run = max(result.max_laugh_run, laugh)
        shape = describe(messages)
        _accumulate_shape(result.bucket(shape.start), shape)
    return result


def late_night_offset(timestamp: int) -> int:
    """凌晨消息距离当天 00:00 的秒数；不在凌晨窗口内返回 -1。

    「聊到最晚的一次」只在 00:00–05:59:59 之间比较（06:00 整点除外），
    否则 23:59 会永远压过 02:00。
    """

    moment = datetime.fromtimestamp(timestamp)
    if moment.hour >= LATE_NIGHT_MAX_HOUR:
        return -1
    return moment.hour * 3600 + moment.minute * 60 + moment.second


__all__ = [
    "Aggregation",
    "Metrics",
    "aggregate",
    "bucket_of",
    "day_key",
    "day_span",
    "decayed_span",
    "decayed_weight",
    "dump_histogram",
    "empty_histogram",
    "late_night_offset",
    "parse_histogram",
    "quantile",
    "side_of",
]
