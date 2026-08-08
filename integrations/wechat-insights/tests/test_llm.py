from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import wechat_insights.llm as llm


class FakeResponse:
    """假 urlopen 响应：上下文管理器 + read()。"""

    def __init__(self, content: bytes):
        self.content = content

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.content


class ChatTests(unittest.TestCase):
    def setUp(self) -> None:
        # 固定配置，避免测试受环境变量影响。
        for attribute, value in (
            ("INSIGHTS_LLM_BASE_URL", "http://llm.test/v1"),
            ("INSIGHTS_LLM_API_KEY", "secret-key"),
            ("INSIGHTS_LLM_MODEL", "test-model"),
            ("INSIGHTS_LLM_TIMEOUT_SECONDS", 30),
        ):
            patcher = patch.object(llm, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_chat_returns_content_and_sends_the_right_request(self) -> None:
        captured: dict = {}

        def fake_urlopen(request, timeout: int) -> FakeResponse:
            captured["url"] = request.get_full_url()
            captured["method"] = request.method
            captured["headers"] = request.headers
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(
                b'{"choices": [{"message": {"content": "{\\"score\\": 77}"}}]}'
            )

        with patch.object(urllib.request, "urlopen", fake_urlopen):
            reply = llm.chat("系统提示", "用户内容")
        self.assertEqual(reply, '{"score": 77}')
        self.assertEqual(captured["url"], "http://llm.test/v1/chat/completions")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(
            captured["headers"].get("Authorization"), "Bearer secret-key"
        )
        # urllib 会把头名规范成 Content-type（HTTP 头大小写不敏感），值不变。
        self.assertEqual(captured["headers"].get("Content-type"), "application/json")
        self.assertEqual(captured["body"]["temperature"], 0)
        self.assertEqual(captured["body"]["model"], "test-model")
        self.assertEqual(
            captured["body"]["messages"],
            [
                {"role": "system", "content": "系统提示"},
                {"role": "user", "content": "用户内容"},
            ],
        )

    def test_http_error_retries_once_then_returns_none(self) -> None:
        calls: list = []

        def fake_urlopen(request, timeout: int) -> None:
            calls.append(request)
            raise urllib.error.HTTPError(
                request.get_full_url(), 500, "Internal Server Error", {}, None
            )

        with patch.object(urllib.request, "urlopen", fake_urlopen), self.assertLogs(
            "wechat-insights", level="WARNING"
        ):
            self.assertIsNone(llm.chat("系统提示", "用户内容"))
        # 共至多 2 次请求：失败自动重试 1 次。
        self.assertEqual(len(calls), 2)

    def test_timeout_is_treated_as_failure_and_retried(self) -> None:
        calls: list = []

        def fake_urlopen(request, timeout: int) -> None:
            calls.append(timeout)
            raise TimeoutError("timed out")

        with patch.object(urllib.request, "urlopen", fake_urlopen), self.assertLogs(
            "wechat-insights", level="WARNING"
        ):
            self.assertIsNone(llm.chat("系统提示", "用户内容"))
        # 超时用的是配置值，且两次请求各用一次完整超时。
        self.assertEqual(calls, [30, 30])

    def test_base_url_trailing_slash_is_tolerated(self) -> None:
        captured: dict = {}

        def fake_urlopen(request, timeout: int) -> FakeResponse:
            captured["url"] = request.get_full_url()
            return FakeResponse(b'{"choices": [{"message": {"content": "ok"}}]}')

        with patch.object(llm, "INSIGHTS_LLM_BASE_URL", "http://llm.test/v1/"):
            with patch.object(urllib.request, "urlopen", fake_urlopen):
                self.assertEqual(llm.chat("系统提示", "用户内容"), "ok")
        self.assertEqual(captured["url"], "http://llm.test/v1/chat/completions")

    def test_response_without_content_is_a_failure(self) -> None:
        calls: list = []

        def fake_urlopen(request, timeout: int) -> FakeResponse:
            calls.append(request)
            return FakeResponse(b'{"choices": [{"message": {}}]}')

        with patch.object(urllib.request, "urlopen", fake_urlopen), self.assertLogs(
            "wechat-insights", level="WARNING"
        ):
            self.assertIsNone(llm.chat("系统提示", "用户内容"))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
