"""metrics.db 的幂等形状迁移：补列，以及去掉被 llm_period 取代的 llm_depth.score。

自己开连接、不复用 MetricsStore 的连接池：去 score 列之前要 VACUUM INTO 备份，
而 VACUUM 不能在事务里跑，备份必须在独立连接、独立事务边界上完成。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path


LOG = logging.getLogger("wechat-insights")

#: CREATE TABLE 之外的幂等补列清单：(表名, 列名...)。全部 TEXT NOT NULL DEFAULT ''，
#: 旧行读回空串 = 「这一项当年没记」。多张表共用一个循环，加表不加分支。
_EXTRA_COLUMNS = (
    # llm_depth 是 3a15872 新增、从未上过生产，这个幂等迁移只为本地
    # 已建库的开发/测试环境兜底：逐个补列，列已存在就跳过。不值得为
    # 它 bump SCHEMA_VERSION 触发全量重建回填。
    ("llm_depth", ("summary", "anomaly_note", "anomalies_key", "tags")),
    # contacts 存着游标与里程碑，同样不能重建：幂等补列，旧行直接
    # 读回空串（未判定的联系人按默认 friend、采样粒度按每周处理），
    # 升级后一切照旧。
    (
        "contacts",
        (
            "kind_auto",
            "kind_manual",
            "history_granularity",
            "history_daily_until",
        ),
    ),
)

#: llm_depth 去 score 列的表重建脚本（原 storage._initialize 内的 executescript 原文）。
_REBUILD_LLM_DEPTH = """
CREATE TABLE llm_depth_new (
    session_id     TEXT PRIMARY KEY,
    scored_at      INTEGER NOT NULL,
    total_messages INTEGER NOT NULL,
    summary        TEXT NOT NULL DEFAULT '',
    anomaly_note   TEXT NOT NULL DEFAULT '',
    anomalies_key  TEXT NOT NULL DEFAULT '',
    tags           TEXT NOT NULL DEFAULT ''
);
INSERT INTO llm_depth_new
    (session_id, scored_at, total_messages, summary,
     anomaly_note, anomalies_key, tags)
    SELECT session_id, scored_at, total_messages, summary,
           anomaly_note, anomalies_key, tags
    FROM llm_depth;
DROP TABLE llm_depth;
ALTER TABLE llm_depth_new RENAME TO llm_depth;
"""


def apply_migrations(path: Path) -> None:
    """把库幂等地调整成当前形状。已是新形状时全部命中 no-op。

    顺序不可换：补列必须先跑完，重建 llm_depth 的 INSERT SELECT 才点得到
    summary/anomaly_note/anomalies_key/tags 四列。
    """

    with closing(sqlite3.connect(path)) as connection, connection:
        for table, columns in _EXTRA_COLUMNS:
            for column in columns:
                try:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} "
                        "TEXT NOT NULL DEFAULT ''"
                    )
                except sqlite3.OperationalError:
                    pass  # 列已存在（新形状的库）
        legacy = "score" in {
            row[1] for row in connection.execute("PRAGMA table_info(llm_depth)")
        }
    if not legacy:
        return
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(_REBUILD_LLM_DEPTH)
