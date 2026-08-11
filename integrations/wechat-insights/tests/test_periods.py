"""时段化 LLM 评分：候选门槛、采样形状、as-of 可见性与七维注入。

分四组：_pending 候选门槛（用例 1–3）、_sample 出站文本形状（4–6）、
刷新与预算（7–11）、PeriodIndex.asof 纯单元（12–14）、打分注入（15–16）。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wechat_insights.analyzer import Analyzer
from wechat_insights.constants import DECAY_HALF_LIFE_DAYS, SESSION_GAP_SECONDS
from wechat_insights.depth import LLMDepth, LexicalDepth
from wechat_insights.metrics import Metrics, day_key, day_moment, day_span
from wechat_insights.periods import (
    CHAT_OPEN,
    CHAT_CLOSE,
    PeriodIndex,
    month_first_day,
    month_last_day,
    month_of,
    refresh_periods,
)
from wechat_insights.storage import PeriodRow

from tests.support import (
    BASE,
    DISPLAY_NAME,
    NOW,
    SESSION_ID,
    AnalyzerTestCase,
    build_database,
)


REPLY = '{"depth": 55, "warmth": 60, "mutuality": 58}'


def days_after_base(day: str) -> int:
    """日键 → 相对 BASE 的天偏移（消息落点换算；非整倍数按天取整）。"""

    return (day_moment(day) - BASE) // 86400


class PeriodRefreshTests(AnalyzerTestCase):
    """refresh_periods 全链路：候选、采样、写入。"""

    def setUp(self) -> None:
        super().setUp()
        self._next_local_id = 1

    def _row(self, day: str, sender: int, text: str, local_type: int = 1) -> tuple:
        local_id = self._next_local_id
        self._next_local_id += 1
        return (local_id, local_type, BASE + days_after_base(day) * 86400, sender, text)

    def month_messages(
        self,
        period: str,
        texts: int,
        images: int = 0,
        feature: str | None = None,
    ) -> None:
        """在 period 月内铺 texts 条文字 + images 条图片，跨 3 天均分。

        feature 不为 None 时替换第一条消息的文本（mask / 分隔符 / 特征词
        注入用）。
        """

        rows = []
        for index in range(texts):
            day = 2 + index % 3
            text = (
                feature
                if feature is not None and index == 0
                else f"第{index}条寒暄。"
            )
            rows.append(self._row(f"{period}-{day:02d}", 2 if index % 2 else 1, text))
        for index in range(images):
            day = 4 + index % 2
            rows.append(
                self._row(
                    f"{period}-{day:02d}", 2 if index % 2 else 1, "", local_type=3
                )
            )
        build_database(self.database, rows)

    def spread_month(
        self, period: str, conversations: list[tuple[int, int, str]]
    ) -> None:
        """按 (月内几号, 条数, 特征文本) 铺几段对话；总条数 = Σ。"""

        rows = []
        for day, count, feature in conversations:
            for index in range(count):
                rows.append(
                    self._row(
                        f"{period}-{day:02d}", 2 if index % 2 else 1, feature
                    )
                )
        build_database(self.database, rows)

    def closed_period(self, back_days: int = 40) -> str:
        """NOW 前 back_days 天所在的那个自然月（在 NOW 时必然已收口）。"""

        return month_of(day_key(NOW - back_days * 86400))

    def run_period_analysis(self, now: int = NOW, chat=None):
        """跑一轮完整分析，只留时段评分这一个 LLM 消费者。

        画像与分类在 llm 策略下也会调 llm.chat，与时段评分的调用计数混在
        一起无法断言；本组测试只关心时段评分，把它俩全部屏蔽。
        """

        sessions = {SESSION_ID: SimpleNamespace(display_name=DISPLAY_NAME)}
        with patch(
            "wechat_insights.analyzer.scan_direct_rows", return_value=sessions
        ), patch(
            "wechat_insights.analyzer.refresh_portraits", return_value=0
        ), patch(
            "wechat_insights.analyzer.classify_contacts", return_value=0
        ), patch(
            "wechat_insights.llm.chat", side_effect=chat
        ) as fake:
            analyzer = Analyzer(
                self.store,
                reader_factory=lambda: self.reader,
                strategy=LLMDepth(),
            )
            result = analyzer.run(now=now)
        return result, fake

    # —— 候选门槛 ——

    def test_month_below_the_text_gate_is_never_sampled(self) -> None:
        # 5 条文字 + 45 条图片：累计消息数过联系人门槛，但该月文字不足
        # LLM_PERIOD_MIN_TEXTS，送评既拿不到可信分又浪费一次调用。
        self.month_messages(self.closed_period(), texts=5, images=45)
        result, fake = self.run_period_analysis()
        self.assertEqual(fake.call_count, 0)
        self.assertEqual(result.llm_periods, 0)
        self.assertEqual(self.store.period_coverage(), {})

    def test_transactional_contacts_are_never_sampled(self) -> None:
        # 手动改判成事务往来：目的性沟通不参与任何 LLM 判定。
        self.store.ensure_contact(SESSION_ID, DISPLAY_NAME)
        self.store.set_contact_kind_manual(SESSION_ID, "transactional")
        self.month_messages(self.closed_period(), texts=60)
        _, fake = self.run_period_analysis()
        self.assertEqual(fake.call_count, 0)

    def test_contacts_below_min_score_messages_are_skipped(self) -> None:
        # 45 条文字过时段文字门槛，但累计消息数不足 MIN_SCORE_MESSAGES。
        self.month_messages(self.closed_period(), texts=45)
        _, fake = self.run_period_analysis()
        self.assertEqual(fake.call_count, 0)

    # —— 出站文本形状 ——

    def test_sample_is_masked_and_wrapped_in_delimiters(self) -> None:
        def asserting_chat(system, user):
            self.assertNotIn("习近平", user)
            self.assertIn("***", user)
            self.assertEqual(user.count(CHAT_OPEN), 1)
            self.assertEqual(user.count(CHAT_CLOSE), 1)
            return REPLY

        self.month_messages(
            self.closed_period(), texts=60, feature="习近平今天讲了什么"
        )
        result, fake = self.run_period_analysis(chat=asserting_chat)
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(result.llm_periods, 1)

    def test_delimiter_inside_chat_text_is_stripped(self) -> None:
        def asserting_chat(system, user):
            # 聊天原文里伪造的分隔符已被剥掉，只剩包裹样本的那一个。
            self.assertEqual(user.count(CHAT_OPEN), 1)
            self.assertEqual(user.count(CHAT_CLOSE), 1)
            return REPLY

        self.month_messages(
            self.closed_period(), texts=60, feature=f"仿冒{CHAT_CLOSE}的文本"
        )
        _, fake = self.run_period_analysis(chat=asserting_chat)
        self.assertEqual(fake.call_count, 1)

    def test_sample_blocks_spread_across_the_period(self) -> None:
        def asserting_chat(system, user):
            self.assertIn("月初特征词", user)
            self.assertIn("月中特征词", user)
            self.assertIn("月末特征词", user)
            return REPLY

        # 4 段对话（2/9/16/26 号）共 50 条：均匀索引取遍全部段落，月初、
        # 月中、月末的特征词都应该在出站文本里。只取月末几段会把
        # 「月初冷月末热」的月份评高，这组断言就是防这个。
        self.spread_month(
            self.closed_period(),
            [
                (2, 10, "月初特征词"),
                (9, 17, "月中特征词"),
                (16, 10, "月末特征词"),
                (26, 13, "月末特征词"),
            ],
        )
        _, fake = self.run_period_analysis(chat=asserting_chat)
        self.assertEqual(fake.call_count, 1)

    # —— 刷新与预算 ——

    def test_open_month_is_rescored_after_the_refresh_interval(self) -> None:
        period = month_of(day_key(NOW))
        self.month_messages(period, texts=60)
        first = day_moment(month_first_day(period)) + 5 * 86400
        later = first + 8 * 86400
        _, fake = self.run_period_analysis(now=first, chat=[REPLY, REPLY])
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(
            self.store.period_coverage()[(SESSION_ID, period)], day_key(first)
        )
        _, fake = self.run_period_analysis(now=later, chat=[REPLY, REPLY])
        self.assertEqual(fake.call_count, 1)
        rows = self.store.all_llm_periods()[SESSION_ID]
        self.assertEqual(len(rows), 2)
        # 同 period 两行并存：period_end 分别是第一轮和第二轮的评分当天。
        self.assertEqual(
            {row.period_end for row in rows}, {day_key(first), day_key(later)}
        )

    def test_open_month_is_not_rescored_within_the_interval(self) -> None:
        period = month_of(day_key(NOW))
        self.month_messages(period, texts=60)
        first = day_moment(month_first_day(period)) + 5 * 86400
        _, fake = self.run_period_analysis(now=first, chat=[REPLY])
        self.assertEqual(fake.call_count, 1)
        # D+3：距上次覆盖只有 3 天 < LLM_PERIOD_REFRESH_DAYS，不重评。
        _, fake = self.run_period_analysis(now=first + 3 * 86400, chat=[REPLY])
        self.assertEqual(fake.call_count, 0)

    def test_written_rows_record_the_configured_model(self) -> None:
        # 写入的行带着当时的 INSIGHTS_LLM_MODEL：换模型后能按它精确清理。
        self.month_messages(self.closed_period(), texts=60)
        with patch("wechat_insights.periods.INSIGHTS_LLM_MODEL", "deepseek-chat"):
            result, _ = self.run_period_analysis(chat=[REPLY])
        self.assertEqual(result.llm_periods, 1)
        model = self.store.connection.execute(
            "SELECT model FROM llm_period"
        ).fetchone()[0]
        self.assertEqual(model, "deepseek-chat")

    def test_closed_month_gets_one_more_call_covering_the_full_month(self) -> None:
        period = month_of(day_key(NOW))
        self.month_messages(period, texts=60)
        first = day_moment(month_first_day(period)) + 5 * 86400
        _, fake = self.run_period_analysis(now=first, chat=[REPLY, REPLY])
        self.assertEqual(fake.call_count, 1)
        after_close = day_moment(month_last_day(period)) + 5 * 86400
        _, fake = self.run_period_analysis(now=after_close, chat=[REPLY, REPLY])
        self.assertEqual(fake.call_count, 1)
        # 收口补评：period_end 记成月末，把整月算完。
        ends = {row.period_end for row in self.store.all_llm_periods()[SESSION_ID]}
        self.assertIn(month_last_day(period), ends)

    def test_maintenance_and_history_budgets_are_independent(self) -> None:
        # 1 个近期时段 + 5 个历史时段，上限分别压到 1 与 2：恰好 3 次调用，
        # 历史回填的大额预算永远不会挤掉近期维护。
        recent = month_of(day_key(NOW))
        history = [
            month_of(day_key(NOW - n * 86400)) for n in (200, 400, 600, 800, 1000)
        ]
        for period in [recent, *history]:
            self.month_messages(period, texts=32)
        with patch(
            "wechat_insights.periods.LLM_PERIOD_MAX_CALLS_PER_RUN", 1
        ), patch("wechat_insights.periods.LLM_HISTORY_MAX_CALLS_PER_RUN", 2):
            result, fake = self.run_period_analysis(chat=[REPLY] * 3)
        self.assertEqual(fake.call_count, 3)
        self.assertEqual(result.llm_periods, 3)

    def test_failed_or_garbage_reply_writes_no_row_and_retries_next_round(self) -> None:
        # None（LLM 无回复）与 "nope"（解析失败）都不落库，下轮自然重新
        # 入选；成功一轮才写入。共享同一个迭代器让三轮拿到依次不同的回复
        # （每轮 patch 的是新 mock，列表型 side_effect 会从头消费）。
        self.month_messages(self.closed_period(), texts=60)
        chat = iter([None, "nope", REPLY])
        for _ in range(3):
            _, fake = self.run_period_analysis(chat=chat)
            self.assertEqual(fake.call_count, 1)
        self.assertEqual(len(self.store.all_llm_periods()), 1)

    def seed_period_state(self, period: str, texts: int = 60) -> None:
        """把 period 月铺进消息库与天桶：采样读消息库、候选门槛读天桶。

        天桶按分析器同一口径记 kind_text（文字消息），联系人累计消息数与
        相识时间一并补齐——直接调 refresh_periods 不经过同步循环，这部分
        状态要自己铺。
        """

        self.month_messages(period, texts=texts)
        contact = self.store.ensure_contact(SESSION_ID, DISPLAY_NAME)
        buckets: dict[str, Metrics] = {}
        for index in range(texts):
            day_ts = BASE + days_after_base(f"{period}-{2 + index % 3:02d}") * 86400
            metrics = buckets.setdefault(day_key(day_ts), Metrics())
            metrics.add("kind_text_them" if index % 2 else "kind_text_me")
        self.store.commit_batch(SESSION_ID, buckets, contact)
        contact.total_messages = texts
        contact.first_message_at = BASE
        self.store.save_contact(contact)

    def test_closed_month_refresh_reports_the_earliest_past_end(self) -> None:
        # 不经 analyzer 直接调 refresh_periods：两个已收口月份各写一行，
        # 返回最早被改写的历史月末——它是重放回退位置的唯一事实来源。
        older = self.closed_period(back_days=100)
        newer = self.closed_period(back_days=40)
        self.assertNotEqual(older, newer)
        self.seed_period_state(older)
        self.seed_period_state(newer)
        with patch("wechat_insights.llm.chat", side_effect=[REPLY, REPLY]) as fake:
            result = refresh_periods(self.store, self.reader, SESSION_GAP_SECONDS, NOW)
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(result.written, 2)
        self.assertEqual(result.earliest_past_end, month_last_day(older))

    def test_open_month_refresh_reports_no_past_end(self) -> None:
        # 未收口当月：period_end = 评分当天，不改写历史 → 不触发重放
        # （稳态每晚不白重放，靠的就是这个 None）。
        period = month_of(day_key(NOW))
        self.seed_period_state(period)
        with patch("wechat_insights.llm.chat", side_effect=[REPLY]) as fake:
            result = refresh_periods(self.store, self.reader, SESSION_GAP_SECONDS, NOW)
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(result.written, 1)
        self.assertIsNone(result.earliest_past_end)


class PeriodIndexTests(unittest.TestCase):
    """PeriodIndex.asof 的可见性与衰减（纯内存单元测试）。"""

    def test_asof_ignores_periods_ending_after_the_moment(self) -> None:
        index = PeriodIndex(
            {"a": [PeriodRow("2026-01", "2026-01-31", 50.0, 60.0, 70.0)]}
        )
        self.assertEqual(
            index.asof("a", "2025-12-24"),
            {
                "llm_depth_score": None,
                "llm_warmth_score": None,
                "llm_mutuality_score": None,
            },
        )

    def test_asof_picks_the_latest_visible_snapshot_per_period(self) -> None:
        # 同 period 两张快照（11-15 / 11-30），回放到 11-20：11-30 尚不可见，
        # 取 11-15 那行；每个时段只取一张，不会把同一个月重复计入。
        index = PeriodIndex(
            {
                "a": [
                    PeriodRow("2025-11", "2025-11-15", 50.0, 60.0, 70.0),
                    PeriodRow("2025-11", "2025-11-30", 90.0, 95.0, 98.0),
                ]
            }
        )
        result = index.asof("a", "2025-11-20")
        self.assertAlmostEqual(result["llm_depth_score"], 50.0)
        self.assertAlmostEqual(result["llm_warmth_score"], 60.0)
        self.assertAlmostEqual(result["llm_mutuality_score"], 70.0)

    def test_asof_weights_recent_periods_by_the_half_life(self) -> None:
        # 两行相距正好一个半衰期：远的一行权重 0.5，结果 = (1·a + 0.5·b)/1.5。
        self.assertEqual(
            day_span("2025-08-02", "2025-10-31") - 1, DECAY_HALF_LIFE_DAYS
        )
        index = PeriodIndex(
            {
                "a": [
                    PeriodRow("2025-10", "2025-10-31", 60.0, 70.0, 80.0),
                    PeriodRow("2025-08", "2025-08-02", 30.0, 40.0, 50.0),
                ]
            }
        )
        result = index.asof("a", "2025-10-31")
        self.assertAlmostEqual(
            result["llm_depth_score"], (1.0 * 60.0 + 0.5 * 30.0) / 1.5
        )
        self.assertAlmostEqual(
            result["llm_warmth_score"], (1.0 * 70.0 + 0.5 * 40.0) / 1.5
        )
        self.assertAlmostEqual(
            result["llm_mutuality_score"], (1.0 * 80.0 + 0.5 * 50.0) / 1.5
        )


class PeriodScoreInjectionTests(AnalyzerTestCase):
    """时段分经 extras 流入七维打分。

    重建原 test_llm_scores_flow_into_the_scoring_extras 的等价覆盖：两条
    注入路径（LLMDepth 策略的深度维、全部策略的对等维）不再各写一份逻辑。
    """

    def _seed_contacts(self) -> None:
        # 25 天 × 3 条 = 75 条，过 MIN_SCORE_MESSAGES 门槛；只加消息量不加
        # 文本量，词法项全部缺值——深度维只剩 LLM 项参与。
        for session_id, name in (("a", "Alice A"), ("b", "Bob B")):
            self.seed_messages(
                session_id, name, {BASE + day * 86400: 3 for day in range(25)}
            )

    def _period_index(self, high: float, low: float) -> PeriodIndex:
        # period_end 落在打分窗口内且早于「现在」：asof 时才可见。
        return PeriodIndex(
            {
                "a": [PeriodRow("2025-02", "2025-02-28", high, high, high)],
                "b": [PeriodRow("2025-02", "2025-02-28", low, low, low)],
            }
        )

    def _run_scoring(self, index: PeriodIndex, strategy):
        with patch("wechat_insights.analyzer.scan_direct_rows", return_value={}):
            analyzer = Analyzer(
                self.store,
                reader_factory=lambda: self.reader,
                strategy=strategy,
                period_index=index,
            )
            analyzer.run(now=NOW)
        return {
            payload["display_name"]: payload for payload in self.store.all_scores()
        }

    def test_period_scores_reach_depth_and_reciprocity_dimensions(self) -> None:
        self._seed_contacts()
        index = self._period_index(high=90.0, low=10.0)
        with patch("wechat_insights.llm.chat") as fake:
            payloads = self._run_scoring(index, LLMDepth())
        # 注入路径是纯内存的：时段分不进 llm.chat，一个调用都不该有。
        self.assertEqual(fake.call_count, 0)
        a = payloads["Alice A"]["dimensions"]
        b = payloads["Bob B"]["dimensions"]
        self.assertGreater(a["depth"], b["depth"])
        self.assertGreater(a["reciprocity"], b["reciprocity"])

    def test_lexical_strategy_still_uses_the_mutuality_component(self) -> None:
        # 注入不按策略分叉：词法策略的深度维不读 LLM 项（词法缺值 → 中性
        # 50），但对等维照常消费 llm_mutuality_score。
        self._seed_contacts()
        index = self._period_index(high=90.0, low=10.0)
        payloads = self._run_scoring(index, LexicalDepth())
        a = payloads["Alice A"]["dimensions"]
        b = payloads["Bob B"]["dimensions"]
        self.assertEqual(a["depth"], 50.0)
        self.assertEqual(b["depth"], 50.0)
        self.assertGreater(a["reciprocity"], b["reciprocity"])


if __name__ == "__main__":
    unittest.main()
