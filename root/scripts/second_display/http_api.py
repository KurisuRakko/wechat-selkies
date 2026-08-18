"""只读状态端点：GET /wechat-second-display/api/status。

路由注册的是带 wechat-second-display/ 前缀的完整路径，与
patches/patch-second-display-nginx.py 的 nginx location 一致：那个 location
的 proxy_pass 故意不带 URI 部分（没有尾斜杠），nginx 因此把原始请求路径
原样转发给这里，而不是剥掉匹配到的前缀——和 wechat_history.notifications
里 /wechat-notifications/api/... 的约定完全一样，直接复用而不是另立一套。

出于隐私考虑，返回内容只有计数和几何数字，不包含任何窗口标题、WM_CLASS
原文等可辨识信息（这些可能带出小程序名称、聊天对象昵称）。
"""

from __future__ import annotations

from aiohttp import web

from .daemon import Daemon

STATUS_PATH = "/wechat-second-display/api/status"


def build_app(daemon: Daemon) -> web.Application:
    app = web.Application()

    async def status(_: web.Request) -> web.Response:
        return web.json_response(
            daemon.status_snapshot(), headers={"Cache-Control": "no-store"}
        )

    app.router.add_get(STATUS_PATH, status)
    return app
