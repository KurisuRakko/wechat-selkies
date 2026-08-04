"""SQLCipher 4 page and WAL decryption.

Adapted from huohuoer/wechat-cli at commit a3789232. The changes here add
constant-time HMAC validation and apply WAL frames only through the last commit.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from pathlib import Path

from Crypto.Cipher import AES

from .errors import fail


PAGE_SIZE = 4096
KEY_SIZE = 32
SALT_SIZE = 16
RESERVE_SIZE = 80  # IV(16) + HMAC-SHA512(64)
SQLITE_HEADER = b"SQLite format 3\x00"
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24


def _mac_key(encryption_key: bytes, database_salt: bytes) -> bytes:
    mac_salt = bytes(value ^ 0x3A for value in database_salt)
    return hashlib.pbkdf2_hmac(
        "sha512", encryption_key, mac_salt, 2, dklen=KEY_SIZE
    )


def verify_page_hmac(
    encryption_key: bytes,
    database_salt: bytes,
    encrypted_page: bytes,
    page_number: int,
) -> bool:
    """Validate one encrypted SQLCipher page without revealing key material."""

    if len(encryption_key) != KEY_SIZE or len(database_salt) != SALT_SIZE:
        return False
    if len(encrypted_page) != PAGE_SIZE or page_number <= 0:
        return False
    start = SALT_SIZE if page_number == 1 else 0
    authenticated = encrypted_page[start : PAGE_SIZE - 64]
    stored = encrypted_page[PAGE_SIZE - 64 :]
    digest = hmac.new(
        _mac_key(encryption_key, database_salt), authenticated, hashlib.sha512
    )
    digest.update(struct.pack("<I", page_number))
    return hmac.compare_digest(digest.digest(), stored)


def verify_encryption_key(encryption_key: bytes, first_page: bytes) -> bool:
    if len(first_page) != PAGE_SIZE:
        return False
    return verify_page_hmac(
        encryption_key, first_page[:SALT_SIZE], first_page, page_number=1
    )


def decrypt_page(encryption_key: bytes, encrypted_page: bytes, page_number: int) -> bytes:
    if len(encrypted_page) != PAGE_SIZE:
        raise fail("INVALID_DB", "数据库页长度无效")
    iv_start = PAGE_SIZE - RESERVE_SIZE
    iv = encrypted_page[iv_start : iv_start + 16]
    if page_number == 1:
        ciphertext = encrypted_page[SALT_SIZE:iv_start]
        plaintext = AES.new(encryption_key, AES.MODE_CBC, iv).decrypt(ciphertext)
        return SQLITE_HEADER + plaintext + (b"\x00" * RESERVE_SIZE)
    ciphertext = encrypted_page[:iv_start]
    plaintext = AES.new(encryption_key, AES.MODE_CBC, iv).decrypt(ciphertext)
    return plaintext + (b"\x00" * RESERVE_SIZE)


def decrypt_database(source: Path, destination: Path, encryption_key: bytes) -> int:
    """Decrypt every logical DB page after validating each page HMAC.

    WCDB may preallocate a much larger sparse/zero-filled physical file. SQLite's
    page count in the decrypted header is authoritative; trailing physical pages
    are not database pages and therefore have no SQLCipher HMAC.
    """

    size = source.stat().st_size
    if size < PAGE_SIZE or size % PAGE_SIZE:
        raise fail("INVALID_DB", "数据库大小不是完整页")
    physical_pages = size // PAGE_SIZE
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    old_umask = os.umask(0o077)
    try:
        with source.open("rb") as encrypted, destination.open("wb") as plaintext:
            first_page = encrypted.read(PAGE_SIZE)
            database_salt = first_page[:SALT_SIZE]
            if not verify_page_hmac(encryption_key, database_salt, first_page, 1):
                raise fail("KEY_STALE", "数据库密钥无效或数据库页校验失败")
            decrypted_first = decrypt_page(encryption_key, first_page, 1)
            logical_pages = struct.unpack(">I", decrypted_first[28:32])[0]
            if logical_pages <= 0 or logical_pages > physical_pages:
                raise fail("INVALID_DB", "SQLite 逻辑页数超出加密文件范围")
            plaintext.write(decrypted_first)

            for page_number in range(2, logical_pages + 1):
                page = encrypted.read(PAGE_SIZE)
                if not verify_page_hmac(
                    encryption_key, database_salt, page, page_number
                ):
                    raise fail("KEY_STALE", "数据库密钥无效或数据库页校验失败")
                plaintext.write(decrypt_page(encryption_key, page, page_number))
        destination.chmod(0o600)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        os.umask(old_umask)
    return logical_pages


def apply_wal(
    encrypted_wal: Path,
    decrypted_database: Path,
    encryption_key: bytes,
    database_salt: bytes,
) -> int:
    """Apply only committed, salt-matching WAL frames to a decrypted DB."""

    if not encrypted_wal.exists() or encrypted_wal.stat().st_size <= WAL_HEADER_SIZE:
        return 0
    frames: list[tuple[int, int, bytes]] = []
    last_commit_index = -1
    committed_page_count = 0
    with encrypted_wal.open("rb") as wal:
        header = wal.read(WAL_HEADER_SIZE)
        if len(header) != WAL_HEADER_SIZE:
            return 0
        wal_salt_1, wal_salt_2 = struct.unpack(">II", header[16:24])
        while True:
            frame_header = wal.read(WAL_FRAME_HEADER_SIZE)
            if not frame_header:
                break
            if len(frame_header) != WAL_FRAME_HEADER_SIZE:
                break
            page = wal.read(PAGE_SIZE)
            if len(page) != PAGE_SIZE:
                break
            page_number, database_size = struct.unpack(">II", frame_header[:8])
            frame_salt_1, frame_salt_2 = struct.unpack(">II", frame_header[8:16])
            if page_number <= 0 or page_number > 1_000_000:
                continue
            if (frame_salt_1, frame_salt_2) != (wal_salt_1, wal_salt_2):
                continue
            frames.append((page_number, database_size, page))
            if database_size:
                last_commit_index = len(frames) - 1
                committed_page_count = database_size

    if last_commit_index < 0:
        return 0

    applied = 0
    with decrypted_database.open("r+b") as target:
        for page_number, _, page in frames[: last_commit_index + 1]:
            if not verify_page_hmac(
                encryption_key, database_salt, page, page_number
            ):
                raise fail("INVALID_WAL", "WAL 页面校验失败")
            target.seek((page_number - 1) * PAGE_SIZE)
            target.write(decrypt_page(encryption_key, page, page_number))
            applied += 1
        target.truncate(committed_page_count * PAGE_SIZE)
    return applied
