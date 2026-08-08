"""对话切分与对话内结构分析（纯逻辑，不碰数据库）。

术语：
- 对话（conversation）：相邻消息间隔不超过 SESSION_GAP_SECONDS 的一串消息。
- 轮次 / 连续块（run）：对话内同一方向连续发出的一组消息。
- 回复（reply）：对话内方向翻转处的那一条消息，延迟为它与上一条的时间差。
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import LONG_CONVERSATION_TURNS, SESSION_GAP_SECONDS


#: 联系人发来的消息方向；沿用 wechat_history reader 的取值。
THEM = "incoming"
#: 我发出去的消息方向。
ME = "outgoing"


@dataclass(frozen=True, slots=True)
class Message:
    """分析用的最小消息表示，不含任何持久化的原文。"""

    timestamp: int
    local_id: int
    direction: str
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class Run:
    """对话内一段同方向的连续消息。"""

    direction: str
    count: int


@dataclass(frozen=True, slots=True)
class Reply:
    """一次方向翻转，即一次回复。"""

    direction: str
    delay: int


@dataclass(frozen=True, slots=True)
class ConversationShape:
    """一段对话的结构摘要。"""

    start: int
    end: int
    starter: str
    ender: str
    runs: tuple[Run, ...]
    replies: tuple[Reply, ...]

    @property
    def turns(self) -> int:
        """轮次数 = 同向连续块的个数。"""

        return len(self.runs)

    @property
    def is_long(self) -> bool:
        return self.turns > LONG_CONVERSATION_TURNS


def split_conversations(
    messages: list[Message], gap_seconds: int = SESSION_GAP_SECONDS
) -> list[list[Message]]:
    """按时间间隔把一串按时间升序排好的消息切成若干段对话。"""

    conversations: list[list[Message]] = []
    current: list[Message] = []
    for message in messages:
        if current and message.timestamp - current[-1].timestamp > gap_seconds:
            conversations.append(current)
            current = []
        current.append(message)
    if current:
        conversations.append(current)
    return conversations


def split_runs(messages: list[Message]) -> list[Run]:
    """把一段对话压缩成同方向连续块序列。"""

    runs: list[Run] = []
    for message in messages:
        if runs and runs[-1].direction == message.direction:
            runs[-1] = Run(message.direction, runs[-1].count + 1)
        else:
            runs.append(Run(message.direction, 1))
    return runs


def collect_replies(messages: list[Message]) -> list[Reply]:
    """收集对话内每一次方向翻转的回复延迟。

    延迟归属于「做出回复的那一方」；跨对话的间隔不算回复，因为调用方传进来的
    永远是单段对话。
    """

    replies: list[Reply] = []
    for previous, current in zip(messages, messages[1:]):
        if current.direction != previous.direction:
            delay = max(0, current.timestamp - previous.timestamp)
            replies.append(Reply(current.direction, delay))
    return replies


def describe(messages: list[Message]) -> ConversationShape:
    """汇总一段对话的结构。传入的消息必须非空且按时间升序。"""

    runs = split_runs(messages)
    return ConversationShape(
        start=messages[0].timestamp,
        end=messages[-1].timestamp,
        starter=messages[0].direction,
        ender=messages[-1].direction,
        runs=tuple(runs),
        replies=tuple(collect_replies(messages)),
    )
