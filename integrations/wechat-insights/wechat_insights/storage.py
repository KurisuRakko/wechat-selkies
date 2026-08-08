"""metrics.db：只存统计结果，不存任何消息原文。

三张表：
- contacts   每个私聊联系人的游标与里程碑
- stats_daily 按天分桶的可加指标（数值列由 constants.METRIC_COLUMNS 生成）
- scores     分析结束时预计算好的看板数据，HTTP 处理器直接吐 JSON

按天而不是按自然月分桶，是为了让「近 30 天 / 近 90 天」这类滚动窗口是精确的；
自然月视图由 SQL 之外的 Python 聚合从同一批天桶合成，没有第二份真相。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    HISTOGRAM_COLUMNS,
    METRIC_COLUMNS,
    SCHEMA_VERSION,
)
from .metrics import Metrics, dump_histogram, parse_histogram


_ROW_COLUMNS = METRIC_COLUMNS + HISTOGRAM_COLUMNS

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    session_id               TEXT PRIMARY KEY,
    hash                     TEXT NOT NULL UNIQUE,
    display_name             TEXT NOT NULL DEFAULT '',
    cursor_timestamp         INTEGER NOT NULL DEFAULT 0,
    -- -1 让首轮能读到 local_id = 0 的第一条消息。
    cursor_local_id          INTEGER NOT NULL DEFAULT -1,
    first_message_at         INTEGER,
    last_message_at          INTEGER,
    total_messages           INTEGER NOT NULL DEFAULT 0,
    longest_silence_seconds  INTEGER NOT NULL DEFAULT 0,
    longest_silence_ended_at INTEGER,
    latest_night_at          INTEGER,
    -- 凌晨窗口内距 00:00 的秒数，-1 表示还没有凌晨消息。
    latest_night_offset      INTEGER NOT NULL DEFAULT -1,
    max_laugh_run            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stats_daily (
    session_id TEXT NOT NULL,
    day        TEXT NOT NULL,
    {",".join(f"{name} INTEGER NOT NULL DEFAULT 0" for name in METRIC_COLUMNS)},
    {",".join(f"{name} TEXT NOT NULL DEFAULT ''" for name in HISTOGRAM_COLUMNS)},
    PRIMARY KEY (session_id, day)
);

CREATE INDEX IF NOT EXISTS stats_daily_day ON stats_daily (day);

CREATE TABLE IF NOT EXISTS scores (
    session_id TEXT PRIMARY KEY,
    hash       TEXT NOT NULL UNIQUE,
    payload    TEXT NOT NULL
);
"""


def contact_hash(session_id: str) -> str:
    """URL 里用的联系人标识：sha256 前 24 位十六进制，不暴露 wxid。"""

    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]


@dataclass(slots=True)
class ContactRow:
    session_id: str
    hash: str
    display_name: str
    cursor_timestamp: int
    cursor_local_id: int
    first_message_at: int | None
    last_message_at: int | None
    total_messages: int
    longest_silence_seconds: int
    longest_silence_ended_at: int | None
    latest_night_at: int | None
    latest_night_offset: int
    max_laugh_run: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ContactRow:
        return cls(**{key: row[key] for key in cls.__slots__})

    def milestones(self) -> dict[str, object]:
        """详情页里程碑卡片用的字段。"""

        days_known = None
        if self.first_message_at and self.last_message_at:
            days_known = max(
                1, (self.last_message_at - self.first_message_at) // 86400 + 1
            )
        clock = None
        if self.latest_night_offset >= 0:
            clock = f"{self.latest_night_offset // 3600:02d}:{self.latest_night_offset % 3600 // 60:02d}"
        return {
            "first_message_at": self.first_message_at,
            "days_known": days_known,
            "total_messages": self.total_messages,
            "longest_silence_seconds": self.longest_silence_seconds or None,
            "longest_silence_ended_at": self.longest_silence_ended_at,
            "latest_night_at": self.latest_night_at,
            "latest_night_clock": clock,
            "max_haha_run": self.max_laugh_run,
        }


def row_to_metrics(row: sqlite3.Row) -> Metrics:
    metrics = Metrics(
        reply_hist_them=parse_histogram(row["reply_hist_them"]),
        reply_hist_me=parse_histogram(row["reply_hist_me"]),
    )
    for name in METRIC_COLUMNS:
        value = int(row[name] or 0)
        if value:
            metrics.counts[name] = value
    return metrics


class MetricsStore:
    """metrics.db 的唯一入口。每个线程各持一条连接，WAL 让读写互不阻塞。"""

    def __init__(self, path: Path, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        self._local = threading.local()
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path)) as setup, setup:
                setup.executescript(_SCHEMA)
                setup.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )

    @property
    def connection(self) -> sqlite3.Connection:
        existing = getattr(self._local, "connection", None)
        if existing is None:
            if self.read_only:
                existing = sqlite3.connect(
                    f"file:{self.path.as_posix()}?mode=ro", uri=True
                )
            else:
                existing = sqlite3.connect(self.path)
                existing.execute("PRAGMA journal_mode=WAL")
                existing.execute("PRAGMA synchronous=NORMAL")
            existing.row_factory = sqlite3.Row
            self._local.connection = existing
        return existing

    def close(self) -> None:
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            existing.close()
            self._local.connection = None

    # —— meta ——

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return default if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self.connection as connection:
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_json(self, key: str, default: object = None) -> object:
        raw = self.get_meta(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_json(self, key: str, value: object) -> None:
        self.set_meta(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    # —— contacts ——

    def ensure_contact(self, session_id: str, display_name: str) -> ContactRow:
        with self.connection as connection:
            connection.execute(
                "INSERT INTO contacts (session_id, hash, display_name) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET display_name = excluded.display_name",
                (session_id, contact_hash(session_id), display_name),
            )
        return self.get_contact(session_id)  # type: ignore[return-value]

    def get_contact(self, session_id: str) -> ContactRow | None:
        row = self.connection.execute(
            "SELECT * FROM contacts WHERE session_id = ?", (session_id,)
        ).fetchone()
        return None if row is None else ContactRow.from_row(row)

    def get_contact_by_hash(self, value: str) -> ContactRow | None:
        row = self.connection.execute(
            "SELECT * FROM contacts WHERE hash = ?", (value,)
        ).fetchone()
        return None if row is None else ContactRow.from_row(row)

    def all_contacts(self) -> list[ContactRow]:
        return [
            ContactRow.from_row(row)
            for row in self.connection.execute("SELECT * FROM contacts")
        ]

    def save_contact(self, contact: ContactRow) -> None:
        with self.connection as connection:
            self._write_contact(connection, contact)

    @staticmethod
    def _write_contact(connection: sqlite3.Connection, contact: ContactRow) -> None:
        connection.execute(
            """
            UPDATE contacts SET
                display_name = ?, cursor_timestamp = ?, cursor_local_id = ?,
                first_message_at = ?, last_message_at = ?, total_messages = ?,
                longest_silence_seconds = ?, longest_silence_ended_at = ?,
                latest_night_at = ?, latest_night_offset = ?, max_laugh_run = ?
            WHERE session_id = ?
            """,
            (
                contact.display_name,
                contact.cursor_timestamp,
                contact.cursor_local_id,
                contact.first_message_at,
                contact.last_message_at,
                contact.total_messages,
                contact.longest_silence_seconds,
                contact.longest_silence_ended_at,
                contact.latest_night_at,
                contact.latest_night_offset,
                contact.max_laugh_run,
                contact.session_id,
            ),
        )

    # —— stats_daily ——

    def merge_daily(self, session_id: str, buckets: dict[str, Metrics]) -> None:
        """把新算出来的天桶并入已有行；同一天可以被多轮分析多次追加。"""

        with self.connection as connection:
            self._write_daily(connection, session_id, buckets)

    def commit_batch(
        self, session_id: str, buckets: dict[str, Metrics], contact: ContactRow
    ) -> None:
        """在同一个事务里写完天桶与游标。

        分开提交的话，进程在两者之间挂掉会让游标落后于已经写入的指标，重启后
        那一批消息会被重复计入。
        """

        with self.connection as connection:
            self._write_daily(connection, session_id, buckets)
            self._write_contact(connection, contact)

    @staticmethod
    def _write_daily(
        connection: sqlite3.Connection, session_id: str, buckets: dict[str, Metrics]
    ) -> None:
        for day, addition in buckets.items():
            row = connection.execute(
                "SELECT * FROM stats_daily WHERE session_id = ? AND day = ?",
                (session_id, day),
            ).fetchone()
            total = row_to_metrics(row) if row is not None else Metrics()
            total.merge(addition)
            values = [total.get(name) for name in METRIC_COLUMNS]
            values.append(dump_histogram(total.reply_hist_them))
            values.append(dump_histogram(total.reply_hist_me))
            connection.execute(
                f"""
                INSERT OR REPLACE INTO stats_daily
                    (session_id, day, {",".join(_ROW_COLUMNS)})
                VALUES ({",".join("?" * (len(_ROW_COLUMNS) + 2))})
                """,
                (session_id, day, *values),
            )

    def load_window(self, start_day: str, end_day: str) -> dict[str, Metrics]:
        """[start_day, end_day] 区间内每个联系人的合计指标。"""

        totals: dict[str, Metrics] = {}
        rows = self.connection.execute(
            "SELECT * FROM stats_daily WHERE day >= ? AND day <= ?",
            (start_day, end_day),
        )
        for row in rows:
            session_id = str(row["session_id"])
            bucket = totals.get(session_id)
            if bucket is None:
                bucket = Metrics()
                totals[session_id] = bucket
            bucket.merge(row_to_metrics(row))
        return totals

    def load_days(self, session_id: str) -> list[tuple[str, Metrics]]:
        """单个联系人的全部天桶，按日期升序。走主键索引，不是全表扫描。"""

        return [
            (str(row["day"]), row_to_metrics(row))
            for row in self.connection.execute(
                "SELECT * FROM stats_daily WHERE session_id = ? ORDER BY day ASC",
                (session_id,),
            )
        ]

    # —— scores ——

    def save_scores(self, payloads: list[tuple[str, dict[str, object]]]) -> None:
        """整体替换预计算结果，保证看板永远看到自洽的一轮数据。

        payload 里只有 hash，没有 session_id —— wxid 不出现在任何 API 响应里。
        """

        with self.connection as connection:
            connection.execute("DELETE FROM scores")
            connection.executemany(
                "INSERT INTO scores (session_id, hash, payload) VALUES (?, ?, ?)",
                [
                    (
                        session_id,
                        str(payload["hash"]),
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    )
                    for session_id, payload in payloads
                ],
            )

    def all_scores(self) -> list[dict[str, object]]:
        return [
            json.loads(row["payload"])
            for row in self.connection.execute("SELECT payload FROM scores")
        ]

    def score_by_hash(self, value: str) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT payload FROM scores WHERE hash = ?", (value,)
        ).fetchone()
        return None if row is None else json.loads(row["payload"])
