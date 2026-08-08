from __future__ import annotations

import unittest

from wechat_insights.depth import LexicalDepth
from wechat_insights.metrics import Metrics
from wechat_insights.scoring import (
    DIMENSION_NAMES,
    detect_anomalies,
    median,
    percentile_rank,
    raw_metrics,
    reply_median,
    score_cohort,
)


STRATEGY = LexicalDepth()


def window(**counts: int) -> Metrics:
    metrics = Metrics()
    for name, value in counts.items():
        metrics.add(name, value)
    return metrics


class PercentileTests(unittest.TestCase):
    def test_rank_is_relative_to_the_cohort(self) -> None:
        cohort = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(percentile_rank(cohort, 0.5), 0.0)
        self.assertEqual(percentile_rank(cohort, 5.0), 100.0)
        self.assertEqual(percentile_rank(cohort, 2.0), 37.5)

    def test_ties_split_the_difference(self) -> None:
        self.assertEqual(percentile_rank([2.0, 2.0], 2.0), 50.0)

    def test_empty_cohort_is_neutral(self) -> None:
        self.assertEqual(percentile_rank([], 1.0), 50.0)

    def test_median_of_even_and_odd_lengths(self) -> None:
        self.assertEqual(median([3.0, 1.0, 2.0]), 2.0)
        self.assertEqual(median([1.0, 2.0, 3.0, 4.0]), 2.5)
        self.assertEqual(median([]), 50.0)


class RawMetricTests(unittest.TestCase):
    def test_absolute_volumes_are_normalised_per_day(self) -> None:
        values = raw_metrics(window(msgs_them=90, chars_them=900), STRATEGY, 90)
        self.assertAlmostEqual(values["msgs_them_per_day"], 1.0)
        self.assertAlmostEqual(values["chars_them_per_day"], 10.0)

    def test_reply_median_needs_enough_samples(self) -> None:
        self.assertIsNone(reply_median([1] + [0] * 23, 1))
        self.assertIsNotNone(reply_median([5] + [0] * 23, 5))

    def test_rates_are_none_when_the_denominator_is_zero(self) -> None:
        values = raw_metrics(window(msgs_them=1), STRATEGY, 30)
        self.assertIsNone(values["started_rate_them"])
        self.assertIsNone(values["avg_len_them"])

    def test_ratio_metrics_use_their_own_denominators(self) -> None:
        values = raw_metrics(
            window(
                conversations=10,
                conv_started_them=4,
                conv_ended_them=7,
                runs_them=20,
                runs_them_multi=5,
                turns_total=60,
                long_convs=2,
                kind_text_them=10,
                chars_them=300,
                questions_them=3,
                long_msgs_them=1,
            ),
            STRATEGY,
            30,
        )
        self.assertAlmostEqual(values["started_rate_them"], 0.4)
        self.assertAlmostEqual(values["ended_rate_them"], 0.7)
        self.assertAlmostEqual(values["followup_rate_them"], 0.25)
        self.assertAlmostEqual(values["avg_turns"], 6.0)
        self.assertAlmostEqual(values["long_conv_rate"], 0.2)
        self.assertAlmostEqual(values["avg_len_them"], 30.0)
        self.assertAlmostEqual(values["question_rate_them"], 0.3)


class CohortScoreTests(unittest.TestCase):
    def test_faster_replies_score_higher_on_responsiveness(self) -> None:
        fast = {"reply_delay_them": 30.0, "fast_rate_them": 0.9}
        slow = {"reply_delay_them": 3600.0, "fast_rate_them": 0.1}
        scores = score_cohort({"fast": fast, "slow": slow}, STRATEGY)
        self.assertGreater(
            scores["fast"]["responsiveness"], scores["slow"]["responsiveness"]
        )
        self.assertEqual(scores["fast"]["responsiveness"], 75.0)

    def test_missing_component_redistributes_its_weight(self) -> None:
        # 只有秒回率可用时，响应维度应该完全由秒回率的百分位决定。
        cohort = {
            "a": {"reply_delay_them": None, "fast_rate_them": 0.9},
            "b": {"reply_delay_them": None, "fast_rate_them": 0.1},
        }
        scores = score_cohort(cohort, STRATEGY)
        self.assertEqual(scores["a"]["responsiveness"], 75.0)
        self.assertEqual(scores["b"]["responsiveness"], 25.0)

    def test_dimension_without_any_data_is_neutral(self) -> None:
        scores = score_cohort({"a": {}}, STRATEGY)
        for name in DIMENSION_NAMES:
            self.assertEqual(scores["a"][name], 50.0)
        self.assertEqual(scores["a"]["overall"], 50.0)

    def test_overall_is_the_mean_of_the_five_dimensions(self) -> None:
        scores = score_cohort(
            {
                "a": {"msgs_them_per_day": 10.0},
                "b": {"msgs_them_per_day": 1.0},
            },
            STRATEGY,
        )
        expected = sum(scores["a"][name] for name in DIMENSION_NAMES) / 5
        self.assertAlmostEqual(scores["a"]["overall"], expected)


class AnomalyTests(unittest.TestCase):
    def test_reply_delay_blowout_is_reported_as_worse(self) -> None:
        samples = window(replies_them=20)
        anomalies = detect_anomalies(
            {"reply_delay_them": 2400.0},
            {"reply_delay_them": 240.0},
            samples,
            samples,
        )
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["direction"], "worse")
        self.assertEqual(anomalies[0]["before"], "4 分钟")
        self.assertEqual(anomalies[0]["after"], "40 分钟")

    def test_small_changes_are_ignored(self) -> None:
        samples = window(replies_them=20)
        self.assertEqual(
            detect_anomalies(
                {"reply_delay_them": 300.0},
                {"reply_delay_them": 240.0},
                samples,
                samples,
            ),
            [],
        )

    def test_thin_samples_are_ignored(self) -> None:
        samples = window(replies_them=2)
        self.assertEqual(
            detect_anomalies(
                {"reply_delay_them": 2400.0},
                {"reply_delay_them": 240.0},
                samples,
                samples,
            ),
            [],
        )

    def test_improvement_is_labelled_better(self) -> None:
        samples = window(replies_them=20)
        anomalies = detect_anomalies(
            {"fast_rate_them": 0.8}, {"fast_rate_them": 0.2}, samples, samples
        )
        self.assertEqual(anomalies[0]["direction"], "better")
        self.assertEqual(anomalies[0]["before"], "20%")
        self.assertEqual(anomalies[0]["after"], "80%")


if __name__ == "__main__":
    unittest.main()
