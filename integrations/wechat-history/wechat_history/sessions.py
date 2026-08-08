"""私聊会话扫描：过滤规则的唯一来源。

通知服务和关系洞察分析器都基于这里的判断挑出「真正的私聊」，避免两处各写一份
排除名单再慢慢跑偏。这个模块只依赖 reader 与 formatting，不引入 Web Push 那条
依赖链，所以独立的分析容器可以只装 pycryptodome 与 zstandard。
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import TARGET_USERNAME
from .formatting import decompress_content
from .reader import HistoryReader, _read_connection


#: 微信内置的系统会话，不是人。
SYSTEM_SESSION_IDS = frozenset(
    {
        "blogapp",
        "facebookapp",
        "feedsapp",
        "filehelper",
        "floatbottle",
        "fmessage",
        "masssendapp",
        "medianote",
        "newsapp",
        "notifymessage",
        "notification_messages",
        "officialaccounts",
        "qmessage",
        "qqmail",
        "tmessage",
        "voip",
        "weixin",
        "weixinreminder",
    }
)


@dataclass(frozen=True, slots=True)
class DirectSessionRow:
    """session.db 里一条已确认属于私聊的会话行。"""

    session_id: str
    display_name: str
    timestamp: int
    local_id: int
    unread_count: int
    summary: str | None


def is_direct_session(
    session_id: str, contacts: dict[str, dict], is_hidden: int
) -> bool:
    """判断一个会话是否是要分析的私聊。

    排除：自己、系统会话、公众号（gh_）、群聊（@chatroom）、隐藏会话，
    以及不在通讯录里的陌生会话。
    """

    return bool(
        session_id
        and session_id != TARGET_USERNAME
        and session_id not in SYSTEM_SESSION_IDS
        and not session_id.startswith("gh_")
        and "@chatroom" not in session_id
        and session_id in contacts
        and is_hidden == 0
    )


def scan_direct_rows(reader: HistoryReader) -> dict[str, DirectSessionRow]:
    """读取 session.db，返回全部私聊会话行。"""

    reader.ensure_account_validated()
    contacts = reader._load_contacts()
    database = reader.cache.get("session/session.db")
    with _read_connection(database) as connection:
        rows = connection.execute(
            """
            SELECT username, unread_count, summary, last_timestamp,
                   last_msg_locald_id, is_hidden
            FROM SessionTable
            WHERE last_timestamp > 0
            """
        ).fetchall()

    sessions: dict[str, DirectSessionRow] = {}
    for row in rows:
        session_id = str(row["username"] or "")
        if not is_direct_session(session_id, contacts, int(row["is_hidden"] or 0)):
            continue
        timestamp = int(row["last_timestamp"] or 0)
        local_id = int(row["last_msg_locald_id"] or 0)
        if timestamp <= 0 or local_id < 0:
            continue
        summary = decompress_content(
            row["summary"], 4 if isinstance(row["summary"], bytes) else 0
        )
        if summary and ":\n" in summary:
            # 群聊格式的「发送者:\n正文」前缀在私聊里偶尔也会出现。
            summary = summary.split(":\n", 1)[1]
        sessions[session_id] = DirectSessionRow(
            session_id=session_id,
            display_name=str(contacts[session_id].get("display_name") or session_id),
            timestamp=timestamp,
            local_id=local_id,
            unread_count=max(0, int(row["unread_count"] or 0)),
            summary=summary,
        )
    return sessions
