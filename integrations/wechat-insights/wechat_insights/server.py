"""aiohttp 服务：定时分析调度 + 只读看板 API + 静态资源。

所有指标都在分析阶段预计算进 scores 表，HTTP 处理器只做主键查询和一次单联系人
的小范围聚合，页面打开是秒开的。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import signal
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import web

from .analyzer import Analyzer
from .classify import KIND_VALUES
from .history import refine_limit_day
from .constants import (
    ANALYZE_TIME,
    AUTH_COOKIE,
    AUTH_TOKEN,
    BIND_HOST,
    BIND_PORT,
    DB_PATH,
    RUN_ON_START,
)
from .depth import get_depth_strategy
from .reporting import (
    generate_narrative,
    monthly_series,
    total_metrics,
    type_composition,
    yearly_report,
)
from .scoring import DIMENSION_NAMES
from .storage import ContactRow, MetricsStore


LOG = logging.getLogger("wechat-insights")

STATIC_ROOT = Path(__file__).resolve().parent / "static"
_HASH_PATTERN = re.compile(r"^[0-9a-f]{24}$")
# 分析失败后的重试间隔，避免密钥失效时每秒刷屏。
RETRY_SECONDS = 900.0


def _report_year(value: str | None) -> int:
    """解析 ?year= 参数；非法或缺失回退到当前年份。

    未来年份不特殊处理：窗口为空时年报自然全是零值，前端已禁用未来年切换。
    """

    if value is not None and re.fullmatch(r"\d{4}", value):
        year = int(value)
        if 2000 <= year <= 2100:
            return year
    return datetime.now().year


def _history_sampling(row: ContactRow) -> dict:
    """联系人的温度采样设置快照，详情接口与改粒度接口共用这一份组装。

    pending 表示「粒度是每日且还有历史没细化完」：逐日细化要跑几小时，
    前端据此显示进行中状态，而不是拿一条残缺的曲线当最终结果。细化网格
    的终点与细化任务共用 history.refine_limit_day——网格停在昨天，今天
    那个点由今日打分路径记，两处不许各算一遍；没有相识日的联系人细化
    任务的 SQL 永远不会处理（first_message_at IS NOT NULL），一律不算
    pending，否则前端会永远显示细化中。
    """

    granularity = "day" if row.history_granularity == "day" else "week"
    until = row.history_daily_until or None
    return {
        "granularity": granularity,
        "daily_until": until,
        "pending": (
            granularity == "day"
            and row.first_message_at is not None
            and (until is None or until < refine_limit_day(int(time.time())))
        ),
    }


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    """解析单个 HH:MM，非法返回 None。"""

    try:
        hour, minute = value.split(":", 1)
        parsed = (int(hour), int(minute))
    except (TypeError, ValueError):
        return None
    if not 0 <= parsed[0] < 24 or not 0 <= parsed[1] < 60:
        return None
    return parsed


def parse_analyze_times(value: str) -> tuple[tuple[int, int], ...]:
    """解析逗号分隔的 HH:MM 列表，非法项丢弃、去重升序；全非法回退 04:30。

    返回值保证非空（至少 (4, 30)），调用方可以放心对时刻组直接取 min()。
    用 str(value) 包一层是为了容忍非 str 输入，与旧实现行为一致。
    """

    valid = {
        item
        for raw in str(value).split(",")
        if (item := _parse_hhmm(raw)) is not None
    }
    if not valid:
        return ((4, 30),)
    return tuple(sorted(valid))


def next_run_at(now: float, times: tuple[tuple[int, int], ...]) -> float:
    """下一个每日分析时刻的 Unix 秒（容器本地时区）。

    times 必须非空，由 parse_analyze_times 保证；返回所有候选时刻里
    此刻之后最早的那个。
    """

    candidates = []
    current = datetime.fromtimestamp(now)
    for hour, minute in times:
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        stamp = target.timestamp()
        candidates.append(stamp if stamp > now else stamp + 86400)
    return min(candidates)


class InsightsRuntime:
    """持有 metrics.db、调度每日分析并给 API 提供状态。"""

    def __init__(self, store: MetricsStore, analyzer_factory=None):
        self.store = store
        self.analyzer_factory = analyzer_factory or (
            lambda progress_cb: Analyzer(store, progress_cb=progress_cb)
        )
        # 已去重升序的每日分析时刻，至少一项。
        self.times = parse_analyze_times(ANALYZE_TIME)
        self.running = False
        self.last_error: dict[str, str] | None = None
        self.last_duration = 0.0
        self.next_run: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        # 最近一次分析的进度快照。整体替换 dict 更新（GIL 下单条赋值原子，
        # 分析线程写、事件循环读，不需要锁），/api/progress 直接吐这份。
        self.progress: dict = {
            "running": False,
            "phase": "",
            "detail": "",
            "done": 0,
            "total": 0,
            "started_at": None,
            "finished_at": None,
            "last_result": None,
        }

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

    def _on_progress(self, fields: dict) -> None:
        """分析线程的进度回调：把阶段字段整体合进快照（整体替换，GIL 下原子）。

        cb 只在工作线程里跑、且严格夹在开跑与收尾两次写之间，不存在读改写
        交错，所以不需要锁。
        """

        self.progress = {**self.progress, **fields}

    async def analyze(self) -> bool:
        if self._lock.locked():
            return False
        async with self._lock:
            self.running = True
            self.progress = {
                "running": True,
                "phase": "sync",
                "detail": "",
                "done": 0,
                "total": 0,
                "started_at": int(time.time()),
                "finished_at": None,
                "last_result": None,
            }
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
                # 成功与异常都到这里：running 归位、记下结束时刻。
                self.running = False
                self.progress = {
                    **self.progress,
                    "running": False,
                    "finished_at": int(time.time()),
                }
            self.last_error = None
            self.last_duration = result.duration_seconds
            self.progress = {
                **self.progress,
                "last_result": {
                    "messages_read": result.messages_read,
                    "scored": result.scored,
                    "llm_scored": result.llm_scored,
                    "llm_periods": result.llm_periods,
                },
            }
            LOG.info("分析完成，用时 %.1f 秒", result.duration_seconds)
            return True

    def _run_blocking(self):
        """在工作线程里跑一轮分析；结束后释放该线程的 sqlite 连接。"""

        analyzer = self.analyzer_factory(self._on_progress)
        try:
            return analyzer.run()
        finally:
            self.store.close()

    async def _schedule_loop(self) -> None:
        # 多时刻调度语义：每次醒来只求「此刻之后最早的时刻」，跑完一轮后
        # 重新求一次；一轮分析耗时超过时刻间隔、或失败后进入 900 秒重试
        # 循环期间跨过的时刻直接跳过，不补跑、不排队——重试循环本身就是在
        # 补这一轮，重试成功后回到常规节奏、从当前时间重新求最近时刻。
        # RUN_ON_START 冷启动（last_analyzed_at() 为空）才立即跑；手动
        # /api/refresh 与定时触发共用 analyze() 的 asyncio.Lock 防抖，多
        # 时刻不改变这一行为。
        immediate = RUN_ON_START and self.last_analyzed_at() is None
        if immediate:
            LOG.info("尚无历史结果，启动后立即执行首轮全量回填")
        while True:
            if not immediate:
                now = time.time()
                self.next_run = next_run_at(now, self.times)
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


def _tokenless_url(request: web.Request) -> str:
    """同一路径、去掉 token 参数的 URL，作为 ?token= 通过后的 302 落点。"""

    pairs = [(key, value) for key, value in request.query.items() if key != "token"]
    target = request.path
    if pairs:
        target += "?" + urlencode(pairs)
    return target


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
    if from_query and request.method in ("GET", "HEAD"):
        # ?token= 只在地址栏里出现这一次：写 cookie 后 302 到不带参数的同一个
        # URL，token 不会留在浏览器历史里、也不会出现在复制出去的链接中。
        # 只对幂等的 GET/HEAD 重定向——POST 等被 302 会按规范转成 GET，会弄坏接口。
        # cookie 被拒绝/禁用时，重定向后的请求没有 token 也没有 cookie，只会得到
        # 401，不会绕回重定向，所以不存在死循环。
        response = web.HTTPFound(_tokenless_url(request))
        response.set_cookie(
            AUTH_COOKIE,
            AUTH_TOKEN,
            httponly=True,
            samesite="Lax",
            path="/",
            max_age=30 * 86400,
        )
        response.headers["Cache-Control"] = "no-store"
        raise response
    response = await handler(request)
    if from_query:
        # 非 GET/HEAD（目前只有 POST /api/refresh 与
        # POST /api/contact/{hash}/kind）没法安全重定向，那就照旧在响应里
        # 写 cookie，之后同样靠 cookie 带凭证。
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

    async def report_page(_: web.Request) -> web.FileResponse:
        return await page("report.html")

    async def _report_narrative(
        store: MetricsStore, year: int, stats: dict
    ) -> str | None:
        """年报叙事：分析没更新时直接用缓存，更新过则下次请求重新生成。

        生成在 asyncio.to_thread 里做（llm.chat 是同步阻塞调用）；失败降级
        为无叙事（返回 None），stats 照常返回。失败不写缓存，下次请求重试。
        """

        analyzed_at = runtime.last_analyzed_at()
        if analyzed_at is None:
            return None
        cache = store.get_json(f"report_narrative_{year}")
        if (
            isinstance(cache, dict)
            and cache.get("last_analyzed_at") == analyzed_at
            and isinstance(cache.get("text"), str)
            and cache["text"]
        ):
            return cache["text"]
        try:
            text = await asyncio.to_thread(generate_narrative, stats)
        except Exception:
            LOG.warning("年报叙事生成失败，本轮降级为无叙事", exc_info=True)
            return None
        if text is None:
            return None
        store.set_json(
            f"report_narrative_{year}",
            {"last_analyzed_at": analyzed_at, "text": text},
        )
        return text

    async def report(request: web.Request) -> web.Response:
        year = _report_year(request.query.get("year"))
        stats = yearly_report(store, year, int(time.time()))
        narrative = None
        # 叙事只在深度策略是 llm（接了大模型端点）时才有；全年一条消息都
        # 没有的年（例如未来的年份）不生成，没有可总结的内容。
        if get_depth_strategy().name == "llm" and stats["overview"]["messages"] > 0:
            narrative = await _report_narrative(store, year, stats)
        return no_store(
            {"ok": True, "year": year, "stats": stats, "narrative": narrative}
        )

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

    def fading() -> list:
        stored = store.get_json("fading", [])
        return stored if isinstance(stored, list) else []

    async def contacts(_: web.Request) -> web.Response:
        items = store.all_scores()
        # 异动明细保留在 payload 里：列表卡片用它渲染「N 项近期异动」角标。
        items.sort(key=lambda item: (item.get("overall") is None, -(item.get("overall") or 0)))
        return no_store(
            {
                "ok": True,
                "generated_at": runtime.last_analyzed_at(),
                "medians": medians(),
                "fading": fading(),
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
        # 关系温度曲线只下发 {day, overall}，七维存着备用、暂不上传。
        history = [
            {"day": day, "overall": overall}
            for day, overall, _dims in store.load_score_history(row.session_id)
        ]
        return no_store(
            {
                "ok": True,
                "contact": payload,
                "medians": medians(),
                "monthly": monthly_series(days),
                "types": type_composition(total_metrics(days)),
                "milestones": row.milestones(),
                "anomalies": anomalies,
                "history": history,
                "history_sampling": _history_sampling(row),
            }
        )

    async def contact_kind(request: web.Request) -> web.Response:
        """手动改判联系人的关系类型；'auto' = 清除手动、回到自动判定。

        改完立即改写该联系人的 scores payload 里的 relation_kind /
        kind_source，界面马上有反馈；下一轮分析会按新类型全面重算（事务
        往来从百分位 cohort 剔除、家人豁免归零与淡出）。改判结果同样只
        显示在响应里，联系人的 wxid 不出现在任何响应中。
        """

        value = request.match_info["hash"]
        if not _HASH_PATTERN.fullmatch(value):
            raise web.HTTPNotFound(text="not found")
        row = store.get_contact_by_hash(value)
        if row is None:
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
        try:
            body = json.loads(await request.text())
        except ValueError:
            return web.json_response(
                {
                    "ok": False,
                    "error": {"code": "BAD_REQUEST", "message": "请求体不是合法 JSON"},
                },
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        kind = body.get("kind") if isinstance(body, dict) else None
        if kind not in (*KIND_VALUES, "auto"):
            return web.json_response(
                {
                    "ok": False,
                    "error": {"code": "BAD_REQUEST", "message": "kind 取值非法"},
                },
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        if kind == "auto":
            store.set_contact_kind_manual(row.session_id, "")
            # 清除手动后回到自动判定；从未判过的自然回落到默认 friend
            # （row 是 UPDATE 前的快照，手动值不能再算进去）。
            effective = row.kind_auto or "friend"
            source = "auto" if row.kind_auto else "default"
        else:
            store.set_contact_kind_manual(row.session_id, kind)
            effective, source = kind, "manual"
        payload = store.score_by_hash(value)
        if payload is not None:
            payload["relation_kind"] = effective
            payload["kind_source"] = source
            store.update_score_payload(row.session_id, payload)
        return no_store(
            {"ok": True, "relation_kind": effective, "kind_source": source}
        )

    async def contact_feedback(request: web.Request) -> web.Response:
        """好感度标记：'up'/'down' 不立即改分，记到联系人行，下一轮分析消化；
        'clear' 清除标记并按 payload 里的 base 快照即时还原分数。

        标记本身只写 contacts 两列，分数变化发生在下一轮分析的校准消化里。
        清除是即时的：payload 里带着最近一次校准的 base 快照，按它还原
        综合分与七维，界面马上回到客观口径。标记只落在响应里，wxid 不
        出现在任何响应中。
        """

        value = request.match_info["hash"]
        if not _HASH_PATTERN.fullmatch(value):
            raise web.HTTPNotFound(text="not found")
        row = store.get_contact_by_hash(value)
        if row is None:
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
        try:
            body = json.loads(await request.text())
        except ValueError:
            return web.json_response(
                {
                    "ok": False,
                    "error": {"code": "BAD_REQUEST", "message": "请求体不是合法 JSON"},
                },
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        action = body.get("action") if isinstance(body, dict) else None
        if action not in ("up", "down", "clear"):
            return web.json_response(
                {
                    "ok": False,
                    "error": {"code": "BAD_REQUEST", "message": "action 取值非法"},
                },
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        payload = store.score_by_hash(value)
        if action == "clear":
            store.set_contact_feedback(row.session_id, "", "")
            store.set_contact_calibration(row.session_id, "")
            if payload is not None:
                calibration = payload.get("calibration")
                if isinstance(calibration, dict) and isinstance(
                    calibration.get("base"), dict
                ):
                    base = calibration["base"]
                    if "overall" in base:
                        payload["overall"] = base["overall"]
                    if isinstance(base.get("dimensions"), dict):
                        payload["dimensions"] = base["dimensions"]
                payload.pop("calibration", None)
                payload.pop("calibration_pending", None)
                store.update_score_payload(row.session_id, payload)
            return no_store({"ok": True, "pending": None, "cleared": True})
        store.set_contact_feedback(row.session_id, action, str(int(time.time())))
        if payload is not None:
            payload["calibration_pending"] = action
            store.update_score_payload(row.session_id, payload)
        return no_store({"ok": True, "pending": action})

    async def contact_breakup(request: web.Request) -> web.Response:
        """绝交标记：'mark' 记下日期与置信度，下一轮分析核实；'clear' 清除
        标记并按 payload 里的 base 快照即时还原分数。

        标记本身只写 contacts 两列，分数变化发生在下一轮分析的核实里。
        清除是即时的：payload 里带着最近一次绝交的 base 快照，按它还原
        综合分与七维，界面马上回到绝交前的口径。重新标记时旧结论同时从
        contacts 与 payload 里撤下，避免旧封顶在核实之前一直压着分数。
        标记只落在响应里，wxid 不出现在任何响应中。
        """

        value = request.match_info["hash"]
        if not _HASH_PATTERN.fullmatch(value):
            raise web.HTTPNotFound(text="not found")
        row = store.get_contact_by_hash(value)
        if row is None:
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
        try:
            body = json.loads(await request.text())
        except ValueError:
            return web.json_response(
                {
                    "ok": False,
                    "error": {"code": "BAD_REQUEST", "message": "请求体不是合法 JSON"},
                },
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        action = body.get("action") if isinstance(body, dict) else None
        if action not in ("mark", "clear"):
            return web.json_response(
                {
                    "ok": False,
                    "error": {"code": "BAD_REQUEST", "message": "action 取值非法"},
                },
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        payload = store.score_by_hash(value)
        if action == "clear":
            store.set_contact_breakup(row.session_id, "")
            store.set_contact_breakup_pending(row.session_id, "")
            if payload is not None:
                breakup = payload.get("breakup")
                if isinstance(breakup, dict) and isinstance(
                    breakup.get("base"), dict
                ):
                    base = breakup["base"]
                    if "overall" in base:
                        payload["overall"] = base["overall"]
                    if isinstance(base.get("dimensions"), dict):
                        payload["dimensions"] = base["dimensions"]
                payload.pop("breakup", None)
                payload.pop("breakup_pending", None)
                store.update_score_payload(row.session_id, payload)
            return no_store({"ok": True, "cleared": True})
        date = body.get("date") if isinstance(body, dict) else None
        certainty = body.get("certainty") if isinstance(body, dict) else None
        if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return web.json_response(
                {
                    "ok": False,
                    "error": {
                        "code": "BAD_REQUEST",
                        "message": "日期格式应为 YYYY-MM-DD",
                    },
                },
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        try:
            time.strptime(date, "%Y-%m-%d")
        except ValueError:
            return web.json_response(
                {
                    "ok": False,
                    "error": {
                        "code": "BAD_REQUEST",
                        "message": "日期格式应为 YYYY-MM-DD",
                    },
                },
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        if date > datetime.now().date().isoformat():
            return web.json_response(
                {
                    "ok": False,
                    "error": {
                        "code": "BAD_REQUEST",
                        "message": "绝交日期不能在未来",
                    },
                },
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        if certainty not in ("certain", "suspected"):
            return web.json_response(
                {
                    "ok": False,
                    "error": {
                        "code": "BAD_REQUEST",
                        "message": "certainty 取值非法",
                    },
                },
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        store.set_contact_breakup_pending(
            row.session_id,
            json.dumps(
                {"date": date, "certainty": certainty, "at": int(time.time())},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        # 重新标记要把旧结论从看板上撤下来：contacts.breakup 列同时清空，
        # 否则旧封顶会在新标记核实之前一直压着分数。
        store.set_contact_breakup(row.session_id, "")
        if payload is not None:
            payload["breakup_pending"] = {"date": date, "certainty": certainty}
            payload.pop("breakup", None)
            store.update_score_payload(row.session_id, payload)
        return no_store(
            {"ok": True, "pending": {"date": date, "certainty": certainty}}
        )

    async def contact_history(request: web.Request) -> web.Response:
        """切换联系人的温度采样粒度：'day' = 逐日细化，'week' = 每周（默认）。

        切到 day 后，下一轮分析从相识日起逐日补点（全史约两小时，断点
        续跑）；切回 week 不删除任何已细化的日点——细节已经算出来了，
        删了再切回来又要重算，留着无害。
        """

        value = request.match_info["hash"]
        if not _HASH_PATTERN.fullmatch(value):
            raise web.HTTPNotFound(text="not found")
        row = store.get_contact_by_hash(value)
        if row is None:
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
        try:
            body = json.loads(await request.text())
        except ValueError:
            return web.json_response(
                {
                    "ok": False,
                    "error": {"code": "BAD_REQUEST", "message": "请求体不是合法 JSON"},
                },
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        granularity = (
            body.get("granularity") if isinstance(body, dict) else None
        )
        if granularity not in ("week", "day"):
            return web.json_response(
                {
                    "ok": False,
                    "error": {
                        "code": "BAD_REQUEST",
                        "message": "granularity 取值非法",
                    },
                },
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        store.set_history_granularity(
            row.session_id, "day" if granularity == "day" else ""
        )
        updated = store.get_contact_by_hash(value)
        return no_store(
            {"ok": True, "history_sampling": _history_sampling(updated)}
        )

    async def progress(_: web.Request) -> web.Response:
        # 快照是整体替换的，事件循环里直接读不需要拷贝。
        return no_store({"ok": True, "progress": runtime.progress})

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
    app.router.add_get("/report", report_page)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/contacts", contacts)
    app.router.add_get("/api/contact/{hash}", contact_detail)
    app.router.add_post("/api/contact/{hash}/kind", contact_kind)
    app.router.add_post("/api/contact/{hash}/history", contact_history)
    app.router.add_post("/api/contact/{hash}/feedback", contact_feedback)
    app.router.add_post("/api/contact/{hash}/breakup", contact_breakup)
    app.router.add_get("/api/report", report)
    app.router.add_get("/api/progress", progress)
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
