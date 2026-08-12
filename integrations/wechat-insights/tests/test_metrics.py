from __future__ import annotations

import unittest
from datetime import datetime

from wechat_insights.conversation import ME, THEM, Message
from wechat_insights.metrics import (
    Metrics,
    aggregate,
    bucket_of,
    day_key,
    day_span,
    decayed_span,
    decayed_weight,
    late_night_offset,
    quantile,
)


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    """按容器本地时区构造时间戳，避免测试依赖具体时区。"""

    return int(datetime(year, month, day, hour, minute).timestamp())


def message(
    timestamp: int, direction: str, text: str = "", kind: str = "text"
) -> Message:
    return Message(
        timestamp=timestamp,
        local_id=timestamp,
        direction=direction,
        kind=kind,
        text=text,
    )


class HistogramTests(unittest.TestCase):
    def test_bucket_covers_a_power_of_two_range(self) -> None:
        self.assertEqual(bucket_of(0), 0)
        self.assertEqual(bucket_of(1), 0)
        self.assertEqual(bucket_of(2), 1)
        self.assertEqual(bucket_of(3), 1)
        self.assertEqual(bucket_of(4), 2)

    def test_very_long_delays_saturate_in_the_last_bucket(self) -> None:
        self.assertEqual(bucket_of(10**9), bucket_of(10**12))

    def test_empty_histogram_has_no_quantile(self) -> None:
        self.assertIsNone(quantile([0] * 24, 0.5))

    def test_median_interpolates_inside_the_bucket(self) -> None:
        histogram = [0] * 24
        histogram[5] = 10  # 全部样本落在 [32, 64) 秒
        value = quantile(histogram, 0.5)
        self.assertIsNotNone(value)
        self.assertAlmostEqual(value, 2**5.5, places=6)

    def test_median_lands_in_the_bucket_holding_the_middle_sample(self) -> None:
        histogram = [0] * 24
        histogram[0] = 9  # 9 个 1 秒级
        histogram[10] = 1  # 1 个很慢的
        value = quantile(histogram, 0.5)
        self.assertLess(value, 2.0)


class DaySpanTests(unittest.TestCase):
    def test_day_span_counts_both_ends(self) -> None:
        self.assertEqual(day_span("2026-03-01", "2026-03-31"), 31)
        self.assertEqual(day_span("2026-03-01", "2026-03-01"), 1)

    def test_rolling_window_spans_match_the_configuration(self) -> None:
        # 「近 30 天」窗口从 30 天前的同一时刻起算，日键含首尾共 31 个；
        # 打分窗口 91 个、基线窗口 90 个。日均的除数必须按实际日键数来，
        # 否则近期窗口的日均比基线凭空大 31/30。
        moment = at(2026, 3, 31, 12)
        self.assertEqual(day_span(day_key(moment - 30 * 86400), day_key(moment)), 31)
        self.assertEqual(day_span(day_key(moment - 90 * 86400), day_key(moment)), 91)
        self.assertEqual(
            day_span(day_key(moment - 120 * 86400), day_key(moment - 31 * 86400)),
            90,
        )


class AggregateTests(unittest.TestCase):
    def test_messages_land_in_their_own_day_bucket(self) -> None:
        first = at(2026, 3, 10, 12)
        second = at(2026, 3, 11, 12)
        result = aggregate([[message(first, THEM)], [message(second, ME)]])
        self.assertEqual(
            sorted(result.buckets), sorted({day_key(first), day_key(second)})
        )
        self.assertEqual(result.buckets[day_key(first)].get("msgs_them"), 1)
        self.assertEqual(result.buckets[day_key(second)].get("msgs_me"), 1)

    def test_conversation_stats_belong_to_the_day_it_started(self) -> None:
        start = at(2026, 3, 10, 23, 30)
        end = at(2026, 3, 11, 0, 30)
        result = aggregate([[message(start, THEM), message(end, ME)]])
        opening = result.buckets[day_key(start)]
        closing = result.buckets[day_key(end)]
        self.assertEqual(opening.get("conversations"), 1)
        self.assertEqual(opening.get("conv_started_them"), 1)
        self.assertEqual(opening.get("conv_ended_me"), 1)
        self.assertEqual(closing.get("conversations"), 0)

    def test_reply_delay_and_fast_reply_are_attributed_to_the_replier(self) -> None:
        start = at(2026, 3, 10, 12)
        result = aggregate(
            [[message(start, ME), message(start + 30, THEM), message(start + 600, ME)]]
        )
        bucket = result.buckets[day_key(start)]
        self.assertEqual(bucket.get("replies_them"), 1)
        self.assertEqual(bucket.get("fast_replies_them"), 1)
        self.assertEqual(bucket.get("replies_me"), 1)
        self.assertEqual(bucket.get("fast_replies_me"), 0)

    def test_consecutive_messages_count_as_one_run_and_one_followup(self) -> None:
        start = at(2026, 3, 10, 12)
        result = aggregate(
            [
                [
                    message(start, THEM),
                    message(start + 10, THEM),
                    message(start + 20, ME),
                ]
            ]
        )
        bucket = result.buckets[day_key(start)]
        self.assertEqual(bucket.get("runs_them"), 1)
        self.assertEqual(bucket.get("runs_them_multi"), 1)
        self.assertEqual(bucket.get("runs_me"), 1)
        self.assertEqual(bucket.get("runs_me_multi"), 0)
        self.assertEqual(bucket.get("turns_total"), 2)

    def test_night_and_weekend_flags_follow_local_time(self) -> None:
        # 2026-03-14 是周六，23:30 落在深夜窗口。
        weekend_night = at(2026, 3, 14, 23, 30)
        result = aggregate([[message(weekend_night, THEM)]])
        bucket = result.buckets[day_key(weekend_night)]
        self.assertEqual(bucket.get("night_msgs_them"), 1)
        self.assertEqual(bucket.get("weekend_msgs_them"), 1)

    def test_non_text_placeholder_never_counts_as_characters(self) -> None:
        start = at(2026, 3, 10, 12)
        result = aggregate(
            [
                [
                    message(start, THEM, "[图片]", kind="image"),
                    message(start + 5, THEM, "在吗？", kind="text"),
                ]
            ]
        )
        bucket = result.buckets[day_key(start)]
        self.assertEqual(bucket.get("chars_them"), 3)
        self.assertEqual(bucket.get("questions_them"), 1)
        self.assertEqual(bucket.get("kind_image_them"), 1)
        self.assertEqual(bucket.get("kind_text_them"), 1)

    def test_unknown_kind_falls_back_to_the_unknown_column(self) -> None:
        start = at(2026, 3, 10, 12)
        result = aggregate([[message(start, ME, kind="quote")]])
        self.assertEqual(result.buckets[day_key(start)].get("kind_unknown_me"), 1)

    def test_cost_weights_voice_and_calls_above_plain_text(self) -> None:
        start = at(2026, 3, 10, 12)
        result = aggregate(
            [
                [
                    message(start, THEM, "字" * 40),
                    message(start + 5, THEM, "[通话]", kind="call"),
                ]
            ]
        )
        # 40 字 = 2 个成本单位，一次通话 = 20。
        self.assertAlmostEqual(result.buckets[day_key(start)].cost("them"), 22.0)

    def test_longest_laugh_run_is_reported_for_the_batch(self) -> None:
        start = at(2026, 3, 10, 12)
        result = aggregate(
            [[message(start, THEM, "哈哈哈"), message(start + 5, ME, "哈哈哈哈哈")]]
        )
        self.assertEqual(result.max_laugh_run, 5)


class MergeWeightedTests(unittest.TestCase):
    def test_counts_and_histograms_are_scaled_by_the_weight(self) -> None:
        base = Metrics()
        base.add("msgs_them", 10)
        base.reply_hist_them[5] = 4

        weighted = Metrics()
        weighted.merge_weighted(base, 0.5)
        weighted.merge_weighted(base, 0.25)

        self.assertEqual(weighted.get("msgs_them"), 7.5)
        self.assertEqual(weighted.reply_hist_them[5], 3.0)
        self.assertEqual(weighted.reply_hist_me[5], 0.0)

    def test_missing_counts_stay_zero(self) -> None:
        weighted = Metrics()
        weighted.merge_weighted(Metrics(), 0.5)
        self.assertEqual(weighted.get("msgs_me"), 0.0)
        self.assertEqual(sum(weighted.reply_hist_them), 0.0)


class DecayTests(unittest.TestCase):
    def test_today_has_full_weight(self) -> None:
        self.assertEqual(decayed_weight(0, 90), 1.0)
        self.assertEqual(decayed_span(1, 90), 1.0)

    def test_weight_halves_at_the_half_life(self) -> None:
        self.assertAlmostEqual(decayed_weight(90, 90), 0.5)

    def test_equivalent_span_is_the_weighted_sum(self) -> None:
        self.assertAlmostEqual(decayed_span(3, 90), 1.0 + 0.5 ** (1 / 90) + 0.5 ** (2 / 90))


class LateNightTests(unittest.TestCase):
    def test_only_the_early_morning_window_counts_as_late(self) -> None:
        self.assertEqual(late_night_offset(at(2026, 3, 10, 23, 30)), -1)
        self.assertEqual(late_night_offset(at(2026, 3, 10, 2, 5)), 2 * 3600 + 5 * 60)

    def test_later_early_morning_beats_earlier(self) -> None:
        self.assertGreater(
            late_night_offset(at(2026, 3, 10, 4, 0)),
            late_night_offset(at(2026, 3, 10, 1, 0)),
        )


if __name__ == "__main__":
    unittest.main()
