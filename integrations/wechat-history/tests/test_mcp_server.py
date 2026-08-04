from __future__ import annotations

import unittest
from unittest.mock import patch

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
