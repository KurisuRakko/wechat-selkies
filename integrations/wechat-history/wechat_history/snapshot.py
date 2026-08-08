"""Consistent, read-only encrypted snapshots and ephemeral decrypted cache."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    ACCOUNT,
    CACHE_ROOT,
    KEYS_FILE,
    KEY_SCHEMA_VERSION,
    MAX_CACHE_BYTES,
    safe_relative_database,
)
from .crypto import PAGE_SIZE, apply_wal, decrypt_database, verify_encryption_key
from .errors import fail


@dataclass(frozen=True, slots=True)
class FileState:
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class SourceState:
    database: FileState
    wal: FileState | None


def _file_state(path: Path) -> FileState:
    value = path.stat()
    return FileState(value.st_ino, value.st_size, value.st_mtime_ns)


def _source_state(database: Path, wal: Path) -> SourceState:
    return SourceState(
        database=_file_state(database),
        wal=_file_state(wal) if wal.exists() else None,
    )


class KeyStore:
    """Strict loader for the one account's secret key document."""

    def __init__(self, path: Path = KEYS_FILE):
        self.path = path
        self.metadata: dict = {}
        self.entries: dict[str, dict] = {}
        self.reload()

    def reload(self) -> None:
        try:
            key_stat = self.path.stat()
        except FileNotFoundError as exc:
            raise fail(
                "KEY_NOT_FOUND",
                "密钥文件不存在；请先登录目标旧账户并运行一次密钥扫描",
            ) from exc
        except OSError as exc:
            raise fail("KEY_PERMISSIONS", "密钥文件不可读") from exc
        if not stat.S_ISREG(key_stat.st_mode):
            raise fail("KEY_INVALID", "密钥路径不是普通文件")
        if os.name == "posix":
            mode = stat.S_IMODE(key_stat.st_mode)
            if mode & 0o077:
                raise fail("KEY_PERMISSIONS", "密钥文件权限必须为 0600")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise fail("KEY_INVALID", "密钥文件损坏或不可读") from exc
        metadata = document.get("_meta")
        if not isinstance(metadata, dict):
            raise fail("KEY_INVALID", "密钥文件缺少安全元数据")
        if metadata.get("schema_version") != KEY_SCHEMA_VERSION:
            raise fail("KEY_INVALID", "密钥文件版本不受支持")
        if metadata.get("target_account_dir") != ACCOUNT.account_dir:
            raise fail("ACCOUNT_MISMATCH", "密钥不属于允许的旧账户")

        entries: dict[str, dict] = {}
        for raw_path, info in document.items():
            if raw_path.startswith("_"):
                continue
            try:
                relative_path = safe_relative_database(raw_path)
            except ValueError as exc:
                raise fail("KEY_INVALID", "密钥文件包含越界数据库条目") from exc
            if not isinstance(info, dict):
                raise fail("KEY_INVALID", "数据库密钥条目格式无效")
            key_text = info.get("enc_key", "")
            salt_text = info.get("salt", "")
            if not re_full_hex(key_text, 64) or not re_full_hex(salt_text, 32):
                raise fail("KEY_INVALID", "数据库密钥条目长度无效")
            entries[relative_path] = dict(info)
        if "contact/contact.db" not in entries or "session/session.db" not in entries:
            raise fail("KEY_INCOMPLETE", "密钥文件缺少联系人或会话数据库")
        if not any(path.startswith("message/message_") for path in entries):
            raise fail("KEY_INCOMPLETE", "密钥文件缺少消息数据库")
        self.metadata = metadata
        self.entries = entries

    def encryption_key(self, relative_path: str) -> bytes:
        normalized = safe_relative_database(relative_path)
        info = self.entries.get(normalized)
        if not info:
            raise fail("KEY_NOT_FOUND", "该目标数据库没有已验证密钥")
        return bytes.fromhex(info["enc_key"])

    def database_paths(self, prefix: str = "") -> list[str]:
        return sorted(path for path in self.entries if path.startswith(prefix))


def re_full_hex(value: object, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    try:
        bytes.fromhex(value)
        return True
    except ValueError:
        return False


class SnapshotCache:
    """Decrypt source DBs only into a private, per-process tmpfs directory."""

    def __init__(
        self,
        keys: KeyStore,
        source_dir: Path | None = None,
        cache_root: Path = CACHE_ROOT,
        max_bytes: int = MAX_CACHE_BYTES,
    ):
        # 默认值延迟到运行期解析，避免 import 期就要求身份已配置。
        if source_dir is None:
            source_dir = ACCOUNT.db_dir
        if source_dir.parent.name != ACCOUNT.account_dir:
            raise fail("ACCOUNT_MISMATCH", "读取器被指向了非目标账户")
        self.keys = keys
        self.source_dir = source_dir
        self.max_bytes = max_bytes
        cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            cache_root.chmod(0o700)
        self.process_dir = Path(
            tempfile.mkdtemp(prefix=f"mcp-{os.getpid()}-", dir=cache_root)
        )
        if os.name == "posix":
            self.process_dir.chmod(0o700)
        self._cache: dict[str, tuple[SourceState, Path]] = {}
        self._closed = False
        atexit.register(self.close)

    def _paths(self, relative_path: str) -> tuple[Path, Path]:
        normalized = safe_relative_database(relative_path)
        database = self.source_dir.joinpath(*normalized.split("/"))
        try:
            database.resolve(strict=True).relative_to(self.source_dir.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise fail("DB_NOT_FOUND", "目标数据库不存在或路径越界") from exc
        if not database.is_file() or database.is_symlink():
            raise fail("DB_NOT_FOUND", "目标数据库不存在")
        return database, Path(f"{database}-wal")

    def _cache_path(self, relative_path: str) -> Path:
        digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:20]
        return self.process_dir / f"{digest}.db"

    def _current_size(self) -> int:
        total = 0
        for path in self.process_dir.iterdir():
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    def _copy_consistent(
        self, database: Path, wal: Path, stem: str
    ) -> tuple[Path, Path | None, SourceState]:
        encrypted_database = self.process_dir / f".{stem}.encrypted"
        encrypted_wal = self.process_dir / f".{stem}.wal"
        for attempt in range(3):
            encrypted_database.unlink(missing_ok=True)
            encrypted_wal.unlink(missing_ok=True)
            try:
                before = _source_state(database, wal)
                required = before.database.size + (before.wal.size if before.wal else 0)
                if self._current_size() + (required * 2) > self.max_bytes:
                    raise fail(
                        "CACHE_LIMIT", f"临时解密缓存超过 {self.max_bytes} 字节限制"
                    )
                shutil.copyfile(database, encrypted_database)
                if before.wal is not None:
                    shutil.copyfile(wal, encrypted_wal)
                after = _source_state(database, wal)
                if before == after:
                    return (
                        encrypted_database,
                        encrypted_wal if before.wal is not None else None,
                        after,
                    )
            except FileNotFoundError:
                pass
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
        encrypted_database.unlink(missing_ok=True)
        encrypted_wal.unlink(missing_ok=True)
        raise fail("SOURCE_BUSY", "微信正在写入数据库，无法取得一致快照")

    def get(self, relative_path: str) -> Path:
        if self._closed:
            raise fail("CACHE_CLOSED", "临时数据库缓存已经关闭")
        normalized = safe_relative_database(relative_path)
        database, wal = self._paths(normalized)
        current = _source_state(database, wal)
        cached = self._cache.get(normalized)
        if cached and cached[0] == current and cached[1].is_file():
            return cached[1]

        key = self.keys.encryption_key(normalized)
        with database.open("rb") as source:
            first_page = source.read(PAGE_SIZE)
        if not verify_encryption_key(key, first_page):
            raise fail("KEY_STALE", "保存的密钥已失效；请显式重新扫描")

        final_path = self._cache_path(normalized)
        new_path = final_path.with_suffix(".new")
        encrypted_database: Path | None = None
        encrypted_wal: Path | None = None
        try:
            encrypted_database, encrypted_wal, captured = self._copy_consistent(
                database, wal, final_path.stem
            )
            with encrypted_database.open("rb") as source:
                database_salt = source.read(16)
            decrypt_database(encrypted_database, new_path, key)
            if encrypted_wal is not None:
                apply_wal(encrypted_wal, new_path, key, database_salt)
            self._validate_sqlite(new_path)
            os.replace(new_path, final_path)
            if os.name == "posix":
                final_path.chmod(0o600)
            self._cache[normalized] = (captured, final_path)
            return final_path
        finally:
            if encrypted_database is not None:
                encrypted_database.unlink(missing_ok=True)
            if encrypted_wal is not None:
                encrypted_wal.unlink(missing_ok=True)
            new_path.unlink(missing_ok=True)

    @staticmethod
    def _validate_sqlite(path: Path) -> None:
        try:
            uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
            with sqlite3.connect(uri, uri=True) as connection:
                result = connection.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise fail("INVALID_DB", "解密数据库完整性检查失败")
        except sqlite3.Error as exc:
            raise fail("INVALID_DB", "解密结果不是可读取的 SQLite 数据库") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        shutil.rmtree(self.process_dir, ignore_errors=True)
        self._cache.clear()
