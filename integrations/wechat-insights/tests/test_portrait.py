from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.support import (
    AnalyzerTestCase,
    BASE,
    DISPLAY_NAME,
    NOW,
    SESSION_ID,
    FakeReader,
    build_database,
    me,
    them,
)
from wechat_insights.depth import LexicalDepth, LLMDepth
from wechat_insights.periods import PeriodRefresh


class PortraitRefreshTests(AnalyzerTestCase):
    """LLM 关系画像的采样、屏蔽、缓存与刷新行为（假 reader + 假 llm.chat）。"""

    def setUp(self) -> None:
        super().setUp()
        # 建一个空的真实分片表：增量读取会打开这个文件，空文件会被当成坏库。
        build_database(self.database, [])

    def run_llm_analysis(self, now: int = NOW, chat=None):
        """以 llm 深度策略跑一轮，返回 (result, 被 patch 的 llm.chat)。"""

        sessions = {SESSION_ID: SimpleNamespace(display_name=DISPLAY_NAME)}
        with patch(
            "wechat_insights.analyzer.scan_direct_rows", return_value=sessions
        ), patch("wechat_insights.llm.chat", side_effect=chat) as fake:
            result = self.analyzer(strategy=LLMDepth()).run(now=now)
        return result, fake

    def test_new_contact_gets_a_portrait_on_first_round(self) -> None:
        build_database(
            self.database,
            [
                them(1, 0, "今天过得怎么样"),
                me(2, 60, "还行"),
                them(3, 120, "最近看了本好书"),
            ],
        )
        result, fake = self.run_llm_analysis(
            chat=lambda system, user: '{"summary": "你们最近聊了日常与近况。"}'
        )
        self.assertEqual(result.llm_scored, 1)
        self.assertEqual(fake.call_count, 1)
        row = self.store.get_llm_depth(SESSION_ID)
        self.assertIsNotNone(row)
        # 打分缓存列已整体移除：画像行里只有摘要、标签与解释。
        self.assertFalse(hasattr(row, "score"))
        self.assertEqual((row.scored_at, row.total_messages), (NOW, 3))
        self.assertEqual(row.summary, "你们最近聊了日常与近况。")

    def test_sample_text_is_masked_before_being_sent(self) -> None:
        build_database(
            self.database,
            [them(1, 0, "习近平今天讲了什么"), me(2, 60, "不知道")],
        )

        def chat(system: str, user: str) -> str:
            # 种子词必须以星号出现，原文绝不能出容器。
            self.assertNotIn("习近平", user)
            self.assertIn("***", user)
            return '{"summary": "你们聊了时事。"}'

        result, _ = self.run_llm_analysis(chat=chat)
        self.assertEqual(result.llm_scored, 1)

    def test_fresh_cache_within_ttl_and_under_message_threshold_skips(self) -> None:
        # 第一轮必须带 tags：tags 缺失（None）本身就是重评条件，会让第二轮
        # 合法地再调一次，就测不到「保鲜期内跳过」了。
        build_database(self.database, [them(1, 0), me(2, 60)])
        self.run_llm_analysis(
            chat=lambda system, user: '{"summary": "你们聊了游戏。", "tags": ["游戏"]}'
        )

        def unexpected(system: str, user: str) -> str:
            raise AssertionError("保鲜期内且消息增量不足，不该再调用 LLM")

        result, fake = self.run_llm_analysis(now=NOW + 5 * 86400, chat=unexpected)
        self.assertEqual(result.llm_scored, 0)
        self.assertEqual(fake.call_count, 0)

    def test_failed_chat_call_does_not_write_cache(self) -> None:
        build_database(self.database, [them(1, 0, "你好呀"), me(2, 60, "你好")])
        result, _ = self.run_llm_analysis(chat=lambda system, user: None)
        self.assertEqual(result.llm_scored, 0)
        self.assertIsNone(self.store.get_llm_depth(SESSION_ID))

    def test_garbage_reply_skips_without_writing_cache(self) -> None:
        build_database(self.database, [them(1, 0, "你好呀"), me(2, 60, "你好")])
        result, _ = self.run_llm_analysis(
            chat=lambda system, user: "这不是 JSON"
        )
        self.assertEqual(result.llm_scored, 0)
        self.assertIsNone(self.store.get_llm_depth(SESSION_ID))

    def test_max_calls_per_run_truncates_the_candidates(self) -> None:
        shards: dict = {}
        for index, session_id in enumerate(("c1", "c2", "c3")):
            path = self.root / f"message_{index}.db"
            build_database(
                path,
                [them(1, 0, "你好呀"), me(2, 60, "你好")],
                session_id=session_id,
            )
            shards[f"message/message_{index}.db"] = path
        self.reader = FakeReader(shards)
        sessions = {
            session_id: SimpleNamespace(display_name=session_id.upper())
            for session_id in ("c1", "c2", "c3")
        }
        with patch(
            "wechat_insights.analyzer.scan_direct_rows", return_value=sessions
        ), patch(
            "wechat_insights.llm.chat",
            side_effect=lambda s, u: '{"summary": "日常寒暄。"}',
        ) as fake, patch("wechat_insights.portrait.LLM_MAX_CALLS_PER_RUN", 2):
            result = self.analyzer(strategy=LLMDepth()).run(now=NOW)
        self.assertEqual(result.llm_scored, 2)
        self.assertEqual(fake.call_count, 2)
        cached = [
            session_id
            for session_id in ("c1", "c2", "c3")
            if self.store.get_llm_depth(session_id) is not None
        ]
        # 单轮只评上限内的前两个，第三个留到下一轮。
        self.assertEqual(cached, ["c1", "c2"])

    def test_full_reply_populates_cache_and_payload(self) -> None:
        rows = []
        for index in range(1, 21):
            offset = index * 120
            rows.append(them(index, offset) if index % 2 else me(index, offset))
        build_database(self.database, rows)
        # 百分位至少需要两个联系人做参照，第二个直接写库。
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        def chat(system: str, user: str) -> str:
            return (
                '{"summary": "你们最近聊工作与近况，相处轻松自然。", '
                '"anomaly_note": "可能是最近见面变少了"}'
            )

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            result, fake = self.run_llm_analysis(chat=chat)
        self.assertEqual(result.llm_scored, 1)
        self.assertEqual(fake.call_count, 1)
        row = self.store.get_llm_depth(SESSION_ID)
        self.assertIsNotNone(row)
        self.assertFalse(hasattr(row, "score"))
        self.assertEqual(row.summary, "你们最近聊工作与近况，相处轻松自然。")
        self.assertEqual(row.anomaly_note, "可能是最近见面变少了")
        payload = {
            p["display_name"]: p for p in self.store.all_scores()
        }[DISPLAY_NAME]
        self.assertTrue(payload["scored"])
        self.assertEqual(
            payload["llm_summary"], "你们最近聊工作与近况，相处轻松自然。"
        )
        self.assertEqual(payload["llm_summary_at"], NOW)

    def test_tags_flow_into_cache_and_payload(self) -> None:
        rows = []
        for index in range(1, 21):
            offset = index * 120
            rows.append(them(index, offset) if index % 2 else me(index, offset))
        build_database(self.database, rows)
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        def chat(system: str, user: str) -> str:
            return (
                '{"summary": "你们最近聊工作与近况。",'
                ' "tags": ["工作吐槽", "深夜谈心"]}'
            )

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            result, _ = self.run_llm_analysis(chat=chat)
        self.assertEqual(result.llm_scored, 1)
        row = self.store.get_llm_depth(SESSION_ID)
        self.assertEqual(row.tags, ["工作吐槽", "深夜谈心"])
        payload = {
            p["display_name"]: p for p in self.store.all_scores()
        }[DISPLAY_NAME]
        self.assertEqual(payload["llm_tags"], ["工作吐槽", "深夜谈心"])

    def test_empty_tags_stay_an_empty_list_in_cache_and_payload(self) -> None:
        rows = []
        for index in range(1, 21):
            offset = index * 120
            rows.append(them(index, offset) if index % 2 else me(index, offset))
        build_database(self.database, rows)
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            result, _ = self.run_llm_analysis(
                chat=lambda s, u: '{"summary": "你们最近聊了日常。", "tags": []}'
            )
        self.assertEqual(result.llm_scored, 1)
        row = self.store.get_llm_depth(SESSION_ID)
        self.assertEqual(row.tags, [])
        payload = {
            p["display_name"]: p for p in self.store.all_scores()
        }[DISPLAY_NAME]
        self.assertEqual(payload["llm_tags"], [])

    def test_missing_or_non_list_tags_are_normalised_to_none(self) -> None:
        rows = []
        for index in range(1, 21):
            offset = index * 120
            rows.append(them(index, offset) if index % 2 else me(index, offset))
        build_database(self.database, rows)
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        # 缺 tags 字段 / 给了字符串：都归一成 None。None 会触发下一轮重评
        # 补齐，所以每轮都真的调了 LLM（llm_scored 仍为 1，只是没有标签）。
        for reply in (
            '{"summary": "你们最近聊了日常。"}',
            '{"summary": "你们最近聊了日常。", "tags": "游戏"}',
        ):
            with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
                result, _ = self.run_llm_analysis(chat=lambda s, u: reply)
            self.assertEqual(result.llm_scored, 1)
        self.assertIsNone(self.store.get_llm_depth(SESSION_ID).tags)

    def test_overlong_and_non_string_tags_are_truncated_to_four(self) -> None:
        rows = []
        for index in range(1, 21):
            offset = index * 120
            rows.append(them(index, offset) if index % 2 else me(index, offset))
        build_database(self.database, rows)
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        reply = (
            '{"summary": "你们最近聊了日常。", "tags": ["超长标签超过八个字",'
            ' "游戏", "深夜谈心", "工作吐槽", "电影", 42]}'
        )
        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            result, _ = self.run_llm_analysis(chat=lambda s, u: reply)
        self.assertEqual(result.llm_scored, 1)
        row = self.store.get_llm_depth(SESSION_ID)
        # 超长截到 8 字、非字符串丢弃、最多保留 4 个（第 5 个之后的直接不管）。
        self.assertEqual(
            row.tags, ["超长标签超过八个", "游戏", "深夜谈心", "工作吐槽"]
        )

    def test_missing_tags_trigger_rescore_on_the_next_round(self) -> None:
        # 第一轮模型没给 tags：缓存行 tags=None。第二轮在保鲜期内、消息
        # 增量不足、异动没变——唯独 tags 缺失触发重评补齐。
        build_database(self.database, [them(1, 0), me(2, 60)])
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        first, _ = self.run_llm_analysis(
            chat=lambda s, u: '{"summary": "你们最近聊了日常。"}'
        )
        self.assertEqual(first.llm_scored, 1)
        self.assertIsNone(self.store.get_llm_depth(SESSION_ID).tags)

        second, fake = self.run_llm_analysis(
            now=NOW + 5 * 86400,
            chat=lambda s, u: '{"summary": "你们最近聊了日常。", "tags": ["游戏"]}',
        )
        self.assertEqual(second.llm_scored, 1)
        self.assertEqual(fake.call_count, 1)
        row = self.store.get_llm_depth(SESSION_ID)
        self.assertEqual((row.summary, row.tags), ("你们最近聊了日常。", ["游戏"]))

    def test_anomaly_fingerprint_change_triggers_rescore(self) -> None:
        # 第一轮：基线窗口 60 条、近期窗口 10 条，日均消息量掉到一半以下，
        # 构成「worse」异动并随分数缓存指纹。
        baseline = [them(i, -40 * 86400 + i * 60) for i in range(1, 61)]
        recent = [them(i, 5 * 86400 + (i - 60) * 60) for i in range(61, 71)]
        build_database(self.database, baseline + recent)
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        rounds = iter(
            [
                '{"score": 40, "summary": "你们最近聊得少了。",'
                ' "anomaly_note": "最近消息变少了"}',
                '{"score": 60, "summary": "你们最近恢复热络。",'
                ' "anomaly_note": "最近聊得更频繁了"}',
            ]
        )

        def chat(system: str, user: str) -> str:
            return next(rounds)

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10), patch(
            # 70 条消息会让 SESSION_ID 成为关系分类候选（≥30 且未判定），
            # 分类会再调一次 chat、把 call_count 弄乱；时段评分同理（-40 天
            # 的月份有 60 条文字）。本测试只关心深度打分。
            "wechat_insights.analyzer.classify_contacts", return_value=0
        ), patch(
            "wechat_insights.analyzer.refresh_periods",
            return_value=PeriodRefresh(0, None),
        ):
            first, fake = self.run_llm_analysis(chat=chat)
        self.assertEqual(first.llm_scored, 1)
        self.assertEqual(fake.call_count, 1)
        row = self.store.get_llm_depth(SESSION_ID)
        self.assertEqual(row.anomalies_key, "msgs_them_per_day:worse")

        # 第二轮：近期窗口再灌 60 条改变异动集合，不新增 DB 消息、也没过
        # 保鲜期/消息数门槛——只有指纹变化触发重评，解释随之更新。
        self.seed_messages("friend", "Alice", {BASE + 6 * 86400: 60})
        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10), patch(
            "wechat_insights.analyzer.classify_contacts", return_value=0
        ), patch(
            "wechat_insights.analyzer.refresh_periods",
            return_value=PeriodRefresh(0, None),
        ):
            second, fake = self.run_llm_analysis(chat=chat)
        self.assertEqual(second.llm_scored, 1)
        self.assertEqual(fake.call_count, 1)
        payload = {
            p["display_name"]: p for p in self.store.all_scores()
        }[DISPLAY_NAME]
        self.assertEqual(payload["llm_summary"], "你们最近恢复热络。")
        self.assertEqual(payload["anomaly_note"], "最近聊得更频繁了")

    def test_stale_anomaly_note_is_withheld_when_the_fingerprint_differs(self) -> None:
        # 手工往缓存写一条「旧指纹 + 旧解释」：本轮异动指纹与它不匹配，
        # 解释针对的是旧异动集合，payload 里必须置 None 而不是展示旧话。
        # 重评期间 LLM 挂掉（chat 返回 None），缓存保持旧行原样。
        baseline = [them(i, -40 * 86400 + i * 60) for i in range(1, 61)]
        recent = [them(i, 5 * 86400 + (i - 60) * 60) for i in range(61, 71)]
        build_database(self.database, baseline + recent)
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})
        self.store.set_llm_depth(
            SESSION_ID, NOW, 70, "旧摘要", "旧解释", "msgs_them_per_day:better"
        )

        def unavailable(system: str, user: str) -> None:
            return None

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            result, _ = self.run_llm_analysis(chat=unavailable)
        self.assertEqual(result.llm_scored, 0)
        payload = {
            p["display_name"]: p for p in self.store.all_scores()
        }[DISPLAY_NAME]
        self.assertTrue(payload["scored"])
        self.assertEqual(payload["llm_summary"], "旧摘要")
        self.assertIsNone(payload["anomaly_note"])

    def test_reply_without_summary_writes_nothing(self) -> None:
        rows = []
        for index in range(1, 21):
            offset = index * 120
            rows.append(them(index, offset) if index % 2 else me(index, offset))
        build_database(self.database, rows)
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            result, _ = self.run_llm_analysis(
                chat=lambda s, u: '{"tags": ["游戏"], "anomaly_note": "最近联系少了"}'
            )
        # summary 是画像行的必需产物，缺失就整行不写，下一轮重评。
        self.assertEqual(result.llm_scored, 0)
        self.assertIsNone(self.store.get_llm_depth(SESSION_ID))

    def test_non_string_summary_writes_nothing(self) -> None:
        rows = []
        for index in range(1, 21):
            offset = index * 120
            rows.append(them(index, offset) if index % 2 else me(index, offset))
        build_database(self.database, rows)
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            result, _ = self.run_llm_analysis(
                chat=lambda s, u: '{"summary": 123, "anomaly_note": "最近联系变少了"}'
            )
        # 非字符串的 summary 同样当缺失处理：不落库，下一轮重评。
        self.assertEqual(result.llm_scored, 0)
        self.assertIsNone(self.store.get_llm_depth(SESSION_ID))

    def test_anomaly_list_is_sent_in_the_user_text_and_masked(self) -> None:
        # 基线 60 条 vs 近期 10 条构成「日均消息量下降」异动；样本里埋种子
        # 词，异动列表拼在样本之后，整个 user 字符串必须一次 mask 后才出站。
        baseline = [them(i, -40 * 86400 + i * 60) for i in range(1, 61)]
        recent = [them(i, 5 * 86400 + (i - 60) * 60) for i in range(61, 70)]
        recent.append(them(70, 5 * 86400 + 9 * 60, "习近平今天讲了什么"))
        build_database(self.database, baseline + recent)

        def chat(system: str, user: str) -> str:
            self.assertIn("近期变化", user)
            self.assertIn("TA 的日均消息量", user)
            self.assertIn("→", user)
            self.assertNotIn("习近平", user)
            self.assertIn("***", user)
            return '{"summary": "你们最近联系变少了。", "anomaly_note": "最近联系变少了"}'

        with patch("wechat_insights.analyzer.classify_contacts", return_value=0):
            # 同上：70 条消息会触发关系分类的第二次 chat 调用，这里关掉。
            result, _ = self.run_llm_analysis(chat=chat)
        self.assertEqual(result.llm_scored, 1)
        row = self.store.get_llm_depth(SESSION_ID)
        self.assertIsNotNone(row)
        self.assertEqual(row.anomaly_note, "最近联系变少了")
        self.assertEqual(row.anomalies_key, "msgs_them_per_day:worse")

    def test_lexical_strategy_never_calls_the_llm(self) -> None:
        build_database(self.database, [them(1, 0, "你好呀"), me(2, 60, "你好")])

        def unexpected(system: str, user: str) -> str:
            raise AssertionError("词法策略不该调用 LLM")

        sessions = {SESSION_ID: SimpleNamespace(display_name=DISPLAY_NAME)}
        with patch(
            "wechat_insights.analyzer.scan_direct_rows", return_value=sessions
        ), patch("wechat_insights.llm.chat", side_effect=unexpected):
            result = self.analyzer(strategy=LexicalDepth()).run(now=NOW)
        self.assertEqual(result.llm_scored, 0)

if __name__ == "__main__":
    unittest.main()
