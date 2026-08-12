"""好感度校准的单元测试：解析、方向、回退、累计、截断与进度上报。

analyzer 的消化接线（进度阶段顺序、历史曲线不吃校准）在 test_analyzer.py。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from wechat_insights.calibrate import parse_reply, refresh_calibrations
from wechat_insights.constants import (
    CALIBRATE_FALLBACK_STEP,
    CALIBRATE_STEP_MAX,
    CALIBRATE_TOTAL_MAX,
    SESSION_GAP_SECONDS,
)
from wechat_insights.depth import LexicalDepth, LLMDepth
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


def _chat_reply(**dims) -> str:
    """构造一条合法的校准回复；dims 只给非零的维度。"""

    return json.dumps(
        {"dims": dims, "note": "最近都是你在主动发起对话"}, ensure_ascii=False
    )


class ParseReplyTests(unittest.TestCase):
    def test_normal_reply(self) -> None:
        reply = '{"dims": {"investment": 4.0, "initiative": 1.5}, "note": "最近都是你在主动"}'
        dims, note = parse_reply(reply)
        self.assertEqual(dims, {"investment": 4.0, "initiative": 1.5})
        self.assertEqual(note, "最近都是你在主动")

    def test_note_truncated_and_non_string(self) -> None:
        long_note = "长" * 50
        dims, note = parse_reply(
            '{"dims": {"depth": 2.0}, "note": "%s"}' % long_note
        )
        self.assertEqual(len(note), 40)
        _, note = parse_reply('{"dims": {"depth": 2.0}, "note": 123}')
        self.assertEqual(note, "")
        _, note = parse_reply('{"dims": {"depth": 2.0}}')
        self.assertEqual(note, "")

    def test_all_zero_or_missing_returns_none(self) -> None:
        self.assertIsNone(parse_reply('{"dims": {"investment": 0, "depth": 0}, "note": "x"}'))
        self.assertIsNone(parse_reply('{"note": "没有 dims"}'))
        self.assertIsNone(parse_reply('{"dims": "不是 dict"}'))
        self.assertIsNone(parse_reply("没有 JSON 块"))
        self.assertIsNone(parse_reply("{解析不了"))

    def test_values_clamped_and_bools_rejected(self) -> None:
        # 超上限夹到 6.0；负值与 bool 视为 0（不产出偏移键）。
        dims, _ = parse_reply(
            '{"dims": {"investment": 99.0, "responsiveness": -3.0, '
            '"depth": true, "constancy": 2.6}, "note": ""}'
        )
        self.assertEqual(dims, {"investment": CALIBRATE_STEP_MAX, "constancy": 2.6})

    def test_reply_never_written_to_log(self) -> None:
        # 解析失败走 warning：即使回复里带敏感词，日志文本也不能出现回复内容。
        with patch("wechat_insights.calibrate.LOG.warning") as warning:
            parse_reply('{"dims": 坏JSON')
            parse_reply("深夜聊得少了")
            self.assertEqual(warning.call_count, 2)
            for call in warning.call_args_list:
                self.assertNotIn("坏JSON", call.args[0])
                self.assertNotIn("深夜聊得少了", call.args[0])


class RefreshCalibrationsTests(AnalyzerTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 让 llm 策略的采样读得到一条消息（sample_transcript 需要 text 行）。
        build_database(self.database, [them(1, 0, "最近怎么样"), me(2, 0, "挺好的呀")])

    def seed_scored(self) -> None:
        # 先建联系人行：set_contact_calibration 之类的 UPDATE 需要行存在。
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
        mark: str = "up",
    ) -> int:
        events: list[dict] = []
        self.store.ensure_contact(SESSION_ID, DISPLAY_NAME)
        self.store.set_contact_feedback(SESSION_ID, mark, str(moment - 60))
        count = refresh_calibrations(
            self.store,
            self.reader,
            strategy,
            SESSION_GAP_SECONDS,
            moment,
            lambda **fields: events.append(dict(fields)),
        )
        self.assertEqual(
            [event["phase"] for event in events], ["calibrate", "calibrate"]
        )
        return count

    def calibration_dims(self) -> dict[str, float]:
        return (self.store.get_contact(SESSION_ID).calibration_data() or {})["dims"]

    def test_llm_up_mark_applies_positive_offsets(self) -> None:
        self.seed_scored()
        with patch(
            "wechat_insights.llm.chat",
            return_value=_chat_reply(investment=4.0),
        ) as chat:
            count = self.refresh(mark="up")
        self.assertEqual(count, 1)
        self.assertEqual(self.calibration_dims(), {"investment": 4.0})
        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.feedback_pending, "")
        self.assertEqual(contact.calibration_data()["source"], "llm")
        # 出站 user 文本过了一次 masking.mask()。
        self.assertIn("当前七维分", chat.call_args.args[1])

    def test_down_mark_applies_negative_offsets(self) -> None:
        self.seed_scored()
        with patch(
            "wechat_insights.llm.chat",
            return_value=_chat_reply(investment=4.0, depth=2.0),
        ):
            self.refresh(mark="down")
        self.assertEqual(
            self.calibration_dims(),
            {"investment": -4.0, "depth": -2.0},
        )

    def test_llm_failure_falls_back_to_uniform_step(self) -> None:
        self.seed_scored()
        with patch("wechat_insights.llm.chat", return_value=None):
            self.refresh()
        expected = {name: CALIBRATE_FALLBACK_STEP for name in DIMENSION_NAMES}
        self.assertEqual(self.calibration_dims(), expected)
        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.calibration_data()["source"], "fallback")

    def test_lexical_strategy_falls_back_without_llm_call(self) -> None:
        self.seed_scored()
        with patch("wechat_insights.llm.chat") as chat:
            self.refresh(strategy=LexicalDepth())
        chat.assert_not_called()
        expected = {name: CALIBRATE_FALLBACK_STEP for name in DIMENSION_NAMES}
        self.assertEqual(self.calibration_dims(), expected)

    def test_unscored_candidate_skipped_and_mark_cleared(self) -> None:
        # 从未打过分：无从校准，只清标记、不落校准。
        count = self.refresh()
        self.assertEqual(count, 0)
        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.feedback_pending, "")
        self.assertEqual(contact.calibration_data(), None)

    def test_cumulative_merge_clamps_to_total_max(self) -> None:
        self.seed_scored()
        # 已有 +10.0 的累计偏移，再 +6.0 应截断在 +12.0。
        self.store.set_contact_calibration(
            SESSION_ID,
            json.dumps(
                {"dims": {"investment": 10.0}, "updated_at": NOW, "source": "llm", "note": ""}
            ),
        )
        with patch(
            "wechat_insights.llm.chat",
            return_value=_chat_reply(investment=6.0),
        ):
            self.refresh(mark="up")
        self.assertEqual(
            self.calibration_dims(), {"investment": CALIBRATE_TOTAL_MAX}
        )

    def test_cumulative_preserves_untouched_dims(self) -> None:
        self.seed_scored()
        # 预置校准只动过 responsiveness；下一轮 LLM 只建议 depth，合并后
        # responsiveness 的累计偏移必须原样保留，不能被静默丢弃。
        self.store.set_contact_calibration(
            SESSION_ID,
            json.dumps(
                {"dims": {"responsiveness": 4.0}, "updated_at": NOW, "source": "llm", "note": ""}
            ),
        )
        with patch(
            "wechat_insights.llm.chat",
            return_value=_chat_reply(depth=3.0),
        ):
            self.refresh(mark="up")
        self.assertEqual(
            self.calibration_dims(),
            {"responsiveness": 4.0, "depth": 3.0},
        )

    def test_merge_through_zero_clears_calibration(self) -> None:
        self.seed_scored()
        # 反方向抵消到净值为 0：校准列清空，等于没校准过。
        self.store.set_contact_calibration(
            SESSION_ID,
            json.dumps(
                {"dims": {"investment": 3.0}, "updated_at": NOW, "source": "llm", "note": ""}
            ),
        )
        with patch(
            "wechat_insights.llm.chat",
            return_value=_chat_reply(investment=3.0),
        ):
            self.refresh(mark="down")
        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.calibration_data(), None)

    def test_offsets_rounded_to_one_decimal(self) -> None:
        self.seed_scored()
        with patch(
            "wechat_insights.llm.chat",
            return_value='{"dims": {"rhythm": 2.25}, "note": "x"}',
        ):
            self.refresh()
        self.assertEqual(self.calibration_dims(), {"rhythm": 2.2})


if __name__ == "__main__":
    unittest.main()
