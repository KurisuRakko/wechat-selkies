"""从微信消息库读取游标之后的原始消息（增量同步与 LLM 采样共用）。

analyzer 的增量读取、classify/portrait/periods 的 LLM 采样都要「从某个游标
读一批消息」，全部收在这一个模块，避免三个采样点各自复制一份游标逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass

from wechat_history.reader import HistoryReader, _read_connection

from .constants import BACKFILL_BATCH
from .conversation import ME, Message, split_conversations


@dataclass(frozen=True, slots=True)
class Cursor:
    """已提交的最后一条消息位置；(0, "", -1) 表示还没读过任何消息。

    local_id 只在单个分片内唯一，必须带上分片路径才能构成全局唯一的全序；
    空分片排在一切真实分片之前。排序键与恢复谓词都用这个三元组。
    """

    timestamp: int
    shard: str
    local_id: int


@dataclass(frozen=True, slots=True)
class MessageBatch:
    """一批读到的消息，以及推进游标需要的元信息。"""

    #: 已剔除无方向系统噪声的消息，按时间升序。
    messages: list[Message]
    #: 与 messages 一一对应的游标位置；对话被截断时按前缀取最后一条。
    positions: list[Cursor]
    #: 本批最后一行（含被剔除的行）的位置；一行都没有时为 None。
    last: Cursor | None
    #: 数据库里还有更多行没读。
    has_more: bool


def _resume_where(cursor: Cursor, shard: str) -> tuple[str, tuple[object, ...]]:
    """按 (时间戳, 分片, local_id) 全序生成单个分片的恢复谓词。

    分片之间共享 local_id，游标必须带上分片，否则同刻同号的两行会互相
    吞掉。游标分片之后的分片，同一时间戳下的行都在游标之后，全部要读；
    游标分片之前的分片则整片已读完，只有更晚的时间戳才有效。
    """

    if shard > cursor.shard:
        return "create_time >= ?", (cursor.timestamp,)
    if shard == cursor.shard:
        return (
            "create_time > ? OR (create_time = ? AND local_id > ?)",
            (cursor.timestamp, cursor.timestamp, cursor.local_id),
        )
    return "create_time > ?", (cursor.timestamp,)


def read_messages_after(
    reader: HistoryReader,
    session_id: str,
    display_name: str,
    contacts: dict[str, dict],
    cursor: Cursor,
    limit: int,
) -> MessageBatch:
    """读取游标之后最多 limit 条消息，按时间升序。

    查询模式照抄 notifications.read_new_messages 的游标窗口，区别是不设上界：
    分析器不需要和会话行对齐，读到哪算哪，剩下的留给下一批。
    """

    table = reader._message_table(session_id)
    session = {
        "session_id": session_id,
        "display_name": display_name,
        "alias": "",
        "kind": "direct",
    }
    collected: list[tuple[tuple[int, str, int], Message | None]] = []
    for relative_db in reader._message_database_keys():
        database = reader.cache.get(relative_db)
        with _read_connection(database) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            names = reader._name_map(connection)
            where, parameters = _resume_where(cursor, relative_db)
            rows = connection.execute(
                f"""
                SELECT local_id, local_type, create_time, real_sender_id,
                       message_content, WCDB_CT_message_content
                FROM [{table}]
                WHERE {where}
                ORDER BY create_time ASC, local_id ASC
                LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()
            for row in rows:
                item = reader._message_item(row, relative_db, session, contacts, names)
                timestamp = int(row["create_time"] or 0)
                local_id = int(row["local_id"] or 0)
                # 没有发送者的系统噪声参与不了「谁回谁」的判定，占位成 None：
                # 它不进统计，但游标仍要跨过它，否则每轮都会重复读到。
                message = (
                    None
                    if item["direction"] == "unknown"
                    else Message(
                        timestamp=timestamp,
                        local_id=local_id,
                        direction=str(item["direction"]),
                        kind=str(item["type"]),
                        text=str(item["text"] or ""),
                    )
                )
                collected.append(((timestamp, relative_db, local_id), message))

    collected.sort(key=lambda entry: entry[0])
    window = collected[:limit]
    kept = [entry for entry in window if entry[1] is not None]
    return MessageBatch(
        messages=[entry[1] for entry in kept],
        positions=[Cursor(*entry[0]) for entry in kept],
        last=Cursor(*window[-1][0]) if window else None,
        has_more=len(collected) > limit,
    )


def transcript_lines(messages: list[Message]) -> list[str]:
    """把一段对话压成「我: … / TA: …」文本行，只取有内容的 text 消息。

    画像、分类、时段评分与好感度校准四个采样点共用这一份格式，
    出站文本形状只有一处定义。
    """

    return [
        f"{'我' if message.direction == ME else 'TA'}: {message.text}"
        for message in messages
        if message.kind == "text" and message.text
    ]


def sample_transcript(
    reader: HistoryReader,
    session_id: str,
    display_name: str,
    sample_start: int,
    gap_seconds: int,
    max_chars: int,
) -> str | None:
    """从采样窗口读一批消息，从最晚往前拼最近几段对话；没有 text 返回 None。

    只取 text 类消息，每行「我: 内容 / TA: 内容」，段与段之间空行分隔，
    总字数达到 max_chars 就停。窗口内消息超过一个批次时只覆盖最旧的那一批
    （读接口只支持从游标向前读），采样质量足够。
    """

    batch = read_messages_after(
        reader,
        session_id,
        display_name,
        {},
        Cursor(sample_start, "", -1),
        BACKFILL_BATCH,
    )
    blocks: list[str] = []
    total = 0
    for conversation in reversed(split_conversations(batch.messages, gap_seconds)):
        lines = transcript_lines(conversation)
        if not lines:
            continue
        blocks.append("\n".join(lines))
        total += sum(len(line) for line in lines)
        if total >= max_chars:
            break
    return None if not blocks else "\n\n".join(blocks)
