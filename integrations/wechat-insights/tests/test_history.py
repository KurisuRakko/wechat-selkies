from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tests.support import (
    AnalyzerTestCase,
    BASE,
    DISPLAY_NAME,
    NOW,
    SESSION_ID,
    backfill_grid,
    daily_grid,
)
from wechat_insights.constants import SCORE_FORMULA_VERSION
from wechat_insights.depth import LLMDepth
from wechat_insights.history import (
    apply_formula_reset,
    refine_limit_day,
    request_replay,
)
from wechat_insights.metrics import day_key
from wechat_insights.periods import PeriodRefresh
from wechat_insights.scoring import DIMENSION_NAMES


class ScoreHistoryTests(AnalyzerTestCase):
    """关系温度历史：scored 记当天综合分、zeroed 记 0、数据不足不记。"""

    def test_history_records_scored_and_zeroed_but_not_thin(self) -> None:
        # Alice 达标被打分；Ghost 两年无往来归零；Thin 有消息但数据不足。
        # 全史回放（部署日之前的周网格）走同一套规则：scored 记综合分、
        # 归零记 0、数据不足不记。seed_messages 绕过同步循环不写里程碑，
        # 补上 first_message_at 让回放照常铺点。
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})
        self.seed_messages(SESSION_ID, DISPLAY_NAME, {BASE + 5 * 86400: 20})
        self.seed_messages("thin", "Thin", {BASE + 5 * 86400: 2})
        for session_id in ("friend2", SESSION_ID, "thin"):
            contact = self.store.get_contact(session_id)
            contact.first_message_at = BASE + 5 * 86400
            self.store.save_contact(contact)
        # Ghost 一条消息都没有：没有相识日 → 回放不铺点，只剩今日的点。
        self.store.ensure_contact("ghost", "Ghost")

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis(now=NOW)

        today = day_key(NOW)
        alice = self.store.load_score_history(SESSION_ID)
        # 回放的周点之外还有今天的每日点（最后一条）。
        self.assertEqual(alice[-1][0], today)
        self.assertGreater(len(alice), 1)
        # 采样点与 scores 表里当轮的 payload 一致（分数 + 七维 JSON）。
        payload = next(
            p for p in self.store.all_scores() if p["display_name"] == DISPLAY_NAME
        )
        self.assertEqual(alice[-1][1], payload["overall"])
        self.assertEqual(json.loads(alice[-1][2]), payload["dimensions"])

        ghost = self.store.load_score_history("ghost")
        # Ghost 只有今日路径记的那一个 0 点（归零也是曲线的一部分）：
        # 没有相识日，回放不写任何历史行。
        self.assertEqual(len(ghost), 1)
        self.assertEqual(ghost[0][0], today)
        self.assertEqual(ghost[0][1], 0.0)
        self.assertEqual(set(json.loads(ghost[0][2]).values()), {0.0})

        self.assertEqual(self.store.load_score_history("thin"), [])

    def test_two_rounds_on_the_same_day_keep_one_point_per_day(self) -> None:
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})
        self.seed_messages(SESSION_ID, DISPLAY_NAME, {BASE + 5 * 86400: 20})

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis(now=NOW)
            rows_after_first = self.store.load_score_history(SESSION_ID)
            # 回放完成即记标记：第二轮不再回放，一行都不新增。
            self.run_analysis(now=NOW)
            rows_after_second = self.store.load_score_history(SESSION_ID)

        self.assertEqual(rows_after_first, rows_after_second)
        # 每天最多一个点（周网格 + 每日点互不重复），最后一条是今天的每日点。
        days = [row[0] for row in rows_after_second]
        self.assertEqual(len(days), len(set(days)))
        self.assertEqual(days[-1], day_key(NOW))


class PrunePreAcquaintanceTests(AnalyzerTestCase):
    """一次性迁移：清掉旧口径在相识日之前铺下的温度采样点，只跑一次。"""

    def test_prune_removes_pre_acquaintance_points_only_once(self) -> None:
        # 旧回放从全局最早统计日给所有人铺点，晚认识的人相识前被记成一长段
        # 0：迁移要删掉这些行并写 meta 标记。第二轮再预置同样的行，标记已
        # 在、迁移不再触发，行保留（回放也已跑过，不会用新口径重铺）。
        self.seed_messages("friend2", "Bob", {BASE + 5 * 86400: 15})
        self.seed_messages(SESSION_ID, DISPLAY_NAME, {BASE + 5 * 86400: 20})
        contact = self.store.get_contact(SESSION_ID)
        contact.first_message_at = BASE + 5 * 86400
        self.store.save_contact(contact)

        acquaintance = day_key(BASE + 5 * 86400)
        stale_day = day_key(BASE)  # 早于相识日的网格首日
        stale_dims = json.dumps(
            {name: 0.0 for name in DIMENSION_NAMES},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.store.record_score_history(stale_day, [(SESSION_ID, 0.0, stale_dims)])

        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis(now=NOW)

        history = self.store.load_score_history(SESSION_ID)
        self.assertTrue(history)
        # 相识前的行已删，剩下的点都从相识日之后开始。
        self.assertTrue(all(row[0] >= acquaintance for row in history))
        self.assertEqual(self.store.get_meta("score_history_pruned_v1"), str(NOW))

        # 第二轮：再预置一条相识前的行，迁移不再触发，行保留。
        self.store.record_score_history(stale_day, [(SESSION_ID, 0.0, stale_dims)])
        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis(now=NOW)
        self.assertIn(
            (stale_day, 0.0, stale_dims), self.store.load_score_history(SESSION_ID)
        )


class HistoryReplayTests(AnalyzerTestCase):
    """全史回放的一致性：回放点与「把 now 拨到那一刻跑一轮」记下的点逐位一致。"""

    def test_replay_point_matches_a_run_at_that_moment(self) -> None:
        # 一致性黄金测试：同一份构造数据，_scores_asof 回放到某时刻的结果 =
        # 把 now 拨到该时刻调 _recompute 路径记下的每日点。构造上把
        # last_message_at 对齐到天边界，contact 口径与窗口末活跃日口径
        # 完全一致（回放与今日的唯一差异点消失），分数必须逐位一致。
        for session_id, name, count in (
            (SESSION_ID, DISPLAY_NAME, 20),
            ("friend2", "Bob", 15),
        ):
            self.seed_messages(
                session_id,
                name,
                {BASE + offset * 86400: count for offset in range(20)},
            )
            contact = self.store.get_contact(session_id)
            contact.last_message_at = BASE + 19 * 86400
            self.store.save_contact(contact)

        past = BASE + 20 * 86400
        with patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            self.run_analysis(now=past)
            # 同一份数据直接调内核回放到同一时刻。
            replay = self.analyzer()._scores_asof(past)

        day = day_key(past)
        alice = {
            row[0]: (row[1], json.loads(row[2]))
            for row in self.store.load_score_history(SESSION_ID)
        }
        self.assertIn(day, alice)
        expected = replay.scores[SESSION_ID]
        self.assertEqual(alice[day][0], round(expected["overall"], 1))
        self.assertEqual(
            alice[day][1],
            {name: round(expected[name], 1) for name in DIMENSION_NAMES},
        )


class HistoryReplayGridTests(AnalyzerTestCase):
    """全史回放网格：周网格日期序列、归零段记 0、家人/事务口径、幂等与 FORCE。

    两个相隔超过 730 天（打分窗口）的消息簇，中间必然出现「窗口内零消息」
    的回放段：friend 记 0，family 跳过（不归零），transactional 永不写。
    """

    # 全史跨度：BASE 起 830 天，好友与家人两簇消息（0..9 天、800..809 天）。
    NOW_LATE = BASE + 830 * 86400

    def setUp(self) -> None:
        super().setUp()
        self.early = {BASE + offset * 86400: 15 for offset in range(10)}
        self.late = {BASE + 800 * 86400 + offset * 86400: 15 for offset in range(10)}
        self.seed_messages("friend2", "Bob", self.early)
        self.seed_messages("friend2", "Bob", self.late)
        self.seed_messages("mom", "Mom", self.early)
        self.seed_messages("mom", "Mom", self.late)
        self.seed_messages("agent", "机票代理", {BASE + 5 * 86400: 500})
        self.store.set_contact_kind_manual("mom", "family")
        self.store.set_contact_kind_manual("agent", "transactional")
        # seed_messages 绕过同步循环不写里程碑，而回放按 first_message_at
        # 判相识起点：给 BASE 起就有消息的三个联系人补上，让它们能参与回放。
        for session_id, first_at in (
            ("friend2", BASE),
            ("mom", BASE),
            ("agent", BASE + 5 * 86400),
        ):
            contact = self.store.get_contact(session_id)
            contact.first_message_at = first_at
            self.store.save_contact(contact)

    def run_with_progress(self, now: int):
        """无同步会话跑一轮（数据全在 stats_daily 里），同时收进度事件。"""

        events: list[dict] = []
        with patch(
            "wechat_insights.analyzer.scan_direct_rows", return_value={}
        ), patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            result = self.analyzer(
                progress_cb=lambda fields: events.append(dict(fields))
            ).run(now=now)
        return result, events

    def test_replay_writes_weekly_grid_with_kind_rules(self) -> None:
        today = day_key(self.NOW_LATE)
        grid = backfill_grid(day_key(BASE), today)

        result, events = self.run_with_progress(self.NOW_LATE)

        # 周网格日期序列：每个网格点都有 friend 的采样（达标段记分、归零段记
        # 0），最后一条是今天的每日点。
        friend = self.store.load_score_history("friend2")
        self.assertEqual([row[0] for row in friend], grid + [today])
        zero_rows = [row for row in friend if row[1] == 0.0]
        self.assertTrue(zero_rows)
        # 归零段的 dims 全 0。
        self.assertEqual(set(json.loads(zero_rows[0][2]).values()), {0.0})
        # 达标段记的是分数不是 0。
        self.assertTrue(any(row[1] != 0.0 for row in friend))

        # family 有采样但不记 0 分（家人久不聊天不代表疏远）。
        mom = self.store.load_score_history("mom")
        self.assertTrue(mom)
        self.assertTrue(all(row[1] != 0.0 for row in mom))
        # transactional 无任何行。
        self.assertEqual(self.store.load_score_history("agent"), [])

        # history_points = 回放写入的总行数（friend 全网格 + family 达标段）。
        self.assertEqual(result.history_points, len(grid) + len(mom) - 1)

        # 进度包含 history 阶段：起点 + 每 7 天一个点，detail 是网格日。
        phases = [event["phase"] for event in events]
        self.assertIn("history", phases)
        history = [event for event in events if event["phase"] == "history"]
        self.assertEqual(
            history[0],
            {"phase": "history", "done": 0, "total": len(grid), "detail": ""},
        )
        self.assertEqual(
            [event["done"] for event in history], list(range(len(grid) + 1))
        )
        self.assertEqual(history[-1]["detail"], grid[-1])

    def test_replay_skips_grid_points_before_acquaintance(self) -> None:
        # late 只在 BASE+800 天之后才有消息：相识日之前的网格点既不打分也不
        # 记 0（不是「归零」，是根本不存在），曲线从相识日之后第一个网格点
        # 才开始；friend2 相识于网格首日，第一点仍是网格首日、行数不变。
        self.seed_messages("late", "Late", self.late)
        contact = self.store.get_contact("late")
        contact.first_message_at = BASE + 800 * 86400
        self.store.save_contact(contact)

        today = day_key(self.NOW_LATE)
        grid = backfill_grid(day_key(BASE), today)
        self.run_with_progress(self.NOW_LATE)

        acquaintance = day_key(BASE + 800 * 86400)
        late = self.store.load_score_history("late")
        self.assertTrue(late)
        self.assertGreaterEqual(late[0][0], acquaintance)
        self.assertTrue(all(row[0] >= acquaintance for row in late))
        friend = self.store.load_score_history("friend2")
        self.assertEqual([row[0] for row in friend], grid + [today])

    def test_marker_makes_reruns_noop_until_force(self) -> None:
        first, _ = self.run_with_progress(self.NOW_LATE)
        rows_after_first = self.store.load_score_history("friend2")
        self.assertGreater(first.history_points, 0)
        self.assertEqual(
            self.store.get_meta("score_history_backfilled"), str(self.NOW_LATE)
        )

        # 标记在则二跑 no-op：不回放、不加行。
        second, _ = self.run_with_progress(self.NOW_LATE)
        self.assertEqual(second.history_points, 0)
        self.assertEqual(self.store.load_score_history("friend2"), rows_after_first)

        # FORCE 强制重跑：行数不变（UPSERT 覆盖、不重复）。
        with patch("wechat_insights.history.INSIGHTS_FORCE_HISTORY_BACKFILL", True):
            forced, _ = self.run_with_progress(self.NOW_LATE)
        self.assertEqual(forced.history_points, first.history_points)
        self.assertEqual(self.store.load_score_history("friend2"), rows_after_first)
        self.assertEqual(
            self.store.get_meta("score_history_backfilled"), str(self.NOW_LATE)
        )


class DailyRefineTests(AnalyzerTestCase):
    """每日粒度细化：逐日补点、断点续跑、幂等、切回周粒度不删点、进度上报。

    两个联系人都从 BASE 起每天有消息：day 切到每日粒度，week 保持每周作
    对照。seed_messages 绕过同步循环不写里程碑，手工补 first_message_at。
    """

    NOW_REFINE = BASE + 60 * 86400

    def setUp(self) -> None:
        super().setUp()
        self.seed_messages(
            "day", "Day", {BASE + offset * 86400: 15 for offset in range(30)}
        )
        self.seed_messages(
            "week", "Week", {BASE + offset * 86400: 15 for offset in range(30)}
        )
        for session_id in ("day", "week"):
            contact = self.store.get_contact(session_id)
            contact.first_message_at = BASE
            self.store.save_contact(contact)
        self.store.set_history_granularity("day", "day")

    def run_refine(self, now: int):
        """无同步会话跑一轮（数据全在 stats_daily 里），同时收进度事件。"""

        events: list[dict] = []
        with patch(
            "wechat_insights.analyzer.scan_direct_rows", return_value={}
        ), patch("wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10):
            result = self.analyzer(
                progress_cb=lambda fields: events.append(dict(fields))
            ).run(now=now)
        return result, events

    def test_daily_refine_fills_every_day_while_weekly_contact_stays_sparse(self) -> None:
        today = day_key(self.NOW_REFINE)
        expected = daily_grid(day_key(BASE), today)

        result, _ = self.run_refine(self.NOW_REFINE)

        # 每日粒度：相识日到今天每一天都有点（连续日期序列）；今天那个点
        # 由今日打分路径记，所以整条曲线依旧逐日连续。
        day_days = [row[0] for row in self.store.load_score_history("day")]
        self.assertEqual(day_days, expected)
        self.assertTrue(any(row[1] != 0.0 for row in self.store.load_score_history("day")))
        # 差的这一点就是今天——细化网格停在昨天，今天归今日打分路径。
        self.assertEqual(result.refined_points, len(expected) - 1)
        # 对照联系人保持每周：周网格点 + 今天的每日点，中间大量缺天。
        week_days = [row[0] for row in self.store.load_score_history("week")]
        self.assertEqual(week_days, backfill_grid(day_key(BASE), today) + [today])

    def test_resume_continues_from_the_saved_progress(self) -> None:
        self.run_refine(self.NOW_REFINE)
        snapshot = self.store.load_score_history("day")

        # 模拟容器在中间某天挂掉：进度停在 X，X 之后的点还没写。
        mid = daily_grid(day_key(BASE), day_key(self.NOW_REFINE))[30]
        with self.store.connection as connection:
            connection.execute(
                "UPDATE contacts SET history_daily_until = ? WHERE session_id = ?",
                (mid, "day"),
            )
            connection.execute(
                "DELETE FROM score_history WHERE session_id = ? AND day > ?",
                ("day", mid),
            )
        truncated = self.store.load_score_history("day")
        self.assertEqual(truncated, snapshot[: len(truncated)])
        self.assertLess(len(truncated), len(snapshot))

        result, _ = self.run_refine(self.NOW_REFINE)

        # 只补了断点之后的天：断点及之前一行没动，之后逐日补回（同一打分
        # 内核、同一份数据，补回来的值与原来逐位一致），进度推进到昨天。
        history = self.store.load_score_history("day")
        self.assertEqual(history, snapshot)
        self.assertEqual(history[: len(truncated)], snapshot[: len(truncated)])
        self.assertGreater(result.refined_points, 0)
        self.assertEqual(
            self.store.get_contact("day").history_daily_until,
            refine_limit_day(self.NOW_REFINE),
        )

    def test_completed_refine_is_idempotent(self) -> None:
        first, _ = self.run_refine(self.NOW_REFINE)
        self.assertGreater(first.refined_points, 0)
        rows_after_first = self.store.load_score_history("day")

        second, _ = self.run_refine(self.NOW_REFINE)
        self.assertEqual(second.refined_points, 0)
        self.assertEqual(self.store.load_score_history("day"), rows_after_first)
        self.assertEqual(
            self.store.get_contact("day").history_daily_until,
            refine_limit_day(self.NOW_REFINE),
        )

    def test_switching_back_to_weekly_keeps_the_daily_points(self) -> None:
        self.run_refine(self.NOW_REFINE)
        rows_before = self.store.load_score_history("day")

        self.store.set_history_granularity("day", "")
        result, _ = self.run_refine(self.NOW_REFINE)

        # 切回每周：不再细化，已算出来的日点全部保留（删了再切回来要重算）。
        self.assertEqual(result.refined_points, 0)
        self.assertEqual(self.store.load_score_history("day"), rows_before)
        self.assertEqual(self.store.get_contact("day").history_granularity, "")

    def test_refine_reports_progress_day_by_day(self) -> None:
        today = day_key(self.NOW_REFINE)
        yesterday = refine_limit_day(self.NOW_REFINE)
        grid = daily_grid(day_key(BASE), yesterday)

        result, events = self.run_refine(self.NOW_REFINE)

        refine = [event for event in events if event["phase"] == "refine"]
        self.assertEqual(
            refine[0],
            {"phase": "refine", "done": 0, "total": len(grid), "detail": ""},
        )
        self.assertEqual(
            [event["done"] for event in refine], list(range(len(grid) + 1))
        )
        details = [event["detail"] for event in refine[1:]]
        self.assertEqual(details, grid)
        # R1 回归闸门：网格停在昨天，今天那个点归今日打分路径，不能出现在
        # 细化进度里。
        self.assertEqual(details[-1], yesterday)
        self.assertNotIn(today, details)
        self.assertEqual(result.refined_points, len(grid))

    def test_next_day_refines_exactly_one_more_day(self) -> None:
        """完成后每轮只补新的一天，不重算全史。

        第一轮把网格细化到昨天；隔天再跑，pending 只剩新露出的这一天，
        第二轮只写它一天。
        """

        self.run_refine(self.NOW_REFINE)
        second, _ = self.run_refine(self.NOW_REFINE + 86400)

        self.assertEqual(second.refined_points, 1)
        self.assertEqual(
            self.store.get_contact("day").history_daily_until,
            day_key(self.NOW_REFINE),
        )


class FormulaResetTests(AnalyzerTestCase):
    """打分口径版本重置：清历史标记、清逐日进度、强制开关语义。"""

    NOW_RESET = BASE + 60 * 86400

    def setUp(self) -> None:
        super().setUp()
        # 两个达标的联系人：一个人没有参照系（score_cohort 返回空表），
        # 历史点一行都写不出来。
        self.seed_messages(
            "day", "Day", {BASE + offset * 86400: 15 for offset in range(30)}
        )
        self.seed_messages(
            "week", "Week", {BASE + offset * 86400: 15 for offset in range(30)}
        )
        for session_id in ("day", "week"):
            contact = self.store.get_contact(session_id)
            contact.first_message_at = BASE
            self.store.save_contact(contact)
        self.store.set_history_granularity("day", "day")

    def run_round(self, now: int):
        """无同步会话跑一轮（数据全在 stats_daily 里）。

        MIN_SCORE_MESSAGES 压到 10：每日 15 条从相识第一天起就达标，否则
        前三天累计 15/30/45 条不足 50，数据不足不记点、网格头部缺 3 天。
        """

        with patch("wechat_insights.analyzer.scan_direct_rows", return_value={}), patch(
            "wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10
        ):
            return self.analyzer().run(now=now)

    def test_formula_version_change_clears_marker_and_daily_progress(self) -> None:
        # 口径升级场景：历史早已回放（旧标记）、逐日进度停在 2026-01-01、
        # 版本号还是旧的。新代码跑一轮 → 标记重写为新时刻、进度清空后
        # 从相识日重新细化到昨天。
        self.store.set_meta("score_history_backfilled", "1700000000")
        self.store.set_meta("score_formula_version", "1")
        with self.store.connection as connection:
            connection.execute(
                "UPDATE contacts SET history_daily_until = '2026-01-01' "
                "WHERE session_id = 'day'"
            )

        result = self.run_round(self.NOW_RESET)

        self.assertGreater(result.history_points, 0)
        self.assertEqual(
            self.store.get_meta("score_history_backfilled"), str(self.NOW_RESET)
        )
        self.assertEqual(
            self.store.get_meta("score_formula_version"), str(SCORE_FORMULA_VERSION)
        )
        # 2026-01-01 的旧进度被清掉（否则细化看到「已到 2026」就什么都不
        # 做），重新细化到昨天，逐日点连成完整的一天一条。
        self.assertEqual(
            self.store.get_contact("day").history_daily_until,
            refine_limit_day(self.NOW_RESET),
        )
        days = [row[0] for row in self.store.load_score_history("day")]
        self.assertEqual(days, daily_grid(day_key(BASE), day_key(self.NOW_RESET)))

    def test_formula_reset_runs_only_once(self) -> None:
        first = self.run_round(self.NOW_RESET)
        self.assertGreater(first.history_points, 0)
        self.assertEqual(
            self.store.get_meta("score_formula_version"), str(SCORE_FORMULA_VERSION)
        )
        # 版本一致：重置不再执行，标记在 → 回放也不再触发。
        second = self.run_round(self.NOW_RESET)
        self.assertEqual(second.history_points, 0)
        self.assertFalse(apply_formula_reset(self.store))

    def test_forced_backfill_also_resets_daily_progress(self) -> None:
        # 先正常跑完一轮：回放标记与逐日进度都写到位。
        self.run_round(self.NOW_RESET)
        # 强制重放 = 旧口径算出来的都不算：逐日进度也要先清掉再重算。
        with patch(
            "wechat_insights.history.INSIGHTS_FORCE_HISTORY_BACKFILL", True
        ), patch.object(
            self.store,
            "rewind_daily_refine_progress",
            wraps=self.store.rewind_daily_refine_progress,
        ) as reset:
            forced = self.run_round(self.NOW_RESET)
        reset.assert_called_once()
        self.assertGreater(forced.history_points, 0)
        self.assertEqual(
            self.store.get_contact("day").history_daily_until,
            refine_limit_day(self.NOW_RESET),
        )
        # 标记挡不住强制开关：第二轮照常重放。
        with patch("wechat_insights.history.INSIGHTS_FORCE_HISTORY_BACKFILL", True):
            again = self.run_round(self.NOW_RESET)
        self.assertGreater(again.history_points, 0)

    def run_round_llm(self, now: int):
        """LLM 策略跑一轮：refresh_periods 只在 llm 策略下被调用。

        画像与分类屏蔽成 0 次调用（与 test_periods 的 run_period_analysis
        同一套隔离手法），本组测试只关心历史重放闸门。
        """

        with patch("wechat_insights.analyzer.scan_direct_rows", return_value={}), patch(
            "wechat_insights.analyzer.MIN_SCORE_MESSAGES", 10
        ), patch("wechat_insights.analyzer.refresh_portraits", return_value=0), patch(
            "wechat_insights.analyzer.classify_contacts", return_value=0
        ):
            return self.analyzer(strategy=LLMDepth()).run(now=now)

    def test_request_replay_clears_the_marker_and_rewinds_to_the_given_day(self) -> None:
        self.store.set_meta("score_history_backfilled", "1700000000")
        with self.store.connection as connection:
            connection.execute(
                "UPDATE contacts SET history_daily_until = '2026-03-10' "
                "WHERE session_id = 'day'"
            )

        request_replay(self.store, "t", since="2026-02-10")

        self.assertIsNone(self.store.get_meta("score_history_backfilled"))
        # 回退目标是 since 的前一天：refine_daily_history 从「进度次日」续跑，
        # 落到这一天等于把 [since, 原进度] 整段重算。
        self.assertEqual(
            self.store.get_contact("day").history_daily_until, "2026-02-09"
        )

    def test_request_replay_without_a_day_rewinds_to_acquaintance(self) -> None:
        # since=None = 全史都不可信（口径升级/强制重放）：进度整体退回相识日。
        with self.store.connection as connection:
            connection.execute(
                "UPDATE contacts SET history_daily_until = '2026-03-10' "
                "WHERE session_id = 'day'"
            )

        request_replay(self.store, "t")

        self.assertEqual(self.store.get_contact("day").history_daily_until, "")

    def test_history_period_rows_trigger_a_replay_in_the_same_round(self) -> None:
        # 第一轮正常跑完：标记已写，第二轮回放必然被标记挡住（0 点）。
        first = self.run_round_llm(self.NOW_RESET)
        self.assertGreater(first.history_points, 0)
        second = self.run_round_llm(self.NOW_RESET)
        self.assertEqual(second.history_points, 0)

        # 第二轮落地了改写历史时点的时段行：请求重放 → 标记被清、逐日进度
        # 退回改写点前一天并重新细化到昨天（曲线在该日之后被重写）。
        since = day_key(self.NOW_RESET - 5 * 86400)
        with patch(
            "wechat_insights.analyzer.refresh_periods",
            return_value=PeriodRefresh(1, since),
        ):
            replayed = self.run_round_llm(self.NOW_RESET)
        self.assertGreater(replayed.history_points, 0)
        self.assertEqual(
            self.store.get_contact("day").history_daily_until,
            refine_limit_day(self.NOW_RESET),
        )

    def test_open_month_rows_do_not_trigger_a_replay(self) -> None:
        # 只动了今天那个点（earliest_past_end=None）：不重放，回放标记原样
        # 挡着，history_points == 0（白噪声闸门）。
        self.run_round_llm(self.NOW_RESET)
        with patch(
            "wechat_insights.analyzer.refresh_periods",
            return_value=PeriodRefresh(1, None),
        ):
            result = self.run_round_llm(self.NOW_RESET)
        self.assertEqual(result.history_points, 0)

    def test_formula_reset_is_skipped_when_the_backup_fails(self) -> None:
        # 备份拿不到就不动数据：版本号不写（下一轮会重试）、回放标记与
        # 逐日进度原样保留，只留一条 ERROR。
        self.store.set_meta("score_history_backfilled", "1700000000")
        self.store.set_meta("score_formula_version", "1")
        with self.store.connection as connection:
            connection.execute(
                "UPDATE contacts SET history_daily_until = '2026-01-01' "
                "WHERE session_id = 'day'"
            )

        with patch(
            "wechat_insights.history.backup_database", return_value=None
        ), self.assertLogs("wechat-insights", level="ERROR") as logs:
            reset = apply_formula_reset(self.store)
        self.assertFalse(reset)
        self.assertEqual(self.store.get_meta("score_formula_version"), "1")
        self.assertEqual(
            self.store.get_meta("score_history_backfilled"), "1700000000"
        )
        self.assertEqual(
            self.store.get_contact("day").history_daily_until, "2026-01-01"
        )
        self.assertTrue(any("备份失败" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
