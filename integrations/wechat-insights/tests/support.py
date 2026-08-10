"""测试共享件：假读取器、库构造器、基础用例与常量（不匹配 test*.py，不会被收集）。

五个测试文件共用同一套 fixture，拆分后各处只放真正的断言。
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wechat_history.formatting import message_kind

from wechat_insights.analyzer import Analyzer
from wechat_insights.depth import DepthStrategy
from wechat_insights.metrics import Metrics, day_key
from wechat_insights.storage import MetricsStore


SESSION_ID = "friend"
DISPLAY_NAME = "Alice"
HOUR = 3600
# 消息全部落在这一天之后，测试里用一个固定的「现在」。
BASE = 1_740_000_000
NOW = BASE + 30 * 86400

# real_sender_id → username，1 是对方，2 是我自己。
_NAMES = {1: SESSION_ID, 2: "me"}


class FakeReader:
    """只提供分析器实际用到的那几个 reader 接口，底下是真实的 sqlite 表。

    databases 按相对路径映射到各分片文件，和真实 reader 的
    message/message_N.db 布局一致。
    """

    def __init__(self, databases: dict[str, Path]):
        self.databases = databases
        self.cache = SimpleNamespace(get=lambda key: databases[key])
        self.closed = False

    @staticmethod
    def _message_table(session_id: str) -> str:
        return f"Msg_{hashlib.md5(session_id.encode('utf-8')).hexdigest()}"

    def _message_database_keys(self) -> list[str]:
        return sorted(self.databases)

    def _name_map(self, _: sqlite3.Connection) -> dict[int, str]:
        return dict(_NAMES)

    def _message_item(self, row, _relative_db, _session, _contacts, names) -> dict:
        sender = names.get(int(row["real_sender_id"] or 0), "")
        if sender == SESSION_ID:
            direction = "incoming"
        elif sender == "me":
            direction = "outgoing"
        else:
            direction = "unknown"
        return {
            "direction": direction,
            "type": message_kind(row["local_type"]),
            "text": row["message_content"] or "",
        }

    def _load_contacts(self) -> dict:
        return {}

    def close(self) -> None:
        self.closed = True


def build_database(
    path: Path,
    rows: list[tuple[int, int, int, int, str]],
    session_id: str = SESSION_ID,
) -> None:
    """rows = (local_id, local_type, create_time, real_sender_id, content)。"""

    table = FakeReader._message_table(session_id)
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS [{table}] (
                local_id INTEGER PRIMARY KEY,
                local_type INTEGER,
                create_time INTEGER,
                real_sender_id INTEGER,
                message_content TEXT,
                WCDB_CT_message_content INTEGER
            )
            """
        )
        connection.executemany(
            f"INSERT OR REPLACE INTO [{table}] VALUES (?, ?, ?, ?, ?, 0)", rows
        )


def them(local_id: int, offset: int, text: str = "在吗？") -> tuple:
    return (local_id, 1, BASE + offset, 1, text)


def me(local_id: int, offset: int, text: str = "在的") -> tuple:
    return (local_id, 1, BASE + offset, 2, text)


class AnalyzerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "message_0.db"
        self.store = MetricsStore(self.root / "metrics.db")
        self.addCleanup(self.store.close)
        self.reader = FakeReader({"message/message_0.db": self.database})

    def analyzer(
        self,
        batch_size: int = 5000,
        strategy: DepthStrategy | None = None,
        progress_cb=None,
    ) -> Analyzer:
        return Analyzer(
            self.store,
            reader_factory=lambda: self.reader,
            strategy=strategy,
            batch_size=batch_size,
            progress_cb=progress_cb,
        )

    def run_analysis(self, now: int = NOW, batch_size: int = 5000):
        sessions = {SESSION_ID: SimpleNamespace(display_name=DISPLAY_NAME)}
        with patch(
            "wechat_insights.analyzer.scan_direct_rows", return_value=sessions
        ):
            return self.analyzer(batch_size).run(now=now)

    def seed_messages(self, session_id: str, name: str, days: dict[int, int]) -> None:
        """绕过读取器，直接把 (时间戳 → 当天 TA 的消息数) 写进 stats_daily。"""

        contact = self.store.ensure_contact(session_id, name)
        buckets = {}
        for timestamp, count in days.items():
            metrics = Metrics()
            metrics.add("msgs_them", count)
            buckets[day_key(timestamp)] = metrics
        self.store.commit_batch(session_id, buckets, contact)


def backfill_grid(earliest: str, today: str) -> list[str]:
    """全史回放的周网格（与 history.backfill_history 同一语义）。"""

    days = []
    cursor = date.fromisoformat(earliest)
    end = date.fromisoformat(today)
    while cursor <= end:
        days.append(cursor.isoformat())
        cursor += timedelta(days=7)
    return days


def daily_grid(start: str, today: str) -> list[str]:
    """逐日细化的网格（与 history.refine_daily_history 同一语义）。"""

    days = []
    cursor = date.fromisoformat(start)
    end = date.fromisoformat(today)
    while cursor <= end:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days
