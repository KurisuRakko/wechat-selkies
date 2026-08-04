from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from wechat_history.constants import TARGET_ACCOUNT_DIR
from wechat_history.errors import HistoryError
from wechat_history.keyscan import prepare_key_directory, require_target_account_active


def login_marker(root: Path, account: str, timestamp_ns: int) -> Path:
    marker = root / account / "config" / "login_configv2"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"marker")
    os.utime(marker, ns=(timestamp_ns, timestamp_ns))
    return marker


class KeyscanGuardTests(unittest.TestCase):
    def test_accepts_only_when_target_has_newest_login_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            login_marker(root, "wxid_other_0000", 100)
            login_marker(root, TARGET_ACCOUNT_DIR, 200)
            database_dir = root / TARGET_ACCOUNT_DIR / "db_storage"
            require_target_account_active(database_dir)

    def test_rejects_when_another_account_is_newer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            login_marker(root, TARGET_ACCOUNT_DIR, 100)
            login_marker(root, "wxid_other_0000", 200)
            database_dir = root / TARGET_ACCOUNT_DIR / "db_storage"
            with self.assertRaises(HistoryError) as raised:
                require_target_account_active(database_dir)
            self.assertEqual(raised.exception.code, "TARGET_ACCOUNT_NOT_ACTIVE")

    def test_key_directory_is_private_before_scanning(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX mode assertion")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "keys" / "keys.json"
            prepare_key_directory(destination)
            self.assertEqual(destination.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
