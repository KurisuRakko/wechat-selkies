"""Security-sensitive constants for the single supported account.

The account identity is never hardcoded: it is injected through environment
variables and resolved lazily on first use. Importing this package must never
fail on a missing identity, so that tooling that merely imports it (tests,
analyzers, service startup) gets an actionable HistoryError at first real read
instead of a confusing ImportError at import time.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import cast

from .errors import fail

#: 目标账户目录名（SOURCE_ROOT 下的单个目录），例如 wxid_xxxx_0000。
ACCOUNT_DIR_ENV = "WECHAT_HISTORY_ACCOUNT_DIR"
#: 账户自身的 wxid，用于排除自己并识别发出的消息。
USERNAME_ENV = "WECHAT_HISTORY_USERNAME"
#: 自身资料（昵称/备注/别名）必须包含的逗号分隔身份标记。
IDENTITY_TOKENS_ENV = "WECHAT_HISTORY_IDENTITY_TOKENS"

UPSTREAM_COMMIT = "a3789232d4f79bf0b30634d9dadbce71e4acd601"

KEY_SCHEMA_VERSION = 1
MAX_REPLY_CHARS = 4000
MAX_SESSION_LIMIT = 200
MAX_MESSAGE_LIMIT = 200
MAX_SEARCH_LIMIT = 100

SOURCE_ROOT = Path(
    os.environ.get("WECHAT_HISTORY_SOURCE_ROOT", "/history-source/xwechat_files")
)
KEYS_FILE = Path(
    os.environ.get("WECHAT_HISTORY_KEYS_FILE", "/run/wechat-history/keys.json")
)
CACHE_ROOT = Path(
    os.environ.get("WECHAT_HISTORY_CACHE_ROOT", "/run/wechat-history-cache")
)

def _cache_ceiling() -> int:
    """解密明文缓存上限（字节），可用环境变量覆盖，越界或非法值回退到默认值。

    这个上限必须 ≤ CACHE_ROOT 那个 tmpfs 的 size=，否则缓存写满前就先撞上 tmpfs
    的内存上限，得到的是 ENOSPC 而不是干净的 CACHE_LIMIT。默认 512 MiB 与文档里的
    tmpfs 默认大小配对；一次读取涉及的分片越多，需要的上限越高，调整时两处一起改
    （实测九个消息分片全量铺开需要 960 MiB 上限 + 1 GiB tmpfs）。
    """

    default = 512 * 1024 * 1024
    try:
        value = int(os.environ["WECHAT_HISTORY_MAX_CACHE_BYTES"])
    except (KeyError, TypeError, ValueError):
        return default
    # 越界值一律忽略：过小会让任何读取都失败，过大等于取消这道保护。
    if 64 * 1024 * 1024 <= value <= 4 * 1024 * 1024 * 1024:
        return value
    return default


MAX_CACHE_BYTES = _cache_ceiling()


class AccountIdentity:
    """目标账户身份（单账户白名单），属性首次访问时从环境变量解析并缓存。

    身份缺失或非法时抛 HistoryError（IDENTITY_UNCONFIGURED），绝不会回退到
    默认账户、自动发现目录或降级为读取全部 —— 机器上可能同时存在其他微信
    账户目录，白名单失败必须拒绝服务，而不是悄悄读错人。
    """

    def __init__(self) -> None:
        self._values: dict[str, object] | None = None

    def _resolve(self) -> dict[str, object]:
        if self._values is None:
            account_dir = os.environ.get(ACCOUNT_DIR_ENV, "").strip()
            username = os.environ.get(USERNAME_ENV, "").strip()
            identity_tokens = tuple(
                token.strip()
                for token in os.environ.get(IDENTITY_TOKENS_ENV, "").split(",")
                if token.strip()
            )
            missing = [
                env
                for env, value in (
                    (ACCOUNT_DIR_ENV, account_dir),
                    (USERNAME_ENV, username),
                    (IDENTITY_TOKENS_ENV, identity_tokens),
                )
                if not value
            ]
            if missing:
                raise fail(
                    "IDENTITY_UNCONFIGURED",
                    "未配置目标账户身份，请设置 "
                    + "、".join(missing)
                    + "（见 docs/wechat-history.md）",
                )
            # 白名单必须是一个目录名，不能变成任意路径。
            if (
                account_dir in (".", "..")
                or "/" in account_dir
                or "\\" in account_dir
            ):
                raise fail(
                    "IDENTITY_UNCONFIGURED",
                    f"{ACCOUNT_DIR_ENV} 必须是单个目录名，不能包含路径分隔符",
                )
            self._values = {
                "account_dir": account_dir,
                "username": username,
                # 对外只暴露截断后的掩码，避免泄露完整 wxid。
                "account_mask": f"{account_dir[:9]}…{account_dir[-4:]}",
                "identity_tokens": identity_tokens,
            }
        return self._values

    @property
    def account_dir(self) -> str:
        return str(self._resolve()["account_dir"])

    @property
    def username(self) -> str:
        return str(self._resolve()["username"])

    @property
    def account_mask(self) -> str:
        return str(self._resolve()["account_mask"])

    @property
    def identity_tokens(self) -> tuple[str, ...]:
        return cast(tuple[str, ...], self._resolve()["identity_tokens"])

    @property
    def db_dir(self) -> Path:
        return SOURCE_ROOT / self.account_dir / "db_storage"


ACCOUNT = AccountIdentity()

_ALLOWED_DB_PATTERNS = (
    re.compile(r"^contact/contact\.db$"),
    re.compile(r"^session/session\.db$"),
    re.compile(r"^message/message_\d+\.db$"),
)


def is_allowed_database(relative_path: str) -> bool:
    """Return whether a DB is part of the minimum read-only feature set."""

    normalized = relative_path.replace("\\", "/")
    return any(pattern.fullmatch(normalized) for pattern in _ALLOWED_DB_PATTERNS)


def safe_relative_database(relative_path: str) -> str:
    """Normalize and validate an account-local database path."""

    normalized = relative_path.replace("\\", "/").strip("/")
    parts = normalized.split("/")
    if not normalized or any(part in ("", ".", "..") for part in parts):
        raise ValueError("unsafe relative database path")
    if not is_allowed_database(normalized):
        raise ValueError("database is outside the allowlist")
    return normalized

