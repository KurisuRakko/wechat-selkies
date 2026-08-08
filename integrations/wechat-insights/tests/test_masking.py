from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import wechat_insights.constants as constants
import wechat_insights.masking as masking


class MaskingTests(unittest.TestCase):
    """种子词命中、长词优先、词表文件加载与坏路径回退。"""

    def test_seed_words_become_equal_length_stars(self) -> None:
        self.assertEqual(masking.mask("习近平"), "***")
        self.assertEqual(masking.mask("天安门"), "***")
        self.assertEqual(masking.mask("台独"), "**")
        self.assertEqual(masking.mask("六四"), "**")

    def test_longer_words_match_first(self) -> None:
        # 「近平」是「习近平」的子串：长词必须优先命中，否则会被拆成两个短词。
        self.assertEqual(masking.mask("习近平谈近平"), "***谈**")
        self.assertEqual(masking.mask("习主席今天很忙"), "***今天很忙")

    def test_unmatched_text_passes_through_unchanged(self) -> None:
        self.assertEqual(masking.mask("今天天气不错"), "今天天气不错")
        self.assertEqual(masking.mask(""), "")
        # 「习」单字不在词表里，原样返回。
        self.assertEqual(masking.mask("习"), "习")

    def test_mask_all_maps_over_an_iterable(self) -> None:
        self.assertEqual(
            masking.mask_all(["习近平", "你好", "天安门"]), ["***", "你好", "***"]
        )

    def test_user_word_file_is_merged_with_seed_words(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "words.txt"
        path.write_text("# 用户词表\n测试词\n\n测试词2\n", encoding="utf-8")
        try:
            with patch.object(constants, "INSIGHTS_MASK_WORDS_FILE", str(path)):
                importlib.reload(masking)
            self.assertEqual(masking.mask("测试词"), "***")
            self.assertEqual(masking.mask("测试词2"), "****")
            # 种子词仍然生效，两个词表是合并去重的关系。
            self.assertEqual(masking.mask("习近平"), "***")
        finally:
            with patch.object(constants, "INSIGHTS_MASK_WORDS_FILE", ""):
                importlib.reload(masking)

    def test_missing_word_file_falls_back_to_seed_words(self) -> None:
        with self.assertLogs("wechat-insights", level="WARNING"):
            with patch.object(
                constants, "INSIGHTS_MASK_WORDS_FILE", "/nonexistent/words.txt"
            ):
                importlib.reload(masking)
        # 坏路径只记告警，种子词表照常生效。
        self.assertEqual(masking.mask("习近平"), "***")
        with patch.object(constants, "INSIGHTS_MASK_WORDS_FILE", ""):
            importlib.reload(masking)


if __name__ == "__main__":
    unittest.main()
