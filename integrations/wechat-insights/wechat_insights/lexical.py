"""文本词法统计。

消息原文只在这里过一遍，抽出字数 / 是否疑问句 / 「哈」连击长度等标量后立即丢弃，
不会写进 metrics.db。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .constants import LONG_MESSAGE_CHARS


# 计算「哈哈哈」这类连击时，笑声字符按一个集合处理，长度取最长的一段。
_LAUGH_RUN = re.compile(r"[哈嘻嘿呵]{2,}")
# 判定疑问句前先剥掉句尾的语气符号和空白，避免「吗。」「?~」漏判。
_TRAILING_NOISE = re.compile(r"[\s。\.!！~～、,，…·\-—]+$")
# 中文里没有标点的疑问句主要靠这几个句末助词。
_QUESTION_TAILS = ("？", "?", "吗", "呢")


@dataclass(frozen=True, slots=True)
class TextFeatures:
    """单条文本消息的词法特征。"""

    chars: int
    is_question: bool
    is_long: bool
    laugh_run: int


def longest_laugh_run(text: str) -> int:
    """返回文本中最长的一段笑声连击长度，没有则为 0。"""

    return max((len(match.group(0)) for match in _LAUGH_RUN.finditer(text)), default=0)


def is_question(text: str) -> bool:
    """粗规则判定疑问句：剥掉句尾噪声后以 ？/? /吗/呢 结尾。"""

    stripped = _TRAILING_NOISE.sub("", text.strip())
    return stripped.endswith(_QUESTION_TAILS)


def analyze_text(text: str) -> TextFeatures:
    """抽取一条文本消息的词法特征。

    只应对 kind == "text" 的消息调用；其它类型在 formatting 里被替换成
    「[图片]」这类占位文本，计入字数会污染长度类指标。
    """

    normalized = text.strip()
    chars = len(normalized)
    return TextFeatures(
        chars=chars,
        is_question=is_question(normalized),
        is_long=chars > LONG_MESSAGE_CHARS,
        laugh_run=longest_laugh_run(normalized),
    )
