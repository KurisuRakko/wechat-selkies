from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.support import (
    AnalyzerTestCase,
    BASE,
    DISPLAY_NAME,
    HOUR,
    NOW,
    SESSION_ID,
    FakeReader,
    backfill_grid,
    build_database,
    me,
    them,
)
from wechat_insights.analyzer import Analyzer
from wechat_insights.depth import LLMDepth
from wechat_insights.metrics import day_key
from wechat_insights.reading import Cursor, read_messages_after


class ReadWindowTests(AnalyzerTestCase):
    def test_cursor_window_excludes_already_read_messages(self) -> None:
        build_database(self.database, [them(1, 0), me(2, 60), them(3, 120)])
        batch = read_messages_after(
            self.reader,
            SESSION_ID,
            DISPLAY_NAME,
            {},
            Cursor(BASE + 60, "message/message_0.db", 2),
            100,
        )
        self.assertEqual([message.local_id for message in batch.messages], [3])

    def test_fresh_cursor_includes_local_id_zero(self) -> None:
        build_database(self.database, [them(0, 0)])
        batch = read_messages_after(
            self.reader, SESSION_ID, DISPLAY_NAME, {}, Cursor(0, "", -1), 100
        )
        self.assertEqual(len(batch.messages), 1)

    def test_messages_without_a_sender_are_dropped(self) -> None:
        build_database(self.database, [them(1, 0), (2, 10000, BASE + 10, 99, "系统")])
        batch = read_messages_after(
            self.reader, SESSION_ID, DISPLAY_NAME, {}, Cursor(0, "", -1), 100
        )
        self.assertEqual([message.local_id for message in batch.messages], [1])

    def test_batch_reports_more_rows_and_the_last_position(self) -> None:
        build_database(self.database, [them(1, 0), me(2, 60), them(3, 120)])
        batch = read_messages_after(
            self.reader, SESSION_ID, DISPLAY_NAME, {}, Cursor(0, "", -1), 2
        )
        self.assertTrue(batch.has_more)
        self.assertEqual(batch.last, Cursor(BASE + 60, "message/message_0.db", 2))

    def test_missing_message_table_reads_nothing(self) -> None:
        build_database(self.database, [them(1, 0)])
        batch = read_messages_after(
            self.reader, "stranger", "Bob", {}, Cursor(0, "", -1), 100
        )
        self.assertEqual(batch.messages, [])
        self.assertIsNone(batch.last)


class IncrementalTests(AnalyzerTestCase):
    def test_first_run_backfills_and_advances_the_cursor(self) -> None:
        build_database(
            self.database, [them(1, 0), me(2, 60), them(3, 120), me(4, 10 * HOUR)]
        )
        result = self.run_analysis()
        self.assertEqual(result.messages_read, 4)

        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.cursor_timestamp, BASE + 10 * HOUR)
        self.assertEqual(contact.cursor_local_id, 4)
        self.assertEqual(contact.total_messages, 4)
        self.assertEqual(contact.first_message_at, BASE)

    def test_second_run_reads_nothing_new(self) -> None:
        build_database(self.database, [them(1, 0), me(2, 60)])
        self.run_analysis()
        second = self.run_analysis()
        self.assertEqual(second.messages_read, 0)
        self.assertEqual(second.per_session, {})
        self.assertEqual(self.store.get_contact(SESSION_ID).total_messages, 2)

    def test_only_the_new_messages_are_counted_on_a_later_run(self) -> None:
        build_database(self.database, [them(1, 0), me(2, 60)])
        self.run_analysis()
        build_database(self.database, [them(3, 10 * HOUR)])
        second = self.run_analysis()
        self.assertEqual(second.messages_read, 1)
        self.assertEqual(self.store.get_contact(SESSION_ID).total_messages, 3)

    def test_an_open_conversation_is_held_back_until_it_settles(self) -> None:
        # 最后一段对话刚发生不久，可能还会有后续消息，本轮不该统计。
        build_database(self.database, [them(1, 0), me(2, 60)])
        recent = BASE + 120
        self.assertEqual(self.run_analysis(now=recent).messages_read, 0)
        self.assertEqual(self.store.get_contact(SESSION_ID).total_messages, 0)

        # 隔了超过一个对话间隔之后，同一段对话连同新消息一起被提交。
        build_database(self.database, [them(3, 300)])
        later = BASE + 300 + 7 * HOUR
        self.assertEqual(self.run_analysis(now=later).messages_read, 3)
        self.assertEqual(self.store.get_contact(SESSION_ID).total_messages, 3)

    def test_closed_conversations_commit_while_the_open_one_waits(self) -> None:
        build_database(
            self.database,
            [them(1, 0), me(2, 60), them(3, 20 * HOUR), me(4, 20 * HOUR + 30)],
        )
        result = self.run_analysis(now=BASE + 20 * HOUR + 60)
        self.assertEqual(result.messages_read, 2)
        self.assertEqual(self.store.get_contact(SESSION_ID).cursor_local_id, 2)

    def test_a_conversation_longer_than_one_batch_still_advances(self) -> None:
        build_database(
            self.database,
            [them(index, index * 10) if index % 2 else me(index, index * 10) for index in range(1, 6)],
        )
        result = self.run_analysis(batch_size=2)
        self.assertEqual(result.messages_read, 5)
        self.assertEqual(self.store.get_contact(SESSION_ID).cursor_local_id, 5)

    def test_cursor_steps_over_trailing_system_noise(self) -> None:
        # 尾部的无方向系统消息不参与统计，但游标必须跨过去，
        # 否则每一轮都会把它们重新读一遍。
        build_database(
            self.database,
            [them(1, 0), (2, 10000, BASE + 10, 99, "系统"), (3, 10000, BASE + 20, 99, "系统")],
        )
        self.assertEqual(self.run_analysis().messages_read, 1)
        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.cursor_local_id, 3)
        self.assertEqual(contact.total_messages, 1)
        self.assertEqual(self.run_analysis().messages_read, 0)

    def test_batches_made_entirely_of_system_noise_do_not_stall_backfill(self) -> None:
        rows = [(index, 10000, BASE + index * 10, 99, "系统") for index in range(1, 5)]
        rows.append(them(5, 50))
        build_database(self.database, rows)
        self.assertEqual(self.run_analysis(batch_size=2).messages_read, 1)
        self.assertEqual(self.store.get_contact(SESSION_ID).cursor_local_id, 5)

    def test_colliding_rows_across_shards_are_never_lost(self) -> None:
        # 微信把消息分到九个分片，local_id 只在单个分片内唯一：两个分片里
        # 会出现完全相同的 (create_time, local_id)。批边界恰好卡在两条冲突
        # 行之间时，游标必须带上分片，否则下轮谓词永远匹配不到另一条，
        # 那条消息会被静默跳过。
        shard0 = self.root / "message_0.db"
        shard1 = self.root / "message_1.db"
        build_database(shard0, [them(1, 0), me(2, 60)])
        build_database(shard1, [them(2, 60), me(3, 120)])
        self.reader = FakeReader(
            {"message/message_0.db": shard0, "message/message_1.db": shard1}
        )

        first = self.run_analysis(batch_size=2)
        self.assertEqual(first.messages_read, 4)
        # 4 条一条都不能少，也不能被重复计入。
        second = self.run_analysis(batch_size=2)
        self.assertEqual(second.messages_read, 0)
        self.assertEqual(self.store.get_contact(SESSION_ID).total_messages, 4)

    def test_a_failing_session_does_not_abort_the_whole_run(self) -> None:
        build_database(self.database, [them(1, 0)])
        sessions = {
            "broken": SimpleNamespace(display_name="Broken"),
            SESSION_ID: SimpleNamespace(display_name=DISPLAY_NAME),
        }

        original = Analyzer._sync_session

        def flaky(self, reader, session_id, display_name, contacts, moment):
            if session_id == "broken":
                raise RuntimeError("boom")
            return original(self, reader, session_id, display_name, contacts, moment)

        with patch(
            "wechat_insights.analyzer.scan_direct_rows", return_value=sessions
        ), patch.object(Analyzer, "_sync_session", flaky), self.assertLogs(
            "wechat-insights", level="ERROR"
        ):
            result = self.analyzer().run(now=NOW)
        self.assertEqual(result.messages_read, 1)


class MilestoneTests(AnalyzerTestCase):
    def test_longest_silence_spans_batches(self) -> None:
        build_database(self.database, [them(1, 0), me(2, 60)])
        self.run_analysis()
        build_database(self.database, [them(3, 20 * 86400)])
        self.run_analysis()

        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.longest_silence_seconds, 20 * 86400 - 60)
        self.assertEqual(contact.longest_silence_ended_at, BASE + 20 * 86400)

    def test_longest_laugh_run_is_remembered(self) -> None:
        build_database(self.database, [them(1, 0, "哈哈哈哈"), me(2, 60, "哈哈")])
        self.run_analysis()
        self.assertEqual(self.store.get_contact(SESSION_ID).max_laugh_run, 4)


class TrendTests(AnalyzerTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 建一个空的真实分片表：增量读取会打开这个文件，空文件会被当成坏库。
        build_database(self.database, [])

    def test_empty_baseline_window_suppresses_the_trend(self) -> None:
        # 两个联系人都达标，但 newer 的基线窗口 [D−120, D−31] 一条消息都
        # 没有：全空基线下基线分恒为 50，趋势就是「近期分 − 50」的假数字，
        # 必须置 null，而不是给出一个误导的箭头或「持平」。
        self.seed_messages(
            "older",
            "Older",
            {
                BASE - 80 * 86400: 5,
                BASE - 70 * 86400: 5,
                BASE - 60 * 86400: 5,
                BASE - 50 * 86400: 5,
                BASE - 40 * 86400: 5,
                BASE + 5 * 86400: 4,
            },
        )
        # newer 的消息全部落在近期窗口（基线窗口在 D−31 之前就结束了）。
        self.seed_messages(
            "newer", "Newer", {BASE: 10, BASE + 5 * 86400: 10}
        )

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()

        scores = {p["display_name"]: p for p in self.store.all_scores()}
        self.assertTrue(scores["Older"]["scored"])
        self.assertIsInstance(scores["Older"]["trends"], dict)
        self.assertTrue(
            all(isinstance(v, float) for v in scores["Older"]["trends"].values())
        )
        self.assertTrue(scores["Newer"]["scored"])
        self.assertIsNone(scores["Newer"]["trends"])

    def test_window_day_counts_calibrate_the_per_day_numbers(self) -> None:
        # 近期窗口 [D−30, D] 是 31 个日键、基线 [D−120, D−31] 是 90 个：
        # 50 条 / 31 天 = 每天 1.6 条、50 条 / 90 天 = 每天 0.6 条。
        # 若近期窗口误除以 30，会显示成每天 1.7 条。
        self.seed_messages(
            "active",
            "Active",
            {
                BASE - 80 * 86400: 10,
                BASE - 70 * 86400: 10,
                BASE - 60 * 86400: 10,
                BASE - 50 * 86400: 10,
                BASE - 40 * 86400: 10,
                BASE: 10,
                BASE + 5 * 86400: 10,
                BASE + 10 * 86400: 10,
                BASE + 15 * 86400: 10,
                BASE + 20 * 86400: 10,
            },
        )
        self.seed_messages(
            "steady", "Steady", {BASE - 60 * 86400: 10, BASE + 10 * 86400: 10}
        )

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()

        scores = {p["display_name"]: p for p in self.store.all_scores()}
        anomalies = {item["metric"]: item for item in scores["Active"]["anomalies"]}
        per_day = anomalies["msgs_them_per_day"]
        self.assertEqual(per_day["before"], "每天 0.6 条")
        self.assertEqual(per_day["after"], "每天 1.6 条")


class ScoreTests(AnalyzerTestCase):
    def test_thin_contacts_are_marked_as_lacking_data(self) -> None:
        build_database(self.database, [them(1, 0), me(2, 60)])
        self.run_analysis()
        payload = self.store.all_scores()[0]
        self.assertFalse(payload["scored"])
        self.assertIsNone(payload["overall"])
        self.assertEqual(payload["sample_note"], "数据不足")

    def test_contacts_with_enough_messages_get_scored(self) -> None:
        rows = []
        for index in range(1, 21):
            offset = index * 120
            rows.append(them(index, offset) if index % 2 else me(index, offset))
        build_database(self.database, rows)
        # 百分位至少需要两个联系人做参照，第二个直接写库。
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            result = self.run_analysis(now=BASE + 10 * 86400)

        self.assertEqual(result.scored, 2)
        payloads = {p["display_name"]: p for p in self.store.all_scores()}
        payload = payloads[DISPLAY_NAME]
        self.assertTrue(payload["scored"])
        self.assertIsNotNone(payload["overall"])
        self.assertEqual(len(payload["dimensions"]), 7)
        self.assertEqual(payload["window_messages"], 20)
        self.assertEqual(self.store.get_json("medians", {}).keys().__len__(), 7)

    def test_single_contact_cohort_is_not_scored(self) -> None:
        # 只有一个人时百分位恒为 50，分数不代表任何相对位置；宁可不打分，
        # 也不能交出一个看着像「平均」的假分。
        rows = []
        for index in range(1, 21):
            offset = index * 120
            rows.append(them(index, offset) if index % 2 else me(index, offset))
        build_database(self.database, rows)

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            result = self.run_analysis(now=BASE + 10 * 86400)

        self.assertEqual(result.scored, 0)
        payload = self.store.all_scores()[0]
        self.assertFalse(payload["scored"])
        self.assertIsNone(payload["overall"])


class DecayWindowTests(AnalyzerTestCase):
    """两年衰减打分窗口的行为，直接验证分析产物而不是内部函数。"""

    def test_stale_activity_invests_less_than_steady_activity(self) -> None:
        # stale：每天 3 条消息，只发生在约一年前（NOW−365 天，天龄 ≈ 360 天、
        # 权重 ≈ 0.06）；steady：同样每天 3 条，集中在最近 30 天（权重 ≈ 1）。
        # 两人 raw 消息数都是 90，达标线一致，可以比 investment。
        stale_start = NOW - 395 * 86400
        steady_start = NOW - 29 * 86400
        self.seed_messages(
            "stale", "Stale", {stale_start + offset * 86400: 3 for offset in range(30)}
        )
        self.seed_messages(
            "steady",
            "Steady",
            {steady_start + offset * 86400: 3 for offset in range(30)},
        )
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()

        payloads = {p["display_name"]: p for p in self.store.all_scores()}
        self.assertTrue(payloads["Stale"]["scored"])
        self.assertTrue(payloads["Steady"]["scored"])
        # 原始计数不受衰减影响：双方在窗口内都是 90 条。
        self.assertEqual(payloads["Stale"]["window_messages"], 90)
        self.assertEqual(payloads["Steady"]["window_messages"], 90)
        self.assertGreater(
            payloads["Steady"]["dimensions"]["investment"],
            payloads["Stale"]["dimensions"]["investment"],
        )
        self.assertGreater(payloads["Steady"]["overall"], payloads["Stale"]["overall"])

    def test_no_messages_in_window_is_zeroed(self) -> None:
        # 没有任何 stats_daily 行（联系人存在但从未在窗口内活跃）。
        self.store.ensure_contact("ghost", "Ghost")

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()

        payloads = {p["display_name"]: p for p in self.store.all_scores()}
        ghost = payloads["Ghost"]
        self.assertFalse(ghost["scored"])
        self.assertTrue(ghost["zeroed"])
        self.assertEqual(ghost["overall"], 0)
        self.assertEqual(set(ghost["dimensions"].values()), {0.0})
        self.assertIsNone(ghost["trends"])
        self.assertEqual(ghost["anomalies"], [])
        self.assertEqual(ghost["sample_note"], "两年内没有往来")
        self.assertEqual(ghost["window_messages"], 0)

    def test_few_messages_without_zeroing_keep_data_insufficient(self) -> None:
        # 有消息但远低于门槛：维持「数据不足」，不归零。
        self.seed_messages("thin", "Thin", {BASE + 5 * 86400: 2})
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()

        payloads = {p["display_name"]: p for p in self.store.all_scores()}
        self.assertFalse(payloads["Thin"]["scored"])
        self.assertNotIn("zeroed", payloads["Thin"])
        self.assertEqual(payloads["Thin"]["sample_note"], "数据不足")

    def test_current_gap_days_reflects_the_last_message(self) -> None:
        # last_message_at 落在窗口内但很早（NOW 前 200 天），current_gap_days
        # 应该远大于窗口内最后活跃天到现在的距离。
        self.seed_messages(
            "early",
            "Early",
            {BASE - 200 * 86400 + offset * 86400: 3 for offset in range(30)},
        )
        self.seed_messages(
            "recent",
            "Recent",
            {BASE - 30 * 86400 + offset * 86400: 3 for offset in range(30)},
        )
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()

        payloads = {p["display_name"]: p for p in self.store.all_scores()}
        self.assertTrue(payloads["Early"]["scored"])
        self.assertTrue(payloads["Recent"]["scored"])
        # Early 的最后消息在 NOW 前 200 天（current_gap_days ≈ 200），
        # Recent 的最后消息在 NOW 前 1 天（current_gap_days ≈ 1）。
        # 恒常维度里 Early 明显更差。
        self.assertGreater(
            payloads["Recent"]["dimensions"]["constancy"],
            payloads["Early"]["dimensions"]["constancy"],
        )


class LeadingGapTests(AnalyzerTestCase):
    """前导空档规则：认识早于窗口起点要计入，首条消息在窗口内的不算。"""

    def test_first_message_before_window_start_counts_the_leading_gap(self) -> None:
        # 认识早于窗口起点（first_message_at = 两年半前），但窗口内第一个活跃天
        # 在窗口起点 100 天后。longest_gap_days = 100。
        self.seed_messages("old_friend", "OldFriend", {BASE - 630 * 86400: 50})
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()

        scores = {p["display_name"]: p for p in self.store.all_scores()}
        self.assertTrue(scores["OldFriend"]["scored"])
        self.assertGreater(scores["OldFriend"]["dimensions"]["constancy"], 0.0)

    def test_first_message_inside_the_window_ignores_the_leading_gap(self) -> None:
        # 首条消息在窗口内（认识于 300 天前），前导空档不算沉默。
        self.seed_messages("new_friend", "NewFriend", {BASE - 300 * 86400: 50})
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()

        scores = {p["display_name"]: p for p in self.store.all_scores()}
        self.assertTrue(scores["NewFriend"]["scored"])
        # 尾部空档（认识后 300 天都没聊）不算前导空档：new_friend 唯一活跃日在窗口内。
        self.assertGreater(scores["NewFriend"]["dimensions"]["constancy"], 0.0)


class RelationKindScoreTests(AnalyzerTestCase):
    """关系类型对打分的差异化处理：事务往来剔除 cohort、家人豁免归零/淡出。"""

    def setUp(self) -> None:
        super().setUp()
        build_database(self.database, [])

    def test_transactional_drops_out_of_the_percentile_cohort(self) -> None:
        # 同数据跑两轮：第一轮把消息量巨大的「机票代理」标成事务往来，第二轮
        # 改回默认 friend。两位朋友的原始指标一模一样，只有参照系变了——
        # 分数差必须来自「事务号被剔除」这一条（msgs_them_per_day 占比 0.2，
        # 500 条/天的巨量把 cohort 上限顶高，正常朋友的相对分被压扁）。
        # 名字避开 DISPLAY_NAME：run_analysis 的同步会话也叫 Alice，payload
        # 按 display_name 建索引会撞名。
        self.seed_messages("friend_a", "小红", {BASE + 5 * 86400: 20})
        self.seed_messages("friend_b", "小明", {BASE + 5 * 86400: 60})
        self.seed_messages("agent", "机票代理", {BASE + 5 * 86400: 500})
        self.store.set_contact_kind_manual("agent", "transactional")

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()
        excluded = {p["display_name"]: p for p in self.store.all_scores()}

        self.store.set_contact_kind_manual("agent", "")
        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()
        included = {p["display_name"]: p for p in self.store.all_scores()}

        self.assertGreater(
            excluded["小红"]["dimensions"]["investment"],
            included["小红"]["dimensions"]["investment"],
        )
        self.assertGreater(
            excluded["小明"]["dimensions"]["investment"],
            included["小明"]["dimensions"]["investment"],
        )

    def test_transactional_payload_is_unscored_and_unrecorded(self) -> None:
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})
        self.seed_messages("agent", "机票代理", {BASE + 5 * 86400: 500})
        self.store.set_contact_kind_auto("agent", "transactional")

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()

        payloads = {p["display_name"]: p for p in self.store.all_scores()}
        agent = payloads["机票代理"]
        self.assertFalse(agent["scored"])
        self.assertNotIn("zeroed", agent)
        self.assertIsNone(agent["overall"])
        self.assertTrue(all(dim is None for dim in agent["dimensions"].values()))
        self.assertEqual(agent["window_messages"], 500)
        self.assertEqual(agent["sample_note"], "事务往来，不参与打分")
        self.assertEqual(agent["relation_kind"], "transactional")
        self.assertEqual(agent["kind_source"], "auto")
        self.assertIsNone(agent["trends"])
        self.assertEqual(agent["anomalies"], [])
        # 不记温度历史、不进「正在淡出」。
        self.assertEqual(self.store.load_score_history("agent"), [])
        self.assertNotIn(
            "机票代理",
            [item["display_name"] for item in self.store.get_json("fading", [])],
        )

    def test_family_with_no_window_messages_is_not_zeroed(self) -> None:
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})
        self.store.ensure_contact("mom", "妈妈")
        self.store.set_contact_kind_manual("mom", "family")

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()

        payloads = {p["display_name"]: p for p in self.store.all_scores()}
        mom = payloads["妈妈"]
        self.assertFalse(mom["scored"])
        self.assertNotIn("zeroed", mom)
        self.assertIsNone(mom["overall"])
        self.assertTrue(all(dim is None for dim in mom["dimensions"].values()))
        self.assertEqual(mom["sample_note"], "家人，久未聊天不代表疏远")
        self.assertEqual(mom["relation_kind"], "family")
        self.assertEqual(mom["kind_source"], "manual")
        # 零消息的家人不记 0 分历史（与普通归零不同）。
        self.assertEqual(self.store.load_score_history("mom"), [])

    def test_family_scores_normally_but_never_fades(self) -> None:
        # 妈妈消息量最高（3 条/天）、沉默 30 天：换成朋友必进「正在淡出」，
        # 家人豁免——其余两位朋友照常进名单，证明机制本身在工作。
        for session_id, name, count in (
            ("mom", "Mom", 3),
            ("friend_a", "FriendA", 2),
            ("friend_b", "FriendB", 1),
        ):
            self.seed_messages(
                session_id,
                name,
                {BASE + offset * 86400: count for offset in range(25)},
            )
            contact = self.store.get_contact(session_id)
            # 回放按 first_message_at 判相识起点，seed 不写里程碑，补上；
            # BASE 晚于打分窗口起点（NOW−730 天），不影响前导空档判定。
            contact.first_message_at = BASE
            contact.last_message_at = NOW - 30 * 86400
            self.store.save_contact(contact)
        self.store.set_contact_kind_manual("mom", "family")

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()

        fading = self.store.get_json("fading", [])
        self.assertEqual(
            [item["display_name"] for item in fading], ["FriendA", "FriendB"]
        )
        # 有数据时家人照常打分、记历史（含回放的周点，最后一点是今天的每日点）。
        payloads = {p["display_name"]: p for p in self.store.all_scores()}
        mom = payloads["Mom"]
        self.assertTrue(mom["scored"])
        self.assertEqual(mom["relation_kind"], "family")
        self.assertEqual(mom["kind_source"], "manual")
        mom_history = self.store.load_score_history("mom")
        self.assertGreater(len(mom_history), 1)
        self.assertEqual(mom_history[-1][0], day_key(NOW))

    def test_kind_source_reflects_manual_auto_and_default(self) -> None:
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})
        self.seed_messages("aunt", "姑妈", {BASE + 5 * 86400: 20})
        self.seed_messages(SESSION_ID, DISPLAY_NAME, {BASE + 5 * 86400: 25})
        self.store.set_contact_kind_auto("aunt", "family")
        self.store.set_contact_kind_manual("friend2", "family")

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()

        payloads = {p["display_name"]: p for p in self.store.all_scores()}
        # 从未判定过的联系人：默认 friend、source=default。
        alice = payloads[DISPLAY_NAME]
        self.assertTrue(alice["scored"])
        self.assertEqual(
            (alice["relation_kind"], alice["kind_source"]), ("friend", "default")
        )
        # 自动判定来的家人照常参与打分，source=auto。
        self.assertTrue(payloads["姑妈"]["scored"])
        self.assertEqual(
            (payloads["姑妈"]["relation_kind"], payloads["姑妈"]["kind_source"]),
            ("family", "auto"),
        )
        self.assertEqual(
            (payloads["Bob"]["relation_kind"], payloads["Bob"]["kind_source"]),
            ("family", "manual"),
        )


class FadeTests(AnalyzerTestCase):
    """「正在淡出」提醒：已打分 + 沉默够久 + 分够高，按综合分降序截断。

    四个活跃联系人里，High/Mid/Low 把 last_message_at 拨到 30 天前
    （current_gap ≈ 30 ≥ 14，全部入选），Fresh 保持最近（gap ≈ 1，被
    沉默条件挡下）。四人除消息量外各项指标完全相同，综合分只由消息量
    的百分位拉开，排序方向确定。
    """

    def setUp(self) -> None:
        super().setUp()
        for session_id, count in (("high", 3), ("mid", 2), ("low", 1)):
            self.seed_messages(
                session_id,
                session_id.title(),
                {BASE + offset * 86400: count for offset in range(25)},
            )
            contact = self.store.get_contact(session_id)
            contact.last_message_at = NOW - 30 * 86400
            self.store.save_contact(contact)
        self.seed_messages(
            "fresh", "Fresh", {BASE + offset * 86400: 3 for offset in range(25)}
        )
        contact = self.store.get_contact("fresh")
        contact.last_message_at = NOW - 86400
        self.store.save_contact(contact)

    def fading(self) -> list:
        return self.store.get_json("fading", [])

    def test_fading_keeps_long_silent_scored_contacts_sorted_by_overall(self) -> None:
        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()

        fading = self.fading()
        self.assertEqual(
            [item["display_name"] for item in fading], ["High", "Mid", "Low"]
        )
        overalls = [item["overall"] for item in fading]
        self.assertEqual(overalls, sorted(overalls, reverse=True))
        for item in fading:
            self.assertEqual(item["gap_days"], 30)
            self.assertGreaterEqual(item["overall"], 40.0)
            self.assertEqual(len(item["hash"]), 24)

    def test_fading_drops_contacts_below_the_overall_floor(self) -> None:
        # 三人的综合分都落在 50 附近：把门槛抬到 60，名单必须清空。
        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10), patch(
            "wechat_insights.analyzer.FADE_MIN_OVERALL", 60
        ):
            self.run_analysis()
        self.assertEqual(self.fading(), [])

    def test_fading_truncates_to_the_list_limit(self) -> None:
        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10), patch(
            "wechat_insights.analyzer.FADE_LIST_LIMIT", 2
        ):
            self.run_analysis()
        fading = self.fading()
        self.assertEqual(
            [item["display_name"] for item in fading], ["High", "Mid"]
        )

    def test_fading_overwrites_a_stale_round_with_an_empty_list(self) -> None:
        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis()
        self.assertEqual(len(self.fading()), 3)
        # 第二轮把沉默门槛抬到不可能达到的高度：名单整体换成空数组，
        # 不能残留上一轮的旧条目。
        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10), patch(
            "wechat_insights.analyzer.FADE_MIN_GAP_DAYS", 365
        ):
            self.run_analysis()
        self.assertEqual(self.fading(), [])


class ProgressTests(AnalyzerTestCase):
    """进度上报：阶段顺序、计数递增，以及 cb 异常绝不影响分析本身。"""

    def run_with_progress(self, strategy=None, **kwargs):
        events: list[dict] = []

        def callback(fields: dict) -> None:
            events.append(dict(fields))

        sessions = {SESSION_ID: SimpleNamespace(display_name=DISPLAY_NAME)}
        with patch(
            "wechat_insights.analyzer.scan_direct_rows", return_value=sessions
        ), patch("wechat_insights.llm.chat", side_effect=lambda s, u: '{"score": 60}'):
            result = self.analyzer(strategy=strategy, progress_cb=callback).run(**kwargs)
        return result, events

    def test_lexical_run_reports_sync_then_score(self) -> None:
        build_database(self.database, [them(1, 0), me(2, 60)])
        _, events = self.run_with_progress(now=NOW)
        # 阶段顺序：sync（起点 + 1 个会话）→ score → history（全史回放网格，
        # 起点 + 每 7 天一个点）。
        grid = backfill_grid(day_key(BASE), day_key(NOW))
        phases = [event["phase"] for event in events]
        self.assertEqual(phases[:3], ["sync", "sync", "score"])
        self.assertEqual(phases[3:], ["history"] * (len(grid) + 1))
        # sync 起点：total=会话数、done=0；随后每个会话前上报一次 display_name。
        self.assertEqual(events[0], {"phase": "sync", "done": 0, "total": 1, "detail": ""})
        self.assertEqual(
            events[1],
            {"phase": "sync", "done": 0, "total": 1, "detail": DISPLAY_NAME},
        )
        # score 阶段没有逐项计数：total=0。
        self.assertEqual(events[2], {"phase": "score", "done": 0, "total": 0, "detail": ""})
        # history 起点 total=网格点数，随后逐点上报日期。
        history = [event for event in events if event["phase"] == "history"]
        self.assertEqual(
            history[0],
            {"phase": "history", "done": 0, "total": len(grid), "detail": ""},
        )
        self.assertEqual(
            history[-1],
            {
                "phase": "history",
                "done": len(grid),
                "total": len(grid),
                "detail": grid[-1],
            },
        )

    def test_llm_run_reports_candidate_progress_between_sync_and_score(self) -> None:
        shards: dict[str, Path] = {}
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
        events: list[dict] = []
        with patch(
            "wechat_insights.analyzer.scan_direct_rows", return_value=sessions
        ), patch("wechat_insights.llm.chat", side_effect=lambda s, u: '{"score": 40}'
        ) as fake, patch("wechat_insights.portrait.LLM_MAX_CALLS_PER_RUN", 2):
            result = self.analyzer(
                strategy=LLMDepth(), progress_cb=lambda fields: events.append(dict(fields))
            ).run(now=NOW)
        self.assertEqual(result.llm_scored, 2)
        self.assertEqual(fake.call_count, 2)
        phases = [event["phase"] for event in events]
        # 阶段顺序：sync（起点 + 3 个会话）→ llm（起点 + 2 个候选）→
        # score → history（全史回放网格）。
        grid = backfill_grid(day_key(BASE), day_key(NOW))
        self.assertEqual(
            phases, ["sync"] * 4 + ["llm"] * 3 + ["score"] + ["history"] * (len(grid) + 1)
        )
        llm = [event for event in events if event["phase"] == "llm"]
        self.assertEqual(llm[0], {"phase": "llm", "done": 0, "total": 2, "detail": ""})
        self.assertEqual(
            [event["done"] for event in llm], [0, 1, 2]
        )
        self.assertEqual(
            [event["detail"] for event in llm[1:]], ["C1", "C2"]
        )
        # 候选是「截断后的」数量，不是全部待评联系人。
        self.assertTrue(all(event["total"] == 2 for event in llm))

    def test_raising_callback_does_not_abort_the_run(self) -> None:
        build_database(self.database, [them(1, 0), me(2, 60), them(3, 120)])

        def exploding(fields: dict) -> None:
            raise RuntimeError("callback boom")

        with patch("wechat_insights.analyzer.scan_direct_rows", return_value={
            SESSION_ID: SimpleNamespace(display_name=DISPLAY_NAME)
        }), self.assertLogs("wechat-insights", level="DEBUG"):
            result = self.analyzer(progress_cb=exploding).run(now=NOW)
        # 上报失败只是记一条调试日志，读取与打分照常完成。
        self.assertEqual(result.messages_read, 3)


if __name__ == "__main__":
    unittest.main()
