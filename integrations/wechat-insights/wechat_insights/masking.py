"""出站敏感词屏蔽。

铁律：mask() 是聊天文本离开本容器的唯一出口。任何要把聊天原文发往外部
服务（LLM、翻译、同步等）的调用点，都必须先过 mask()，禁止绕过。

内置种子词表是为了避免触发 API 端点的内容风控导致拒答/封号，不是价值
判断；用户可用 INSIGHTS_MASK_WORDS_FILE 指向自己的词表文件增删。

词表在模块加载时编译成一次匹配的交替式正则，按词长降序排列，长词优先
命中，避免「近平」先命中把「习近平」拆坏。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from .constants import INSIGHTS_MASK_WORDS_FILE

LOG = logging.getLogger("wechat-insights")

#: 内置种子词表：命中即替换为等长星号。
_SEED_WORDS = (
    "习近平",
    "近平",
    "习主席",
    "毛泽东",
    "邓小平",
    "江泽民",
    "胡锦涛",
    "李克强",
    "共产党",
    "六四",
    "天安门",
    "法轮功",
    "台独",
    "疆独",
    "港独",
    "达赖",
)


def _load_user_words() -> tuple[str, ...]:
    """读取用户词表文件；读不到时记 WARNING 并返回空（只用种子词表）。"""

    if not INSIGHTS_MASK_WORDS_FILE:
        return ()
    words: list[str] = []
    try:
        with open(INSIGHTS_MASK_WORDS_FILE, encoding="utf-8") as handle:
            for line in handle:
                word = line.strip()
                if word and not word.startswith("#"):
                    words.append(word)
    except OSError:
        LOG.warning(
            "屏蔽词表 %s 读不到，只用内置种子词表", INSIGHTS_MASK_WORDS_FILE
        )
        return ()
    return tuple(words)


#: 生效的全部词（种子 + 用户词表，去重），按词长降序，长词优先匹配。
_WORDS = tuple(
    sorted(set(_SEED_WORDS) | set(_load_user_words()), key=len, reverse=True)
)

_PATTERN = re.compile("|".join(re.escape(word) for word in _WORDS))


def mask(text: str) -> str:
    """把命中的敏感词替换成等长星号；未命中时原样返回。"""

    return _PATTERN.sub(lambda match: "*" * len(match.group(0)), text)


def mask_all(texts: Iterable[str]) -> list[str]:
    """批量屏蔽，返回与输入等长的新列表。"""

    return [mask(text) for text in texts]
