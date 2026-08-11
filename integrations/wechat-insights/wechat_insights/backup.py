"""破坏性操作前的整库自动备份（VACUUM INTO），只在不可逆时刻调用。

备份拿不到就不动数据；本模块绝不抛异常、绝不阻断进程——降级由调用方决定。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from .constants import BACKUP_KEEP


LOG = logging.getLogger("wechat-insights")

#: 触发原因的固定 slug（进文件名，必须文件名安全）。
REASON_FORMULA_RESET = "formula-reset"
REASON_LLM_DEPTH_REBUILD = "llm-depth-rebuild"


def backup_path(db_path: Path, reason: str, day: str) -> Path:
    """备份文件路径：<库名>-backup-<原因>-<日期>.db，与库同目录。

    用 db_path.stem 而不是写死 'metrics'：INSIGHTS_DB_PATH 可配，测试也用临时名。
    """

    return db_path.parent / f"{db_path.stem}-backup-{reason}-{day}.db"


def backup_database(db_path: Path, reason: str) -> Path | None:
    """VACUUM INTO 一份完整快照，成功返回备份路径，拿不到备份返回 None。

    同一天同一原因已有备份就直接复用那一份（不覆盖）：VACUUM INTO 拒绝已存在的
    目标，而重试破坏性操作时旧备份记的正是「动手之前」的状态，覆盖它等于把要保护
    的东西删掉。备份成功后按 BACKUP_KEEP 清理更旧的几份。任何失败（磁盘满、权限、
    库损坏）只记 ERROR 并返回 None，绝不向外抛——调用方负责降级。
    """

    target = backup_path(db_path, reason, date.today().isoformat())
    if target.exists():
        LOG.info("复用当天已有的备份：%s", target.name)
        return target
    try:
        # 独立连接、不进任何事务上下文（VACUUM 不能在事务里跑）。WAL 里
        # 已提交的内容照样进快照。
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute("VACUUM INTO ?", (target.as_posix(),))
    except (sqlite3.Error, OSError) as error:
        LOG.error(
            "破坏性操作前的备份失败（%s），已放弃备份：%s",
            type(error).__name__,
            target.name,
        )
        return None
    # WARNING 级：这是「刚刚做了件大事」，运维要能在日志里一眼看到。
    LOG.warning("破坏性操作前已备份：%s（%d 字节）", target.name, target.stat().st_size)
    prune_backups(db_path)
    return target


def prune_backups(db_path: Path) -> int:
    """只保留最近 BACKUP_KEEP 份备份，删掉更旧的，返回删除个数。

    按 mtime 降序保留（文件名中间夹着原因段，字典序排不出新旧）；
    glob 严格限定 f"{db_path.stem}-backup-*.db"，碰不到 metrics.db 本身。
    单份删除失败只记 WARNING 继续——清理失败不该让备份变成失败。
    """

    backups = sorted(
        db_path.parent.glob(f"{db_path.stem}-backup-*.db"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    deleted = 0
    for old in backups[BACKUP_KEEP:]:
        try:
            old.unlink()
            deleted += 1
        except OSError:
            LOG.warning("删除过期备份失败，跳过：%s", old.name)
    if deleted:
        LOG.info("清理过期备份：删除 %d 份", deleted)
    return deleted
