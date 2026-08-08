from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wechat_history.formatting import message_kind

from wechat_insights.analyzer import Analyzer, Cursor, read_messages_after
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


def build_database(path: Path, rows: list[tuple[int, int, int, int, str]]) -> None:
    """rows = (local_id, local_type, create_time, real_sender_id, content)。"""

    table = FakeReader._message_table(SESSION_ID)
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

    def analyzer(self, batch_size: int = 5000) -> Analyzer:
        return Analyzer(
            self.store, reader_factory=lambda: self.reader, batch_size=batch_size
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


if __name__ == "__main__":
    unittest.main()
