from __future__ import annotations

import unittest

from wechat_insights.conversation import (
    ME,
    THEM,
    Message,
    Reply,
    Run,
    collect_replies,
    describe,
    split_conversations,
    split_runs,
)
from wechat_insights.lexical import analyze_text, is_question, longest_laugh_run


HOUR = 3600


def message(offset: int, direction: str, text: str = "", kind: str = "text") -> Message:
    return Message(
        timestamp=offset, local_id=offset, direction=direction, kind=kind, text=text
    )


class SplitConversationTests(unittest.TestCase):
    def test_gap_longer_than_threshold_starts_a_new_conversation(self) -> None:
        messages = [
            message(0, THEM),
            message(60, ME),
            message(7 * HOUR, ME),
            message(7 * HOUR + 30, THEM),
        ]
        conversations = split_conversations(messages, 6 * HOUR)
        self.assertEqual([len(group) for group in conversations], [2, 2])

    def test_gap_exactly_at_threshold_stays_in_the_same_conversation(self) -> None:
        messages = [message(0, THEM), message(6 * HOUR, ME)]
        self.assertEqual(len(split_conversations(messages, 6 * HOUR)), 1)

    def test_empty_input_yields_no_conversation(self) -> None:
        self.assertEqual(split_conversations([], 6 * HOUR), [])


class RunAndReplyTests(unittest.TestCase):
    def test_consecutive_same_direction_messages_collapse_into_one_run(self) -> None:
        messages = [
            message(0, THEM),
            message(10, THEM),
            message(20, THEM),
            message(30, ME),
        ]
        self.assertEqual(split_runs(messages), [Run(THEM, 3), Run(ME, 1)])

    def test_reply_delay_is_measured_at_the_direction_flip(self) -> None:
        messages = [
            message(0, THEM),
            message(10, THEM),
            message(100, ME),
            message(400, THEM),
        ]
        self.assertEqual(
            collect_replies(messages), [Reply(ME, 90), Reply(THEM, 300)]
        )

    def test_replies_never_cross_a_conversation_boundary(self) -> None:
        messages = [message(0, THEM), message(9 * HOUR, ME)]
        conversations = split_conversations(messages, 6 * HOUR)
        self.assertEqual(
            [collect_replies(group) for group in conversations], [[], []]
        )

    def test_shape_records_starter_ender_and_turns(self) -> None:
        shape = describe(
            [
                message(0, THEM),
                message(10, ME),
                message(20, THEM),
                message(30, THEM),
            ]
        )
        self.assertEqual(shape.starter, THEM)
        self.assertEqual(shape.ender, THEM)
        self.assertEqual(shape.turns, 3)
        self.assertFalse(shape.is_long)

    def test_more_than_twenty_turns_counts_as_a_long_conversation(self) -> None:
        messages = [
            message(index * 10, THEM if index % 2 else ME) for index in range(24)
        ]
        self.assertTrue(describe(messages).is_long)


class LexicalTests(unittest.TestCase):
    def test_question_marks_and_final_particles_are_both_detected(self) -> None:
        self.assertTrue(is_question("在吗"))
        self.assertTrue(is_question("你说呢？"))
        self.assertTrue(is_question("really?"))
        self.assertTrue(is_question("吃了吗。"))
        self.assertFalse(is_question("我吃了"))

    def test_laugh_run_takes_the_longest_streak(self) -> None:
        self.assertEqual(longest_laugh_run("哈哈 哈哈哈哈哈 好"), 5)
        self.assertEqual(longest_laugh_run("哈"), 0)

    def test_long_message_threshold_uses_stripped_length(self) -> None:
        features = analyze_text("  " + "字" * 51 + "  ")
        self.assertEqual(features.chars, 51)
        self.assertTrue(features.is_long)
        self.assertFalse(analyze_text("字" * 50).is_long)


if __name__ == "__main__":
    unittest.main()
