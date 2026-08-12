"""metrics.db：只存统计结果，不存任何消息原文。

六张表：
- contacts   每个私聊联系人的游标与里程碑
- stats_daily 按天分桶的可加指标（数值列由 constants.METRIC_COLUMNS 生成）
- scores     分析结束时预计算好的看板数据，HTTP 处理器直接吐 JSON
- score_history 关系温度历史：每个联系人每天一个综合分采样点
- llm_depth  可选的大模型画像缓存（session_id → 画像摘要 + 异动解释 +
             话题标签 + 打分时刻 + 打分时累计消息数 + 异动指纹）
- llm_period 时段化大模型评分快照（一个联系人 × 一个自然月 × 一次评分一行）

按天而不是按自然月分桶，是为了让「近 30 天 / 近 90 天 / 近两年」这类滚动窗口是
精确的；自然月视图由 SQL 之外的 Python 聚合从同一批天桶合成，没有第二份真相。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    HISTOGRAM_COLUMNS,
    METRIC_COLUMNS,
    SCHEMA_VERSION,
)
from .metrics import Metrics, day_span, dump_histogram, parse_histogram
from .migrations import apply_migrations


LOG = logging.getLogger("wechat-insights")

_ROW_COLUMNS = METRIC_COLUMNS + HISTOGRAM_COLUMNS

#: get_llm_depth / all_llm_depth 的共享列清单，两处查询保持一致。
_LLM_DEPTH_COLUMNS = (
    "scored_at, total_messages, summary, anomaly_note, anomalies_key, tags"
)

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
    -- 游标所在消息分片；local_id 只在单个分片内唯一，不带分片会漏消息。
    cursor_shard             TEXT NOT NULL DEFAULT '',
    first_message_at         INTEGER,
    last_message_at          INTEGER,
    total_messages           INTEGER NOT NULL DEFAULT 0,
    longest_silence_seconds  INTEGER NOT NULL DEFAULT 0,
    longest_silence_ended_at INTEGER,
    latest_night_at          INTEGER,
    -- 凌晨窗口内距 00:00 的秒数，-1 表示还没有凌晨消息。
    latest_night_offset      INTEGER NOT NULL DEFAULT -1,
    max_laugh_run            INTEGER NOT NULL DEFAULT 0,
    -- LLM 自动判定的关系类型：'' = 尚未判定，'friend'/'family'/'transactional'。
    -- 分类是稳定属性，写过就不再重评；手动改判随时可以覆盖它。
    kind_auto                TEXT NOT NULL DEFAULT '',
    -- 用户手动改判的关系类型：'' = 未设置（沿用自动判定或默认 friend）。
    kind_manual              TEXT NOT NULL DEFAULT '',
    -- 关系温度采样粒度：'' = 每周一个采样点（默认），'day' = 从相识日起逐日细化。
    history_granularity      TEXT NOT NULL DEFAULT '',
    -- 逐日细化已推进到（含）的日键；'' = 还没开始。断点续跑的进度点。
    history_daily_until      TEXT NOT NULL DEFAULT '',
    -- 好感度校准：feedback_pending 是 ''/'up'/'down' 未消化标记；
    -- feedback_pending_at 是标记时刻 epoch 秒的十进制文本；
    -- calibration 是累计校准 JSON，'' = 无校准。
    feedback_pending         TEXT NOT NULL DEFAULT '',
    feedback_pending_at      TEXT NOT NULL DEFAULT '',
    calibration              TEXT NOT NULL DEFAULT ''
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

-- 关系温度历史：每个联系人每天一个综合分采样点（UPSERT 覆盖，一天一点）；
-- dims 是七维分的紧凑 JSON，暂不下发、留着备用。归零的联系人记 0，
-- 归零也是曲线的一部分。
CREATE TABLE IF NOT EXISTS score_history (
    session_id TEXT NOT NULL,
    day        TEXT NOT NULL,
    overall    REAL NOT NULL,
    dims       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, day)
);

-- LLM 画像缓存；total_messages 是打分时联系人的累计消息数，
-- 与 contacts.total_messages 的差值做「新增多少条就重评」的基准。
-- summary/anomaly_note 是同一批调用生成的关系画像与异动解释，
-- anomalies_key 是打分时异动清单的指纹，指纹变了才需要重评重写解释。
-- tags 是同一批调用顺带生成的话题标签（紧凑 JSON；'' 表示没有，[] 合法）。
-- 数值信号（深度/温暖/对等）已由 llm_period 表接管，这里不再有分数列。
CREATE TABLE IF NOT EXISTS llm_depth (
    session_id     TEXT PRIMARY KEY,
    scored_at      INTEGER NOT NULL,
    total_messages INTEGER NOT NULL,
    summary        TEXT NOT NULL DEFAULT '',
    anomaly_note   TEXT NOT NULL DEFAULT '',
    anomalies_key  TEXT NOT NULL DEFAULT '',
    tags           TEXT NOT NULL DEFAULT ''
);

-- 时段化 LLM 分：一个联系人 × 一个自然月 × 一次评分快照一行。
-- period_end = 这次评分覆盖到（含）的日键：当月未收口时是评分当天，收口后是月末。
-- 主键带 period_end，所以同一个月可以有多张快照；回放到某个时刻时每个月只取
-- period_end ≤ 该时刻的最新一张，历史因此既看不到未来、也能被逐字重放出来。
-- 旧快照永不清理：它们就是「那一刻我们知道什么」的唯一记录。
-- model 记着算这一行时用的模型名：换模型后按它精确清理旧模型算出来的行，
-- 不必清空整表（'' = 补列前的老行，模型未知）。
CREATE TABLE IF NOT EXISTS llm_period (
    session_id TEXT NOT NULL,
    period     TEXT NOT NULL,          -- 'YYYY-MM'
    period_end TEXT NOT NULL,          -- 'YYYY-MM-DD'
    depth      REAL NOT NULL,
    warmth     REAL NOT NULL,
    mutuality  REAL NOT NULL,
    scored_at  INTEGER NOT NULL,       -- 运维审计用：这一行是哪一轮算出来的
    model      TEXT NOT NULL DEFAULT '',  -- 算这一行时用的模型名；'' = 补列前的老行（未知）
    PRIMARY KEY (session_id, period, period_end)
);

CREATE INDEX IF NOT EXISTS llm_period_session ON llm_period (session_id, period);
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
    cursor_shard: str
    first_message_at: int | None
    last_message_at: int | None
    total_messages: int
    longest_silence_seconds: int
    longest_silence_ended_at: int | None
    latest_night_at: int | None
    latest_night_offset: int
    max_laugh_run: int
    kind_auto: str
    kind_manual: str
    history_granularity: str = ""
    history_daily_until: str = ""
    feedback_pending: str = ""
    feedback_pending_at: str = ""
    calibration: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ContactRow:
        return cls(**{key: row[key] for key in cls.__slots__})

    def calibration_data(self) -> dict | None:
        """解析累计校准 JSON；无校准或解析失败返回 None。"""

        if not self.calibration:
            return None
        try:
            data = json.loads(self.calibration)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def relation_kind(self) -> str:
        """当前生效的关系类型：手动改判优先，其次自动判定，都空默认 friend。

        合法值是 'friend'/'family'/'transactional'；默认 friend 保证任何旧
        数据按原样处理——升级前的关系在升级后依然是普通朋友。
        """

        return self.kind_manual or self.kind_auto or "friend"

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


@dataclass(frozen=True, slots=True)
class LLMDepthRow:
    """一条 LLM 画像缓存：画像摘要 + 异动解释 + 话题标签 + 指纹。"""

    scored_at: int
    total_messages: int
    summary: str
    anomaly_note: str | None
    anomalies_key: str
    tags: list[str] | None


@dataclass(frozen=True, slots=True)
class PeriodRow:
    """一条时段化 LLM 分快照。"""

    period: str
    period_end: str
    depth: float
    warmth: float
    mutuality: float


@dataclass(slots=True)
class WindowStats:
    """打分窗口内一个联系人的两种合计与活跃度轨迹。

    raw 是无权重的合计（门槛判定用）；weighted 按 weight_of(day) 加权
    （打分用）。active_weight 是「有消息的天的权重之和」，longest_gap_days
    是窗口内相邻活跃天之间的最大间隔天数（不含首尾外侧的空档），两者共同
    支撑恒常维度。
    """

    raw: Metrics
    weighted: Metrics
    active_weight: float
    first_day: str | None
    last_day: str | None
    longest_gap_days: int


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
            self._initialize()

    def _initialize(self) -> None:
        """建表并写入 schema 版本；遇到旧版本或损坏的库直接整体重建。

        metrics.db 只是统计结果，没有值得保留的历史数据，版本不匹配时
        重建比迁移简单可靠：下一轮分析会从零开始回填。
        """
        if self.path.exists() and self._stale_schema():
            LOG.warning(
                "metrics.db 的 schema 版本与当前不兼容，整体重建后重新回填"
            )
            for suffix in ("", "-wal", "-shm"):
                self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)
        with closing(sqlite3.connect(self.path)) as setup, setup:
            setup.executescript(_SCHEMA)
            setup.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        # 补列与去列在独立连接里做：去 score 列之前要 VACUUM INTO 备份，
        # 而 VACUUM 不能在事务里跑。建表必须先完成，补列才有表可补。
        apply_migrations(self.path)

    def _stale_schema(self) -> bool:
        """库文件已存在，但 schema 版本不是当前版本（含读不出版本的损坏库）。"""

        try:
            with closing(sqlite3.connect(self.path)) as connection:
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
        except sqlite3.Error:
            return True
        return row is None or str(row[0]) != str(SCHEMA_VERSION)

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

    def delete_meta(self, key: str) -> None:
        """删掉一个 meta 键（口径版本重置要清 score_history_backfilled 标记）。"""

        with self.connection as connection:
            connection.execute("DELETE FROM meta WHERE key = ?", (key,))

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
                cursor_shard = ?,
                first_message_at = ?, last_message_at = ?, total_messages = ?,
                longest_silence_seconds = ?, longest_silence_ended_at = ?,
                latest_night_at = ?, latest_night_offset = ?, max_laugh_run = ?,
                kind_auto = ?, kind_manual = ?,
                history_granularity = ?, history_daily_until = ?,
                feedback_pending = ?, feedback_pending_at = ?,
                calibration = ?
            WHERE session_id = ?
            """,
            (
                contact.display_name,
                contact.cursor_timestamp,
                contact.cursor_local_id,
                contact.cursor_shard,
                contact.first_message_at,
                contact.last_message_at,
                contact.total_messages,
                contact.longest_silence_seconds,
                contact.longest_silence_ended_at,
                contact.latest_night_at,
                contact.latest_night_offset,
                contact.max_laugh_run,
                contact.kind_auto,
                contact.kind_manual,
                contact.history_granularity,
                contact.history_daily_until,
                contact.feedback_pending,
                contact.feedback_pending_at,
                contact.calibration,
                contact.session_id,
            ),
        )

    def set_contact_kind_manual(self, session_id: str, kind: str) -> None:
        """设置或清除手动关系类型（'' = 清除、回到自动判定）。

        只改这一列，kind_auto 原样保留——清除手动后自动判定结果立即恢复。
        """

        with self.connection as connection:
            connection.execute(
                "UPDATE contacts SET kind_manual = ? WHERE session_id = ?",
                (kind, session_id),
            )

    def set_contact_kind_auto(self, session_id: str, kind: str) -> None:
        """写入自动判定的关系类型；分类只判一次，写入后不再重评。"""

        with self.connection as connection:
            connection.execute(
                "UPDATE contacts SET kind_auto = ? WHERE session_id = ?",
                (kind, session_id),
            )

    def set_history_granularity(self, session_id: str, granularity: str) -> None:
        """设置联系人的关系温度采样粒度：'day' = 逐日细化，'' = 每周（默认）。

        只改这一列；切回每周不重置 history_daily_until，已细化的日点保留。
        """

        with self.connection as connection:
            connection.execute(
                "UPDATE contacts SET history_granularity = ? WHERE session_id = ?",
                (granularity, session_id),
            )

    def set_contact_feedback(self, session_id: str, direction: str, at: str) -> None:
        """写入/清除好感度标记（'' = 清除 pending 与标记时刻）。只改这两列。"""

        with self.connection as connection:
            connection.execute(
                "UPDATE contacts SET feedback_pending = ?, feedback_pending_at = ? "
                "WHERE session_id = ?",
                (direction, at, session_id),
            )

    def set_contact_calibration(self, session_id: str, payload: str) -> None:
        """写入/清除累计校准 JSON（'' = 清除校准）。只改这一列。"""

        with self.connection as connection:
            connection.execute(
                "UPDATE contacts SET calibration = ? WHERE session_id = ?",
                (payload, session_id),
            )

    def contacts_needing_daily_refine(self, limit_day: str) -> list[ContactRow]:
        """待逐日细化的联系人：粒度是每日、进度还没到 limit_day、且知道相识日。

        history_daily_until 是断点续跑的进度：空串（还没开始）和小于
        limit_day 的日键都算未完成，容器重启后从这里继续，不重头再来。
        limit_day 由 analyzer.refine_limit_day 给出——两个调用方共用同一个
        网格终点，不许各算一遍。
        """

        return [
            ContactRow.from_row(row)
            for row in self.connection.execute(
                "SELECT * FROM contacts "
                "WHERE history_granularity = 'day' AND history_daily_until < ? "
                "AND first_message_at IS NOT NULL",
                (limit_day,),
            )
        ]

    def mark_daily_refined(self, session_ids: list[str], day: str) -> None:
        """批量把一批联系人的逐日细化进度推进到 day（断点续跑的进度点）。

        每完成一天调一次：即使这天一个采样点都没写入（如零消息的家人），
        只要重算跑过了，进度就推进。
        """

        with self.connection as connection:
            connection.executemany(
                "UPDATE contacts SET history_daily_until = ? WHERE session_id = ?",
                [(day, session_id) for session_id in session_ids],
            )

    def rewind_daily_refine_progress(self, day: str = "") -> int:
        """把跑过 day 的逐日细化进度退回到 day，返回受影响行数。

        打分口径变了以后，旧口径算出来的日点必须整段重算，否则同一条曲线上
        周网格点是新口径、日点是旧口径，出现锯齿。day=""（默认）等于退回
        相识日、即旧的「清零」语义；进度本来就没到 day 的联系人不动——它们
        还要正常往前跑到那儿。
        """

        with self.connection as connection:
            cursor = connection.execute(
                "UPDATE contacts SET history_daily_until = ? "
                "WHERE history_granularity = 'day' AND history_daily_until > ?",
                (day, day),
            )
            return cursor.rowcount

    # —— llm_depth ——

    @staticmethod
    def _llm_depth_row(row: sqlite3.Row) -> LLMDepthRow:
        """从一行查询结果构造 LLMDepthRow。

        空串的 anomaly_note / tags 归一成 None；tags 的 '[]' 是合法值
        （模型明确给了空数组），要还原成空列表而不是 None——两者含义不同：
        None 表示「老缓存行没有 tags」，[] 表示「有 tags 字段但模型没给」。
        """

        tags = None
        raw_tags = row["tags"] or ""
        if raw_tags:
            try:
                parsed = json.loads(raw_tags)
            except ValueError:
                parsed = None
            if isinstance(parsed, list) and all(
                isinstance(item, str) for item in parsed
            ):
                tags = parsed
        return LLMDepthRow(
            scored_at=row["scored_at"],
            total_messages=row["total_messages"],
            summary=row["summary"] or "",
            anomaly_note=row["anomaly_note"] or None,
            anomalies_key=row["anomalies_key"] or "",
            tags=tags,
        )

    def get_llm_depth(self, session_id: str) -> LLMDepthRow | None:
        row = self.connection.execute(
            f"SELECT {_LLM_DEPTH_COLUMNS} FROM llm_depth WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return None if row is None else self._llm_depth_row(row)

    def set_llm_depth(
        self,
        session_id: str,
        scored_at: int,
        total_messages: int,
        summary: str = "",
        anomaly_note: str | None = None,
        anomalies_key: str = "",
        tags: list[str] | None = None,
    ) -> None:
        """写入或覆盖一条 LLM 画像缓存（UPSERT）。

        tags 为 None 存 ''（读回是 None，触发重评补齐），list 存紧凑
        JSON——空数组 [] 也是合法值，照存。
        """

        tags_json = (
            ""
            if tags is None
            else json.dumps(tags, ensure_ascii=False, separators=(",", ":"))
        )
        with self.connection as connection:
            connection.execute(
                "INSERT INTO llm_depth "
                "(session_id, scored_at, total_messages, summary, "
                "anomaly_note, anomalies_key, tags) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "scored_at = excluded.scored_at, "
                "total_messages = excluded.total_messages, "
                "summary = excluded.summary, anomaly_note = excluded.anomaly_note, "
                "anomalies_key = excluded.anomalies_key, tags = excluded.tags",
                (
                    session_id,
                    scored_at,
                    total_messages,
                    summary,
                    anomaly_note or "",
                    anomalies_key,
                    tags_json,
                ),
            )

    def all_llm_depth(self) -> dict[str, LLMDepthRow]:
        """全部联系人的 LLM 画像缓存行，供打分注入与画像/异动解释用。"""

        return {
            str(row["session_id"]): self._llm_depth_row(row)
            for row in self.connection.execute(
                f"SELECT session_id, {_LLM_DEPTH_COLUMNS} FROM llm_depth"
            )
        }

    # —— llm_period ——

    def monthly_text_counts(self) -> dict[str, dict[str, int]]:
        """{session_id: {'YYYY-MM': 该月双方文字消息条数}}，时段候选的门槛依据。

        一次全表扫描（生产 18704 行，约几十毫秒），每轮只调一次。
        """

        counts: dict[str, dict[str, int]] = {}
        for row in self.connection.execute(
            "SELECT session_id, substr(day, 1, 7) AS period, "
            "SUM(kind_text_them + kind_text_me) AS texts "
            "FROM stats_daily GROUP BY session_id, period"
        ):
            counts.setdefault(str(row["session_id"]), {})[
                str(row["period"])
            ] = int(row["texts"] or 0)
        return counts

    def period_coverage(self) -> dict[tuple[str, str], str]:
        """{(session_id, period): 该时段已评过的最大 period_end}，判定是否需要重评。"""

        return {
            (str(row["session_id"]), str(row["period"])): str(row["max_end"])
            for row in self.connection.execute(
                "SELECT session_id, period, MAX(period_end) AS max_end "
                "FROM llm_period GROUP BY session_id, period"
            )
        }

    def set_llm_period(
        self,
        session_id: str,
        period: str,
        period_end: str,
        depth: float,
        warmth: float,
        mutuality: float,
        scored_at: int,
        model: str = "",
    ) -> None:
        """写入一条时段分快照（UPSERT，同一 period_end 重跑就覆盖）。

        model 记着算这一行时用的 INSIGHTS_LLM_MODEL，换模型后按它精确清理；
        '' 表示未记录（补列前写下的老行）。
        """

        with self.connection as connection:
            connection.execute(
                "INSERT INTO llm_period "
                "(session_id, period, period_end, depth, warmth, mutuality, "
                "scored_at, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, period, period_end) DO UPDATE SET "
                "depth = excluded.depth, warmth = excluded.warmth, "
                "mutuality = excluded.mutuality, scored_at = excluded.scored_at, "
                "model = excluded.model",
                (
                    session_id,
                    period,
                    period_end,
                    depth,
                    warmth,
                    mutuality,
                    scored_at,
                    model,
                ),
            )

    def all_llm_periods(self) -> dict[str, list[PeriodRow]]:
        """全部时段分，按 session_id 分组、组内按 (period, period_end) 升序。"""

        grouped: dict[str, list[PeriodRow]] = {}
        for row in self.connection.execute(
            "SELECT session_id, period, period_end, depth, warmth, mutuality "
            "FROM llm_period ORDER BY session_id, period, period_end"
        ):
            grouped.setdefault(str(row["session_id"]), []).append(
                PeriodRow(
                    period=str(row["period"]),
                    period_end=str(row["period_end"]),
                    depth=float(row["depth"]),
                    warmth=float(row["warmth"]),
                    mutuality=float(row["mutuality"]),
                )
            )
        return grouped

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

    def load_window_stats(
        self,
        start_day: str,
        end_day: str,
        weight_of: Callable[[str], float],
    ) -> dict[str, WindowStats]:
        """打分窗口专用加载：一次扫描同时产出加权与未加权合计。

        weight_of(day) 返回该日键的衰减权重；行按 day 升序返回，窗口内
        相邻活跃天之间的最大间隔（不含首尾外侧空档）在扫描过程中就地累计。
        只有消息数为 0 的天桶行理论上不存在，但防御性地跳过全零行的活跃天
        判定（以 messages_total() > 0 为准）。
        """

        totals: dict[str, WindowStats] = {}
        rows = self.connection.execute(
            "SELECT * FROM stats_daily WHERE day >= ? AND day <= ? ORDER BY day ASC",
            (start_day, end_day),
        )
        for row in rows:
            session_id = str(row["session_id"])
            day = str(row["day"])
            entry = totals.get(session_id)
            if entry is None:
                entry = WindowStats(Metrics(), Metrics(), 0.0, None, None, 0)
                totals[session_id] = entry
            metrics = row_to_metrics(row)
            entry.raw.merge(metrics)
            weight = weight_of(day)
            entry.weighted.merge_weighted(metrics, weight)
            if metrics.messages_total() <= 0:
                continue
            entry.active_weight += weight
            if entry.last_day is not None:
                gap = day_span(entry.last_day, day) - 1
                if gap > entry.longest_gap_days:
                    entry.longest_gap_days = gap
            if entry.first_day is None:
                entry.first_day = day
            entry.last_day = day
        return totals

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

    def earliest_stats_day(self) -> str | None:
        """stats_daily 里最早的日键；一张天桶都没有时返回 None。

        关系温度全史回放的网格起点用。走 MIN(day)，不需要扫描全表。
        """

        row = self.connection.execute("SELECT MIN(day) FROM stats_daily").fetchone()
        return None if row is None or row[0] is None else str(row[0])

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

    def update_score_payload(
        self, session_id: str, payload: dict[str, object]
    ) -> None:
        """改写单个联系人的预计算 payload（UPSERT），不动其他行。

        手动改判等小修用：save_scores 是整体替换，为改一个字段重写全表
        不划算。
        """

        with self.connection as connection:
            connection.execute(
                "INSERT INTO scores (session_id, hash, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET payload = excluded.payload",
                (
                    session_id,
                    str(payload["hash"]),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    # —— score_history ——

    def record_score_history(
        self, day: str, rows: list[tuple[str, float, str]]
    ) -> None:
        """记录一批联系人当天的综合分采样（UPSERT，一次事务）。

        同一 (session_id, day) 多次记录直接覆盖——一天只保留最后一次分析的
        分数，曲线是「每天一个点」而不是「每轮一个点」。
        """

        with self.connection as connection:
            connection.executemany(
                "INSERT INTO score_history (session_id, day, overall, dims) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id, day) DO UPDATE SET "
                "overall = excluded.overall, dims = excluded.dims",
                [
                    (session_id, day, float(overall), dims)
                    for session_id, overall, dims in rows
                ],
            )

    def load_score_history(self, session_id: str) -> list[tuple[str, float, str]]:
        """单个联系人的全部历史采样点，按日期升序。走主键索引。"""

        return [
            (str(row["day"]), float(row["overall"]), str(row["dims"]))
            for row in self.connection.execute(
                "SELECT day, overall, dims FROM score_history "
                "WHERE session_id = ? ORDER BY day ASC",
                (session_id,),
            )
        ]

    def prune_score_history_before(self, cutoffs: dict[str, str]) -> int:
        """一次性迁移：删掉相识日之前的温度采样点，返回删除的总行数。

        cutoffs = {session_id: 相识日键}；对每个联系人删除 day < 相识日的
        score_history 行。所有删除在同一个事务里完成。
        """

        deleted = 0
        with self.connection as connection:
            for session_id, cutoff_day in cutoffs.items():
                cursor = connection.execute(
                    "DELETE FROM score_history WHERE session_id = ? AND day < ?",
                    (session_id, cutoff_day),
                )
                deleted += cursor.rowcount
        return deleted
