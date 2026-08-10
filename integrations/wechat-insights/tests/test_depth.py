from __future__ import annotations

import unittest
from unittest.mock import patch

from wechat_insights.depth import LLMDepth, LexicalDepth, get_depth_strategy
from wechat_insights.scoring import score_cohort


class LLMDepthStrategyTests(unittest.TestCase):
    def test_components_weights_sum_to_one(self) -> None:
        self.assertAlmostEqual(
            sum(component.weight for component in LLMDepth().components()), 1.0
        )

    def test_llm_components_keep_lexical_ratio_at_thirty_percent(self) -> None:
        llm_components = {c.metric: c.weight for c in LLMDepth().components()}
        lexical = {c.metric: c.weight for c in LexicalDepth().components()}
        for metric, weight in lexical.items():
            self.assertAlmostEqual(llm_components[metric], weight * 0.3)
        # 词法三项整体缩到 0.30（内部仍是 4:3:3），LLM 两项各 0.35、合计 0.7。
        self.assertAlmostEqual(
            sum(llm_components[metric] for metric in lexical), 0.3
        )
        self.assertAlmostEqual(llm_components["llm_depth_score"], 0.35)
        self.assertAlmostEqual(llm_components["llm_warmth_score"], 0.35)
        self.assertAlmostEqual(
            llm_components["llm_depth_score"] + llm_components["llm_warmth_score"],
            0.7,
        )
        self.assertEqual(
            set(llm_components),
            set(lexical) | {"llm_depth_score", "llm_warmth_score"},
        )

    def test_raw_metrics_delegate_to_lexical(self) -> None:
        self.assertEqual(
            LLMDepth().raw_metrics({}), LexicalDepth().raw_metrics({})
        )

    def test_llm_strategy_is_registered_by_name(self) -> None:
        # 不配 base_url 时 llm 会被保护逻辑回退，这里显式配上再查注册表。
        with patch(
            "wechat_insights.depth.INSIGHTS_LLM_BASE_URL", "http://llm.test/v1"
        ):
            self.assertIsInstance(get_depth_strategy("llm"), LLMDepth)

    def test_llm_without_base_url_falls_back_to_lexical(self) -> None:
        with patch("wechat_insights.depth.INSIGHTS_LLM_BASE_URL", ""):
            self.assertEqual(get_depth_strategy("llm").name, "lexical")

    def test_llm_with_base_url_is_kept(self) -> None:
        with patch(
            "wechat_insights.depth.INSIGHTS_LLM_BASE_URL", "http://llm.test/v1"
        ):
            self.assertEqual(get_depth_strategy("llm").name, "llm")

    def test_missing_llm_score_degrades_to_lexical_terms(self) -> None:
        # b 没有 LLM 分：LLM 两项的 0.7 权重回流给词法三项，深度维度仍可算。
        cohort = {
            "a": {
                "avg_len_them": 100.0,
                "question_rate_them": 0.8,
                "long_msg_rate_them": 0.8,
                "llm_depth_score": 90.0,
                "llm_warmth_score": 90.0,
            },
            "b": {
                "avg_len_them": 10.0,
                "question_rate_them": 0.2,
                "long_msg_rate_them": 0.2,
            },
        }
        scores = score_cohort(cohort, LLMDepth())
        self.assertIn("depth", scores["a"])
        self.assertIn("depth", scores["b"])
        self.assertGreater(scores["a"]["depth"], scores["b"]["depth"])

    def test_llm_score_dominates_when_lexical_terms_are_equal(self) -> None:
        # 词法三项完全相同的两人百分位相同（并列取中点 → 50），LLM 两项成为
        # 唯一分项，深度维度完全由它们决定。
        base = {
            "avg_len_them": 50.0,
            "question_rate_them": 0.5,
            "long_msg_rate_them": 0.5,
        }
        scores = score_cohort(
            {
                "deep": {
                    **base,
                    "llm_depth_score": 90.0,
                    "llm_warmth_score": 90.0,
                },
                "shallow": {
                    **base,
                    "llm_depth_score": 10.0,
                    "llm_warmth_score": 10.0,
                },
            },
            LLMDepth(),
        )
        # 词法三项并列取中点 → 50；LLM 两项 90 在 [90, 10] 里排 75 分位、
        # 10 排 25 分位。深度 = 0.3×50 + 0.7×75 = 67.5 / 0.3×50 + 0.7×25 = 32.5。
        self.assertAlmostEqual(scores["deep"]["depth"], 67.5)
        self.assertAlmostEqual(scores["shallow"]["depth"], 32.5)


if __name__ == "__main__":
    unittest.main()
