from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

# 身份通过环境变量注入（与生产同一机制）；这里固定合成值，保证测试确定性，
# 不携带任何真实身份。
os.environ["WECHAT_HISTORY_ACCOUNT_DIR"] = "wxid_testaccount_0000"
os.environ["WECHAT_HISTORY_USERNAME"] = "wxid_testaccount"
os.environ["WECHAT_HISTORY_IDENTITY_TOKENS"] = "测试身份,testidentity"

import zstandard as zstd

from wechat_history.constants import ACCOUNT
from wechat_history.errors import HistoryError
from wechat_history.reader import HistoryReader
from wechat_history.snapshot import KeyStore


class FakeCache:
    def __init__(self, paths: dict[str, Path]):
        self.paths = paths

    def get(self, relative_path: str) -> Path:
        return self.paths[relative_path]

    def close(self) -> None:
        pass


def create_contact_database(path: Path, nickname: str, alias: str = "testalias") -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT, alias TEXT)"
        )
        connection.execute(
            "INSERT INTO contact VALUES (?, ?, '', ?)",
            (ACCOUNT.username, nickname, alias),
        )


class ReaderIdentityTests(unittest.TestCase):
    def test_accepts_expected_self_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contact = root / "contact.db"
            create_contact_database(contact, "测试身份 TestIdentity")
            source = root / ACCOUNT.account_dir / "db_storage"
            reader = HistoryReader.__new__(HistoryReader)
            reader.key_store = object()
            reader.source_dir = source
            reader.cache = FakeCache({"contact/contact.db": contact})
            reader.account_root = source.parent
            reader.source_root = source.parent.parent
            reader._profile = None
            profile = reader.ensure_account_validated()
            self.assertTrue(profile["identity_verified"])
            self.assertIn("TestIdentity", profile["display_name"])

    def test_rejects_wrong_self_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contact = root / "contact.db"
            create_contact_database(contact, "Unrelated Account", alias="unrelated")
            source = root / ACCOUNT.account_dir / "db_storage"
            reader = HistoryReader.__new__(HistoryReader)
            reader.key_store = object()
            reader.source_dir = source
            reader.cache = FakeCache({"contact/contact.db": contact})
            reader.account_root = source.parent
            reader.source_root = source.parent.parent
            reader._profile = None
            with self.assertRaises(HistoryError) as raised:
                reader.ensure_account_validated()
            self.assertEqual(raised.exception.code, "ACCOUNT_MISMATCH")


class KeyStoreTests(unittest.TestCase):
    def test_rejects_a_key_file_for_another_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "keys.json"
            path.write_text(
                json.dumps(
                    {
                        "_meta": {
                            "schema_version": 1,
                            "target_account_dir": "wxid_other_0000",
                        },
                        "contact/contact.db": {
                            "enc_key": "00" * 32,
                            "salt": "00" * 16,
                        },
                    }
                ),
                encoding="utf-8",
            )
            if os.name == "posix":
                path.chmod(0o600)
            with self.assertRaises(HistoryError) as raised:
                KeyStore(path)
            self.assertEqual(raised.exception.code, "ACCOUNT_MISMATCH")


class SearchTests(unittest.TestCase):
    def test_searches_zstd_compressed_message_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "message.db"
            session_id = "filehelper"
            table = f"Msg_{hashlib.md5(session_id.encode('utf-8')).hexdigest()}"
            compressed = zstd.ZstdCompressor().compress("压缩搜索目标".encode("utf-8"))
            with sqlite3.connect(database) as connection:
                connection.execute(
                    f"""CREATE TABLE [{table}] (
                        local_id INTEGER, local_type INTEGER, create_time INTEGER,
                        real_sender_id INTEGER, message_content BLOB,
                        WCDB_CT_message_content INTEGER
                    )"""
                )
                connection.execute(
                    f"INSERT INTO [{table}] VALUES (1, 1, 100, 0, ?, 4)",
                    (compressed,),
                )

            reader = HistoryReader.__new__(HistoryReader)
            reader.cache = FakeCache({"message/message_0.db": database})
            reader.ensure_account_validated = lambda: {"identity_verified": True}
            reader._resolve_session = lambda value: {
                "session_id": value,
                "display_name": "文件传输助手",
                "alias": "",
                "kind": "direct",
            }
            reader._message_database_keys = lambda: ["message/message_0.db"]
            reader._load_contacts = lambda: {}
            result = reader.search_messages("搜索目标", session_id=session_id)
            self.assertEqual(len(result["items"]), 1)
            self.assertEqual(result["items"][0]["text"], "压缩搜索目标")
            self.assertEqual(result["search_mode"], "streaming_decompressed_text")


class ReplySessionTests(unittest.TestCase):
    def test_filehelper_can_be_unique_even_when_not_in_recent_sessions(self) -> None:
        reader = HistoryReader.__new__(HistoryReader)
        reader.ensure_account_validated = lambda: {"identity_verified": True}
        reader._resolve_session = lambda value: {
            "session_id": value,
            "display_name": "文件传输助手",
            "alias": "",
            "kind": "direct",
        }
        reader._session_items = lambda: []
        reader._load_contacts = lambda: {
            "filehelper": {"display_name": "文件传输助手"}
        }
        result = reader.reply_session("filehelper")
        self.assertEqual(result["ui_query"], "文件传输助手")


if __name__ == "__main__":
    unittest.main()
