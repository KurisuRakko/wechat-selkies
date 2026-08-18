"""容器内常驻入口：python3 -m second_display。

ENABLE_WECHAT_SECOND_DISPLAY 的唯一判定点在 s6 的 run 脚本里（关闭时这个
模块根本不会被启动），这里只管"开着的时候该怎么跑"。

一个 asyncio loop 里跑两件事：daemon 的阻塞式 X11 主循环整体丢进线程池
（run_in_executor）执行，不占用 loop 本身；aiohttp 的只读 HTTP 端点跑在
loop 自己的线程上。两者之间除了 Daemon.status_snapshot() 返回的现成字典
引用之外不共享任何状态，见 daemon.py 模块顶部关于线程安全的说明。
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from Xlib import display as xdisplay

from .daemon import Daemon
from .http_api import build_app

LOG = logging.getLogger("wechat-second-display")

BIND_HOST = "127.0.0.1"
BIND_PORT = 8768


async def _run(daemon: Daemon) -> None:
    loop = asyncio.get_running_loop()
    app = build_app(daemon)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, BIND_HOST, BIND_PORT)
    await site.start()
    LOG.info("listening on %s:%d", BIND_HOST, BIND_PORT)
    # daemon.run_forever() 正常不会返回；异常会在这里向上抛出并让进程退出，
    # 交给 s6 supervisor 重启，而不是在内部悄悄吞掉未知异常。
    await loop.run_in_executor(None, daemon.run_forever)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="wechat-second-display %(levelname)s %(message)s",
    )
    disp = xdisplay.Display()
    root = disp.screen().root
    daemon = Daemon(disp, root)
    asyncio.run(_run(daemon))


if __name__ == "__main__":
    main()
