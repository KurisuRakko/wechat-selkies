from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from wechat_insights.constants import SCHEMA_VERSION
from wechat_insights.metrics import Metrics, quantile
from wechat_insights.reporting import monthly_series, total_metrics, type_composition
from wechat_insights.storage import MetricsStore, WindowStats, contact_hash


def bucket(**counts: int) -> Metrics:
    metrics = Metrics()
    for name, value in counts.items():
        metrics.add(name, value)
    return metrics


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
    def test_set_and_get_round_trip(self) -> None:
        self.assertIsNone(self.store.get_llm_depth("friend"))
        self.store.set_llm_depth("friend", 66.0, 1_700_000_000, 120)
        row = self.store.get_llm_depth("friend")
        self.assertEqual(row.score, 66.0)
        self.assertEqual(row.scored_at, 1_700_000_000)
        self.assertEqual(row.total_messages, 120)

    def test_setting_twice_is_an_upsert(self) -> None:
        self.store.set_llm_depth("friend", 66.0, 1_700_000_000, 120)
        self.store.set_llm_depth("friend", 80.0, 1_700_000_100, 150)
        self.assertEqual(len(self.store.all_llm_depth()), 1)
        row = self.store.get_llm_depth("friend")
        self.assertEqual(
            (row.score, row.scored_at, row.total_messages),
            (80.0, 1_700_000_100, 150),
        )

    def test_all_llm_depth_maps_session_ids_to_scores(self) -> None:
        self.store.set_llm_depth("a", 10.0, 1, 10)
        self.store.set_llm_depth("b", 20.0, 2, 20)
        self.assertEqual(self.store.all_llm_depth(), {"a": 10.0, "b": 20.0})

    def test_table_survives_reinitializing_an_existing_database(self) -> None:
        # 对既有库再走一次 _initialize：executescript 全部 IF NOT EXISTS，
        # 新表照常补齐、旧数据不动。
        self.store.set_llm_depth("friend", 30.0, 1, 10)
        self.store.close()
        reopened = MetricsStore(self.store.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_llm_depth("friend").score, 30.0)
        reopened.set_llm_depth("friend", 40.0, 2, 20)
        self.assertEqual(reopened.get_llm_depth("friend").score, 40.0)


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
