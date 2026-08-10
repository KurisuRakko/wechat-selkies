"""自动备份：VACUUM INTO 命名快照、同日同因复用、保留策略与失败降级。

全部用 tempdir，不碰真实 /data；每个用例自己建一个带一张表一行数据的 sqlite 库。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from wechat_insights.backup import (
    REASON_FORMULA_RESET,
    REASON_LLM_DEPTH_REBUILD,
    backup_database,
    backup_path,
    prune_backups,
)


def _seed_db(path: Path) -> None:
    """建一个带一张表一行数据的库，供「备份能读回那一行」断言用。"""

    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO items (name) VALUES ('哨兵行')")


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.db_path = self.directory / "metrics.db"
        _seed_db(self.db_path)

    def test_backup_writes_a_named_snapshot_next_to_the_database(self) -> None:
        size_before = self.db_path.stat().st_size
        target = backup_database(self.db_path, REASON_FORMULA_RESET)
        expected = backup_path(
            self.db_path, REASON_FORMULA_RESET, date.today().isoformat()
        )
        self.assertEqual(target, expected)
        self.assertTrue(expected.exists())
        with sqlite3.connect(expected) as connection:
            self.assertEqual(
                connection.execute("SELECT name FROM items").fetchone()[0], "哨兵行"
            )
        # metrics.db 本身未被改动。
        self.assertEqual(self.db_path.stat().st_size, size_before)

    def test_same_day_and_reason_reuses_the_existing_backup(self) -> None:
        expected = backup_path(
            self.db_path, REASON_FORMULA_RESET, date.today().isoformat()
        )
        expected.write_bytes(b"SENTINEL")
        target = backup_database(self.db_path, REASON_FORMULA_RESET)
        self.assertEqual(target, expected)
        # 文件内容逐字不变：旧备份是「动手之前」的状态，不许覆盖。
        self.assertEqual(expected.read_bytes(), b"SENTINEL")

    def test_different_reasons_get_separate_files(self) -> None:
        first = backup_database(self.db_path, REASON_FORMULA_RESET)
        second = backup_database(self.db_path, REASON_LLM_DEPTH_REBUILD)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.name, f"metrics-backup-formula-reset-{date.today().isoformat()}.db")
        self.assertEqual(second.name, f"metrics-backup-llm-depth-rebuild-{date.today().isoformat()}.db")
        self.assertTrue(first.exists() and second.exists())

    def test_backup_failure_returns_none(self) -> None:
        # patch 到一个不存在的子目录：VACUUM INTO 报 unable to open database file。
        # 不用 chmod 0o500 造失败——以 root 跑测试时权限位无效，那种写法会在
        # 容器里假绿。
        missing = self.directory / "no-such-dir" / "x.db"
        with patch("wechat_insights.backup.backup_path", return_value=missing):
            with self.assertLogs("wechat-insights", level="ERROR") as logs:
                target = backup_database(self.db_path, REASON_FORMULA_RESET)
        self.assertIsNone(target)
        self.assertTrue(any("备份失败" in line for line in logs.output))

    def test_prune_keeps_the_newest_backups_only(self) -> None:
        names = [
            f"metrics-backup-formula-reset-{date.today().isoformat()}.db",
            "metrics-backup-formula-reset-2026-08-01.db",
            "metrics-backup-llm-depth-rebuild-2026-07-01.db",
            "metrics-backup-formula-reset-2026-06-01.db",
        ]
        for index, name in enumerate(names):
            path = self.directory / name
            path.write_bytes(b"x")
            os.utime(path, (index + 1_000_000, index + 1_000_000))
        with patch("wechat_insights.backup.BACKUP_KEEP", 2):
            deleted = prune_backups(self.db_path)
        self.assertEqual(deleted, 2)
        remaining = sorted(p.name for p in self.directory.glob("metrics-backup-*.db"))
        # 保留的正是 mtime 最新的那两份。
        self.assertEqual(remaining, sorted(names[-2:]))

    def test_prune_never_touches_the_database_or_unrelated_files(self) -> None:
        backup = self.directory / f"metrics-backup-formula-reset-{date.today().isoformat()}.db"
        backup.write_bytes(b"x")
        for name in ("metrics.db-wal", "other.db", "notes.txt"):
            (self.directory / name).write_bytes(b"x")
        with patch("wechat_insights.backup.BACKUP_KEEP", 1):
            self.assertEqual(prune_backups(self.db_path), 0)
        for name in ("metrics.db", "metrics.db-wal", "other.db", "notes.txt", backup.name):
            self.assertTrue((self.directory / name).exists(), name)

    def test_backup_of_a_wal_database_includes_committed_rows(self) -> None:
        # 源库切 WAL、连接保持打开、不显式 checkpoint：已提交的行只落在
        # -wal 文件里，备份用独立连接 + VACUUM INTO 仍读得到——这是
        # 「备份是完整的」这条主张的证据。
        connection = sqlite3.connect(self.db_path)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO items (name) VALUES ('WAL 行')")
        connection.commit()

        target = backup_database(self.db_path, REASON_FORMULA_RESET)
        self.assertIsNotNone(target)
        with sqlite3.connect(target) as old:
            names = {row[0] for row in old.execute("SELECT name FROM items")}
        self.assertEqual(names, {"哨兵行", "WAL 行"})


if __name__ == "__main__":
    unittest.main()
