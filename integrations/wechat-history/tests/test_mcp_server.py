from __future__ import annotations

import os
import unittest
from unittest.mock import patch

# 身份通过环境变量注入（与生产同一机制）；这里固定合成值，保证测试确定性。
os.environ["WECHAT_HISTORY_ACCOUNT_DIR"] = "wxid_testaccount_0000"
os.environ["WECHAT_HISTORY_USERNAME"] = "wxid_testaccount"
os.environ["WECHAT_HISTORY_IDENTITY_TOKENS"] = "测试身份,testidentity"

from wechat_history.errors import fail
from wechat_history.mcp_server import HistoryService


class HealthCheckTests(unittest.TestCase):
    def test_reports_window_and_safe_status_when_key_is_missing(self) -> None:
        service = HistoryService()
        with patch(
            "wechat_history.mcp_server.probe_wechat_window_status",
            return_value="visible",
        ), patch(
            "wechat_history.mcp_server.HistoryReader",
            side_effect=fail("KEY_NOT_FOUND", "safe missing key"),
        ):
            result = service.health_check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["key_status"], "missing")
        self.assertEqual(result["database_status"], "unavailable")
        self.assertEqual(result["snapshot_status"], "unavailable")
        self.assertEqual(result["wechat_window"], "visible")
        self.assertFalse(result["network_listener"])
        self.assertFalse(result["automatic_send"])


if __name__ == "__main__":
    unittest.main()
