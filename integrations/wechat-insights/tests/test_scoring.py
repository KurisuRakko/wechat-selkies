from __future__ import annotations

import unittest

from wechat_insights.depth import LexicalDepth
from wechat_insights.metrics import Metrics
from wechat_insights.scoring import (
    DIMENSION_NAMES,
    anomalies_key,
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

    def test_recent_window_divides_by_its_actual_day_span(self) -> None:
        # 近期窗口 [D−30, D] 覆盖 31 个日键，除数必须用 31 而不是 30，
        # 否则 31 条消息会被算成日均 1.03 条。
        values = raw_metrics(window(msgs_them=31), STRATEGY, 31)
        self.assertAlmostEqual(values["msgs_them_per_day"], 1.0)

    def test_reply_median_needs_enough_samples(self) -> None:
        self.assertIsNone(reply_median([1] + [0] * 23, 1))
        self.assertIsNotNone(reply_median([5] + [0] * 23, 5))

    def test_rates_are_none_when_the_denominator_is_zero(self) -> None:
        values = raw_metrics(window(msgs_them=1), STRATEGY, 30)
        self.assertIsNone(values["started_rate_them"])
        self.assertIsNone(values["avg_len_them"])

    def test_balance_metrics_are_min_over_max(self) -> None:
        values = raw_metrics(
            window(
                msgs_them=3,
                msgs_me=6,
                chars_them=100,
                chars_me=400,
                conv_started_them=1,
                conv_started_me=3,
            ),
            STRATEGY,
            30,
        )
        self.assertAlmostEqual(values["balance_msgs"], 0.5)
        self.assertAlmostEqual(values["balance_chars"], 0.25)
        # 双方投入的下限 = min(100字/20, 400字/20) / 30：两边的绝对量级都
        # 参与，只有一边大不算共同投入。
        self.assertAlmostEqual(values["mutual_cost_per_day"], 5 / 30)
        # 旧原始值必须真的消失，不留死键。
        self.assertNotIn("balance_started", values)

    def test_extras_are_merged_into_the_raw_values(self) -> None:
        values = raw_metrics(
            window(msgs_them=1), STRATEGY, 30, extras={"active_day_rate": 0.4}
        )
        self.assertEqual(values["active_day_rate"], 0.4)
        # 不传 extras 时没有该键。
        self.assertNotIn("active_day_rate", raw_metrics(window(msgs_them=1), STRATEGY, 30))

    def test_days_accept_a_float_equivalent_span(self) -> None:
        values = raw_metrics(window(msgs_them=3), STRATEGY, 2.5)
        self.assertAlmostEqual(values["msgs_them_per_day"], 1.2)

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


class DimensionTests(unittest.TestCase):
    def test_dimension_names_have_all_seven(self) -> None:
        self.assertEqual(
            DIMENSION_NAMES,
            (
                "responsiveness",
                "initiative",
                "investment",
                "rhythm",
                "depth",
                "constancy",
                "reciprocity",
            ),
        )

    def test_constancy_dimension_without_any_data_is_neutral(self) -> None:
        # 不传 extras（recent/baseline 窗口就是如此）时恒常整维缺值退回 50，
        # 趋势恒为 0 是有意的。
        scores = score_cohort({"a": {}, "b": {}}, STRATEGY)
        self.assertEqual(scores["a"]["constancy"], 50.0)
        self.assertEqual(scores["b"]["constancy"], 50.0)
        self.assertEqual(scores["a"]["overall"], 50.0)

    def test_reciprocity_ranks_balanced_contacts_higher(self) -> None:
        # 两个联系人的四个新组成项方向一致，两人 cohort 百分位仍是 75/25；
        # 一边倒的联系人在每个原始值上都被压到低分位。
        balanced = {
            "mutual_cost_per_day": 8.0,
            "balance_msgs": 0.9,
            "balance_chars": 0.9,
            "llm_mutuality_score": 90.0,
        }
        one_sided = {
            "mutual_cost_per_day": 0.5,
            "balance_msgs": 0.1,
            "balance_chars": 0.1,
            "llm_mutuality_score": 10.0,
        }
        scores = score_cohort({"balanced": balanced, "one_sided": one_sided}, STRATEGY)
        self.assertGreater(
            scores["balanced"]["reciprocity"], scores["one_sided"]["reciprocity"]
        )
        self.assertEqual(scores["balanced"]["reciprocity"], 75.0)
        self.assertEqual(scores["one_sided"]["reciprocity"], 25.0)

    def test_reciprocity_rewards_mutual_volume_over_pure_ratio(self) -> None:
        # 本次修复的核心语义：同样都是「双向」，投入量级大的拿到更高分。
        # A：双方各 1000 成本、比例 0.8；B：双方各 10 成本、比例 1.0。
        # 两人 cohort 下体积项与比例项权重恰好对称（0.35 vs 0.35），纯靠
        # 这两个数字会精确抵消成平局，所以 LLM 双向性也按量级给（A 高、
        # B 低）——真实关系里「一起投入很多」的双向性本来就比「都投很少」
        # 更可信，这也正是 LLM 分放进对等维的意义。
        a = {
            "mutual_cost_per_day": 1000.0,
            "balance_msgs": 0.8,
            "balance_chars": 0.8,
            "llm_mutuality_score": 90.0,
        }
        b = {
            "mutual_cost_per_day": 10.0,
            "balance_msgs": 1.0,
            "balance_chars": 1.0,
            "llm_mutuality_score": 70.0,
        }
        scores = score_cohort({"a": a, "b": b}, STRATEGY)
        self.assertGreater(scores["a"]["reciprocity"], scores["b"]["reciprocity"])
        self.assertAlmostEqual(scores["a"]["reciprocity"], 57.5)
        self.assertAlmostEqual(scores["b"]["reciprocity"], 42.5)

    def test_reciprocity_without_llm_redistributes_to_three_components(self) -> None:
        # llm_mutuality_score 缺失时权重按 0.5 / 0.286 / 0.214 归一回流，
        # 这就是离线部署拿到的那份对等维修复——缺值机制的自然结果，不是特判。
        cohort = {
            "a": {
                "mutual_cost_per_day": 100.0,
                "balance_msgs": 0.9,
                "balance_chars": 0.9,
            },
            "b": {
                "mutual_cost_per_day": 1.0,
                "balance_msgs": 0.1,
                "balance_chars": 0.1,
            },
        }
        scores = score_cohort(cohort, STRATEGY)
        expected = 0.5 * 75 + (0.2 / 0.7) * 75 + (0.15 / 0.7) * 75
        self.assertAlmostEqual(scores["a"]["reciprocity"], expected)
        self.assertAlmostEqual(scores["b"]["reciprocity"], 25.0)

    def test_constancy_ranks_active_contacts_higher(self) -> None:
        steady = {"active_day_rate": 0.8, "current_gap_days": 1.0, "longest_gap_days": 3.0}
        fading = {"active_day_rate": 0.2, "current_gap_days": 120.0, "longest_gap_days": 100.0}
        scores = score_cohort({"steady": steady, "fading": fading}, STRATEGY)
        self.assertGreater(scores["steady"]["constancy"], scores["fading"]["constancy"])


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
        scores = score_cohort({"a": {}, "b": {}}, STRATEGY)
        for name in DIMENSION_NAMES:
            self.assertEqual(scores["a"][name], 50.0)
        self.assertEqual(scores["a"]["overall"], 50.0)

    def test_cohort_of_one_is_not_scored(self) -> None:
        # 只有一个人时没有任何「他人」可参照，百分位恒为 50，不构成测量，
        # 返回空表让上层按未打分处理。
        self.assertEqual(
            score_cohort({"a": {"msgs_them_per_day": 10.0}}, STRATEGY), {}
        )

    def test_overall_is_the_mean_of_the_seven_dimensions(self) -> None:
        scores = score_cohort(
            {
                "a": {"msgs_them_per_day": 10.0},
                "b": {"msgs_them_per_day": 1.0},
            },
            STRATEGY,
        )
        expected = sum(scores["a"][name] for name in DIMENSION_NAMES) / 7
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

    def test_anomalies_key_is_sorted_and_order_independent(self) -> None:
        # 同一组异动不管条目顺序如何，指纹必须一致；排序按 metric 不按
        # detect_anomalies 的展示顺序（worse 在前）。
        first = [
            {"metric": "msgs_them_per_day", "direction": "worse"},
            {"metric": "avg_len_them", "direction": "better"},
        ]
        second = [
            {"metric": "avg_len_them", "direction": "better"},
            {"metric": "msgs_them_per_day", "direction": "worse"},
        ]
        self.assertEqual(
            anomalies_key(first), "avg_len_them:better|msgs_them_per_day:worse"
        )
        self.assertEqual(anomalies_key(first), anomalies_key(second))

    def test_empty_anomalies_have_an_empty_key(self) -> None:
        self.assertEqual(anomalies_key([]), "")


if __name__ == "__main__":
    unittest.main()
