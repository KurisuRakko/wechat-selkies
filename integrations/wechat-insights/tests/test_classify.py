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
    build_database,
    me,
    them,
)
from wechat_insights.depth import LLMDepth


class ClassifyTests(AnalyzerTestCase):
    """关系类型分类：候选筛选、只判一次、全时段采样过 mask、判定落库。

    分类在 run() 里由 classify_contacts 模块级函数执行（analyzer 顶部导入），
    测试直接跑整轮 run()，chat 调用数即真实出站次数。
    """

    def setUp(self) -> None:
        super().setUp()
        # 建一个空的真实分片表：分类采样会打开候选所在的分片文件。
        build_database(self.database, [])

    def run_classify(self, chat, now: int = NOW):
        """以 llm 深度策略跑一轮（无同步会话），返回 (result, patch 的 llm.chat)。"""

        with patch(
            "wechat_insights.analyzer.scan_direct_rows", return_value={}
        ), patch("wechat_insights.llm.chat", side_effect=chat) as fake:
            result = self.analyzer(strategy=LLMDepth()).run(now=now)
        return result, fake

    def seed_contact(
        self,
        session_id: str,
        name: str,
        total_messages: int,
        first_at: int | None = None,
        last_at: int | None = None,
    ) -> None:
        """绕过同步循环直接构造一个可分类的联系人（含全时段里程碑）。

        last_message_at 默认落在 60 天采样窗之外，避免被 LLM 深度打分的
        候选逻辑抢走本次 patch 的 chat 调用。
        """

        contact = self.store.ensure_contact(session_id, name)
        contact.total_messages = total_messages
        contact.first_message_at = (
            first_at if first_at is not None else BASE - 400 * 86400
        )
        contact.last_message_at = (
            last_at if last_at is not None else BASE - 100 * 86400
        )
        self.store.save_contact(contact)

    def test_candidates_are_filtered_and_classified_only_once(self) -> None:
        # Heavy 消息量最高、未判定：唯一的候选。Light 不足 30 条、Manual
        # 手动设置过、Done 已自动判定过——都不是候选，chat 只被调一次。
        build_database(
            self.database,
            [them(i, -400 * 86400 + i * 300 * 86400 // 40) for i in range(1, 41)],
            session_id="heavy",
        )
        self.seed_contact("heavy", "Heavy", 40)
        self.seed_contact("light", "Light", 10)
        self.seed_contact("manual", "Manual", 40)
        self.store.set_contact_kind_manual("manual", "family")
        self.seed_contact("done", "Done", 40)
        self.store.set_contact_kind_auto("done", "family")

        def chat(system: str, user: str) -> str:
            # 判定输入：备注名 + 全时段样本，整体已 mask。
            self.assertIn("备注名：Heavy", user)
            self.assertIn("TA: 在吗？", user)
            return '{"kind": "friend", "confidence": 0.9}'

        result, fake = self.run_classify(chat=chat)
        self.assertEqual(result.classified, 1)
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(self.store.get_contact("heavy").kind_auto, "friend")
        self.assertEqual(self.store.get_contact("light").kind_auto, "")
        self.assertEqual(self.store.get_contact("manual").kind_auto, "")
        self.assertEqual(self.store.get_contact("manual").kind_manual, "family")
        self.assertEqual(self.store.get_contact("done").kind_auto, "family")

        # 第二轮：heavy 已判定，没有新候选，chat 不再被调用。
        second, fake2 = self.run_classify(
            now=NOW + 86400, chat=lambda s, u: '{"kind": "family", "confidence": 0.9}'
        )
        self.assertEqual(second.classified, 0)
        self.assertEqual(fake2.call_count, 0)

    def test_sample_spans_the_full_history_and_is_masked(self) -> None:
        # 三年跨度：最早的证据（-700 天）在 60 天采样窗根本看不见，三段都要
        # 送出去；种子词必须星号化后才能出站。last 落在 60 天窗之外，深度
        # 打分不掺和，chat 只被分类调一次。
        rows = [
            them(1, -700 * 86400, "习近平今天讲了什么"),
            them(2, -350 * 86400, "最近怎么样"),
            them(3, -40 * 86400, "周末一起吃饭吗"),
        ]
        build_database(self.database, rows)
        self.seed_contact(
            SESSION_ID, "老友", 30,
            first_at=BASE - 700 * 86400, last_at=BASE - 40 * 86400,
        )

        def chat(system: str, user: str) -> str:
            self.assertIn("备注名：老友", user)
            # 三段全时段样本都在：最老的一段也在，且已被脱敏。
            self.assertIn("今天讲了什么", user)
            self.assertIn("最近怎么样", user)
            self.assertIn("周末一起吃饭吗", user)
            self.assertNotIn("习近平", user)
            self.assertIn("***", user)
            return '{"kind": "family", "confidence": 0.9}'

        result, fake = self.run_classify(chat=chat)
        self.assertEqual(result.classified, 1)
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(self.store.get_contact(SESSION_ID).kind_auto, "family")

    def test_low_confidence_falls_back_to_friend(self) -> None:
        build_database(self.database, [them(1, 0, "你好"), me(2, 60, "你好")])
        self.seed_contact(SESSION_ID, "Alice", 30)

        result, fake = self.run_classify(
            chat=lambda s, u: '{"kind": "transactional", "confidence": 0.3}'
        )
        self.assertEqual(result.classified, 1)
        self.assertEqual(fake.call_count, 1)
        # 写入即视为已判定：低置信度落默认 friend，不再重评。
        self.assertEqual(self.store.get_contact(SESSION_ID).kind_auto, "friend")

    def test_invalid_kind_value_falls_back_to_friend(self) -> None:
        build_database(self.database, [them(1, 0, "你好")])
        self.seed_contact(SESSION_ID, "Alice", 30)

        result, _ = self.run_classify(
            chat=lambda s, u: '{"kind": "boss", "confidence": 0.9}'
        )
        self.assertEqual(result.classified, 1)
        self.assertEqual(self.store.get_contact(SESSION_ID).kind_auto, "friend")

    def test_parse_failure_writes_nothing_and_retries_next_round(self) -> None:
        build_database(self.database, [them(1, 0, "你好")])
        self.seed_contact(SESSION_ID, "Alice", 30)

        result, fake = self.run_classify(chat=lambda s, u: "说人话，别用 JSON")
        self.assertEqual(result.classified, 0)
        self.assertEqual(fake.call_count, 1)
        # 没写任何判定：kind_auto 保持空，下一轮仍是候选。
        self.assertEqual(self.store.get_contact(SESSION_ID).kind_auto, "")

        second, fake2 = self.run_classify(
            now=NOW + 86400, chat=lambda s, u: '{"kind": "friend", "confidence": 0.9}'
        )
        self.assertEqual(second.classified, 1)
        self.assertEqual(fake2.call_count, 1)
        self.assertEqual(self.store.get_contact(SESSION_ID).kind_auto, "friend")

    def test_no_text_messages_skip_the_llm_call(self) -> None:
        # 全部是图片消息：采样拿不到 text，直接落 friend、不浪费调用。
        build_database(
            self.database,
            [(i, 3, BASE + i * 60, 1, "") for i in range(1, 31)],
        )
        self.seed_contact(SESSION_ID, "Alice", 30)

        result, fake = self.run_classify(chat=lambda s, u: "不该被调用")
        self.assertEqual(result.classified, 1)
        self.assertEqual(fake.call_count, 0)
        self.assertEqual(self.store.get_contact(SESSION_ID).kind_auto, "friend")

    def test_max_per_run_truncates_the_candidates(self) -> None:
        # 三个候选按消息量降序截断到 2 个，第三个留到下一轮。
        for index, session_id in enumerate(("c1", "c2", "c3")):
            path = self.root / f"message_{index}.db"
            build_database(path, [them(i, i * 120) for i in range(1, 41)],
                           session_id=session_id)
            self.reader.databases[f"message/message_{index}.db"] = path
            self.seed_contact(session_id, session_id.upper(), (3 - index) * 40 + 40)

        with patch("wechat_insights.classify.CLASSIFY_MAX_PER_RUN", 2):
            result, fake = self.run_classify(
                chat=lambda s, u: '{"kind": "friend", "confidence": 0.9}'
            )
        self.assertEqual(result.classified, 2)
        self.assertEqual(fake.call_count, 2)
        judged = [
            session_id
            for session_id in ("c1", "c2", "c3")
            if self.store.get_contact(session_id).kind_auto
        ]
        self.assertEqual(judged, ["c1", "c2"])


if __name__ == "__main__":
    unittest.main()
