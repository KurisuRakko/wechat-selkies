from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wechat_history.formatting import message_kind

from wechat_insights.analyzer import Analyzer, Cursor, read_messages_after
from wechat_insights.depth import DepthStrategy, LexicalDepth, LLMDepth
from wechat_insights.metrics import Metrics, day_key
from wechat_insights.storage import MetricsStore


SESSION_ID = "friend"
DISPLAY_NAME = "Alice"
HOUR = 3600
# 消息全部落在这一天之后，测试里用一个固定的「现在」。
BASE = 1_740_000_000
NOW = BASE + 30 * 86400

# real_sender_id → username，1 是对方，2 是我自己。
_NAMES = {1: SESSION_ID, 2: "me"}


class FakeReader:
    """只提供分析器实际用到的那几个 reader 接口，底下是真实的 sqlite 表。

    databases 按相对路径映射到各分片文件，和真实 reader 的
    message/message_N.db 布局一致。
    """

    def __init__(self, databases: dict[str, Path]):
        self.databases = databases
        self.cache = SimpleNamespace(get=lambda key: databases[key])
        self.closed = False

    @staticmethod
    def _message_table(session_id: str) -> str:
        return f"Msg_{hashlib.md5(session_id.encode('utf-8')).hexdigest()}"

    def _message_database_keys(self) -> list[str]:
        return sorted(self.databases)

    def _name_map(self, _: sqlite3.Connection) -> dict[int, str]:
        return dict(_NAMES)

    def _message_item(self, row, _relative_db, _session, _contacts, names) -> dict:
        sender = names.get(int(row["real_sender_id"] or 0), "")
        if sender == SESSION_ID:
            direction = "incoming"
        elif sender == "me":
            direction = "outgoing"
        else:
            direction = "unknown"
        return {
            "direction": direction,
            "type": message_kind(row["local_type"]),
            "text": row["message_content"] or "",
        }

    def _load_contacts(self) -> dict:
        return {}

    def close(self) -> None:
        self.closed = True


def build_database(
    path: Path,
    rows: list[tuple[int, int, int, int, str]],
    session_id: str = SESSION_ID,
) -> None:
    """rows = (local_id, local_type, create_time, real_sender_id, content)。"""

    table = FakeReader._message_table(session_id)
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS [{table}] (
                local_id INTEGER PRIMARY KEY,
                local_type INTEGER,
                create_time INTEGER,
                real_sender_id INTEGER,
                message_content TEXT,
                WCDB_CT_message_content INTEGER
            )
            """
        )
        connection.executemany(
            f"INSERT OR REPLACE INTO [{table}] VALUES (?, ?, ?, ?, ?, 0)", rows
        )


def them(local_id: int, offset: int, text: str = "在吗？") -> tuple:
    return (local_id, 1, BASE + offset, 1, text)


def me(local_id: int, offset: int, text: str = "在的") -> tuple:
    return (local_id, 1, BASE + offset, 2, text)


class AnalyzerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "message_0.db"
        self.store = MetricsStore(self.root / "metrics.db")
        self.addCleanup(self.store.close)
        self.reader = FakeReader({"message/message_0.db": self.database})

    def analyzer(
        self,
        batch_size: int = 5000,
        strategy: DepthStrategy | None = None,
        progress_cb=None,
    ) -> Analyzer:
        return Analyzer(
            self.store,
            reader_factory=lambda: self.reader,
            strategy=strategy,
            batch_size=batch_size,
            progress_cb=progress_cb,
        )

    def run_analysis(self, now: int = NOW, batch_size: int = 5000):
        sessions = {SESSION_ID: SimpleNamespace(display_name=DISPLAY_NAME)}
        with patch(
            "wechat_insights.analyzer.scan_direct_rows", return_value=sessions
        ):
            return self.analyzer(batch_size).run(now=now)

    def seed_messages(self, session_id: str, name: str, days: dict[int, int]) -> None:
        """绕过读取器，直接把 (时间戳 → 当天 TA 的消息数) 写进 stats_daily。"""

        contact = self.store.ensure_contact(session_id, name)
        buckets = {}
        for timestamp, count in days.items():
            metrics = Metrics()
            metrics.add("msgs_them", count)
            buckets[day_key(timestamp)] = metrics
        self.store.commit_batch(session_id, buckets, contact)


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


class LLMDepthRefreshTests(AnalyzerTestCase):
    """大模型深度打分的采样、屏蔽、缓存与刷新行为（假 reader + 假 llm.chat）。"""

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

    def test_new_contact_is_scored_and_cached_on_first_round(self) -> None:
        build_database(
            self.database,
            [
                them(1, 0, "今天过得怎么样"),
                me(2, 60, "还行"),
                them(3, 120, "最近看了本好书"),
            ],
        )
        result, fake = self.run_llm_analysis(chat=lambda system, user: '{"score": 66}')
        self.assertEqual(result.llm_scored, 1)
        self.assertEqual(fake.call_count, 1)
        row = self.store.get_llm_depth(SESSION_ID)
        self.assertIsNotNone(row)
        self.assertEqual((row.score, row.scored_at, row.total_messages), (66.0, NOW, 3))

    def test_sample_text_is_masked_before_being_sent(self) -> None:
        build_database(
            self.database,
            [them(1, 0, "习近平今天讲了什么"), me(2, 60, "不知道")],
        )

        def chat(system: str, user: str) -> str:
            # 种子词必须以星号出现，原文绝不能出容器。
            self.assertNotIn("习近平", user)
            self.assertIn("***", user)
            return '{"score": 55}'

        result, _ = self.run_llm_analysis(chat=chat)
        self.assertEqual(result.llm_scored, 1)

    def test_fresh_cache_within_ttl_and_under_message_threshold_skips(self) -> None:
        # 第一轮必须带 tags：tags 缺失（None）本身就是重评条件，会让第二轮
        # 合法地再调一次，就测不到「保鲜期内跳过」了。
        build_database(self.database, [them(1, 0), me(2, 60)])
        self.run_llm_analysis(
            chat=lambda system, user: '{"score": 50, "tags": ["游戏"]}'
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
        with patch(
            "wechat_insights.analyzer.scan_direct_rows", return_value=sessions
        ), patch(
            "wechat_insights.llm.chat", side_effect=lambda s, u: '{"score": 40}'
        ) as fake, patch("wechat_insights.analyzer.LLM_MAX_CALLS_PER_RUN", 2):
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
                '{"score": 72, "summary": "你们最近聊工作与近况，相处轻松自然。", '
                '"anomaly_note": "可能是最近见面变少了"}'
            )

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            result, fake = self.run_llm_analysis(chat=chat)
        self.assertEqual(result.llm_scored, 1)
        self.assertEqual(fake.call_count, 1)
        row = self.store.get_llm_depth(SESSION_ID)
        self.assertIsNotNone(row)
        self.assertEqual(row.score, 72.0)
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
                '{"score": 72, "summary": "你们最近聊工作与近况。",'
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
                chat=lambda s, u: '{"score": 55, "tags": []}'
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
        for reply in ('{"score": 55}', '{"score": 55, "tags": "游戏"}'):
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
            '{"score": 55, "tags": ["超长标签超过八个字", "游戏", "深夜谈心",'
            ' "工作吐槽", "电影", 42]}'
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

        first, _ = self.run_llm_analysis(chat=lambda s, u: '{"score": 50}')
        self.assertEqual(first.llm_scored, 1)
        self.assertIsNone(self.store.get_llm_depth(SESSION_ID).tags)

        second, fake = self.run_llm_analysis(
            now=NOW + 5 * 86400,
            chat=lambda s, u: '{"score": 50, "tags": ["游戏"]}',
        )
        self.assertEqual(second.llm_scored, 1)
        self.assertEqual(fake.call_count, 1)
        row = self.store.get_llm_depth(SESSION_ID)
        self.assertEqual((row.score, row.tags), (50.0, ["游戏"]))

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

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            first, fake = self.run_llm_analysis(chat=chat)
        self.assertEqual(first.llm_scored, 1)
        self.assertEqual(fake.call_count, 1)
        row = self.store.get_llm_depth(SESSION_ID)
        self.assertEqual(row.anomalies_key, "msgs_them_per_day:worse")

        # 第二轮：近期窗口再灌 60 条改变异动集合，不新增 DB 消息、也没过
        # 保鲜期/消息数门槛——只有指纹变化触发重评，解释随之更新。
        self.seed_messages("friend", "Alice", {BASE + 6 * 86400: 60})
        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
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
            SESSION_ID, 50.0, NOW, 70, "旧摘要", "旧解释", "msgs_them_per_day:better"
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

    def test_missing_summary_keeps_the_score(self) -> None:
        rows = []
        for index in range(1, 21):
            offset = index * 120
            rows.append(them(index, offset) if index % 2 else me(index, offset))
        build_database(self.database, rows)
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            result, _ = self.run_llm_analysis(chat=lambda s, u: '{"score": 55}')
        self.assertEqual(result.llm_scored, 1)
        row = self.store.get_llm_depth(SESSION_ID)
        self.assertIsNotNone(row)
        self.assertEqual((row.score, row.summary), (55.0, ""))
        payload = {
            p["display_name"]: p for p in self.store.all_scores()
        }[DISPLAY_NAME]
        self.assertIsNone(payload["llm_summary"])
        self.assertIsNone(payload["llm_summary_at"])

    def test_garbage_summary_and_empty_note_keep_the_score(self) -> None:
        rows = []
        for index in range(1, 21):
            offset = index * 120
            rows.append(them(index, offset) if index % 2 else me(index, offset))
        build_database(self.database, rows)
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            result, _ = self.run_llm_analysis(
                chat=lambda s, u: '{"score": 55, "summary": 123, "anomaly_note": ""}'
            )
        self.assertEqual(result.llm_scored, 1)
        row = self.store.get_llm_depth(SESSION_ID)
        self.assertIsNotNone(row)
        self.assertEqual(row.score, 55.0)
        self.assertEqual(row.summary, "")
        self.assertIsNone(row.anomaly_note)
        payload = {
            p["display_name"]: p for p in self.store.all_scores()
        }[DISPLAY_NAME]
        self.assertIsNone(payload["llm_summary"])

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
            return '{"score": 45, "anomaly_note": "最近联系变少了"}'

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

    def test_llm_scores_flow_into_the_scoring_extras(self) -> None:
        # 两人窗口内只有纯消息计数、没有任何词法文本指标（三项全部缺值），
        # llm 组件的 0.5 权重整份生效：深度维度完全由 LLM 分决定（75 vs 25）。
        for session_id, name in (("a", "A"), ("b", "B")):
            self.seed_messages(
                session_id,
                name,
                {BASE + offset * 86400: 3 for offset in range(25)},
            )
        self.store.set_llm_depth("a", 90.0, NOW, 75)
        self.store.set_llm_depth("b", 10.0, NOW, 75)

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 50):
            result, fake = self.run_llm_analysis(chat=lambda s, u: '{"score": 1}')
        # 两个联系人的 last_message_at 是 None（seed 走的不是同步循环），
        # 不会进采样候选，chat 不应被调用。
        self.assertEqual(fake.call_count, 0)
        payloads = {p["display_name"]: p for p in self.store.all_scores()}
        self.assertTrue(payloads["A"]["scored"])
        self.assertAlmostEqual(payloads["A"]["dimensions"]["depth"], 75.0)
        self.assertAlmostEqual(payloads["B"]["dimensions"]["depth"], 25.0)


class ScoreHistoryTests(AnalyzerTestCase):
    """关系温度历史：scored 记当天综合分、zeroed 记 0、数据不足不记。"""

    def test_history_records_scored_and_zeroed_but_not_thin(self) -> None:
        # Alice 达标被打分；Ghost 两年无往来归零；Thin 有消息但数据不足。
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})
        self.seed_messages(SESSION_ID, DISPLAY_NAME, {BASE + 5 * 86400: 20})
        self.seed_messages("thin", "Thin", {BASE + 5 * 86400: 2})
        self.store.ensure_contact("ghost", "Ghost")

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis(now=NOW)

        today = day_key(NOW)
        alice = self.store.load_score_history(SESSION_ID)
        self.assertEqual(len(alice), 1)
        self.assertEqual(alice[0][0], today)
        # 采样点与 scores 表里当轮的 payload 一致（分数 + 七维 JSON）。
        payload = next(
            p for p in self.store.all_scores() if p["display_name"] == DISPLAY_NAME
        )
        self.assertEqual(alice[0][1], payload["overall"])
        self.assertEqual(json.loads(alice[0][2]), payload["dimensions"])

        ghost = self.store.load_score_history("ghost")
        self.assertEqual(ghost[0][0], today)
        self.assertEqual(ghost[0][1], 0.0)
        self.assertEqual(set(json.loads(ghost[0][2]).values()), {0.0})

        self.assertEqual(self.store.load_score_history("thin"), [])

    def test_two_rounds_on_the_same_day_keep_one_point_per_day(self) -> None:
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})
        self.seed_messages(SESSION_ID, DISPLAY_NAME, {BASE + 5 * 86400: 20})

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis(now=NOW)
            self.run_analysis(now=NOW)

        rows = self.store.load_score_history(SESSION_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], day_key(NOW))


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
        _, events = self.run_with_progress()
        self.assertEqual(
            [event["phase"] for event in events], ["sync", "sync", "score"]
        )
        # sync 起点：total=会话数、done=0；随后每个会话前上报一次 display_name。
        self.assertEqual(events[0], {"phase": "sync", "done": 0, "total": 1, "detail": ""})
        self.assertEqual(
            events[1],
            {"phase": "sync", "done": 0, "total": 1, "detail": DISPLAY_NAME},
        )
        # score 阶段没有逐项计数：total=0。
        self.assertEqual(events[2], {"phase": "score", "done": 0, "total": 0, "detail": ""})

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
        ) as fake, patch("wechat_insights.analyzer.LLM_MAX_CALLS_PER_RUN", 2):
            result = self.analyzer(
                strategy=LLMDepth(), progress_cb=lambda fields: events.append(dict(fields))
            ).run(now=NOW)
        self.assertEqual(result.llm_scored, 2)
        self.assertEqual(fake.call_count, 2)
        phases = [event["phase"] for event in events]
        # 阶段顺序：sync（起点 + 3 个会话）→ llm（起点 + 2 个候选）→ score。
        self.assertEqual(
            phases, ["sync"] * 4 + ["llm"] * 3 + ["score"]
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
