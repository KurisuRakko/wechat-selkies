from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import AioHTTPTestCase

from wechat_insights.metrics import Metrics
from wechat_insights.server import (
    InsightsRuntime,
    create_app,
    next_run_at,
    parse_analyze_time,
)
from wechat_insights.storage import MetricsStore, contact_hash


SESSION_ID = "friend"
HASH = contact_hash(SESSION_ID)


def payload(scored: bool = True) -> dict:
    return {
        "hash": HASH,
        "display_name": "Alice",
        "scored": scored,
        "overall": 73.4 if scored else None,
        "dimensions": {
            "responsiveness": 80.1,
            "initiative": 60.0,
            "investment": 71.2,
            "rhythm": 55.0,
            "depth": 66.3,
        },
        "trends": {"overall": -1.7},
        "recent_messages": 312,
        "window_messages": 1200,
        "last_message_at": 1_700_000_000,
        "sample_note": "" if scored else "数据不足",
        "anomalies": [{"metric": "reply_delay_them", "label": "TA 回复延迟中位数"}],
    }


class ScheduleTests(AioHTTPTestCase):
    """纯函数，不需要客户端，但放在一起方便一次跑完。"""

    async def get_application(self):
        return create_app(self.runtime)

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MetricsStore(Path(self.temporary.name) / "metrics.db")
        self.addCleanup(self.store.close)
        self.store.set_meta("last_analyzed_at", "1700000000")
        self.runtime = InsightsRuntime(self.store, analyzer_factory=lambda: None)
        await super().asyncSetUp()

    def test_invalid_analyze_time_falls_back(self) -> None:
        self.assertEqual(parse_analyze_time("06:15"), (6, 15))
        self.assertEqual(parse_analyze_time("25:00"), (4, 30))
        self.assertEqual(parse_analyze_time("nonsense"), (4, 30))

    def test_next_run_is_always_in_the_future(self) -> None:
        now = 1_700_000_000.0
        self.assertGreater(next_run_at(now, 4, 30), now)
        self.assertLessEqual(next_run_at(now, 4, 30) - now, 86400)


class ApiTests(AioHTTPTestCase):
    async def get_application(self):
        return create_app(self.runtime)

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MetricsStore(Path(self.temporary.name) / "metrics.db")
        self.addCleanup(self.store.close)
        self.store.set_meta("last_analyzed_at", "1700000000")
        self.store.ensure_contact(SESSION_ID, "Alice")
        self.store.save_scores([(SESSION_ID, payload())])
        self.store.set_json("medians", {"responsiveness": 50.0})
        bucket = Metrics()
        bucket.add("msgs_them", 4)
        bucket.add("kind_text_them", 4)
        bucket.add_reply("incoming", 30)
        bucket.add_reply("incoming", 45)
        bucket.add_reply("incoming", 60)
        self.store.merge_daily(SESSION_ID, {"2026-03-10": bucket})
        self.runtime = InsightsRuntime(self.store, analyzer_factory=lambda: None)
        await super().asyncSetUp()

    async def test_status_reports_the_last_analysis(self) -> None:
        body = await (await self.client.get("/api/status")).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["last_analyzed_at"], 1_700_000_000)
        self.assertIsNotNone(body["last_analyzed_iso"])
        self.assertFalse(body["running"])
        self.assertEqual(body["contacts"], 1)
        self.assertEqual(body["scored_contacts"], 1)

    async def test_contact_list_omits_anomalies_and_fills_missing_medians(self) -> None:
        body = await (await self.client.get("/api/contacts")).json()
        self.assertEqual(len(body["items"]), 1)
        self.assertNotIn("anomalies", body["items"][0])
        self.assertEqual(body["medians"]["responsiveness"], 50.0)
        # 分析器还没写过的维度用中性值补齐，前端不必处理缺字段。
        self.assertEqual(body["medians"]["depth"], 50.0)

    async def test_detail_returns_series_milestones_and_anomalies(self) -> None:
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["contact"]["display_name"], "Alice")
        self.assertEqual(body["monthly"][0]["month"], "2026-03")
        self.assertIsNotNone(body["monthly"][0]["reply_median_them"])
        self.assertEqual(body["types"], [{"kind": "text", "count": 4}])
        self.assertIn("first_message_at", body["milestones"])
        self.assertEqual(len(body["anomalies"]), 1)

    async def test_unknown_contact_returns_a_typed_404(self) -> None:
        response = await self.client.get(f"/api/contact/{'0' * 24}")
        self.assertEqual(response.status, 404)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "CONTACT_NOT_FOUND")

    async def test_malformed_hash_is_rejected(self) -> None:
        self.assertEqual((await self.client.get("/api/contact/nope")).status, 404)

    async def test_refresh_is_debounced(self) -> None:
        await self.runtime._lock.acquire()
        try:
            response = await self.client.post("/api/refresh")
            self.assertEqual(response.status, 409)
            body = await response.json()
            self.assertEqual(body["error"]["code"], "ANALYSIS_RUNNING")
        finally:
            self.runtime._lock.release()

    async def test_refresh_starts_an_analysis(self) -> None:
        with patch.object(InsightsRuntime, "analyze", return_value=True):
            response = await self.client.post("/api/refresh")
        self.assertEqual(response.status, 202)
        self.assertTrue((await response.json())["started"])

    async def test_pages_are_served(self) -> None:
        self.assertEqual((await self.client.get("/")).status, 200)
        self.assertEqual((await self.client.get(f"/contact/{HASH}")).status, 200)


class AuthTests(AioHTTPTestCase):
    async def get_application(self):
        return create_app(self.runtime)

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MetricsStore(Path(self.temporary.name) / "metrics.db")
        self.addCleanup(self.store.close)
        self.store.set_meta("last_analyzed_at", "1700000000")
        self.runtime = InsightsRuntime(self.store, analyzer_factory=lambda: None)
        patcher = patch("wechat_insights.server.AUTH_TOKEN", "s3cret")
        patcher.start()
        self.addCleanup(patcher.stop)
        await super().asyncSetUp()

    async def test_requests_without_a_token_are_rejected(self) -> None:
        response = await self.client.get("/api/status")
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())["error"]["code"], "UNAUTHORIZED")

    async def test_static_assets_are_protected_too(self) -> None:
        self.assertEqual((await self.client.get("/static/app.css")).status, 401)

    async def test_bearer_header_is_accepted(self) -> None:
        response = await self.client.get(
            "/api/status", headers={"Authorization": "Bearer s3cret"}
        )
        self.assertEqual(response.status, 200)

    async def test_query_token_redirects_and_sets_a_cookie(self) -> None:
        # ?token= 只该出现在地址栏一次：校验通过后 302 到不带参数的同一个 URL，
        # 并在响应里写 cookie；token 不进浏览器历史，也不会出现在复制出去的链接里。
        response = await self.client.get(
            "/api/status?token=s3cret", allow_redirects=False
        )
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/api/status")
        self.assertIn("wechat_insights_token", response.cookies)
        # 重定向后的请求不带 token，靠 cookie 单独通过；也不会再重定向（无死循环）。
        self.assertEqual((await self.client.get("/api/status")).status, 200)

    async def test_redirect_keeps_other_query_params(self) -> None:
        response = await self.client.get(
            "/api/status?foo=1&token=s3cret", allow_redirects=False
        )
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/api/status?foo=1")

    async def test_pages_redirect_too(self) -> None:
        response = await self.client.get("/?token=s3cret", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/")
        # 跟随重定向后页面正常返回，且地址栏不再有 token。
        self.assertEqual((await self.client.get("/?token=s3cret")).status, 200)

    async def test_bearer_header_does_not_redirect(self) -> None:
        response = await self.client.get(
            "/api/status", headers={"Authorization": "Bearer s3cret"}
        )
        self.assertEqual(response.status, 200)
        self.assertNotIn("wechat_insights_token", response.cookies)

    async def test_post_with_query_token_is_not_redirected(self) -> None:
        # 302 会把 POST 转成 GET、破坏接口语义，所以非 GET/HEAD 只写 cookie。
        with patch.object(InsightsRuntime, "analyze", return_value=True):
            response = await self.client.post("/api/refresh?token=s3cret")
        self.assertEqual(response.status, 202)
        self.assertIn("wechat_insights_token", response.cookies)

    async def test_unknown_paths_require_auth_too(self) -> None:
        # 404 响应同样要过鉴权，不能成为未授权探测的信息源。
        self.assertEqual((await self.client.get("/api/nope")).status, 401)
        response = await self.client.get(
            "/api/nope", headers={"Authorization": "Bearer s3cret"}
        )
        self.assertEqual(response.status, 404)

    async def test_head_requests_require_auth(self) -> None:
        self.assertEqual(
            (await self.client.request("HEAD", "/api/status")).status, 401
        )
        response = await self.client.request(
            "HEAD", "/api/status", headers={"Authorization": "Bearer s3cret"}
        )
        self.assertEqual(response.status, 200)
        # HEAD 查询参数同样会 302 清掉 token。
        redirected = await self.client.request(
            "HEAD", "/api/status?token=s3cret", allow_redirects=False
        )
        self.assertEqual(redirected.status, 302)
        self.assertEqual(redirected.headers["Location"], "/api/status")

    async def test_options_requests_require_auth(self) -> None:
        self.assertEqual((await self.client.options("/")).status, 401)
        response = await self.client.options(
            "/", headers={"Authorization": "Bearer s3cret"}
        )
        # 路径存在但没注册 OPTIONS：aiohttp 回 405（带 Allow 头），而不是放行。
        self.assertEqual(response.status, 405)

    async def test_wrong_token_is_rejected(self) -> None:
        self.assertEqual((await self.client.get("/api/status?token=nope")).status, 401)
