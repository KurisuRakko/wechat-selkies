"""Single-account WeChat session and message reader."""

from __future__ import annotations

import base64
import hashlib
import heapq
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .constants import (
    EXPECTED_IDENTITY_TOKENS,
    MAX_MESSAGE_LIMIT,
    MAX_SEARCH_LIMIT,
    MAX_SESSION_LIMIT,
    TARGET_ACCOUNT_MASK,
    TARGET_DB_DIR,
    TARGET_USERNAME,
)
from .errors import HistoryError, fail
from .formatting import decompress_content, format_message, message_kind
from .keyscan import process_start_ticks, require_target_account_active
from .snapshot import KeyStore, SnapshotCache


_MESSAGE_KEY = re.compile(r"^message/message_\d+\.db$")
_MESSAGE_TABLE = re.compile(r"^Msg_[0-9a-f]{32}$")


def _iso_timestamp(value: int | float) -> str:
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def _encode_cursor(scope: str, values: list[object]) -> str:
    raw = json.dumps(
        {"v": 1, "scope": scope, "values": values},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None, scope: str) -> list[object] | None:
    if not cursor:
        return None
    try:
        padded = cursor + ("=" * (-len(cursor) % 4))
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if value.get("v") != 1 or value.get("scope") != scope:
            raise ValueError
        values = value["values"]
        if not isinstance(values, list):
            raise ValueError
        return values
    except Exception as exc:
        raise fail("INVALID_CURSOR", "分页 cursor 无效或不属于当前工具") from exc


def _parse_iso8601(value: str | None, field: str) -> int | None:
    if not value:
        return None
    try:
        normalized = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return int(parsed.timestamp())
    except (TypeError, ValueError) as exc:
        raise fail("INVALID_TIME", f"{field} 必须是 ISO-8601 时间") from exc


def _validate_limit(value: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > maximum:
        raise fail("INVALID_LIMIT", f"limit 必须在 1 到 {maximum} 之间")
    return value


@contextmanager
def _read_connection(path: Path) -> Iterator[sqlite3.Connection]:
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True
        )
        connection.row_factory = sqlite3.Row
        yield connection
    except sqlite3.Error as exc:
        raise fail("SCHEMA_UNSUPPORTED", "微信数据库结构无法读取") from exc
    finally:
        if "connection" in locals():
            connection.close()


class HistoryReader:
    def __init__(
        self,
        key_store: KeyStore | None = None,
        source_dir: Path = TARGET_DB_DIR,
        cache: SnapshotCache | None = None,
    ):
        self.key_store = key_store or KeyStore()
        self.source_dir = source_dir
        self.cache = cache or SnapshotCache(self.key_store, source_dir=source_dir)
        self.account_root = source_dir.parent
        self.source_root = self.account_root.parent
        self._profile: dict | None = None

    def close(self) -> None:
        self.cache.close()

    def _load_contacts(self) -> dict[str, dict]:
        database = self.cache.get("contact/contact.db")
        contacts: dict[str, dict] = {}
        with _read_connection(database) as connection:
            rows = connection.execute(
                "SELECT username, nick_name, remark, alias FROM contact"
            ).fetchall()
        for row in rows:
            username = row["username"] or ""
            if not username:
                continue
            nickname = row["nick_name"] or ""
            remark = row["remark"] or ""
            alias = row["alias"] or ""
            contacts[username] = {
                "session_id": username,
                "nickname": nickname,
                "remark": remark,
                "alias": alias,
                "display_name": remark or nickname or alias or username,
            }
        return contacts

    def ensure_account_validated(self) -> dict:
        if self._profile is not None:
            return self._profile
        contacts = self._load_contacts()
        profile = contacts.get(TARGET_USERNAME)
        if profile is None:
            raise fail(
                "ACCOUNT_MISMATCH",
                "解密数据库中找不到目标旧账户的自身资料",
            )
        identity_text = " ".join(
            str(profile.get(field, ""))
            for field in ("nickname", "remark", "alias", "display_name")
        ).casefold()
        if not any(token.casefold() in identity_text for token in EXPECTED_IDENTITY_TOKENS):
            raise fail(
                "ACCOUNT_MISMATCH",
                "旧账户自身资料与“杨博文 / Spencer / KurisuRakko”不匹配",
            )
        self._profile = {
            "account": TARGET_ACCOUNT_MASK,
            "display_name": profile["display_name"],
            "identity_verified": True,
        }
        return self._profile

    def _session_rows(self) -> list[sqlite3.Row]:
        self.ensure_account_validated()
        database = self.cache.get("session/session.db")
        with _read_connection(database) as connection:
            return connection.execute(
                """
                SELECT username, unread_count, summary, last_timestamp,
                       last_msg_type, last_msg_sender, last_sender_display_name
                FROM SessionTable
                WHERE last_timestamp > 0
                ORDER BY last_timestamp DESC, username ASC
                """
            ).fetchall()

    @staticmethod
    def _session_kind(session_id: str) -> str:
        if "@chatroom" in session_id:
            return "group"
        if session_id.startswith("gh_"):
            return "official"
        return "direct"

    def _session_items(self) -> list[dict]:
        contacts = self._load_contacts()
        items: list[dict] = []
        for row in self._session_rows():
            session_id = row["username"] or ""
            if not session_id:
                continue
            contact = contacts.get(session_id, {})
            preview = decompress_content(row["summary"], 4 if isinstance(row["summary"], bytes) else 0)
            preview = preview or ""
            if ":\n" in preview:
                preview = preview.split(":\n", 1)[1]
            timestamp = int(row["last_timestamp"] or 0)
            items.append(
                {
                    "session_id": session_id,
                    "display_name": contact.get("display_name", session_id),
                    "kind": self._session_kind(session_id),
                    "unread_count": int(row["unread_count"] or 0),
                    "last_message_at": _iso_timestamp(timestamp),
                    "last_message_timestamp": timestamp,
                    "last_message_type": message_kind(row["last_msg_type"]),
                    "preview": preview[:500],
                }
            )
        items.sort(key=lambda item: (-item["last_message_timestamp"], item["session_id"]))
        return items

    def list_sessions(
        self,
        query: str | None = None,
        kind: str | None = None,
        unread_only: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        limit = _validate_limit(limit, MAX_SESSION_LIMIT)
        if kind not in (None, "direct", "group", "official"):
            raise fail("INVALID_KIND", "kind 只能是 direct、group 或 official")
        items = self._session_items()
        needle = (query or "").strip().casefold()
        if needle:
            items = [
                item
                for item in items
                if needle in item["display_name"].casefold()
                or needle in item["session_id"].casefold()
            ]
        if kind:
            items = [item for item in items if item["kind"] == kind]
        if unread_only:
            items = [item for item in items if item["unread_count"] > 0]

        anchor = _decode_cursor(cursor, "sessions")
        if anchor is not None:
            try:
                anchor_key = (-int(anchor[0]), str(anchor[1]))
            except (IndexError, TypeError, ValueError) as exc:
                raise fail("INVALID_CURSOR", "会话分页 cursor 内容无效") from exc
            items = [
                item
                for item in items
                if (-item["last_message_timestamp"], item["session_id"]) > anchor_key
            ]

        page = items[:limit]
        next_cursor = None
        if len(items) > limit and page:
            last = page[-1]
            next_cursor = _encode_cursor(
                "sessions", [last["last_message_timestamp"], last["session_id"]]
            )
        for item in page:
            item.pop("last_message_timestamp", None)
        return {"items": page, "next_cursor": next_cursor}

    def _resolve_session(self, session_id: str) -> dict:
        if not isinstance(session_id, str) or not session_id or len(session_id) > 255 or "\x00" in session_id:
            raise fail("SESSION_NOT_FOUND", "session_id 无效")
        contacts = self._load_contacts()
        sessions = {item["session_id"]: item for item in self._session_items()}
        if session_id not in sessions and session_id not in contacts:
            raise fail("SESSION_NOT_FOUND", "目标账户中不存在该会话")
        contact = contacts.get(session_id, {})
        return {
            "session_id": session_id,
            "display_name": contact.get("display_name", sessions.get(session_id, {}).get("display_name", session_id)),
            "alias": contact.get("alias", ""),
            "kind": self._session_kind(session_id),
        }

    def _message_database_keys(self) -> list[str]:
        keys = [
            key
            for key in self.key_store.database_paths("message/")
            if _MESSAGE_KEY.fullmatch(key)
        ]
        if not keys:
            raise fail("KEY_INCOMPLETE", "没有可读取的消息数据库密钥")
        return keys

    @staticmethod
    def _message_table(session_id: str) -> str:
        return f"Msg_{hashlib.md5(session_id.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _name_map(connection: sqlite3.Connection) -> dict[int, str]:
        try:
            return {
                int(row[0]): str(row[1])
                for row in connection.execute("SELECT rowid, user_name FROM Name2Id")
                if row[1]
            }
        except sqlite3.Error:
            return {}

    @staticmethod
    def _message_identifier(relative_db: str, local_id: object) -> str:
        digest = hashlib.sha256(f"{relative_db}:{local_id}".encode("utf-8")).hexdigest()
        return f"m_{digest[:24]}"

    def _message_item(
        self,
        row: sqlite3.Row,
        relative_db: str,
        session: dict,
        contacts: dict[str, dict],
        names: dict[int, str],
    ) -> dict:
        timestamp = int(row["create_time"] or 0)
        content = decompress_content(
            row["message_content"], row["WCDB_CT_message_content"]
        )
        if content is None:
            content = ""
        sender_hint, text, metadata = format_message(
            row["local_id"], row["local_type"], content, session["kind"] == "group"
        )
        sender_id = names.get(int(row["real_sender_id"] or 0), "") or sender_hint
        if sender_id == TARGET_USERNAME:
            direction = "outgoing"
            sender_name = "me"
        elif sender_id:
            direction = "incoming"
            sender_name = contacts.get(sender_id, {}).get("display_name", sender_id)
        else:
            direction = "unknown"
            sender_name = ""
        sort_key = (timestamp, relative_db, int(row["local_id"] or 0))
        return {
            "_sort_key": sort_key,
            "message_id": self._message_identifier(relative_db, row["local_id"]),
            "session_id": session["session_id"],
            "sent_at": _iso_timestamp(timestamp),
            "direction": direction,
            "sender_name": sender_name,
            "type": message_kind(row["local_type"]),
            "text": text,
            "metadata": metadata,
        }

    def get_messages(
        self,
        session_id: str,
        limit: int = 100,
        before_cursor: str | None = None,
    ) -> dict:
        limit = _validate_limit(limit, MAX_MESSAGE_LIMIT)
        self.ensure_account_validated()
        session = self._resolve_session(session_id)
        table = self._message_table(session_id)
        if not _MESSAGE_TABLE.fullmatch(table):
            raise fail("SESSION_NOT_FOUND", "会话消息表名无效")
        anchor_values = _decode_cursor(before_cursor, "messages")
        anchor: tuple[int, str, int] | None = None
        if anchor_values is not None:
            try:
                anchor = (int(anchor_values[0]), str(anchor_values[1]), int(anchor_values[2]))
            except (IndexError, TypeError, ValueError) as exc:
                raise fail("INVALID_CURSOR", "消息分页 cursor 内容无效") from exc
        contacts = self._load_contacts()
        collected: list[dict] = []
        for relative_db in self._message_database_keys():
            database = self.cache.get(relative_db)
            with _read_connection(database) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    continue
                names = self._name_map(connection)
                parameters: list[object] = []
                where = ""
                if anchor is not None:
                    where = "WHERE create_time <= ?"
                    parameters.append(anchor[0])
                rows = connection.execute(
                    f"""
                    SELECT local_id, local_type, create_time, real_sender_id,
                           message_content, WCDB_CT_message_content
                    FROM [{table}]
                    {where}
                    ORDER BY create_time DESC, local_id DESC
                    LIMIT ?
                    """,
                    (*parameters, limit + 1),
                ).fetchall()
                for row in rows:
                    item = self._message_item(
                        row, relative_db, session, contacts, names
                    )
                    if anchor is None or item["_sort_key"] < anchor:
                        collected.append(item)

        collected.sort(key=lambda item: item["_sort_key"], reverse=True)
        page = collected[:limit]
        next_cursor = None
        if len(collected) > limit and page:
            next_cursor = _encode_cursor("messages", list(page[-1]["_sort_key"]))
        page.reverse()
        for item in page:
            item.pop("_sort_key", None)
        return {"session": session, "items": page, "next_cursor": next_cursor}

    def search_messages(
        self,
        query: str,
        session_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        limit = _validate_limit(limit, MAX_SEARCH_LIMIT)
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise fail("INVALID_QUERY", "搜索词必须为 1 到 200 个字符")
        start = _parse_iso8601(since, "since")
        end = _parse_iso8601(until, "until")
        if start is not None and end is not None and start > end:
            raise fail("INVALID_TIME", "since 不能晚于 until")
        anchor_values = _decode_cursor(cursor, "search")
        anchor: tuple[int, str, int] | None = None
        if anchor_values is not None:
            try:
                anchor = (int(anchor_values[0]), str(anchor_values[1]), int(anchor_values[2]))
            except (IndexError, TypeError, ValueError) as exc:
                raise fail("INVALID_CURSOR", "搜索分页 cursor 内容无效") from exc

        self.ensure_account_validated()
        sessions = (
            [self._resolve_session(session_id)]
            if session_id
            else [
                {
                    "session_id": item["session_id"],
                    "display_name": item["display_name"],
                    "kind": item["kind"],
                    "alias": "",
                }
                for item in self._session_items()
            ]
        )
        contacts = self._load_contacts()
        needle = query.strip().casefold()
        ranked: list[tuple[tuple[int, str, int], int, dict]] = []
        sequence = 0
        for relative_db in self._message_database_keys():
            database = self.cache.get(relative_db)
            with _read_connection(database) as connection:
                names = self._name_map(connection)
                available = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                    )
                }
                for session in sessions:
                    table = self._message_table(session["session_id"])
                    if table not in available or not _MESSAGE_TABLE.fullmatch(table):
                        continue
                    clauses: list[str] = []
                    parameters: list[object] = []
                    if start is not None:
                        clauses.append("create_time >= ?")
                        parameters.append(start)
                    if end is not None:
                        clauses.append("create_time <= ?")
                        parameters.append(end)
                    if anchor is not None:
                        clauses.append("create_time <= ?")
                        parameters.append(anchor[0])
                    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                    rows = connection.execute(
                        f"""
                        SELECT local_id, local_type, create_time, real_sender_id,
                               message_content, WCDB_CT_message_content
                        FROM [{table}]
                        {where}
                        ORDER BY create_time DESC, local_id DESC
                        """,
                        parameters,
                    )
                    for row in rows:
                        searchable = decompress_content(
                            row["message_content"], row["WCDB_CT_message_content"]
                        )
                        if searchable is None or needle not in searchable.casefold():
                            continue
                        item = self._message_item(
                            row, relative_db, session, contacts, names
                        )
                        if anchor is None or item["_sort_key"] < anchor:
                            item["session_name"] = session["display_name"]
                            sequence += 1
                            heapq.heappush(
                                ranked, (item["_sort_key"], sequence, item)
                            )
                            if len(ranked) > limit + 1:
                                heapq.heappop(ranked)

        collected = [
            entry[2]
            for entry in sorted(ranked, key=lambda entry: entry[0], reverse=True)
        ]
        page = collected[:limit]
        next_cursor = None
        if len(collected) > limit and page:
            next_cursor = _encode_cursor("search", list(page[-1]["_sort_key"]))
        for item in page:
            item.pop("_sort_key", None)
        return {
            "items": page,
            "next_cursor": next_cursor,
            "search_mode": "streaming_decompressed_text",
            "note": "流式解压文本但不建立持久索引；媒体内容不参与搜索",
        }

    def reply_session(self, session_id: str) -> dict:
        self.ensure_account_validated()
        session = self._resolve_session(session_id)
        all_sessions = self._session_items()
        duplicate_count = sum(
            1
            for item in all_sessions
            if item["display_name"].casefold() == session["display_name"].casefold()
        )
        if session_id == "filehelper":
            contacts = self._load_contacts()
            contact_duplicate_count = sum(
                1
                for item in contacts.values()
                if item["display_name"].casefold()
                == session["display_name"].casefold()
            )
            if contact_duplicate_count != 1:
                raise fail(
                    "AMBIGUOUS_SESSION",
                    "文件传输助手显示名不唯一，拒绝操作微信界面",
                )
            ui_query = session["display_name"]
        elif session["kind"] == "direct":
            ui_query = session.get("alias") or session_id
        else:
            ui_query = session["display_name"]
        if session_id != "filehelper" and session["kind"] != "direct" and duplicate_count != 1:
            raise fail("AMBIGUOUS_SESSION", "会话显示名不唯一，拒绝操作微信界面")
        if not ui_query or len(ui_query) > 255:
            raise fail("AMBIGUOUS_SESSION", "无法生成唯一的微信界面搜索词")
        return {**session, "ui_query": ui_query}

    def require_reply_account_active(self) -> None:
        """Conservative guard: scan PID must still exist and target login must be newest."""

        metadata = self.key_store.metadata
        try:
            scanned_pid = int(metadata["wechat_pid"])
            scanned_start = int(metadata["wechat_start_ticks"])
        except (KeyError, TypeError, ValueError) as exc:
            raise fail("REPLY_ACCOUNT_UNCONFIRMED", "密钥文件缺少微信登录状态标记") from exc
        try:
            if process_start_ticks(scanned_pid) != scanned_start or not Path(
                f"/proc/{scanned_pid}/comm"
            ).read_text().strip().lower().startswith(("wechat", "weixin")):
                raise fail("REPLY_ACCOUNT_UNCONFIRMED", "微信已重启；请在目标账户重新扫描密钥")
        except (OSError, HistoryError) as exc:
            raise fail("REPLY_ACCOUNT_UNCONFIRMED", "微信已退出或重启") from exc

        try:
            require_target_account_active(self.source_dir)
        except HistoryError as exc:
            raise fail(
                "REPLY_ACCOUNT_UNCONFIRMED",
                "当前微信不是目标旧账户；请先手动切换并重新扫描密钥",
            ) from exc

    def status(self) -> dict:
        profile = self.ensure_account_validated()
        return {
            **profile,
            "key_status": "valid",
            "database_status": "readable",
            "message_database_count": len(self._message_database_keys()),
        }
