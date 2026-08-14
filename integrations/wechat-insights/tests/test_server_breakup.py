"""绝交标记的 HTTP 接口测试：mark/clear 动作、脏数据校验、截断展示。

拆自 test_server.py（职责聚焦 + 控制单文件行数）；核实/推算的判定逻辑
本身（refresh_breakups / guess_breakup_dates）在 test_breakup.py，这里
只测接口契约与 detail 接口的截断展示。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from tests.server_support import ApiTestCase, HASH, SESSION_ID


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


class BreakupApiTests(ApiTestCase):
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
