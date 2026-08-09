"""友谊年报（yearly_report）与叙事输入的测试。

yearly_report 是纯本地聚合，直接对着真实 SQLite 库构造两年数据验证各版块；
叙事测试验证输入只含匿名聚合数字——名单与消息原文绝不能进 prompt。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from wechat_insights.metrics import Metrics
from wechat_insights.reporting import (
    REPORT_NARRATIVE_MAX_CHARS,
    REPORT_NARRATIVE_SYSTEM_PROMPT,
    generate_narrative,
    narrative_user_text,
    yearly_report,
)
from wechat_insights.storage import MetricsStore


def bucket(**counts: int) -> Metrics:
    metrics = Metrics()
    for name, value in counts.items():
        metrics.add(name, value)
    return metrics


# 固定「现在」：2026-08-15 中午，测试不依赖真实时钟。
NOW = int(datetime(2026, 8, 15, 12, 0).timestamp())


class YearlyReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MetricsStore(Path(self.temporary.name) / "metrics.db")
        self.addCleanup(self.store.close)
        self._seed()

    def _seed(self) -> None:
        """两个年度的固定数据：

        - 老友：2025 年 200 条、2026 年 5 条 → 淡出；
        - 常青：2025 年 300 条、2026 年 30 条（恰好 10%，不淡出）；
        - 话痨 / 夜猫 / 周末搭子 / 新朋友 / 哈哈怪：只在 2026 年有数据。
        """
        for session_id, name in (
            ("old_friend", "老友"),
            ("steady", "常青"),
            ("active", "话痨"),
            ("night_owl", "夜猫"),
            ("weekend_pal", "周末搭子"),
            ("new_pal", "新朋友"),
            ("king", "哈哈怪"),
        ):
            self.store.ensure_contact(session_id, name)

        self.store.merge_daily(
            "old_friend",
            {"2025-06-01": bucket(msgs_them=100, msgs_me=100)},
        )
        self.store.merge_daily("old_friend", {"2026-06-01": bucket(msgs_me=5)})
        self.store.merge_daily(
            "steady",
            {"2025-06-01": bucket(msgs_them=150, msgs_me=150)},
        )
        self.store.merge_daily(
            "steady", {"2026-06-01": bucket(msgs_them=15, msgs_me=15)}
        )
        self.store.merge_daily(
            "active", {"2026-06-10": bucket(msgs_them=300, msgs_me=200)}
        )
        self.store.merge_daily(
            "night_owl",
            {
                "2026-06-11": bucket(
                    msgs_them=10, msgs_me=2, night_msgs_them=8, night_msgs_me=2
                )
            },
        )
        self.store.merge_daily(
            "weekend_pal",
            {"2026-06-13": bucket(msgs_them=2, msgs_me=2, weekend_msgs_them=6)},
        )
        self.store.merge_daily(
            "new_pal", {"2026-07-01": bucket(msgs_them=3, msgs_me=3)}
        )
        self.store.merge_daily("king", {"2026-06-20": bucket(msgs_them=1, msgs_me=1)})

        # 里程碑字段：新朋友的第一条消息落在 2026 年内；哈哈怪的全时段连击。
        new_pal = self.store.get_contact("new_pal")
        new_pal.first_message_at = int(datetime(2026, 6, 15).timestamp())
        self.store.save_contact(new_pal)
        king = self.store.get_contact("king")
        king.max_laugh_run = 23
        self.store.save_contact(king)

    def report(self) -> dict:
        return yearly_report(self.store, 2026, NOW)

    def test_overview_sums_the_year_window(self) -> None:
        report = self.report()
        self.assertEqual(report["year"], 2026)
        self.assertEqual(
            report["window"], {"start": "2026-01-01", "end": "2026-08-15"}
        )
        self.assertEqual(report["overview"]["messages"], 559)
        self.assertEqual(report["overview"]["incoming"], 331)
        self.assertEqual(report["overview"]["outgoing"], 228)
        # 当年窗口内有数据行才算「覆盖的联系人」。
        self.assertEqual(report["overview"]["contacts"], 7)

    def test_top_lists_the_five_busiest_contacts_descending(self) -> None:
        report = self.report()
        self.assertEqual(
            [row["display_name"] for row in report["top"]],
            ["话痨", "常青", "夜猫", "新朋友", "老友"],
        )
        self.assertEqual(report["top"][0]["messages"], 500)
        self.assertEqual(len(report["top"][0]["hash"]), 24)

    def test_night_and_weekend_rankings_only_keep_matching_rows(self) -> None:
        report = self.report()
        self.assertEqual(len(report["night"]), 1)
        self.assertEqual(report["night"][0]["display_name"], "夜猫")
        self.assertEqual(report["night"][0]["messages"], 10)
        self.assertEqual(len(report["weekend"]), 1)
        self.assertEqual(report["weekend"][0]["display_name"], "周末搭子")
        self.assertEqual(report["weekend"][0]["messages"], 6)

    def test_new_friends_only_count_contacts_met_inside_the_year(self) -> None:
        report = self.report()
        self.assertEqual(len(report["new_friends"]), 1)
        self.assertEqual(report["new_friends"][0]["display_name"], "新朋友")
        self.assertEqual(report["new_friends"][0]["messages"], 6)

    def test_faded_compares_against_the_previous_year(self) -> None:
        report = self.report()
        # 老友 200 → 5（不足 10%，淡出）；常青 300 → 30（恰好 10%，不淡出）。
        self.assertEqual(len(report["faded"]), 1)
        faded = report["faded"][0]
        self.assertEqual(faded["display_name"], "老友")
        self.assertEqual((faded["previous_messages"], faded["messages"]), (200, 5))

    def test_haha_king_is_an_all_time_achievement(self) -> None:
        report = self.report()
        self.assertEqual(report["haha_king"]["display_name"], "哈哈怪")
        self.assertEqual(report["haha_king"]["max_laugh_run"], 23)

    def test_monthly_grid_has_twelve_cells(self) -> None:
        report = self.report()
        self.assertEqual(len(report["monthly"]), 12)
        self.assertEqual(report["monthly"][0]["month"], "2026-01")
        self.assertEqual(report["monthly"][5]["month"], "2026-06")
        # 六月 553 条（除七月的新朋友外全部落位），七月 6 条，其余月份为 0。
        self.assertEqual(report["monthly"][5]["count"], 553)
        self.assertEqual(report["monthly"][6]["count"], 6)
        self.assertEqual(report["monthly"][0]["count"], 0)

    def test_future_year_comes_back_empty_but_well_shaped(self) -> None:
        # 未来年（前端已禁用，仅防御性可达）：当年窗口为空，上一年 500 条的
        # 话痨按规则自然落入「淡出」（0 < 10%），其余版块空但有形。
        report = yearly_report(self.store, 2027, NOW)
        self.assertEqual(report["overview"]["messages"], 0)
        self.assertEqual(report["top"], [])
        self.assertEqual(
            [(row["display_name"], row["messages"]) for row in report["faded"]],
            [("话痨", 0)],
        )
        self.assertEqual(len(report["monthly"]), 12)
        self.assertEqual(report["monthly"][0]["count"], 0)


class NarrativeTests(unittest.TestCase):
    """叙事只拿匿名聚合数字：名单、消息原文都不许出现在输入里。"""

    def sample_report(self) -> dict:
        return {
            "year": 2026,
            "overview": {
                "messages": 100,
                "contacts": 3,
                "incoming": 60,
                "outgoing": 40,
            },
            "top": [{"messages": 80}, {"messages": 15}],
            "night": [{"messages": 10}],
            "weekend": [{"messages": 5}],
            "new_friends": [],
            "faded": [],
            "monthly": [{"count": index} for index in range(1, 13)],
        }

    def test_user_text_contains_only_anonymous_numbers(self) -> None:
        report = self.sample_report()
        # 榜单行故意带名字与哈希：输入文本必须对它们视而不见。
        report["top"] = [
            {"display_name": "老友", "hash": "a" * 24, "messages": 80},
            {"display_name": "话痨", "hash": "b" * 24, "messages": 15},
        ]
        text = narrative_user_text(report)
        self.assertNotIn("老友", text)
        self.assertNotIn("话痨", text)
        self.assertIn("总消息数 100 条", text)
        self.assertIn("80、15", text)

    def test_generate_narrative_sends_mask_output_and_truncates(self) -> None:
        report = self.sample_report()
        expected_user = narrative_user_text(report)
        with patch(
            "wechat_insights.llm.chat", return_value="今年的故事 " * 200
        ) as fake:
            text = generate_narrative(report)
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(fake.call_args.args[0], REPORT_NARRATIVE_SYSTEM_PROMPT)
        # 出站文本是 mask 后的聚合数字（本输入没有敏感词，mask 是恒等）。
        self.assertEqual(fake.call_args.args[1], expected_user)
        # 两端空白剥掉，超长回复截到上限以内。
        self.assertLessEqual(len(text), REPORT_NARRATIVE_MAX_CHARS)
        self.assertTrue(text.startswith("今年的故事"))

    def test_generate_narrative_failure_returns_none(self) -> None:
        with patch("wechat_insights.llm.chat", return_value=None):
            self.assertIsNone(generate_narrative(self.sample_report()))


if __name__ == "__main__":
    unittest.main()
