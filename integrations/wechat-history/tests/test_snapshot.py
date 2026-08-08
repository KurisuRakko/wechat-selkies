from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# 身份通过环境变量注入（与生产同一机制）；这里固定合成值，保证测试确定性。
os.environ["WECHAT_HISTORY_ACCOUNT_DIR"] = "wxid_testaccount_0000"
os.environ["WECHAT_HISTORY_USERNAME"] = "wxid_testaccount"
os.environ["WECHAT_HISTORY_IDENTITY_TOKENS"] = "测试身份,testidentity"

from tests.test_crypto import encrypted_page
from wechat_history import constants
from wechat_history.constants import ACCOUNT
from wechat_history.errors import HistoryError
from wechat_history.snapshot import FileState, KeyStore, SnapshotCache, SourceState


class SnapshotTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path]:
        key = bytes(range(32))
        source = root / "source" / ACCOUNT.account_dir / "db_storage"
        entries = (
            "contact/contact.db",
            "session/session.db",
            "message/message_0.db",
        )
        document: dict[str, object] = {
            "_meta": {
                "schema_version": 1,
                "target_account_dir": ACCOUNT.account_dir,
                "wechat_pid": 1,
                "wechat_start_ticks": 1,
            }
        }
        for index, relative in enumerate(entries):
            path = source.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            salt = bytes([index + 1]) * 16
            plaintext = bytearray(4096)
            plaintext[:16] = b"SQLite format 3\x00"
            path.write_bytes(encrypted_page(key, salt, 1, bytes(plaintext)))
            document[relative] = {
                "enc_key": key.hex(),
                "salt": salt.hex(),
                "size": 4096,
            }
        keys_path = root / "keys.json"
        keys_path.write_text(json.dumps(document), encoding="utf-8")
        if os.name == "posix":
            keys_path.chmod(0o600)
        return source, keys_path

    def test_snapshot_never_changes_encrypted_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, keys_path = self._fixture(root)
            original = (source / "contact" / "contact.db").read_bytes()
            cache = SnapshotCache(
                KeyStore(keys_path), source_dir=source, cache_root=root / "cache"
            )

            def fake_decrypt(_source: Path, destination: Path, _key: bytes) -> int:
                destination.write_bytes(b"SQLite format 3\x00" + bytes(4080))
                return 1

            with patch("wechat_history.snapshot.decrypt_database", fake_decrypt), patch.object(
                SnapshotCache, "_validate_sqlite", return_value=None
            ):
                decrypted = cache.get("contact/contact.db")
                self.assertTrue(decrypted.exists())
            self.assertEqual((source / "contact" / "contact.db").read_bytes(), original)
            cache.close()
            self.assertFalse(cache.process_dir.exists())

    def test_busy_source_fails_closed_after_three_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, keys_path = self._fixture(root)
            cache = SnapshotCache(
                KeyStore(keys_path), source_dir=source, cache_root=root / "cache"
            )
            first = SourceState(FileState(1, 4096, 1), None)
            changed = SourceState(FileState(1, 4096, 2), None)
            with patch(
                "wechat_history.snapshot._source_state",
                side_effect=[first, changed, first, changed, first, changed],
            ):
                with self.assertRaises(HistoryError) as raised:
                    cache._copy_consistent(
                        source / "contact" / "contact.db",
                        source / "contact" / "contact.db-wal",
                        "busy",
                    )
            self.assertEqual(raised.exception.code, "SOURCE_BUSY")
            cache.close()


class CacheCeilingTests(unittest.TestCase):
    """明文缓存上限的环境变量覆盖：这是主容器与看板容器共用的唯一旋钮。"""

    @staticmethod
    def _reload_with(value: str | None) -> int:
        """在给定环境下重载 constants，取出上限后把模块恢复成真实环境的样子。"""

        with patch.dict("os.environ"):
            if value is None:
                os.environ.pop("WECHAT_HISTORY_MAX_CACHE_BYTES", None)
            else:
                os.environ["WECHAT_HISTORY_MAX_CACHE_BYTES"] = value
            importlib.reload(constants)
            limit = constants.MAX_CACHE_BYTES
        importlib.reload(constants)
        return limit

    def test_default_is_512_mib(self) -> None:
        self.assertEqual(self._reload_with(None), 512 * 1024 * 1024)

    def test_override_inside_the_bounds_wins(self) -> None:
        self.assertEqual(self._reload_with("1006632960"), 1006632960)

    def test_out_of_range_and_garbage_fall_back_to_the_default(self) -> None:
        # 过小会让任何读取都失败、过大等于取消这道保护，非法值同理一律忽略。
        for value in ("1024", "9999999999", "big", ""):
            with self.subTest(value=value):
                self.assertEqual(self._reload_with(value), 512 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()

