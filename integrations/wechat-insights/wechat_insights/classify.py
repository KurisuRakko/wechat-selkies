"""联系人关系类型分类：friend / family / transactional（仅 llm 深度策略启用）。

分类是稳定属性：一个联系人只判一次——写过 kind_auto 就不再重评，手动
改判过则完全跳过；手动设置优先于自动判定。事务往来（订票、快递、房东、
客服这类目的性沟通）不参与关系打分、也不进百分位参照系，避免污染正常
朋友的相对分；家人久不聊天也不会被判「淡出 / 归零」。
"""

from __future__ import annotations

import json
import logging

from . import llm
from .constants import CLASSIFY_MAX_PER_RUN, CLASSIFY_SAMPLE_CHARS
from .conversation import ME, split_conversations
from .masking import mask
from .storage import ContactRow, MetricsStore


LOG = logging.getLogger("wechat-insights")

#: 合法关系类型；'friend' 是默认值，任何旧数据都按 friend 处理。
KIND_VALUES = ("friend", "family", "transactional")

#: 候选门槛：累计消息数至少这么多条才值得判定。事务号往往消息多、
#: 污染最重，按消息量降序优先处理；低于门槛的永远默认 friend。
CLASSIFY_MIN_MESSAGES = 30

#: 判定可信度低于该值的一律落 friend（写入即视为已判定，不再重评）。
CLASSIFY_MIN_CONFIDENCE = 0.6

#: 采样段数与每段的消息批次上限。
_CLASSIFY_SEGMENTS = 3
_CLASSIFY_BATCH_LIMIT = 200

_CLASSIFY_SYSTEM_PROMPT = (
    "你要判断一位微信联系人与用户的关系类型。依据备注名与三段不同时期的聊天片段"
    "（已脱敏），输出 JSON：{\"kind\": \"friend\"|\"family\"|\"transactional\", "
    "\"confidence\": 0到1的小数}。判定标准：family=家人亲戚（称呼、家事、长辈语气）；"
    "transactional=事务往来（订票、快递、房东、客服、代购等目的性交易沟通）；其余一律"
    " friend。拿不准就 friend 且 confidence 低。只回复 JSON。"
)


def classify_contacts(
    store: MetricsStore, reader, gap_seconds: int, report_cb=None
) -> int:
    """给候选联系人做一轮关系类型判定，返回本轮判定的个数。

    候选 = 从未自动判定、也未手动设置、且累计消息数达标（≥30）的联系人，
    按消息量降序（事务号往往消息多、污染最重，优先处理）截断到
    CLASSIFY_MAX_PER_RUN，剩下的下一轮再判。只判一次：写进 kind_auto 的
    联系人不再是候选，手动改判过的同样跳过。
    """

    pending = [
        contact
        for contact in store.all_contacts()
        if not contact.kind_auto
        and not contact.kind_manual
        and contact.total_messages >= CLASSIFY_MIN_MESSAGES
    ]
    pending.sort(key=lambda contact: -contact.total_messages)
    selected = pending[:CLASSIFY_MAX_PER_RUN]
    # 没有候选就不报阶段：进度条上闪过「关系分类 0/0」只是噪音。
    if report_cb is not None and selected:
        report_cb(phase="classify", done=0, total=len(selected), detail="")
    classified = 0
    for done, contact in enumerate(selected, start=1):
        if report_cb is not None:
            report_cb(
                phase="classify",
                done=done,
                total=len(selected),
                detail=contact.display_name,
            )
        kind = _classify_one(reader, contact, gap_seconds)
        if kind is not None:
            store.set_contact_kind_auto(contact.session_id, kind)
            classified += 1
    LOG.info("关系类型分类：本轮判定 %d 个联系人", classified)
    return classified


def _classify_one(reader, contact: ContactRow, gap_seconds: int) -> str | None:
    """对一个联系人做一次分类调用；None = 本轮判定失败、下轮再试。

    采样拿不到任何 text（如全是图片）→ 直接写 'friend'（视为已判定，不
    浪费一次调用）；判定结果不理想（kind 非法或 confidence 不足）也落
    'friend' 并写入——分类是稳定属性，写入即视为已判定。只有解析失败
    （LLM 没回可用 JSON）才不写，下一轮再试。user 文本整体过 mask()
    后才出站。
    """

    sample = _classify_sample(reader, contact, gap_seconds)
    if sample is None:
        return "friend"
    reply = llm.chat(
        _CLASSIFY_SYSTEM_PROMPT, mask(f"备注名：{contact.display_name}\n\n{sample}")
    )
    if reply is None:
        return None
    return _parse_classify_reply(reply)


def _parse_classify_reply(reply: str) -> str | None:
    """解析分类回复；解析不出 JSON 返回 None（不写、下轮再试）。

    kind 不在三值内或 confidence < 0.6 → 返回 'friend'（写入即视为已判
    定）。不把回复内容写进日志——日志里出现任何聊天相关文本都违背「原文
    不出容器」的原则。
    """

    start = reply.find("{")
    end = reply.rfind("}")
    if start < 0 or end <= start:
        LOG.warning("关系类型分类回复里没有 JSON 块")
        return None
    try:
        data = json.loads(reply[start : end + 1])
        kind = str(data["kind"])
        confidence = float(data["confidence"])
    except (ValueError, KeyError, TypeError):
        LOG.warning("关系类型分类回复里解析不出 kind/confidence")
        return None
    if kind not in KIND_VALUES or confidence < CLASSIFY_MIN_CONFIDENCE:
        return "friend"
    return kind


def _classify_sample(reader, contact: ContactRow, gap_seconds: int) -> str | None:
    """全时段采样：把 [first_message_at, last_message_at] 均分 3 个时间点，
    每个点读一小批消息、取切出的第一段对话的 text 消息。

    设计动机：家人的证据可能在多年之前（小时候的称呼、年轻时的家事），
    最近 60 天的采样窗抓不到——分类必须覆盖整个往来史，所以不复用
    _llm_sample 的近期窗口。相邻采样点可能落在同一批消息里（往来史很短
    时），同一段对话只取一次，避免三段内容完全重复。总字数达到
    CLASSIFY_SAMPLE_CHARS 就停；没有 text 返回 None。
    """

    if contact.first_message_at is None or contact.last_message_at is None:
        return None
    # 延迟导入打破循环依赖：analyzer 在顶部导入本模块，而
    # read_messages_after / Cursor 定义在 analyzer 里，运行期再取即可。
    from .analyzer import Cursor, read_messages_after

    span = contact.last_message_at - contact.first_message_at
    blocks: list[str] = []
    seen_starts: set[tuple[int, str, int]] = set()
    total = 0
    for index in range(_CLASSIFY_SEGMENTS):
        point = contact.first_message_at + span * index // (_CLASSIFY_SEGMENTS - 1)
        batch = read_messages_after(
            reader,
            contact.session_id,
            contact.display_name,
            {},
            Cursor(point, "", -1),
            _CLASSIFY_BATCH_LIMIT,
        )
        conversations = split_conversations(batch.messages, gap_seconds)
        if not conversations:
            continue
        first_position = batch.positions[0]
        key = (
            first_position.timestamp,
            first_position.shard,
            first_position.local_id,
        )
        if key in seen_starts:
            continue
        seen_starts.add(key)
        lines = [
            f"{'我' if message.direction == ME else 'TA'}: {message.text}"
            for message in conversations[0]
            if message.kind == "text" and message.text
        ]
        if not lines:
            continue
        block = "\n".join(lines)
        blocks.append(block)
        total += len(block)
        if total >= CLASSIFY_SAMPLE_CHARS:
            break
    return None if not blocks else "\n\n".join(blocks)
