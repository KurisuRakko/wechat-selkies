"""按天聚合的指标累加器与回复延迟直方图。

指标全部是可加的：跨天、跨窗口合成只需要逐项相加，所以「按天存储、展示时滚动
合成」不需要任何额外的中间表。回复延迟这种需要分位数的量存成对数直方图，
同样可以逐桶相加，代价是分位数带一点桶内插值误差。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime

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


# —— 指标累加器 ——


@dataclass(slots=True)
class Metrics:
    """一个（联系人, 时间桶）下的全部可加指标。"""

    counts: dict[str, int] = field(default_factory=dict)
    reply_hist_them: list[int] = field(default_factory=empty_histogram)
    reply_hist_me: list[int] = field(default_factory=empty_histogram)

    def add(self, name: str, value: int = 1) -> None:
        if name not in _METRIC_SET:
            raise KeyError(f"unknown metric column: {name}")
        self.counts[name] = self.counts.get(name, 0) + value

    def get(self, name: str) -> int:
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

    def is_empty(self) -> bool:
        return not any(self.counts.values())

    def messages_total(self) -> int:
        return self.get("msgs_them") + self.get("msgs_me")

    def cost(self, side: str) -> float:
        """按类型加权的「投入成本」，文字按字数折算。"""

        total = self.get(f"chars_{side}") / TEXT_CHARS_PER_COST_UNIT
        for kind, weight in COST_WEIGHTS.items():
            total += self.get(f"kind_{kind}_{side}") * weight
        return total


def merged(chunks: list[Metrics]) -> Metrics:
    """把若干个桶合成一个。"""

    result = Metrics()
    for chunk in chunks:
        result.merge(chunk)
    return result


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

    「聊到最晚的一次」只在 00:00–06:00 之间比较，否则 23:59 会永远压过 02:00。
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
    "dump_histogram",
    "empty_histogram",
    "late_night_offset",
    "merged",
    "parse_histogram",
    "quantile",
    "side_of",
]
