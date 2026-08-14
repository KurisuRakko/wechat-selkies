"""鉴权用例：从 test_server.py 拆出（原文件已贴着 1000 行上限）。

与原文件共用 AioHTTPTestCase 写法；AUTH_TOKEN 用进程级 patcher 固定，
用例之间互不干扰。
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import AioHTTPTestCase

from wechat_insights.server import InsightsRuntime, create_app
from wechat_insights.storage import MetricsStore, contact_hash

SESSION_ID = "friend"
HASH = contact_hash(SESSION_ID)


class AuthTests(AioHTTPTestCase):
    async def get_application(self):
        return create_app(self.runtime)

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MetricsStore(Path(self.temporary.name) / "metrics.db")
        self.addCleanup(self.store.close)
        self.store.set_meta("last_analyzed_at", "1700000000")
        # 关系类型改判要改库，AuthTests 里备一个联系人供带 token 的请求用。
        self.store.ensure_contact(SESSION_ID, "Alice")
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

    async def test_progress_requires_a_token(self) -> None:
        self.assertEqual((await self.client.get("/api/progress")).status, 401)
        response = await self.client.get(
            "/api/progress", headers={"Authorization": "Bearer s3cret"}
        )
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())["progress"]["running"])

    async def test_report_api_requires_a_token(self) -> None:
        self.assertEqual((await self.client.get("/api/report")).status, 401)
        response = await self.client.get(
            "/api/report?year=2026", headers={"Authorization": "Bearer s3cret"}
        )
        self.assertEqual(response.status, 200)

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

    async def test_kind_override_requires_a_token(self) -> None:
        response = await self.client.post(
            f"/api/contact/{HASH}/kind", json={"kind": "family"}
        )
        self.assertEqual(response.status, 401)
        response = await self.client.post(
            f"/api/contact/{HASH}/kind",
            json={"kind": "family"},
            headers={"Authorization": "Bearer s3cret"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.store.get_contact(SESSION_ID).kind_manual, "family")

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
