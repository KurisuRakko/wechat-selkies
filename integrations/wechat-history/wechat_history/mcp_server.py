"""Private stdio MCP server for one verified WeChat account."""

from __future__ import annotations

import atexit
import logging
import sys
import threading
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer

from .constants import TARGET_ACCOUNT_MASK
from .errors import HistoryError
from .reader import HistoryReader
from .reply import ReplyPreparer, probe_wechat_window_status


logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="wechat-history %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("wechat-history")


class HistoryService:
    def __init__(self):
        self._reader: HistoryReader | None = None
        self._reply: ReplyPreparer | None = None
        self._lock = threading.RLock()

    def _components(self) -> tuple[HistoryReader, ReplyPreparer]:
        if self._reader is None:
            self._reader = HistoryReader()
            self._reply = ReplyPreparer(self._reader)
        return self._reader, self._reply  # type: ignore[return-value]

    def close(self) -> None:
        with self._lock:
            if self._reader is not None:
                self._reader.close()
            self._reader = None
            self._reply = None

    def invoke(self, operation: Callable[[HistoryReader, ReplyPreparer], dict]) -> dict:
        with self._lock:
            try:
                reader, reply = self._components()
                result = operation(reader, reply)
                return {"ok": True, **result}
            except HistoryError as exc:
                return exc.payload()
            except Exception as exc:
                # Never log exception text: it may contain a filesystem path.
                LOGGER.error("internal failure type=%s", type(exc).__name__)
                return {
                    "ok": False,
                    "error": {
                        "code": "INTERNAL",
                        "message": "内部错误；未在日志中输出路径或密钥",
                    },
                }

    def health_check(self) -> dict:
        with self._lock:
            window_status = probe_wechat_window_status()
            common = {
                "account": TARGET_ACCOUNT_MASK,
                "wechat_window": window_status,
                "transport": "stdio",
                "network_listener": False,
                "automatic_send": False,
            }
            try:
                reader, _ = self._components()
                return {
                    "ok": True,
                    **reader.status(),
                    "snapshot_status": "ready",
                    **common,
                }
            except HistoryError as exc:
                key_status = {
                    "KEY_NOT_FOUND": "missing",
                    "KEY_PERMISSIONS": "unreadable",
                    "KEY_INVALID": "invalid",
                    "KEY_INCOMPLETE": "incomplete",
                    "KEY_STALE": "stale",
                }.get(exc.code, "unavailable")
                return {
                    **exc.payload(),
                    "identity_verified": False,
                    "key_status": key_status,
                    "database_status": "unavailable",
                    "snapshot_status": "unavailable",
                    **common,
                }
            except Exception as exc:
                LOGGER.error("internal failure type=%s", type(exc).__name__)
                return {
                    "ok": False,
                    "error": {
                        "code": "INTERNAL",
                        "message": "内部错误；未在日志中输出路径或密钥",
                    },
                    "identity_verified": False,
                    "key_status": "unavailable",
                    "database_status": "unavailable",
                    "snapshot_status": "unavailable",
                    **common,
                }


SERVICE = HistoryService()
atexit.register(SERVICE.close)

MCP = MCPServer(
    "wechat-history",
    instructions=(
        "只读取已验证的旧微信账户。prepare_reply 只填写草稿，"
        "绝不会点击发送或模拟 Enter；发送前必须由用户在微信界面确认。"
    ),
)


@MCP.tool()
def health_check() -> dict[str, Any]:
    """检查固定账户、密钥、数据库、微信窗口和发送禁用状态。"""

    return SERVICE.health_check()


@MCP.tool()
def list_sessions(
    query: str | None = None,
    kind: str | None = None,
    unread_only: bool = False,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """列出目标旧账户的会话；kind 可为 direct、group 或 official。"""

    return SERVICE.invoke(
        lambda reader, _: reader.list_sessions(
            query=query,
            kind=kind,
            unread_only=unread_only,
            limit=limit,
            cursor=cursor,
        )
    )


@MCP.tool()
def get_messages(
    session_id: str,
    limit: int = 100,
    before_cursor: str | None = None,
) -> dict[str, Any]:
    """按时间读取一个会话的消息；媒体仅返回类型与安全元数据。"""

    return SERVICE.invoke(
        lambda reader, _: reader.get_messages(
            session_id=session_id, limit=limit, before_cursor=before_cursor
        )
    )


@MCP.tool()
def search_messages(
    query: str,
    session_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """搜索目标账户的数据库文本，可选会话和 ISO-8601 时间范围。"""

    return SERVICE.invoke(
        lambda reader, _: reader.search_messages(
            query=query,
            session_id=session_id,
            since=since,
            until=until,
            limit=limit,
            cursor=cursor,
        )
    )


@MCP.tool()
def prepare_reply(session_id: str, text: str) -> dict[str, Any]:
    """把草稿填入微信输入框；不会发送，用户必须目视确认后手动发送。"""

    return SERVICE.invoke(lambda _, reply: reply.prepare(session_id, text))


def main() -> None:
    MCP.run(transport="stdio")


if __name__ == "__main__":
    main()
