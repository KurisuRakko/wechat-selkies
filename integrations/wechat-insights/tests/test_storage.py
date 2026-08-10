from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from wechat_insights.constants import SCHEMA_VERSION
from wechat_insights.metrics import Metrics, quantile
from wechat_insights.reporting import monthly_series, total_metrics, type_composition
from wechat_insights.storage import MetricsStore, WindowStats, contact_hash


def bucket(**counts: int) -> Metrics:
    metrics = Metrics()
    for name, value in counts.items():
        metrics.add(name, value)
    return metrics


def seed_legacy_llm_depth(path: Path) -> None:
    """建一个带 score 列的老形状 metrics.db（llm_depth 去列迁移的输入形状）。"""

    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE llm_depth (
                session_id     TEXT PRIMARY KEY,
                score          REAL NOT NULL,
                scored_at      INTEGER NOT NULL,
                total_messages INTEGER NOT NULL,
                summary        TEXT NOT NULL DEFAULT '',
                anomaly_note   TEXT NOT NULL DEFAULT '',
                anomalies_key  TEXT NOT NULL DEFAULT '',
                tags           TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO meta (key, value) VALUES ('schema_version', '2');
            INSERT INTO llm_depth VALUES ('friend', 55.0, 1000, 42,
                '旧摘要', '旧解释', 'old:key', '["游戏"]');
            """
        )


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MetricsStore(Path(self.temporary.name) / "metrics.db")
        self.addCleanup(self.store.close)


class HashTests(unittest.TestCase):
    def test_hash_is_stable_and_short(self) -> None:
        value = contact_hash("wxid_example")
        self.assertEqual(len(value), 24)
        self.assertEqual(value, contact_hash("wxid_example"))
        self.assertNotEqual(value, contact_hash("wxid_other"))


class DailyMergeTests(StoreTestCase):
    def test_repeated_merges_accumulate_counts_and_histograms(self) -> None:
        first = bucket(msgs_them=2)
        first.add_reply("incoming", 30)
        second = bucket(msgs_them=3)
        second.add_reply("incoming", 30)

        self.store.merge_daily("friend", {"2026-03-10": first})
        self.store.merge_daily("friend", {"2026-03-10": second})

        days = self.store.load_days("friend")
        self.assertEqual(len(days), 1)
        stored = days[0][1]
        self.assertEqual(stored.get("msgs_them"), 5)
        self.assertEqual(stored.get("replies_them"), 2)
        self.assertEqual(stored.get("fast_replies_them"), 2)
        self.assertEqual(sum(stored.reply_hist_them), 2)
        self.assertIsNotNone(quantile(stored.reply_hist_them, 0.5))

    def test_window_query_respects_the_date_range(self) -> None:
        self.store.merge_daily("friend", {"2026-03-01": bucket(msgs_them=1)})
        self.store.merge_daily("friend", {"2026-03-20": bucket(msgs_them=4)})
        inside = self.store.load_window("2026-03-10", "2026-03-31")
        self.assertEqual(inside["friend"].get("msgs_them"), 4)
        self.assertEqual(self.store.load_window("2026-04-01", "2026-04-30"), {})

    def test_commit_batch_writes_metrics_and_cursor_together(self) -> None:
        contact = self.store.ensure_contact("friend", "Alice")
        contact.cursor_timestamp = 1_700_000_000
        contact.cursor_local_id = 42
        contact.cursor_shard = "message/message_3.db"
        self.store.commit_batch(
            "friend", {"2026-03-10": bucket(msgs_them=3)}, contact
        )
        stored = self.store.get_contact("friend")
        self.assertEqual(self.store.load_days("friend")[0][1].get("msgs_them"), 3)
        self.assertEqual(stored.cursor_local_id, 42)
        self.assertEqual(stored.cursor_shard, "message/message_3.db")

    def test_window_query_sums_across_contacts(self) -> None:
        self.store.merge_daily("a", {"2026-03-10": bucket(msgs_them=1, msgs_me=2)})
        self.store.merge_daily("b", {"2026-03-11": bucket(msgs_them=5)})
        totals = self.store.load_window("2026-03-01", "2026-03-31")
        self.assertEqual(totals["a"].messages_total(), 3)
        self.assertEqual(totals["b"].messages_total(), 5)


class WindowStatsTests(StoreTestCase):
    def test_window_stats_accumulates_raw_weighted_and_activity(self) -> None:
        # 隔天活跃（2026-03-10 与 03-12），中间空 03-11：最长间隔 = 2 天。
        self.store.merge_daily(
            "friend", {"2026-03-10": bucket(msgs_them=3), "2026-03-12": bucket(msgs_them=5)}
        )
        weights = {"2026-03-10": 0.5, "2026-03-11": 0.25, "2026-03-12": 1.0}

        stats = self.store.load_window_stats("2026-03-01", "2026-03-31", weights.get)
        entry = stats["friend"]
        self.assertIsInstance(entry, WindowStats)
        self.assertEqual(entry.raw.messages_total(), 8)
        self.assertEqual(entry.weighted.get("msgs_them"), 3 * 0.5 + 5 * 1.0)
        self.assertEqual(entry.active_weight, 1.5)
        self.assertEqual(entry.first_day, "2026-03-10")
        self.assertEqual(entry.last_day, "2026-03-12")
        self.assertEqual(entry.longest_gap_days, 2)

    def test_window_limits_and_ordering_bound_the_scan(self) -> None:
        self.store.merge_daily("friend", {"2026-03-10": bucket(msgs_them=1)})
        self.store.merge_daily("friend", {"2026-03-20": bucket(msgs_them=2)})
        weights = {"2026-03-10": 0.5, "2026-03-20": 0.25}

        stats = self.store.load_window_stats("2026-03-11", "2026-03-19", weights.get)
        self.assertNotIn("friend", stats)
        self.assertEqual(self.store.load_window_stats("2026-04-01", "2026-04-30", weights.get), {})

    def test_zero_message_rows_do_not_count_as_active_days(self) -> None:
        empty = bucket()
        self.store.merge_daily("friend", {"2026-03-10": empty, "2026-03-12": bucket(msgs_them=1)})
        weights = {"2026-03-10": 1.0, "2026-03-12": 1.0}

        stats = self.store.load_window_stats("2026-03-01", "2026-03-31", weights.get)
        entry = stats["friend"]
        self.assertEqual(entry.first_day, "2026-03-12")
        self.assertEqual(entry.active_weight, 1.0)
        self.assertEqual(entry.longest_gap_days, 0)


class ContactTests(StoreTestCase):
    def test_contact_is_created_once_and_renamed_in_place(self) -> None:
        created = self.store.ensure_contact("friend", "旧名字")
        renamed = self.store.ensure_contact("friend", "新名字")
        self.assertEqual(created.hash, renamed.hash)
        self.assertEqual(renamed.display_name, "新名字")
        self.assertEqual(len(self.store.all_contacts()), 1)

    def test_fresh_cursor_can_read_local_id_zero(self) -> None:
        contact = self.store.ensure_contact("friend", "Alice")
        self.assertEqual(contact.cursor_timestamp, 0)
        self.assertEqual(contact.cursor_local_id, -1)
        # 空分片排在一切真实分片之前，保证首轮全量读取。
        self.assertEqual(contact.cursor_shard, "")

    def test_milestones_derive_days_known_and_night_clock(self) -> None:
        contact = self.store.ensure_contact("friend", "Alice")
        contact.first_message_at = 1_600_000_000
        contact.last_message_at = 1_600_000_000 + 86400 * 9
        contact.total_messages = 120
        contact.latest_night_offset = 4 * 3600 + 52 * 60
        contact.latest_night_at = 1_600_000_000
        self.store.save_contact(contact)

        milestones = self.store.get_contact("friend").milestones()
        self.assertEqual(milestones["days_known"], 10)
        self.assertEqual(milestones["total_messages"], 120)
        self.assertEqual(milestones["latest_night_clock"], "04:52")

    def test_missing_history_leaves_milestones_empty(self) -> None:
        milestones = self.store.ensure_contact("friend", "Alice").milestones()
        self.assertIsNone(milestones["days_known"])
        self.assertIsNone(milestones["latest_night_clock"])
        self.assertIsNone(milestones["longest_silence_seconds"])


class ScoreTests(StoreTestCase):
    def test_saved_payloads_never_contain_the_wxid(self) -> None:
        payload = {"hash": contact_hash("friend"), "display_name": "Alice"}
        self.store.save_scores([("wxid_secret", payload)])
        stored = self.store.score_by_hash(payload["hash"])
        self.assertEqual(stored["display_name"], "Alice")
        self.assertNotIn("session_id", stored)
        self.assertNotIn("wxid_secret", str(stored))

    def test_saving_replaces_the_previous_round(self) -> None:
        self.store.save_scores([("a", {"hash": "a" * 24, "display_name": "A"})])
        self.store.save_scores([("b", {"hash": "b" * 24, "display_name": "B"})])
        self.assertEqual(len(self.store.all_scores()), 1)
        self.assertIsNone(self.store.score_by_hash("a" * 24))

    def test_meta_json_round_trips(self) -> None:
        self.store.set_json("medians", {"depth": 42.0})
        self.assertEqual(self.store.get_json("medians"), {"depth": 42.0})
        self.assertEqual(self.store.get_json("missing", {}), {})


class LLMDepthTests(StoreTestCase):
    def test_set_and_get_round_trip_with_all_columns(self) -> None:
        self.assertIsNone(self.store.get_llm_depth("friend"))
        self.store.set_llm_depth(
            "friend", 1_700_000_000, 120, "关系画像", "异动解释", "a:worse|b:better"
        )
        row = self.store.get_llm_depth("friend")
        # 打分缓存列已整体移除，数值信号由 llm_period 表接管。
        self.assertFalse(hasattr(row, "score"))
        self.assertEqual(row.scored_at, 1_700_000_000)
        self.assertEqual(row.total_messages, 120)
        self.assertEqual(row.summary, "关系画像")
        self.assertEqual(row.anomaly_note, "异动解释")
        self.assertEqual(row.anomalies_key, "a:worse|b:better")

    def test_empty_anomaly_note_is_normalised_to_none(self) -> None:
        self.store.set_llm_depth("friend", 1, 10, "摘要", "")
        row = self.store.get_llm_depth("friend")
        self.assertIsNone(row.anomaly_note)
        self.assertEqual(row.summary, "摘要")

    def test_setting_twice_is_an_upsert_of_every_column(self) -> None:
        self.store.set_llm_depth("friend", 1, 120, "旧摘要", "旧解释", "old")
        self.store.set_llm_depth("friend", 2, 150, "新摘要", "新解释", "new")
        self.assertEqual(len(self.store.all_llm_depth()), 1)
        row = self.store.get_llm_depth("friend")
        self.assertEqual(
            (row.scored_at, row.total_messages),
            (2, 150),
        )
        self.assertEqual(
            (row.summary, row.anomaly_note, row.anomalies_key),
            ("新摘要", "新解释", "new"),
        )

    def test_all_llm_depth_maps_session_ids_to_rows(self) -> None:
        self.store.set_llm_depth("a", 1, 10, "摘要A", "解释A", "a")
        self.store.set_llm_depth("b", 2, 20)
        rows = self.store.all_llm_depth()
        self.assertEqual(
            (rows["a"].summary, rows["a"].anomaly_note),
            ("摘要A", "解释A"),
        )
        self.assertEqual(rows["b"].summary, "")
        self.assertIsNone(rows["b"].anomaly_note)

    def test_table_survives_reinitializing_an_existing_database(self) -> None:
        # 对既有库再走一次 _initialize：executescript 全部 IF NOT EXISTS，
        # 新表照常补齐、旧数据不动；补列迁移同样幂等。
        self.store.set_llm_depth("friend", 1, 10, "摘要", "解释", "k")
        self.store.close()
        reopened = MetricsStore(self.store.path)
        self.addCleanup(reopened.close)
        row = reopened.get_llm_depth("friend")
        self.assertEqual(row.summary, "摘要")
        reopened.set_llm_depth("friend", 2, 20, "新摘要")
        row = reopened.get_llm_depth("friend")
        self.assertEqual(row.summary, "新摘要")

    def test_old_shaped_table_is_migrated_in_place(self) -> None:
        # 3a15872 形状的 llm_depth（没有三新列）：本地开发/测试库在加列前
        # 建过表，_initialize 要幂等地补列并保留旧行。先建新形状的库，
        # 再手动删掉三列模拟旧形状，重新初始化后三列回来、旧数据还在。
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "metrics.db"
        seed = MetricsStore(path)
        seed.set_llm_depth("friend", 1_000, 42, "旧摘要", "旧解释", "old:key")
        seed.close()
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE llm_depth DROP COLUMN summary")
            connection.execute("ALTER TABLE llm_depth DROP COLUMN anomaly_note")
            connection.execute("ALTER TABLE llm_depth DROP COLUMN anomalies_key")

        store = MetricsStore(path)
        self.addCleanup(store.close)
        columns = {
            row[1] for row in store.connection.execute("PRAGMA table_info(llm_depth)")
        }
        self.assertLessEqual(
            {"summary", "anomaly_note", "anomalies_key", "tags"}, columns
        )
        row = store.get_llm_depth("friend")
        self.assertEqual(row.total_messages, 42)
        self.assertEqual(row.summary, "")
        self.assertIsNone(row.anomaly_note)
        self.assertEqual(row.anomalies_key, "")

    def test_tags_column_is_migrated_in_place(self) -> None:
        # 更早形状的 llm_depth 连 tags 列都没有：_initialize 幂等地补上，
        # 旧行读回 tags 是 None（触发下一轮重评补齐），而不是崩在缺列上。
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "metrics.db"
        seed = MetricsStore(path)
        seed.set_llm_depth("friend", 1_000, 42, "旧摘要")
        seed.close()
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE llm_depth DROP COLUMN tags")

        store = MetricsStore(path)
        self.addCleanup(store.close)
        columns = {
            row[1] for row in store.connection.execute("PRAGMA table_info(llm_depth)")
        }
        self.assertIn("tags", columns)
        row = store.get_llm_depth("friend")
        self.assertEqual(row.summary, "旧摘要")
        self.assertIsNone(row.tags)

    def test_existing_llm_depth_score_column_is_migrated_away(self) -> None:
        # 带 score 列的老形状 llm_depth：score 是 NOT NULL 且无默认值，只
        # 停止写入会让 INSERT 违反约束，必须重建整张表把它去掉。摘要/标签
        # 等其余列原样保留。
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "metrics.db"
        seed_legacy_llm_depth(path)

        store = MetricsStore(path)
        self.addCleanup(store.close)
        columns = {
            row[1] for row in store.connection.execute("PRAGMA table_info(llm_depth)")
        }
        self.assertNotIn("score", columns)
        self.assertLessEqual({"scored_at", "summary", "anomalies_key", "tags"}, columns)
        row = store.get_llm_depth("friend")
        self.assertFalse(hasattr(row, "score"))
        self.assertEqual(row.total_messages, 42)
        self.assertEqual(row.summary, "旧摘要")
        self.assertEqual(row.anomalies_key, "old:key")
        self.assertEqual(row.tags, ["游戏"])
        # 迁移后的形状可以正常再次写入（重建前这一步会违反 score 的 NOT NULL）。
        store.set_llm_depth("friend", 1001, 43, "新摘要")
        self.assertEqual(store.get_llm_depth("friend").summary, "新摘要")

    def test_llm_depth_rebuild_backs_up_before_dropping_the_score_column(self) -> None:
        # 去 score 列之前必须留一份快照：备份拍在动手之前，里面还有 score。
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "metrics.db"
        seed_legacy_llm_depth(path)

        store = MetricsStore(path)
        self.addCleanup(store.close)
        columns = {
            row[1] for row in store.connection.execute("PRAGMA table_info(llm_depth)")
        }
        self.assertNotIn("score", columns)
        backup = path.parent / (
            f"metrics-backup-llm-depth-rebuild-{date.today().isoformat()}.db"
        )
        self.assertTrue(backup.exists())
        with sqlite3.connect(backup) as old:
            old_columns = {
                row[1] for row in old.execute("PRAGMA table_info(llm_depth)")
            }
        self.assertIn("score", old_columns)

    def test_failed_backup_leaves_the_legacy_score_column_in_place(self) -> None:
        # 备份拿不到就跳过重建（服务照常能起来）：score 列原样保留，其余
        # 补列不受影响，日志留一条 ERROR 供运维定位。
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "metrics.db"
        seed_legacy_llm_depth(path)

        with patch(
            "wechat_insights.migrations.backup_database", return_value=None
        ), self.assertLogs("wechat-insights", level="ERROR") as logs:
            store = MetricsStore(path)
        self.addCleanup(store.close)
        columns = {
            row[1] for row in store.connection.execute("PRAGMA table_info(llm_depth)")
        }
        self.assertIn("score", columns)
        # 其余补列照常：llm_depth 的补列清单与 contacts 的补列都跑过了。
        self.assertLessEqual({"summary", "tags"}, columns)
        contact_columns = {
            row[1] for row in store.connection.execute("PRAGMA table_info(contacts)")
        }
        self.assertIn("kind_auto", contact_columns)
        self.assertTrue(any("备份失败" in line for line in logs.output))

    def test_tags_round_trip_none_empty_list_and_list(self) -> None:
        # None → ''（读回 None，触发重评补齐）；[] 是合法值照存、读回空
        # 列表；普通列表走紧凑 JSON 往返。
        self.store.set_llm_depth("friend", 1, 10, "摘要", None, "", None)
        self.assertIsNone(self.store.get_llm_depth("friend").tags)
        self.store.set_llm_depth("friend", 1, 10, "摘要", None, "", [])
        self.assertEqual(self.store.get_llm_depth("friend").tags, [])
        self.store.set_llm_depth(
            "friend", 1, 10, "摘要", None, "", ["游戏", "深夜谈心"]
        )
        self.assertEqual(
            self.store.get_llm_depth("friend").tags, ["游戏", "深夜谈心"]
        )

    def test_corrupt_tags_json_reads_as_none(self) -> None:
        self.store.set_llm_depth("friend", 1, 10, "摘要", None, "", ["游戏"])
        with self.store.connection as connection:
            connection.execute(
                "UPDATE llm_depth SET tags = 'oops' WHERE session_id = 'friend'"
            )
        self.assertIsNone(self.store.get_llm_depth("friend").tags)


class PeriodTests(StoreTestCase):
    """llm_period 时段分快照的写入、覆盖与聚合查询。"""

    def test_set_and_get_round_trip(self) -> None:
        self.assertEqual(self.store.all_llm_periods(), {})
        self.store.set_llm_period("friend", "2025-10", "2025-10-31", 55.0, 70.0, 62.0, 1_000)
        rows = self.store.all_llm_periods()
        self.assertEqual(list(rows), ["friend"])
        row = rows["friend"][0]
        self.assertEqual(
            (row.period, row.period_end, row.depth, row.warmth, row.mutuality),
            ("2025-10", "2025-10-31", 55.0, 70.0, 62.0),
        )

    def test_same_period_with_different_period_end_coexist(self) -> None:
        # 当月未收口时每轮重评都会新写一张快照（period_end = 评分当天），
        # 旧快照保留：回放到旧时刻时取的是当时那一张。
        self.store.set_llm_period("friend", "2025-10", "2025-10-15", 50.0, 60.0, 55.0, 1)
        self.store.set_llm_period("friend", "2025-10", "2025-10-22", 52.0, 61.0, 56.0, 2)
        rows = self.store.all_llm_periods()["friend"]
        self.assertEqual([row.period_end for row in rows], ["2025-10-15", "2025-10-22"])
        self.assertEqual(rows[0].depth, 50.0)
        self.assertEqual(rows[1].depth, 52.0)

    def test_same_period_end_overwrites_the_snapshot(self) -> None:
        # 同一天重跑一轮 = 同一把键，直接覆盖旧分，不产生重复行。
        self.store.set_llm_period("friend", "2025-10", "2025-10-31", 50.0, 60.0, 55.0, 1)
        self.store.set_llm_period("friend", "2025-10", "2025-10-31", 90.0, 80.0, 85.0, 2)
        rows = self.store.all_llm_periods()["friend"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].depth, rows[0].warmth, rows[0].mutuality), (90.0, 80.0, 85.0))

    def test_monthly_text_counts_buckets_across_months(self) -> None:
        self.store.merge_daily(
            "friend",
            {
                "2025-10-31": bucket(kind_text_them=5, kind_text_me=3),
                "2025-11-01": bucket(kind_text_them=2, kind_text_me=1),
            },
        )
        self.store.merge_daily("other", {"2025-11-15": bucket(kind_text_them=7)})
        counts = self.store.monthly_text_counts()
        self.assertEqual(counts["friend"]["2025-10"], 8)
        self.assertEqual(counts["friend"]["2025-11"], 3)
        self.assertEqual(counts["other"]["2025-11"], 7)

    def test_period_coverage_tracks_the_latest_period_end(self) -> None:
        self.store.set_llm_period("friend", "2025-10", "2025-10-15", 50.0, 60.0, 55.0, 1)
        self.store.set_llm_period("friend", "2025-10", "2025-10-31", 55.0, 65.0, 60.0, 2)
        self.store.set_llm_period("friend", "2025-11", "2025-11-30", 60.0, 70.0, 65.0, 3)
        coverage = self.store.period_coverage()
        self.assertEqual(coverage[("friend", "2025-10")], "2025-10-31")
        self.assertEqual(coverage[("friend", "2025-11")], "2025-11-30")

    def test_period_rows_group_by_session_in_period_order(self) -> None:
        self.store.set_llm_period("b", "2025-10", "2025-10-31", 1.0, 1.0, 1.0, 1)
        self.store.set_llm_period("a", "2025-09", "2025-09-30", 2.0, 2.0, 2.0, 2)
        self.store.set_llm_period("a", "2025-10", "2025-10-31", 3.0, 3.0, 3.0, 3)
        rows = self.store.all_llm_periods()
        self.assertEqual(list(rows), ["a", "b"])
        self.assertEqual([row.period for row in rows["a"]], ["2025-09", "2025-10"])


class MetaMaintenanceTests(StoreTestCase):
    def test_delete_meta_removes_a_key(self) -> None:
        self.store.set_meta("score_history_backfilled", "2026-03-10")
        self.store.delete_meta("score_history_backfilled")
        self.assertIsNone(self.store.get_meta("score_history_backfilled"))

    def test_delete_meta_of_a_missing_key_is_a_no_op(self) -> None:
        self.store.delete_meta("never_written")
        self.assertIsNone(self.store.get_meta("never_written"))

    def test_reset_daily_refine_progress_clears_only_daily_contacts(self) -> None:
        daily = self.store.ensure_contact("daily_friend", "Daily")
        weekly = self.store.ensure_contact("weekly_friend", "Weekly")
        self.store.set_history_granularity("daily_friend", "day")
        self.store.mark_daily_refined(["daily_friend"], "2026-03-10")
        self.store.mark_daily_refined(["weekly_friend"], "2026-03-10")

        cleared = self.store.reset_daily_refine_progress()
        self.assertEqual(cleared, 1)
        self.assertEqual(self.store.get_contact("daily_friend").history_daily_until, "")
        # 每周粒度的联系人进度原样保留（重置只针对每日细化的联系人）。
        self.assertEqual(
            self.store.get_contact("weekly_friend").history_daily_until, "2026-03-10"
        )


class ScoreHistoryTests(StoreTestCase):
    def test_same_day_records_overwrite_instead_of_accumulating(self) -> None:
        self.store.record_score_history(
            "2026-03-10", [("friend", 72.0, '{"responsiveness":80.0}')]
        )
        self.store.record_score_history(
            "2026-03-10", [("friend", 55.0, '{"responsiveness":60.0}')]
        )
        self.assertEqual(
            self.store.load_score_history("friend"),
            [("2026-03-10", 55.0, '{"responsiveness":60.0}')],
        )

    def test_load_returns_rows_ascending_and_scoped_to_the_session(self) -> None:
        self.store.record_score_history(
            "2026-03-12", [("friend", 1.0, "{}"), ("other", 9.0, "{}")]
        )
        self.store.record_score_history("2026-03-10", [("friend", 2.0, "{}")])
        self.store.record_score_history("2026-03-11", [("friend", 3.0, "{}")])
        self.assertEqual(
            self.store.load_score_history("friend"),
            [
                ("2026-03-10", 2.0, "{}"),
                ("2026-03-11", 3.0, "{}"),
                ("2026-03-12", 1.0, "{}"),
            ],
        )
        self.assertEqual(self.store.load_score_history("other"), [("2026-03-12", 9.0, "{}")])

    def test_empty_batch_writes_nothing(self) -> None:
        self.store.record_score_history("2026-03-10", [])
        self.assertEqual(self.store.load_score_history("friend"), [])


class ContactKindTests(StoreTestCase):
    """关系类型两列：幂等补列迁移、relation_kind 优先级、手动/自动读写。"""

    def test_kind_columns_migrate_in_place_idempotently(self) -> None:
        # 模拟生产旧库（没有两列）：_initialize 幂等补列、旧行保留。
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "metrics.db"
        seed = MetricsStore(path)
        seed.ensure_contact("friend", "Alice")
        seed.close()
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE contacts DROP COLUMN kind_auto")
            connection.execute("ALTER TABLE contacts DROP COLUMN kind_manual")

        store = MetricsStore(path)
        self.addCleanup(store.close)
        columns = {
            row[1] for row in store.connection.execute("PRAGMA table_info(contacts)")
        }
        self.assertLessEqual({"kind_auto", "kind_manual"}, columns)
        # 旧联系人读回两个空串：未判定的按默认 friend 处理。
        row = store.get_contact("friend")
        self.assertEqual((row.kind_auto, row.kind_manual), ("", ""))
        self.assertEqual(row.relation_kind(), "friend")
        # 再初始化一次仍然幂等（列已存在，ALTER 静默跳过）。
        store.close()
        reopened = MetricsStore(path)
        self.addCleanup(reopened.close)
        self.assertIn(
            "kind_auto",
            {r[1] for r in reopened.connection.execute("PRAGMA table_info(contacts)")},
        )

    def test_save_contact_round_trips_the_kind_columns(self) -> None:
        contact = self.store.ensure_contact("friend", "Alice")
        contact.kind_auto = "family"
        contact.kind_manual = "transactional"
        self.store.save_contact(contact)
        stored = self.store.get_contact("friend")
        self.assertEqual(
            (stored.kind_auto, stored.kind_manual), ("family", "transactional")
        )

    def test_relation_kind_prefers_manual_then_auto_then_default(self) -> None:
        self.store.ensure_contact("friend", "Alice")
        self.assertEqual(self.store.get_contact("friend").relation_kind(), "friend")
        self.store.set_contact_kind_auto("friend", "family")
        self.assertEqual(self.store.get_contact("friend").relation_kind(), "family")
        self.store.set_contact_kind_manual("friend", "transactional")
        self.assertEqual(
            self.store.get_contact("friend").relation_kind(), "transactional"
        )
        # 清除手动：回到自动判定结果；手动操作不碰 kind_auto 本身。
        self.store.set_contact_kind_manual("friend", "")
        self.assertEqual(self.store.get_contact("friend").relation_kind(), "family")
        self.assertEqual(self.store.get_contact("friend").kind_auto, "family")

    def test_update_score_payload_overwrites_only_the_given_row(self) -> None:
        self.store.save_scores(
            [
                ("a", {"hash": "a" * 24, "display_name": "A"}),
                ("b", {"hash": "b" * 24, "display_name": "B"}),
            ]
        )
        payload = self.store.score_by_hash("a" * 24)
        payload["relation_kind"] = "transactional"
        self.store.update_score_payload("a", payload)
        self.assertEqual(
            self.store.score_by_hash("a" * 24)["relation_kind"], "transactional"
        )
        # 其他行原样保留。
        self.assertNotIn("relation_kind", self.store.score_by_hash("b" * 24))


class SchemaMigrationTests(unittest.TestCase):
    def test_stale_schema_is_rebuilt_from_scratch(self) -> None:
        # 模拟旧版本 metrics.db：contacts 没有 cursor_shard 列。
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "metrics.db"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE contacts (session_id TEXT PRIMARY KEY)"
            )
            connection.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', '1')"
            )

        store = MetricsStore(path)
        self.addCleanup(store.close)
        columns = {
            row[1] for row in store.connection.execute("PRAGMA table_info(contacts)")
        }
        self.assertIn("cursor_shard", columns)
        self.assertEqual(store.get_meta("schema_version"), str(SCHEMA_VERSION))


class ReportingTests(StoreTestCase):
    def test_monthly_series_merges_days_into_calendar_months(self) -> None:
        first = bucket(msgs_them=3, msgs_me=1)
        for _ in range(5):
            first.add_reply("incoming", 30)
        self.store.merge_daily("friend", {"2026-03-02": first})
        self.store.merge_daily("friend", {"2026-03-28": bucket(msgs_them=2)})
        self.store.merge_daily("friend", {"2026-04-01": bucket(msgs_me=7)})

        series = monthly_series(self.store.load_days("friend"))
        self.assertEqual([item["month"] for item in series], ["2026-03", "2026-04"])
        self.assertEqual(series[0]["in"], 5)
        self.assertEqual(series[0]["out"], 1)
        self.assertIsNotNone(series[0]["reply_median_them"])
        # 四月没有回复样本，折线该断开而不是画成 0。
        self.assertIsNone(series[1]["reply_median_them"])

    def test_type_composition_drops_empty_kinds_and_sorts_by_volume(self) -> None:
        self.store.merge_daily(
            "friend",
            {
                "2026-03-02": bucket(
                    kind_text_them=10, kind_text_me=5, kind_image_them=2
                )
            },
        )
        composition = type_composition(total_metrics(self.store.load_days("friend")))
        self.assertEqual(
            composition, [{"kind": "text", "count": 15}, {"kind": "image", "count": 2}]
        )


if __name__ == "__main__":
    unittest.main()
