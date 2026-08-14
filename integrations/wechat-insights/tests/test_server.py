from __future__ import annotations

import asyncio
import json
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import AioHTTPTestCase

from wechat_insights.analyzer import AnalysisResult
from wechat_insights.history import refine_limit_day
from wechat_insights.metrics import Metrics, day_key
from wechat_insights.server import (
    InsightsRuntime,
    create_app,
    next_run_at,
    parse_analyze_times,
)
from wechat_insights.storage import MetricsStore, contact_hash


SESSION_ID = "friend"
HASH = contact_hash(SESSION_ID)


class FakeAnalyzer:
    """跑一轮就返回的假分析器：按真实节奏上报 sync → score 阶段。"""

    def __init__(self, progress_cb=None):
        self.progress_cb = progress_cb
        self.result = AnalysisResult(
            started_at=1_700_000_000,
            duration_seconds=1.5,
            messages_read=12,
            scored=7,
            llm_scored=3,
        )

    def run(self):
        if self.progress_cb is not None:
            self.progress_cb({"phase": "sync", "done": 0, "total": 1, "detail": ""})
            self.progress_cb(
                {"phase": "sync", "done": 1, "total": 1, "detail": "Alice"}
            )
            self.progress_cb({"phase": "score", "done": 0, "total": 0, "detail": ""})
        return self.result


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


#: 绝交标记接口测试共用的日期与置信度。
BREAKUP_MARK = {"date": "2020-01-01", "certainty": "certain"}

#: 一条 confirmed/quarrel/llm 的绝交结论（测试用）。
BREAKUP_VERDICT = {
    "verdict": "confirmed",
    "kind": "quarrel",
    "date": "2020-01-01",
    "certainty": "certain",
    "note": "",
    "decided_at": 1700000001,
    "source": "llm",
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
        self.assertEqual(parse_analyze_times("06:15"), ((6, 15),))
        self.assertEqual(parse_analyze_times("25:00"), ((4, 30),))
        self.assertEqual(parse_analyze_times("nonsense"), ((4, 30),))

    def test_next_run_is_always_in_the_future(self) -> None:
        now = 1_700_000_000.0
        self.assertGreater(next_run_at(now, ((4, 30),)), now)
        self.assertLessEqual(next_run_at(now, ((4, 30),)) - now, 86400)

    def test_analyze_times_parses_multiple_sorted_and_deduped(self) -> None:
        self.assertEqual(
            parse_analyze_times("04:30,12:30,20:30"),
            ((4, 30), (12, 30), (20, 30)),
        )
        self.assertEqual(
            parse_analyze_times("20:30, 04:30 ,12:30"),
            ((4, 30), (12, 30), (20, 30)),
        )
        self.assertEqual(parse_analyze_times("04:30,4:30,04:30"), ((4, 30),))

    def test_analyze_times_keeps_the_valid_subset(self) -> None:
        self.assertEqual(
            parse_analyze_times("04:30,99:99,12:30"),
            ((4, 30), (12, 30)),
        )
        self.assertEqual(parse_analyze_times("04:30,,12:30"), ((4, 30), (12, 30)))
        self.assertEqual(parse_analyze_times("04:30,12:30:00"), ((4, 30),))
        self.assertEqual(parse_analyze_times("nope,25:61"), ((4, 30),))

    def test_analyze_times_never_returns_empty(self) -> None:
        for raw in ("", ",", ",,", "nonsense", "24:00", "-1:30", "04:70", "1:2:3", None):
            parsed = parse_analyze_times(raw)
            self.assertIsInstance(parsed, tuple)
            self.assertTrue(parsed)
            for hour, minute in parsed:
                self.assertTrue(0 <= hour < 24)
                self.assertTrue(0 <= minute < 60)

    def test_next_run_picks_the_earliest_upcoming_time(self) -> None:
        # 固定基准日 2026-06-15，本地朴素时间构造，不依赖运行时区。
        day = datetime(2026, 6, 15).timestamp()
        times = ((4, 30), (12, 30), (20, 30))

        def expected_at(hour: int, minute: int) -> float:
            return datetime(2026, 6, 15, hour, minute).timestamp()

        def expected_next_day(hour: int, minute: int) -> float:
            return datetime(2026, 6, 16, hour, minute).timestamp()

        self.assertEqual(next_run_at(day + 5 * 3600, times), expected_at(12, 30))
        self.assertEqual(next_run_at(day + 13 * 3600, times), expected_at(20, 30))
        self.assertEqual(next_run_at(day + 23 * 3600, times), expected_next_day(4, 30))
        # 正好落在时刻上要跳到下一个。
        self.assertEqual(next_run_at(day + 12.5 * 3600, times), expected_at(20, 30))
        # 单时刻退化情形。
        self.assertEqual(
            next_run_at(day + 5 * 3600, ((4, 30),)), expected_next_day(4, 30)
        )

    def test_runtime_reads_the_configured_times(self) -> None:
        with patch("wechat_insights.server.ANALYZE_TIME", "20:30,04:30,12:30"):
            runtime = InsightsRuntime(self.store, analyzer_factory=lambda: None)
        self.assertEqual(runtime.times, ((4, 30), (12, 30), (20, 30)))

    async def test_schedule_loop_waits_for_the_earliest_slot(self) -> None:
        # 独立的 runtime：create_app 的 cleanup_ctx 已经给 self.runtime 起了
        # 一个真实循环，这里再起一个会多开一条调度任务。asyncSetUp 已写入
        # last_analyzed_at，immediate 必为 False，循环第一件事就是算 next_run
        # 并 sleep，不会调用分析器。
        with patch("wechat_insights.server.ANALYZE_TIME", "04:30,12:30,20:30"):
            runtime = InsightsRuntime(self.store, analyzer_factory=lambda: None)
        started = time.time()
        await runtime.start()
        try:
            for _ in range(100):
                if runtime.next_run is not None:
                    break
                await asyncio.sleep(0)
            self.assertIsNotNone(runtime.next_run)
            moment = datetime.fromtimestamp(runtime.next_run)
            self.assertIn((moment.hour, moment.minute), {(4, 30), (12, 30), (20, 30)})
            self.assertGreater(runtime.next_run, started)
            self.assertLessEqual(runtime.next_run - started, 9 * 3600)
        finally:
            await runtime.close()


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
        # 相识日按真实分析过的联系人补上：没有相识日的联系人细化任务永远
        # 不会处理（见 test_detail_pending_false_without_a_first_message）。
        contact = self.store.get_contact(SESSION_ID)
        contact.first_message_at = 1_700_000_000
        self.store.save_contact(contact)
        self.store.save_scores([(SESSION_ID, payload())])
        self.store.set_json("medians", {"responsiveness": 50.0})
        bucket = Metrics()
        bucket.add("msgs_them", 4)
        bucket.add("kind_text_them", 4)
        bucket.add_reply("incoming", 30)
        bucket.add_reply("incoming", 45)
        bucket.add_reply("incoming", 60)
        self.store.merge_daily(SESSION_ID, {"2026-03-10": bucket})
        # 分析器工厂在测试里记录最后创建的实例，进度测试用它驱动一轮。
        self.last_analyzer = None

        def factory(progress_cb):
            self.last_analyzer = FakeAnalyzer(progress_cb)
            return self.last_analyzer

        self.runtime = InsightsRuntime(self.store, analyzer_factory=factory)
        await super().asyncSetUp()

    async def test_status_reports_the_last_analysis(self) -> None:
        body = await (await self.client.get("/api/status")).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["last_analyzed_at"], 1_700_000_000)
        self.assertIsNotNone(body["last_analyzed_iso"])
        self.assertFalse(body["running"])
        self.assertEqual(body["contacts"], 1)
        self.assertEqual(body["scored_contacts"], 1)

    async def test_status_reports_the_next_run_timestamp(self) -> None:
        # 先等应用生命周期里的循环写入自己的 next_run，避免断言撞上它的
        # 写入；之后循环已停在数小时的 asyncio.sleep 上，不会回写。
        for _ in range(100):
            if self.runtime.next_run is not None:
                break
            await asyncio.sleep(0)
        self.assertIsNotNone(self.runtime.next_run)
        self.runtime.next_run = 1_700_000_123.7
        body = await (await self.client.get("/api/status")).json()
        self.assertEqual(body["next_run_at"], 1_700_000_123)

    async def test_contact_list_keeps_anomalies_and_fills_missing_medians(self) -> None:
        body = await (await self.client.get("/api/contacts")).json()
        self.assertEqual(len(body["items"]), 1)
        # 异动明细留在 payload：列表卡片用数量渲染「N 项近期异动」角标。
        self.assertEqual(len(body["items"][0]["anomalies"]), 1)
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

    async def test_detail_includes_the_score_history_ascending(self) -> None:
        self.store.record_score_history(
            "2026-03-08", [(SESSION_ID, 70.5, '{"responsiveness":80.0}')]
        )
        self.store.record_score_history(
            "2026-03-09", [(SESSION_ID, 68.0, '{"responsiveness":75.0}')]
        )
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        # 只下发 day/overall，dims 不下发。
        self.assertEqual(
            body["history"],
            [
                {"day": "2026-03-08", "overall": 70.5},
                {"day": "2026-03-09", "overall": 68.0},
            ],
        )

    async def test_detail_history_defaults_to_empty(self) -> None:
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        self.assertEqual(body["history"], [])

    async def test_contact_list_includes_fading(self) -> None:
        self.store.set_json(
            "fading",
            [{"hash": HASH, "display_name": "Alice", "gap_days": 20, "overall": 70.0}],
        )
        body = await (await self.client.get("/api/contacts")).json()
        self.assertEqual(
            body["fading"], [{"hash": HASH, "display_name": "Alice", "gap_days": 20, "overall": 70.0}]
        )

    async def test_contact_list_fading_defaults_to_empty(self) -> None:
        body = await (await self.client.get("/api/contacts")).json()
        self.assertEqual(body["fading"], [])

    async def test_unknown_contact_returns_a_typed_404(self) -> None:
        response = await self.client.get(f"/api/contact/{'0' * 24}")
        self.assertEqual(response.status, 404)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "CONTACT_NOT_FOUND")

    async def test_malformed_hash_is_rejected(self) -> None:
        self.assertEqual((await self.client.get("/api/contact/nope")).status, 404)

    async def test_contact_kind_override_updates_row_and_payload(self) -> None:
        response = await self.client.post(
            f"/api/contact/{HASH}/kind", json={"kind": "transactional"}
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["relation_kind"], "transactional")
        self.assertEqual(body["kind_source"], "manual")
        # 联系人行落库手动类型。
        self.assertEqual(
            self.store.get_contact(SESSION_ID).kind_manual, "transactional"
        )
        # scores payload 就地改写：列表页与详情页马上能看到新类型，不用等
        # 下一轮分析（下一轮会完全重算，这里的改写只是即时反馈）。
        stored = self.store.score_by_hash(HASH)
        self.assertEqual(stored["relation_kind"], "transactional")
        self.assertEqual(stored["kind_source"], "manual")
        # 只改这一个联系人，不碰其他行。
        self.assertEqual(len(self.store.all_scores()), 1)

    async def test_contact_kind_auto_clears_the_manual_override(self) -> None:
        self.store.set_contact_kind_auto(SESSION_ID, "family")
        self.store.set_contact_kind_manual(SESSION_ID, "transactional")
        response = await self.client.post(
            f"/api/contact/{HASH}/kind", json={"kind": "auto"}
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        # 清除手动后回到自动判定结果（没有自动判定时落默认 friend）。
        self.assertEqual(body["relation_kind"], "family")
        self.assertEqual(body["kind_source"], "auto")
        self.assertEqual(self.store.get_contact(SESSION_ID).kind_manual, "")
        stored = self.store.score_by_hash(HASH)
        self.assertEqual(
            (stored["relation_kind"], stored["kind_source"]), ("family", "auto")
        )

    async def test_contact_kind_auto_falls_back_to_friend_when_never_judged(self) -> None:
        response = await self.client.post(
            f"/api/contact/{HASH}/kind", json={"kind": "auto"}
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["relation_kind"], "friend")
        self.assertEqual(body["kind_source"], "default")
        self.assertEqual(self.store.get_contact(SESSION_ID).kind_manual, "")

    async def test_contact_kind_rejects_invalid_values(self) -> None:
        for bad in ("boss", "FRIEND", "", 42, None):
            response = await self.client.post(
                f"/api/contact/{HASH}/kind", json={"kind": bad}
            )
            self.assertEqual(response.status, 400)
            body = await response.json()
            self.assertEqual(body["error"]["code"], "BAD_REQUEST")
        # 手动改判只接受 JSON body。
        self.assertEqual(
            (await self.client.post(f"/api/contact/{HASH}/kind")).status, 400
        )

    async def test_contact_kind_requires_an_existing_contact(self) -> None:
        response = await self.client.post(
            f"/api/contact/{'0' * 24}/kind", json={"kind": "family"}
        )
        self.assertEqual(response.status, 404)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "CONTACT_NOT_FOUND")
        self.assertEqual(
            (await self.client.post("/api/contact/nope/kind", json={"kind": "family"})).status,
            404,
        )

    async def test_contact_feedback_marks_and_updates_payload(self) -> None:
        response = await self.client.post(
            f"/api/contact/{HASH}/feedback", json={"action": "up"}
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["pending"], "up")
        # 标记写进联系人行：下一轮分析消化。
        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.feedback_pending, "up")
        self.assertNotEqual(contact.feedback_pending_at, "")
        # payload 就地写入排队提示：详情页与列表页马上可见。
        stored = self.store.score_by_hash(HASH)
        self.assertEqual(stored["calibration_pending"], "up")
        # 标记本身不立即改分：overall 保持原值。
        self.assertEqual(stored["overall"], 73.4)
        self.assertNotIn("calibration", stored)

    async def test_contact_feedback_rejects_invalid_values(self) -> None:
        for bad in ("raise", "UP", "", 42, None):
            response = await self.client.post(
                f"/api/contact/{HASH}/feedback", json={"action": bad}
            )
            self.assertEqual(response.status, 400)
            body = await response.json()
            self.assertEqual(body["error"]["code"], "BAD_REQUEST")
        # 只接受 JSON body。
        self.assertEqual(
            (await self.client.post(f"/api/contact/{HASH}/feedback")).status, 400
        )

    async def test_contact_feedback_requires_an_existing_contact(self) -> None:
        response = await self.client.post(
            f"/api/contact/{'0' * 24}/feedback", json={"action": "up"}
        )
        self.assertEqual(response.status, 404)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "CONTACT_NOT_FOUND")
        self.assertEqual(
            (
                await self.client.post(
                    "/api/contact/nope/feedback", json={"action": "up"}
                )
            ).status,
            404,
        )

    async def test_contact_feedback_clear_restores_the_base_score(self) -> None:
        # 模拟「已消化过一轮」的完整状态：校准生效 + 标记排队。
        self.store.set_contact_feedback(SESSION_ID, "up", "1700000001")
        self.store.set_contact_calibration(
            SESSION_ID,
            json.dumps(
                {
                    "dims": {"investment": 4.0},
                    "updated_at": 1700000001,
                    "source": "llm",
                    "note": "",
                }
            ),
        )
        stored = self.store.score_by_hash(HASH)
        stored["calibration"] = {
            "offsets": {"investment": 4.0},
            "overall_delta": 0.6,
            "base": {"overall": 73.4, "dimensions": dict(stored["dimensions"])},
            "note": None,
            "updated_at": 1700000001,
        }
        stored["calibration_pending"] = "up"
        self.store.update_score_payload(SESSION_ID, stored)
        response = await self.client.post(
            f"/api/contact/{HASH}/feedback", json={"action": "clear"}
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["ok"])
        self.assertIsNone(body["pending"])
        self.assertTrue(body["cleared"])
        # 联系人行的标记与校准都清空。
        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual(contact.feedback_pending, "")
        self.assertEqual(contact.calibration, "")
        # payload 按 base 快照还原：综合分与七维回客观口径，角标消失。
        after = self.store.score_by_hash(HASH)
        self.assertEqual(after["overall"], 73.4)
        self.assertEqual(after["dimensions"], stored["calibration"]["base"]["dimensions"])
        self.assertNotIn("calibration", after)
        self.assertNotIn("calibration_pending", after)

    async def test_contact_breakup_mark_writes_pending_and_updates_payload(self) -> None:
        response = await self.client.post(
            f"/api/contact/{HASH}/breakup", json={"action": "mark", **BREAKUP_MARK}
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["pending"], BREAKUP_MARK)
        # 标记写进联系人行（下一轮分析核实），payload 就地写排队提示。
        pending = self.store.get_contact(SESSION_ID).breakup_pending_data()
        self.assertEqual((pending["date"], pending["certainty"]), ("2020-01-01", "certain"))
        self.assertIsInstance(pending["at"], int)
        self.assertEqual(self.store.get_contact(SESSION_ID).breakup, "")
        stored = self.store.score_by_hash(HASH)
        self.assertEqual(stored["breakup_pending"], BREAKUP_MARK)
        # 标记本身不立即改分。
        self.assertEqual(stored["overall"], 73.4)
        self.assertNotIn("breakup", stored)

    async def test_contact_breakup_remark_pulls_down_the_old_conclusion(self) -> None:
        # 重新标记要撤下旧结论：旧封顶不会在新标记核实之前一直压着分数。
        self.store.set_contact_breakup(SESSION_ID, json.dumps(BREAKUP_VERDICT))
        stored = self.store.score_by_hash(HASH)
        stored["breakup"] = {
            **BREAKUP_VERDICT,
            "overall_delta": -63.4,
            "base": {"overall": 73.4, "dimensions": dict(stored["dimensions"])},
        }
        self.store.update_score_payload(SESSION_ID, stored)
        response = await self.client.post(
            f"/api/contact/{HASH}/breakup",
            json={"action": "mark", "date": "2020-01-02", "certainty": "suspected"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.store.get_contact(SESSION_ID).breakup, "")
        after = self.store.score_by_hash(HASH)
        self.assertNotIn("breakup", after)
        self.assertEqual(
            after["breakup_pending"], {"date": "2020-01-02", "certainty": "suspected"}
        )

    async def test_contact_breakup_clear_restores_the_base_score(self) -> None:
        # 模拟「已核实过一轮」的完整状态：结论生效 + 标记排队。
        self.store.set_contact_breakup_pending(
            SESSION_ID, json.dumps({**BREAKUP_MARK, "at": 1700000001})
        )
        self.store.set_contact_breakup(SESSION_ID, json.dumps(BREAKUP_VERDICT))
        stored = self.store.score_by_hash(HASH)
        stored["breakup"] = {
            **BREAKUP_VERDICT,
            "overall_delta": -63.4,
            "base": {"overall": 73.4, "dimensions": dict(stored["dimensions"])},
        }
        stored["breakup_pending"] = BREAKUP_MARK
        stored["overall"] = 10.0
        self.store.update_score_payload(SESSION_ID, stored)
        response = await self.client.post(
            f"/api/contact/{HASH}/breakup", json={"action": "clear"}
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual((body["ok"], body["cleared"]), (True, True))
        # 联系人行的标记与结论都清空，payload 按 base 快照还原。
        contact = self.store.get_contact(SESSION_ID)
        self.assertEqual((contact.breakup, contact.breakup_pending), ("", ""))
        after = self.store.score_by_hash(HASH)
        self.assertEqual(after["overall"], 73.4)
        self.assertEqual(after["dimensions"], stored["breakup"]["base"]["dimensions"])
        self.assertNotIn("breakup", after)
        self.assertNotIn("breakup_pending", after)

    async def test_contact_breakup_rejects_invalid_action_and_certainty(self) -> None:
        for bad in ("down", "MARK", "", 42, None):
            response = await self.client.post(
                f"/api/contact/{HASH}/breakup", json={"action": bad}
            )
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())["error"]["code"], "BAD_REQUEST")
        for bad in ("definitely", "CERTAIN", "", 42, None):
            response = await self.client.post(
                f"/api/contact/{HASH}/breakup",
                json={"action": "mark", "date": "2020-01-01", "certainty": bad},
            )
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())["error"]["code"], "BAD_REQUEST")
        self.assertEqual(
            (await self.client.post(f"/api/contact/{HASH}/breakup")).status, 400
        )

    async def test_contact_breakup_rejects_bad_and_future_dates(self) -> None:
        for bad in ("2020/01/01", "not-a-date", "2020-13-40", "", None):
            response = await self.client.post(
                f"/api/contact/{HASH}/breakup",
                json={"action": "mark", "date": bad, "certainty": "certain"},
            )
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())["error"]["message"], "日期格式应为 YYYY-MM-DD")
        future = (datetime.now() + timedelta(days=30)).date().isoformat()
        response = await self.client.post(
            f"/api/contact/{HASH}/breakup",
            json={"action": "mark", "date": future, "certainty": "certain"},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["message"], "绝交日期不能在未来")

    async def test_contact_breakup_requires_an_existing_contact(self) -> None:
        response = await self.client.post(
            f"/api/contact/{'0' * 24}/breakup", json={"action": "mark", **BREAKUP_MARK}
        )
        self.assertEqual(response.status, 404)
        self.assertEqual((await response.json())["error"]["code"], "CONTACT_NOT_FOUND")
        self.assertEqual(
            (
                await self.client.post(
                    "/api/contact/nope/breakup", json={"action": "mark", **BREAKUP_MARK}
                )
            ).status,
            404,
        )

    async def test_contact_history_switches_granularity(self) -> None:
        response = await self.client.post(
            f"/api/contact/{HASH}/history", json={"granularity": "day"}
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(
            body["history_sampling"],
            {"granularity": "day", "daily_until": None, "pending": True},
        )
        self.assertEqual(self.store.get_contact(SESSION_ID).history_granularity, "day")

        # 切回每周：粒度写回空串；已细化的日点不删（库侧没有删除逻辑）。
        response = await self.client.post(
            f"/api/contact/{HASH}/history", json={"granularity": "week"}
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["history_sampling"]["granularity"], "week")
        self.assertFalse(body["history_sampling"]["pending"])
        self.assertEqual(self.store.get_contact(SESSION_ID).history_granularity, "")

    async def test_contact_history_rejects_invalid_values(self) -> None:
        for bad in ("daily", "DAY", "", 42, None):
            response = await self.client.post(
                f"/api/contact/{HASH}/history", json={"granularity": bad}
            )
            self.assertEqual(response.status, 400)
            body = await response.json()
            self.assertEqual(body["error"]["code"], "BAD_REQUEST")
            self.assertEqual(body["error"]["message"], "granularity 取值非法")
        # 只接受 JSON body。
        self.assertEqual(
            (await self.client.post(f"/api/contact/{HASH}/history")).status, 400
        )

    async def test_contact_history_requires_an_existing_contact(self) -> None:
        response = await self.client.post(
            f"/api/contact/{'0' * 24}/history", json={"granularity": "day"}
        )
        self.assertEqual(response.status, 404)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "CONTACT_NOT_FOUND")
        self.assertEqual(
            (
                await self.client.post(
                    "/api/contact/nope/history", json={"granularity": "day"}
                )
            ).status,
            404,
        )

    async def test_detail_reports_history_sampling_from_the_store(self) -> None:
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        self.assertEqual(
            body["history_sampling"],
            {"granularity": "week", "daily_until": None, "pending": False},
        )

        # 切到每日但还没开始细化：pending 为真。
        self.store.set_history_granularity(SESSION_ID, "day")
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        self.assertEqual(body["history_sampling"]["granularity"], "day")
        self.assertTrue(body["history_sampling"]["pending"])

        # 细化已推进到今天：pending 为假，daily_until 是今天的日键。
        today = day_key(int(time.time()))
        self.store.mark_daily_refined([SESSION_ID], today)
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        self.assertEqual(
            body["history_sampling"],
            {"granularity": "day", "daily_until": today, "pending": False},
        )

        # 细化推进到昨天（真实终态，边界与细化任务共用 refine_limit_day）：
        # 依旧不 pending。
        yesterday = refine_limit_day(int(time.time()))
        self.store.mark_daily_refined([SESSION_ID], yesterday)
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        self.assertEqual(
            body["history_sampling"],
            {"granularity": "day", "daily_until": yesterday, "pending": False},
        )

        # 进度停在更早（前天）：还有历史没细化完，pending 为真。
        before = (date.fromisoformat(yesterday) - timedelta(days=1)).isoformat()
        self.store.mark_daily_refined([SESSION_ID], before)
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        self.assertEqual(
            body["history_sampling"],
            {"granularity": "day", "daily_until": before, "pending": True},
        )

    async def test_detail_pending_false_without_a_first_message(self) -> None:
        # 另建一个空联系人（ensure_contact 不写 first_message_at，永远是
        # None）。细化任务的 SQL 要求相识日非空，永远不会处理这样的联系人，
        # 切到每日也不能显示「细化中」，否则前端永远转圈。
        empty_session = "stranger"
        empty_hash = contact_hash(empty_session)
        self.store.ensure_contact(empty_session, "Stranger")
        empty_payload = payload()
        empty_payload["hash"] = empty_hash
        self.store.update_score_payload(empty_session, empty_payload)
        self.store.set_history_granularity(empty_session, "day")
        body = await (await self.client.get(f"/api/contact/{empty_hash}")).json()
        self.assertEqual(body["history_sampling"]["granularity"], "day")
        self.assertFalse(body["history_sampling"]["pending"])

    # 绝交截断：只有 confirmed 结论才在 detail 下发时截断曲线，且只在真的
    # 截掉了东西时才带 history_cutoff（前端据此画「绝交」标记线）。
    def _record_history(self, *points) -> None:
        """测试用的温度采样点：按 (day, overall) 逐个写入 score_history。"""
        for day, overall in points:
            self.store.record_score_history(day, [(SESSION_ID, overall, "")])

    async def test_detail_cutoff_none_without_a_breakup(self) -> None:
        self._record_history(("2026-03-08", 70.5), ("2026-03-09", 68.0))
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        self.assertEqual(len(body["history"]), 2)
        self.assertIsNone(body["history_cutoff"])

    async def test_detail_cutoff_keeps_points_through_the_breakup_day(self) -> None:
        # 截断包含绝交日当天：只保留 day <= date 的点，cutoff 带齐三个字段。
        self._record_history(
            ("2026-03-08", 70.5), ("2026-03-09", 68.0), ("2026-03-10", 66.0)
        )
        self.store.set_contact_breakup(
            SESSION_ID, json.dumps({**BREAKUP_VERDICT, "date": "2026-03-09"})
        )
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        self.assertEqual(
            body["history"],
            [
                {"day": "2026-03-08", "overall": 70.5},
                {"day": "2026-03-09", "overall": 68.0},
            ],
        )
        self.assertEqual(
            body["history_cutoff"],
            {"day": "2026-03-09", "kind": "quarrel", "certainty": "certain"},
        )

    async def test_detail_cutoff_none_when_verdict_is_rejected(self) -> None:
        # 核实否决：绝交没成立，曲线照常完整下发。
        self._record_history(("2026-03-08", 70.5), ("2026-03-09", 68.0))
        self.store.set_contact_breakup(
            SESSION_ID,
            json.dumps({**BREAKUP_VERDICT, "verdict": "rejected", "kind": ""}),
        )
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        self.assertEqual(len(body["history"]), 2)
        self.assertIsNone(body["history_cutoff"])

    async def test_detail_cutoff_none_with_pending_only(self) -> None:
        # 只有未核实的标记、没有结论：不截断。
        self._record_history(("2026-03-08", 70.5), ("2026-03-09", 68.0))
        self.store.set_contact_breakup_pending(SESSION_ID, json.dumps(BREAKUP_MARK))
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        self.assertEqual(len(body["history"]), 2)
        self.assertIsNone(body["history_cutoff"])

    async def test_detail_cutoff_none_when_breakup_day_past_the_last_point(self) -> None:
        # 绝交日晚于最后一个采样点：什么都没截掉，不下发 cutoff，免得前端
        # 在数据范围外画标记线撑坏 time 轴。
        self._record_history(("2026-03-08", 70.5), ("2026-03-09", 68.0))
        self.store.set_contact_breakup(
            SESSION_ID, json.dumps({**BREAKUP_VERDICT, "date": "2026-03-20"})
        )
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        self.assertEqual(len(body["history"]), 2)
        self.assertIsNone(body["history_cutoff"])

    async def test_detail_cutoff_empties_history_when_before_the_first_point(self) -> None:
        # 绝交日早于第一个采样点：曲线为空，但 cutoff 仍要下发，前端据此
        # 渲染「没有可显示的区间」而不是整卡消失。
        self._record_history(("2026-03-08", 70.5), ("2026-03-09", 68.0))
        self.store.set_contact_breakup(
            SESSION_ID, json.dumps({**BREAKUP_VERDICT, "date": "2026-03-01"})
        )
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        self.assertEqual(body["history"], [])
        self.assertEqual(body["history_cutoff"]["day"], "2026-03-01")

    async def test_detail_cutoff_ignores_a_dirty_breakup_date(self) -> None:
        # 脏数据防御：日期形状不对或缺失都不截断、不抛异常。
        verdicts = [
            {**BREAKUP_VERDICT, "date": "not-a-date"},
            {k: v for k, v in BREAKUP_VERDICT.items() if k != "date"},
        ]
        self._record_history(("2026-03-08", 70.5), ("2026-03-09", 68.0))
        for verdict in verdicts:
            self.store.set_contact_breakup(SESSION_ID, json.dumps(verdict))
            body = await (await self.client.get(f"/api/contact/{HASH}")).json()
            self.assertEqual(len(body["history"]), 2)
            self.assertIsNone(body["history_cutoff"])

    async def test_detail_cutoff_keeps_score_history_rows_intact(self) -> None:
        # 截断只发生在下发时：请求 detail 之后库里的采样点一条不少，
        # 清除绝交标记后完整曲线能立刻恢复。
        self._record_history(
            ("2026-03-08", 70.5), ("2026-03-09", 68.0), ("2026-03-10", 66.0)
        )
        self.store.set_contact_breakup(
            SESSION_ID, json.dumps({**BREAKUP_VERDICT, "date": "2026-03-09"})
        )
        before = self.store.load_score_history(SESSION_ID)
        body = await (await self.client.get(f"/api/contact/{HASH}")).json()
        self.assertEqual(len(body["history"]), 2)
        self.assertEqual(self.store.load_score_history(SESSION_ID), before)

    async def test_progress_reports_idle_state_by_default(self) -> None:
        body = await (await self.client.get("/api/progress")).json()
        self.assertTrue(body["ok"])
        progress = body["progress"]
        self.assertFalse(progress["running"])
        self.assertEqual(progress["phase"], "")
        self.assertIsNone(progress["started_at"])
        self.assertIsNone(progress["finished_at"])
        self.assertIsNone(progress["last_result"])

    async def test_progress_snapshot_after_an_analysis_round(self) -> None:
        # 驱动一轮真实 analyze：工厂把 cb 传给假分析器，阶段字段合进快照。
        self.assertTrue(await self.runtime.analyze())
        self.assertIsNotNone(self.last_analyzer)
        self.assertTrue(callable(self.last_analyzer.progress_cb))
        body = await (await self.client.get("/api/progress")).json()
        progress = body["progress"]
        self.assertFalse(progress["running"])
        # 阶段字段来自分析线程的上报，收尾后仍保留最后一次（score）。
        self.assertEqual(progress["phase"], "score")
        self.assertEqual(progress["done"], 0)
        self.assertIsNotNone(progress["started_at"])
        self.assertIsNotNone(progress["finished_at"])
        self.assertEqual(
            progress["last_result"],
            {"messages_read": 12, "scored": 7, "llm_scored": 3, "llm_periods": 0},
        )

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
        self.assertEqual((await self.client.get("/report")).status, 200)

    async def test_report_returns_the_yearly_structure(self) -> None:
        # 词法策略（默认）下不生成叙事；结构稳定，前端只依赖这些键。
        with patch(
            "wechat_insights.server.get_depth_strategy",
            return_value=SimpleNamespace(name="lexical"),
        ):
            body = await (await self.client.get("/api/report?year=2026")).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["year"], 2026)
        self.assertIsNone(body["narrative"])
        stats = body["stats"]
        self.assertEqual(stats["year"], 2026)
        # 窗口收口到「今天」：3/10 的数据永远落在窗口内即可。
        self.assertEqual(stats["window"]["start"], "2026-01-01")
        self.assertGreaterEqual(stats["window"]["end"], "2026-03-10")
        self.assertEqual(
            stats["overview"],
            {
                "messages": 4,
                "contacts": 1,
                "incoming": 4,
                "outgoing": 0,
                "excluded_transactional": 0,
            },
        )
        self.assertEqual(stats["top"][0]["display_name"], "Alice")
        self.assertEqual(stats["top"][0]["messages"], 4)
        # 月度固定 12 格：3 月 4 条，其余月份（含 1 月）为 0。
        self.assertEqual(len(stats["monthly"]), 12)
        self.assertEqual(stats["monthly"][2]["count"], 4)
        self.assertEqual(stats["monthly"][0]["count"], 0)
        self.assertEqual(stats["new_friends"], [])
        self.assertEqual(stats["faded"], [])
        self.assertIsNone(stats["haha_king"])

    async def test_report_with_bad_year_falls_back_to_the_current_year(self) -> None:
        with patch(
            "wechat_insights.server.get_depth_strategy",
            return_value=SimpleNamespace(name="lexical"),
        ):
            body = await (await self.client.get("/api/report?year=oops")).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["year"], datetime.now().year)


class ReportNarrativeTests(AioHTTPTestCase):
    """年报叙事：llm 策略下才生成；缓存命中不重复调用；失败降级 stats 照常。"""

    async def get_application(self):
        return create_app(self.runtime)

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MetricsStore(Path(self.temporary.name) / "metrics.db")
        self.addCleanup(self.store.close)
        self.store.set_meta("last_analyzed_at", "1700000000")
        self.store.ensure_contact(SESSION_ID, "Alice")
        bucket = Metrics()
        bucket.add("msgs_them", 4)
        self.store.merge_daily(SESSION_ID, {"2026-03-10": bucket})
        self.runtime = InsightsRuntime(self.store, analyzer_factory=lambda: None)
        # 测试里固定 llm 策略：不依赖部署环境的深度策略变量。
        self.strategy = SimpleNamespace(name="llm")
        await super().asyncSetUp()

    async def test_narrative_is_generated_once_and_cached_until_next_analysis(self) -> None:
        with patch(
            "wechat_insights.server.get_depth_strategy", return_value=self.strategy
        ), patch(
            "wechat_insights.llm.chat", return_value="今年的故事…"
        ) as fake:
            first = await (await self.client.get("/api/report?year=2026")).json()
            second = await (await self.client.get("/api/report?year=2026")).json()
        # 分析没更新：第二次请求直接读缓存，不重复调用 LLM。
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(first["narrative"], "今年的故事…")
        self.assertEqual(second["narrative"], "今年的故事…")
        cache = self.store.get_json("report_narrative_2026")
        self.assertEqual(cache["last_analyzed_at"], 1_700_000_000)
        self.assertEqual(cache["text"], "今年的故事…")

    async def test_narrative_regenerates_after_a_new_analysis(self) -> None:
        replies = iter(["第一版", "第二版"])
        with patch(
            "wechat_insights.server.get_depth_strategy", return_value=self.strategy
        ), patch(
            "wechat_insights.llm.chat",
            side_effect=lambda system, user: next(replies),
        ) as fake:
            first = await (await self.client.get("/api/report?year=2026")).json()
            # 新一轮分析完成：分析时间戳变了，旧缓存失效，下次请求重新生成。
            self.store.set_meta("last_analyzed_at", "1700000001")
            second = await (await self.client.get("/api/report?year=2026")).json()
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(
            (first["narrative"], second["narrative"]), ("第一版", "第二版")
        )
        cache = self.store.get_json("report_narrative_2026")
        self.assertEqual(cache["last_analyzed_at"], 1_700_000_001)

    async def test_narrative_failure_degrades_with_stats_intact(self) -> None:
        with patch(
            "wechat_insights.server.get_depth_strategy", return_value=self.strategy
        ), patch("wechat_insights.llm.chat", return_value=None) as fake:
            body = await (await self.client.get("/api/report?year=2026")).json()
        self.assertEqual(fake.call_count, 1)
        self.assertIsNone(body["narrative"])
        # 失败只影响叙事：年报统计照常返回。
        self.assertEqual(body["stats"]["overview"]["messages"], 4)
