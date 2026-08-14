"""绝交检测的单元测试：解析、消息统计、核实决策矩阵、mask 关卡与封顶应用。

analyzer 的接线（阶段顺序、封顶作用在校准之后、历史曲线不吃绝交）在
test_analyzer.py；HTTP 接口在 test_server.py。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from wechat_insights.analyzer import Analyzer
from wechat_insights.breakup import (
    messages_after,
    parse_reply,
    refresh_breakups,
)
from wechat_insights.constants import (
    BREAKUP_CAP_CERTAIN,
    SESSION_GAP_SECONDS,
)
from wechat_insights.depth import LexicalDepth, LLMDepth
from wechat_insights.metrics import Metrics, day_key
from wechat_insights.scoring import DIMENSION_NAMES
from wechat_insights.storage import contact_hash

from tests.support import (
    AnalyzerTestCase,
    DISPLAY_NAME,
    NOW,
    SESSION_ID,
    build_database,
    me,
    them,
)


def _breakup_reply(verdict: str, kind: str = "quarrel", note: str = "") -> str:
    """构造一条合法的绝交核实回复；kind 只在 broken 时会被读取。"""

    payload: dict = {"verdict": verdict, "note": note}
    if verdict == "broken":
        payload["kind"] = kind
    return json.dumps(payload, ensure_ascii=False)


class ParseReplyTests(unittest.TestCase):
    def test_broken_reply(self) -> None:
        parsed = parse_reply('{"verdict":"broken","kind":"quarrel","note":"激烈争吵"}')
        self.assertEqual(
            parsed, {"verdict": "broken", "kind": "quarrel", "note": "激烈争吵"}
        )

    def test_normal_and_unclear_replies(self) -> None:
        self.assertEqual(
            parse_reply('{"verdict":"normal","note":"之后仍在正常聊天"}'),
            {"verdict": "normal", "note": "之后仍在正常聊天"},
        )
        self.assertEqual(
            parse_reply('{"verdict":"unclear","note":"看不出明显迹象"}'),
            {"verdict": "unclear", "note": "看不出明显迹象"},
        )

    def test_missing_verdict_or_bad_json_returns_none(self) -> None:
        self.assertIsNone(parse_reply('{"kind":"quarrel","note":"x"}'))
        self.assertIsNone(parse_reply('{"verdict":"weird"}'))
        self.assertIsNone(parse_reply('{"verdict":123}'))
        self.assertIsNone(parse_reply("没有 JSON 块"))
        self.assertIsNone(parse_reply("{解析不了"))

    def test_note_truncated_and_non_string(self) -> None:
        long_note = "长" * 50
        parsed = parse_reply(
            '{"verdict":"broken","kind":"quarrel","note":"%s"}' % long_note
        )
        self.assertEqual(len(parsed["note"]), 40)
        self.assertEqual(parse_reply('{"verdict":"normal","note":123}')["note"], "")
        self.assertEqual(parse_reply('{"verdict":"normal"}')["note"], "")

    def test_broken_without_kind_defaults_to_quarrel(self) -> None:
        self.assertEqual(
            parse_reply('{"verdict":"broken","note":"x"}')["kind"], "quarrel"
        )
        self.assertEqual(
            parse_reply('{"verdict":"broken","kind":"weird","note":"x"}')["kind"],
            "quarrel",
        )

    def test_reply_never_written_to_log(self) -> None:
        # 解析失败走 warning：即使回复里带敏感词，日志文本也不能出现回复内容。
        with patch("wechat_insights.breakup.LOG.warning") as warning:
            parse_reply('{"verdict": 坏JSON')
            parse_reply("深夜聊得少了")
            self.assertEqual(warning.call_count, 2)
            for call in warning.call_args_list:
                self.assertNotIn("坏JSON", call.args[0])
                self.assertNotIn("深夜聊得少了", call.args[0])


class MessagesAfterTests(AnalyzerTestCase):
    def test_counts_only_days_after_the_mark(self) -> None:
        for day, count in (
            ("2026-03-01", 3),
            ("2026-03-02", 5),
            ("2026-03-03", 2),
        ):
            metrics = Metrics()
            metrics.add("msgs_them", count)
            metrics.add("msgs_me", 1)
            self.store.merge_daily(SESSION_ID, {day: metrics})
        # 03-02 与 03-03 两天合计 (5+1)+(2+1)=9；标记日当天不统计。
        self.assertEqual(messages_after(self.store, SESSION_ID, "2026-03-01"), 9)
        self.assertEqual(messages_after(self.store, SESSION_ID, "2026-03-02"), 3)
        self.assertEqual(messages_after(self.store, SESSION_ID, "2026-03-03"), 0)


class RefreshBreakupsTests(AnalyzerTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 让 llm 策略的采样窗口读得到消息：标记日默认取 28 天前，采样窗口
        # 覆盖到这些落在 BASE+5 天的行。
        build_database(
            self.database,
            [them(1, 5 * 86400, "别再联系了"), me(2, 5 * 86400 + 60, "好，如你所愿")],
        )

    def seed_scored(self) -> None:
        # 先建联系人行：set_contact_breakup 之类的 UPDATE 需要行存在。
        self.store.ensure_contact(SESSION_ID, DISPLAY_NAME)
        self.store.save_scores(
            [
                (
                    SESSION_ID,
                    {
                        "hash": contact_hash(SESSION_ID),
                        "scored": True,
                        "overall": 73.4,
                        "dimensions": {name: 70.0 for name in DIMENSION_NAMES},
                    },
                )
            ]
        )

    def refresh(
        self,
        moment: int = NOW,
        strategy=LLMDepth(),
        certainty: str = "certain",
        day: str | None = None,
    ) -> int:
        events: list[dict] = []
        self.store.ensure_contact(SESSION_ID, DISPLAY_NAME)
        mark_day = day or day_key(moment - 28 * 86400)
        self.store.set_contact_breakup_pending(
            SESSION_ID,
            json.dumps(
                {"date": mark_day, "certainty": certainty, "at": moment - 60},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        count = refresh_breakups(
            self.store,
            self.reader,
            strategy,
            SESSION_GAP_SECONDS,
            moment,
            lambda **fields: events.append(dict(fields)),
        )
        self.assertEqual(
            [event["phase"] for event in events], ["breakup", "breakup"]
        )
        return count

    def test_llm_broken_confirms_with_llm_kind(self) -> None:
        self.seed_scored()
        with patch(
            "wechat_insights.llm.chat",
            return_value=_breakup_reply("broken", kind="quarrel", note="激烈争吵"),
        ) as chat:
            count = self.refresh()
        self.assertEqual(count, 1)
        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.breakup_pending, "")
        data = contact.breakup_data()
        self.assertEqual(data["verdict"], "confirmed")
        self.assertEqual(data["kind"], "quarrel")
        self.assertEqual(data["source"], "llm")
        self.assertEqual(data["note"], "激烈争吵")
        # 出站 user 文本过了一次 masking.mask()，且带着标记日期与统计。
        self.assertIn("用户标记的绝交日期", chat.call_args.args[1])

    def test_llm_normal_rejects(self) -> None:
        self.seed_scored()
        # 标记日取 3 天前：冷断判定不达标（不足 14 天），决策走 LLM。
        day = day_key(NOW - 3 * 86400)
        build_database(
            self.database,
            [them(1, 27 * 86400, "最近怎么样"), me(2, 27 * 86400 + 60, "挺好呀")],
        )
        with patch(
            "wechat_insights.llm.chat",
            return_value=_breakup_reply("normal", note="该日期后仍在正常聊天"),
        ):
            count = self.refresh(day=day)
        self.assertEqual(count, 1)
        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.breakup_pending, "")
        data = contact.breakup_data()
        self.assertEqual(data["verdict"], "rejected")
        self.assertEqual(data["source"], "llm")
        self.assertEqual(data["note"], "该日期后仍在正常聊天")

    def test_llm_unclear_with_certain_mark_is_asserted(self) -> None:
        self.seed_scored()
        day = day_key(NOW - 3 * 86400)
        build_database(
            self.database,
            [them(1, 27 * 86400, "最近怎么样"), me(2, 27 * 86400 + 60, "挺好呀")],
        )
        with patch(
            "wechat_insights.llm.chat", return_value=_breakup_reply("unclear")
        ):
            count = self.refresh(day=day)
        self.assertEqual(count, 1)
        data = self.store.get_contact(SESSION_ID).breakup_data()
        self.assertEqual(data["verdict"], "confirmed")
        self.assertEqual(data["kind"], "asserted")
        self.assertEqual(data["source"], "asserted")

    def test_llm_unclear_with_suspected_mark_keeps_pending(self) -> None:
        self.seed_scored()
        day = day_key(NOW - 3 * 86400)
        build_database(
            self.database,
            [them(1, 27 * 86400, "最近怎么样"), me(2, 27 * 86400 + 60, "挺好呀")],
        )
        with patch(
            "wechat_insights.llm.chat", return_value=_breakup_reply("unclear")
        ):
            count = self.refresh(day=day, certainty="suspected")
        self.assertEqual(count, 0)
        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.breakup, "")
        self.assertEqual(contact.breakup_pending_data()["date"], day)

    def test_stats_silence_confirms_without_llm(self) -> None:
        self.seed_scored()
        # 标记日 20 天前 + 之后 0 条消息：冷断统计达标，词法策略下直接确认。
        day = day_key(NOW - 20 * 86400)
        with patch("wechat_insights.llm.chat") as chat:
            count = self.refresh(strategy=LexicalDepth(), day=day)
        chat.assert_not_called()
        self.assertEqual(count, 1)
        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.breakup_pending, "")
        data = contact.breakup_data()
        self.assertEqual(data["verdict"], "confirmed")
        self.assertEqual(data["kind"], "silence")
        self.assertEqual(data["source"], "stats")
        self.assertTrue(data["note"].endswith("天仅 0 条往来"))

    def test_recent_suspected_mark_without_llm_keeps_pending(self) -> None:
        self.seed_scored()
        # 只过了 3 天：冷断不达标、又没有 LLM，存疑标记保留到下一轮再核。
        day = day_key(NOW - 3 * 86400)
        count = self.refresh(strategy=LexicalDepth(), day=day, certainty="suspected")
        self.assertEqual(count, 0)
        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.breakup, "")
        self.assertEqual(contact.breakup_pending_data()["date"], day)

    def test_unscored_candidate_skipped_and_mark_cleared(self) -> None:
        # 从未打过分：无从核实，只清标记、不落结论。
        count = self.refresh()
        self.assertEqual(count, 0)
        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.breakup_pending, "")
        self.assertEqual(contact.breakup_data(), None)

    def test_outbound_sample_is_masked(self) -> None:
        self.seed_scored()
        build_database(
            self.database,
            [them(1, 5 * 86400, "最近怎么样"), me(2, 5 * 86400 + 60, "习近平来了")],
        )
        with patch(
            "wechat_insights.llm.chat",
            return_value=_breakup_reply("broken", note="激烈争吵"),
        ) as chat:
            self.refresh()
        user = chat.call_args.args[1]
        # 敏感词在出站前被星号化：原文不得离开容器。
        self.assertNotIn("习近平", user)
        self.assertIn("***", user)

    def test_unparseable_pending_is_cleared(self) -> None:
        self.seed_scored()
        self.store.set_contact_breakup_pending(SESSION_ID, "{不是 JSON")
        count = refresh_breakups(
            self.store, self.reader, LexicalDepth(), SESSION_GAP_SECONDS, NOW
        )
        self.assertEqual(count, 0)
        self.assertEqual(self.store.get_contact(SESSION_ID).breakup_pending, "")

    def test_pending_with_invalid_date_is_cleared(self) -> None:
        self.seed_scored()
        self.store.set_contact_breakup_pending(
            SESSION_ID,
            json.dumps(
                {"date": "2026-13-40", "certainty": "certain", "at": NOW},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        count = refresh_breakups(
            self.store, self.reader, LexicalDepth(), SESSION_GAP_SECONDS, NOW
        )
        self.assertEqual(count, 0)
        self.assertEqual(self.store.get_contact(SESSION_ID).breakup_pending, "")


class ApplyBreakupTests(AnalyzerTestCase):
    """_apply_breakup：封顶缩放、否决不改分、低分不缩放、无结论不动。"""

    def contact_with_breakup(self, breakup: dict):
        self.store.ensure_contact(SESSION_ID, DISPLAY_NAME)
        self.store.set_contact_breakup(
            SESSION_ID,
            json.dumps(breakup, ensure_ascii=False, separators=(",", ":")),
        )
        return self.store.get_contact(SESSION_ID)

    @staticmethod
    def payload(overall: float = 70.0) -> dict:
        return {
            "overall": overall,
            "dimensions": {name: overall for name in DIMENSION_NAMES},
        }

    def confirmed_breakup(self) -> dict:
        return {
            "verdict": "confirmed",
            "kind": "quarrel",
            "date": "2026-01-01",
            "certainty": "certain",
            "note": "激烈争吵",
            "decided_at": NOW,
            "source": "llm",
        }

    def test_confirmed_certain_caps_overall_and_dims(self) -> None:
        payload = self.payload(70.0)
        Analyzer._apply_breakup(
            payload, self.contact_with_breakup(self.confirmed_breakup())
        )
        self.assertEqual(
            payload["dimensions"], {name: BREAKUP_CAP_CERTAIN for name in DIMENSION_NAMES}
        )
        self.assertEqual(payload["overall"], BREAKUP_CAP_CERTAIN)
        self.assertEqual(payload["breakup"]["verdict"], "confirmed")
        self.assertEqual(payload["breakup"]["overall_delta"], -60.0)
        self.assertEqual(payload["breakup"]["base"]["overall"], 70.0)
        self.assertEqual(
            payload["breakup"]["base"]["dimensions"],
            {name: 70.0 for name in DIMENSION_NAMES},
        )

    def test_confirmed_scales_each_dimension_by_the_same_ratio(self) -> None:
        payload = {
            "overall": 70.0,
            "dimensions": {
                "responsiveness": 98.0,
                **{name: 70.0 for name in list(DIMENSION_NAMES)[1:]},
            },
        }
        Analyzer._apply_breakup(
            payload, self.contact_with_breakup(self.confirmed_breakup())
        )
        # 七维等比缩放：ratio = 10 / 70，每个维度都乘同一个倍数。
        self.assertEqual(
            payload["dimensions"]["responsiveness"],
            round(98.0 * BREAKUP_CAP_CERTAIN / 70.0, 1),
        )
        self.assertEqual(payload["dimensions"]["initiative"], 10.0)
        self.assertEqual(payload["dimensions"]["reciprocity"], 10.0)

    def test_rejected_leaves_scores_untouched(self) -> None:
        payload = self.payload(70.0)
        Analyzer._apply_breakup(
            payload,
            self.contact_with_breakup(
                {
                    "verdict": "rejected",
                    "kind": "",
                    "date": "2026-01-01",
                    "certainty": "certain",
                    "note": "之后仍在正常聊天",
                    "decided_at": NOW,
                    "source": "llm",
                }
            ),
        )
        self.assertEqual(payload["overall"], 70.0)
        self.assertEqual(
            payload["dimensions"], {name: 70.0 for name in DIMENSION_NAMES}
        )
        self.assertEqual(
            payload["breakup"],
            {
                "verdict": "rejected",
                "note": "之后仍在正常聊天",
                "date": "2026-01-01",
            },
        )

    def test_base_below_cap_keeps_score_and_still_shows_chip_data(self) -> None:
        payload = self.payload(8.0)
        Analyzer._apply_breakup(
            payload, self.contact_with_breakup(self.confirmed_breakup())
        )
        self.assertEqual(payload["overall"], 8.0)
        self.assertEqual(payload["breakup"]["overall_delta"], 0.0)
        self.assertEqual(payload["breakup"]["base"]["overall"], 8.0)

    def test_contact_without_breakup_is_untouched(self) -> None:
        payload = self.payload(70.0)
        self.store.ensure_contact(SESSION_ID, DISPLAY_NAME)
        Analyzer._apply_breakup(payload, self.store.get_contact(SESSION_ID))
        self.assertEqual(payload["overall"], 70.0)
        self.assertNotIn("breakup", payload)


if __name__ == "__main__":
    unittest.main()
