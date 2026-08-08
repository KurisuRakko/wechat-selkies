from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wechat_insights.metrics import Metrics, quantile
from wechat_insights.reporting import monthly_series, total_metrics, type_composition
from wechat_insights.storage import MetricsStore, contact_hash


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
        self.store.commit_batch(
            "friend", {"2026-03-10": bucket(msgs_them=3)}, contact
        )
        self.assertEqual(self.store.load_days("friend")[0][1].get("msgs_them"), 3)
        self.assertEqual(self.store.get_contact("friend").cursor_local_id, 42)

    def test_window_query_sums_across_contacts(self) -> None:
        self.store.merge_daily("a", {"2026-03-10": bucket(msgs_them=1, msgs_me=2)})
        self.store.merge_daily("b", {"2026-03-11": bucket(msgs_them=5)})
        totals = self.store.load_window("2026-03-01", "2026-03-31")
        self.assertEqual(totals["a"].messages_total(), 3)
        self.assertEqual(totals["b"].messages_total(), 5)


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
