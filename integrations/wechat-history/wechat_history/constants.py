"""Security-sensitive constants for the single supported account."""

from __future__ import annotations

import os
import re
from pathlib import Path


TARGET_ACCOUNT_DIR = "wxid_bo75mcv4n6hi12_7498"
TARGET_USERNAME = "wxid_bo75mcv4n6hi12"
TARGET_ACCOUNT_MASK = "wxid_bo75…7498"
EXPECTED_IDENTITY_TOKENS = ("杨博文", "spencer", "kurisurakko")
UPSTREAM_COMMIT = "a3789232d4f79bf0b30634d9dadbce71e4acd601"

KEY_SCHEMA_VERSION = 1
MAX_CACHE_BYTES = 512 * 1024 * 1024
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

TARGET_ACCOUNT_ROOT = SOURCE_ROOT / TARGET_ACCOUNT_DIR
TARGET_DB_DIR = TARGET_ACCOUNT_ROOT / "db_storage"

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

