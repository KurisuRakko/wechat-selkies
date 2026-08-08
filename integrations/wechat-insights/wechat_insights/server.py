"""aiohttp 服务：定时分析调度 + 只读看板 API + 静态资源。

所有指标都在分析阶段预计算进 scores 表，HTTP 处理器只做主键查询和一次单联系人
的小范围聚合，页面打开是秒开的。
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import signal
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

from .analyzer import Analyzer
from .constants import (
    ANALYZE_TIME,
    AUTH_COOKIE,
    AUTH_TOKEN,
    BIND_HOST,
    BIND_PORT,
    DB_PATH,
    RUN_ON_START,
)
from .reporting import monthly_series, total_metrics, type_composition
from .scoring import DIMENSION_NAMES
from .storage import MetricsStore


LOG = logging.getLogger("wechat-insights")

STATIC_ROOT = Path(__file__).resolve().parent / "static"
_HASH_PATTERN = re.compile(r"^[0-9a-f]{24}$")
# 分析失败后的重试间隔，避免密钥失效时每秒刷屏。
RETRY_SECONDS = 900.0


def parse_analyze_time(value: str) -> tuple[int, int]:
    """解析 HH:MM，非法值回退到 04:30。"""

    try:
        hour, minute = value.split(":", 1)
        parsed = (int(hour), int(minute))
    except (AttributeError, ValueError):
        parsed = (4, 30)
    if not 0 <= parsed[0] < 24 or not 0 <= parsed[1] < 60:
        return 4, 30
    return parsed


def next_run_at(now: float, hour: int, minute: int) -> float:
    """下一个每日分析时刻的 Unix 秒（容器本地时区）。"""

    current = datetime.fromtimestamp(now)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    stamp = target.timestamp()
    return stamp if stamp > now else stamp + 86400


class InsightsRuntime:
    """持有 metrics.db、调度每日分析并给 API 提供状态。"""

    def __init__(self, store: MetricsStore, analyzer_factory=None):
        self.store = store
        self.analyzer_factory = analyzer_factory or (lambda: Analyzer(store))
        self.hour, self.minute = parse_analyze_time(ANALYZE_TIME)
        self.running = False
        self.last_error: dict[str, str] | None = None
        self.last_duration = 0.0
        self.next_run: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._schedule_loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def last_analyzed_at(self) -> int | None:
        raw = self.store.get_meta("last_analyzed_at")
        return int(raw) if raw and raw.isdigit() else None

    async def trigger(self) -> bool:
        """手动触发一轮分析；已在跑则返回 False（防抖）。"""

        if self._lock.locked():
            return False
        asyncio.create_task(self.analyze())
        return True

    async def analyze(self) -> bool:
        if self._lock.locked():
            return False
        async with self._lock:
            self.running = True
            try:
                result = await asyncio.to_thread(self._run_blocking)
            except Exception as exc:
                # HistoryError 自带脱敏过的 code/safe_message，其余异常只暴露类型名，
                # 路径和密钥永远不会进到浏览器里。
                code = str(getattr(exc, "code", "") or type(exc).__name__)
                self.last_error = {
                    "code": code,
                    "message": getattr(exc, "safe_message", "")
                    or "分析失败，请检查密钥与只读挂载",
                }
                LOG.warning("分析失败（%s）", code, exc_info=True)
                return False
            finally:
                self.running = False
            self.last_error = None
            self.last_duration = result.duration_seconds
            LOG.info("分析完成，用时 %.1f 秒", result.duration_seconds)
            return True

    def _run_blocking(self):
        """在工作线程里跑一轮分析；结束后释放该线程的 sqlite 连接。"""

        analyzer = self.analyzer_factory()
        try:
            return analyzer.run()
        finally:
            self.store.close()

    async def _schedule_loop(self) -> None:
        immediate = RUN_ON_START and self.last_analyzed_at() is None
        if immediate:
            LOG.info("尚无历史结果，启动后立即执行首轮全量回填")
        while True:
            if not immediate:
                now = time.time()
                self.next_run = next_run_at(now, self.hour, self.minute)
                await asyncio.sleep(max(1.0, self.next_run - now))
            immediate = False
            # 失败（多半是密钥过期）就按固定间隔重试，成功后回到每日节奏。
            while not await self.analyze():
                self.next_run = time.time() + RETRY_SECONDS
                await asyncio.sleep(RETRY_SECONDS)


def _extract_token(request: web.Request) -> tuple[str, bool]:
    """从请求里取出 token，并标记它是不是来自查询参数。"""

    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip(), False
    query = request.query.get("token")
    if query:
        return query, True
    return request.cookies.get(AUTH_COOKIE, ""), False


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """启用 token 后，包括静态资源在内的所有请求都要带凭证。"""

    if not AUTH_TOKEN:
        return await handler(request)
    token, from_query = _extract_token(request)
    if not hmac.compare_digest(token, AUTH_TOKEN):
        return web.json_response(
            {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "需要访问令牌"}},
            status=401,
            headers={"Cache-Control": "no-store"},
        )
    response = await handler(request)
    if from_query:
        # 第一次用 ?token= 通过后写 cookie，之后的静态资源请求就不必再带参数。
        response.set_cookie(
            AUTH_COOKIE,
            AUTH_TOKEN,
            httponly=True,
            samesite="Lax",
            path="/",
            max_age=30 * 86400,
        )
    return response


def create_app(runtime: InsightsRuntime) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    store = runtime.store

    def no_store(payload: dict) -> web.Response:
        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    def medians() -> dict[str, float]:
        stored = store.get_json("medians", {})
        if not isinstance(stored, dict):
            stored = {}
        return {name: float(stored.get(name, 50.0)) for name in DIMENSION_NAMES}

    async def page(name: str) -> web.FileResponse:
        return web.FileResponse(STATIC_ROOT / name)

    async def index(_: web.Request) -> web.FileResponse:
        return await page("index.html")

    async def contact_page(_: web.Request) -> web.FileResponse:
        return await page("contact.html")

    async def status(_: web.Request) -> web.Response:
        analyzed = runtime.last_analyzed_at()
        contacts = store.all_contacts()
        return no_store(
            {
                "ok": True,
                "version": 1,
                "last_analyzed_at": analyzed,
                "last_analyzed_iso": (
                    datetime.fromtimestamp(analyzed).astimezone().isoformat(
                        timespec="seconds"
                    )
                    if analyzed
                    else None
                ),
                "last_duration_seconds": round(runtime.last_duration, 1),
                "running": runtime.running,
                "contacts": len(contacts),
                "scored_contacts": sum(
                    1 for item in store.all_scores() if item.get("scored")
                ),
                "next_run_at": int(runtime.next_run) if runtime.next_run else None,
                "error": runtime.last_error,
            }
        )

    async def contacts(_: web.Request) -> web.Response:
        items = store.all_scores()
        # 列表页不需要异动明细，去掉以免响应无谓变大。
        for item in items:
            item.pop("anomalies", None)
        items.sort(key=lambda item: (item.get("overall") is None, -(item.get("overall") or 0)))
        return no_store(
            {
                "ok": True,
                "generated_at": runtime.last_analyzed_at(),
                "medians": medians(),
                "items": items,
            }
        )

    async def contact_detail(request: web.Request) -> web.Response:
        value = request.match_info["hash"]
        if not _HASH_PATTERN.fullmatch(value):
            raise web.HTTPNotFound(text="not found")
        row = store.get_contact_by_hash(value)
        payload = store.score_by_hash(value)
        if row is None or payload is None:
            return web.json_response(
                {
                    "ok": False,
                    "error": {
                        "code": "CONTACT_NOT_FOUND",
                        "message": "找不到该联系人，可能还没跑过分析",
                    },
                },
                status=404,
                headers={"Cache-Control": "no-store"},
            )
        days = store.load_days(row.session_id)
        anomalies = payload.pop("anomalies", [])
        return no_store(
            {
                "ok": True,
                "contact": payload,
                "medians": medians(),
                "monthly": monthly_series(days),
                "types": type_composition(total_metrics(days)),
                "milestones": row.milestones(),
                "anomalies": anomalies,
            }
        )

    async def refresh(_: web.Request) -> web.Response:
        if not await runtime.trigger():
            return web.json_response(
                {
                    "ok": False,
                    "error": {
                        "code": "ANALYSIS_RUNNING",
                        "message": "分析正在进行中",
                    },
                },
                status=409,
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response(
            {"ok": True, "started": True},
            status=202,
            headers={"Cache-Control": "no-store"},
        )

    app.router.add_get("/", index)
    app.router.add_get("/contact/{hash}", contact_page)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/contacts", contacts)
    app.router.add_get("/api/contact/{hash}", contact_detail)
    app.router.add_post("/api/refresh", refresh)
    app.router.add_static("/static/", STATIC_ROOT)

    async def lifecycle(_: web.Application):
        await runtime.start()
        yield
        await runtime.close()

    app.cleanup_ctx.append(lifecycle)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="wechat-insights %(levelname)s %(message)s"
    )
    if not AUTH_TOKEN:
        LOG.warning(
            "未设置 INSIGHTS_AUTH_TOKEN：看板无鉴权，"
            "务必只把端口发布到 127.0.0.1，不要暴露公网"
        )
    runtime = InsightsRuntime(MetricsStore(DB_PATH))
    web.run_app(
        create_app(runtime),
        host=BIND_HOST,
        port=BIND_PORT,
        print=None,
        access_log=None,
        handle_signals=hasattr(signal, "SIGTERM"),
    )


if __name__ == "__main__":
    main()
