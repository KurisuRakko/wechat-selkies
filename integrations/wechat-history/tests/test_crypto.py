from __future__ import annotations

import hashlib
import hmac
import struct
import tempfile
import unittest
from pathlib import Path

from Crypto.Cipher import AES

from wechat_history.crypto import (
    PAGE_SIZE,
    RESERVE_SIZE,
    SQLITE_HEADER,
    apply_wal,
    decrypt_database,
    decrypt_page,
    verify_encryption_key,
)
from wechat_history.errors import HistoryError


def encrypted_page(key: bytes, salt: bytes, page_number: int, plaintext: bytes) -> bytes:
    iv = bytes([page_number % 251 + 1]) * 16
    start = 16 if page_number == 1 else 0
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(
        plaintext[start : PAGE_SIZE - RESERVE_SIZE]
    )
    prefix = (salt if page_number == 1 else b"") + ciphertext + iv
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
    digest = hmac.new(mac_key, prefix[start:], hashlib.sha512)
    digest.update(struct.pack("<I", page_number))
    result = prefix + digest.digest()
    assert len(result) == PAGE_SIZE
    return result


class CryptoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = bytes(range(32))
        self.salt = bytes(range(16))
        self.plaintext = bytearray(PAGE_SIZE)
        self.plaintext[:16] = SQLITE_HEADER
        self.plaintext[16:64] = bytes(range(48))

    def test_key_hmac_and_page_decryption(self) -> None:
        page = encrypted_page(self.key, self.salt, 1, bytes(self.plaintext))
        self.assertTrue(verify_encryption_key(self.key, page))
        self.assertFalse(verify_encryption_key(bytes(reversed(self.key)), page))
        decrypted = decrypt_page(self.key, page, 1)
        self.assertEqual(decrypted[:64], bytes(self.plaintext[:64]))
        self.assertEqual(decrypted[-RESERVE_SIZE:], bytes(RESERVE_SIZE))

    def test_database_rejects_wrong_key(self) -> None:
        page = encrypted_page(self.key, self.salt, 1, bytes(self.plaintext))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"
            output = Path(temporary) / "output.db"
            source.write_bytes(page)
            with self.assertRaises(HistoryError) as raised:
                decrypt_database(source, output, bytes(reversed(self.key)))
            self.assertEqual(raised.exception.code, "KEY_STALE")
            self.assertFalse(output.exists())

    def test_database_ignores_zero_filled_physical_preallocation(self) -> None:
        plaintext = bytearray(PAGE_SIZE)
        plaintext[:16] = SQLITE_HEADER
        struct.pack_into(">I", plaintext, 28, 1)
        page = encrypted_page(self.key, self.salt, 1, bytes(plaintext))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"
            output = Path(temporary) / "output.db"
            source.write_bytes(page + bytes(PAGE_SIZE * 4))
            logical_pages = decrypt_database(source, output, self.key)
            self.assertEqual(logical_pages, 1)
            self.assertEqual(output.stat().st_size, PAGE_SIZE)
            self.assertEqual(output.read_bytes()[:16], SQLITE_HEADER)

    def test_wal_applies_only_through_last_commit(self) -> None:
        page = encrypted_page(self.key, self.salt, 1, bytes(self.plaintext))
        wal_header = bytearray(32)
        wal_header[16:24] = struct.pack(">II", 11, 22)
        committed_header = struct.pack(">IIIIII", 1, 1, 11, 22, 0, 0)
        uncommitted_plain = bytearray(self.plaintext)
        uncommitted_plain[32:40] = b"ignored!"
        uncommitted_page = encrypted_page(
            self.key, self.salt, 1, bytes(uncommitted_plain)
        )
        uncommitted_header = struct.pack(">IIIIII", 1, 0, 11, 22, 0, 0)
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "plain.db"
            database.write_bytes(bytes(PAGE_SIZE * 2))
            wal = Path(temporary) / "source.db-wal"
            wal.write_bytes(
                bytes(wal_header)
                + committed_header
                + page
                + uncommitted_header
                + uncommitted_page
            )
            applied = apply_wal(wal, database, self.key, self.salt)
            self.assertEqual(applied, 1)
            self.assertEqual(database.stat().st_size, PAGE_SIZE)
            self.assertEqual(database.read_bytes()[:16], SQLITE_HEADER)


if __name__ == "__main__":
    unittest.main()
