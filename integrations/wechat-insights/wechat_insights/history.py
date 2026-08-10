"""关系温度历史：相识前清理、全史周网格回放与每日粒度逐日细化。

打分内核（analyzer.ScoresAsOf）通过回调注入，本模块不直接导入 analyzer，
避免运行期循环导入；类型标注用 TYPE_CHECKING 只做检查不参与运行。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import TYPE_CHECKING

from .constants import INSIGHTS_FORCE_HISTORY_BACKFILL
from .metrics import day_key, day_moment, day_span
from .scoring import DIMENSION_NAMES
from .storage import ContactRow, MetricsStore

if TYPE_CHECKING:
    from .analyzer import ScoresAsOf

LOG = logging.getLogger("wechat-insights")

#: 打分内核的回调签名：moment → 那一刻的 ScoresAsOf 快照。
ScoresAsOfFn = Callable[[int], "ScoresAsOf"]


def refine_limit_day(moment: int) -> str:
    """逐日细化的网格终点：今天的前一天。

    今天那个点归今日打分路径（_recompute）所有——它注入 LLM 深度分、沉默
    天数按真实时刻算，回放口径算不出同一个值。细化网格必须停在昨天，否则
    每轮分析都会用回放口径覆盖当天的真实分，曲线末点系统性偏低。
    """

    return (date.fromisoformat(day_key(moment)) - timedelta(days=1)).isoformat()


def history_rows(
    asof: "ScoresAsOf", day: str, only: set[str] | None = None
) -> list[tuple[str, float, str]]:
    """把一个 asof 快照换算成当天要写的温度采样行。

    only 为 None 时处理快照里的全部联系人（全史回放用），否则只处理
    集合里的 session_id（逐日细化用）。规则与每日记点一致：相识日
    之前不存在关系（既不打分也不算归零，曲线从第一条消息那天起）；
    达标打分记当天综合分与七维；窗口零消息的普通联系人记 0、事务
    往来与零消息家人跳过；有消息但样本不够不记。
    """

    rows: list[tuple[str, float, str]] = []
    for session_id, contact in asof.contacts.items():
        if only is not None and session_id not in only:
            continue
        if (
            contact.first_message_at is None
            or day_key(contact.first_message_at) > day
        ):
            # 相识之前不存在关系：既不打分也不算「归零」，曲线从第一条
            # 消息那天起；没有任何消息的联系人无从确定起点，不回放。
            continue
        kind = contact.relation_kind()
        dims = asof.scores.get(session_id)
        if dims is not None:
            # 达标打分：记当天的综合分与七维（与每日记点同一格式）。
            rows.append(
                (
                    session_id,
                    round(dims["overall"], 1),
                    json.dumps(
                        {
                            name: round(dims[name], 1)
                            for name in DIMENSION_NAMES
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            )
            continue
        window = asof.stats.get(session_id)
        if window is None or window.raw.messages_total() <= 0:
            if kind in ("transactional", "family"):
                # 事务往来永不记温度；家人零消息不归零、不记 0 分——
                # 两者都与每日规则一致。
                continue
            # 归零也是曲线的一部分：记 0。
            rows.append(
                (
                    session_id,
                    0.0,
                    json.dumps(
                        {name: 0.0 for name in DIMENSION_NAMES},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            )
        # 有消息但样本不够：数据不足，不记（与每日规则一致）。
    return rows


def prune_pre_acquaintance(store: MetricsStore, moment: int) -> int:
    """一次性迁移：清掉旧口径在相识之前铺下的前导 0 段。

    旧回放从全局最早统计日给所有人铺点，晚认识的人相识前被记成一长段 0；
    新口径从第一条消息起画，这里清掉历史库里的前导 0 段，靠 meta 标记
    score_history_pruned_v1 只跑一次。first_message_at 为 None 的联系人
    跳过——无从确定起点，且这类行里混着今日路径记的每日点，不能盲删。
    """

    if store.get_meta("score_history_pruned_v1"):
        return 0
    cutoffs = {
        contact.session_id: day_key(contact.first_message_at)
        for contact in store.all_contacts()
        if contact.first_message_at is not None
    }
    pruned = store.prune_score_history_before(cutoffs)
    store.set_meta("score_history_pruned_v1", str(moment))
    LOG.info("清理相识前的温度采样点：删除 %d 行", pruned)
    return pruned


def backfill_history(
    store: MetricsStore,
    scores_asof: ScoresAsOfFn,
    moment: int,
    report_cb=None,
) -> int:
    """把关系温度回放到全史：从最早统计日起每 7 天一个采样点，直到今天。

    网格是全局的（所有联系人里最早的统计日起），但每位联系人只从自己
    第一条消息那天起才记点：相识之前不打分也不归零，曲线没有前导 0 段。
    与今日打分共用同一个打分内核，历史回放没有第二份真相；周点与部署
    日起的每日点重叠时 UPSERT 覆盖，无害。完成后记 meta 标记
    score_history_backfilled（值 = moment），之后每轮跳过；设
    INSIGHTS_FORCE_HISTORY_BACKFILL=true 强制重跑（打分口径升级后重放历史
    用）。回放与今日口径的差异见 _scores_asof 的注释。
    """

    if not INSIGHTS_FORCE_HISTORY_BACKFILL and store.get_meta(
        "score_history_backfilled"
    ):
        return 0

    earliest = store.earliest_stats_day()
    if earliest is None:
        return 0  # 一条天桶都没有（首轮同步还没落地），无史可回放。
    today = day_key(moment)
    grid: list[str] = []
    cursor = earliest
    while cursor <= today:
        grid.append(cursor)
        cursor = (date.fromisoformat(cursor) + timedelta(days=7)).isoformat()
    if not grid:
        return 0

    started = time.monotonic()
    points = 0
    if report_cb is not None:
        report_cb(phase="history", done=0, total=len(grid), detail="")
    for done, day in enumerate(grid, start=1):
        if report_cb is not None:
            report_cb(phase="history", done=done, total=len(grid), detail=day)
        asof = scores_asof(day_moment(day))
        rows = history_rows(asof, day)
        # 每点一个事务：单个点失败不连坐整段历史。
        store.record_score_history(day, rows)
        points += len(rows)
    store.set_meta("score_history_backfilled", str(moment))
    LOG.info(
        "全史回放：%d 天网格 %d 个采样点，写入 %d 行，用时 %.1f 秒",
        day_span(earliest, today),
        len(grid),
        points,
        time.monotonic() - started,
    )
    return points


def refine_daily_history(
    store: MetricsStore,
    scores_asof: ScoresAsOfFn,
    moment: int,
    report_cb=None,
) -> int:
    """把切到每日粒度的联系人从相识日起逐日补点，直到昨天（今天那个点
    归今日打分路径）。

    同一天的打分内核快照服务所有待细化联系人：重算成本只与网格天数有关，
    与人数无关。每完成一天就把进度写进 history_daily_until（每天都写，不
    攒批），容器重启后从断点续跑，不重头再来。逐日点会覆盖同一天已有的
    周点（同一主键 UPSERT），打分内核一致所以值相同，无害。
    """

    limit = refine_limit_day(moment)
    pending = store.contacts_needing_daily_refine(limit)
    if not pending:
        return 0

    def start_of(contact: ContactRow) -> str:
        # 起点 = 相识日与「上次进度的次日」取晚；进度为空表示还没开始。
        acquaintance = day_key(contact.first_message_at)
        if not contact.history_daily_until:
            return acquaintance
        resumed = (
            date.fromisoformat(contact.history_daily_until)
            + timedelta(days=1)
        ).isoformat()
        return max(acquaintance, resumed)

    starts = {contact.session_id: start_of(contact) for contact in pending}
    grid: list[str] = []
    cursor = min(starts.values())
    while cursor <= limit:
        grid.append(cursor)
        cursor = (date.fromisoformat(cursor) + timedelta(days=1)).isoformat()
    if not grid:
        return 0

    started = time.monotonic()
    points = 0
    if report_cb is not None:
        report_cb(phase="refine", done=0, total=len(grid), detail="")
    for done, day in enumerate(grid, start=1):
        if report_cb is not None:
            report_cb(phase="refine", done=done, total=len(grid), detail=day)
        asof = scores_asof(day_moment(day))
        # 这一天只服务已经到达自己起点的联系人：起点更晚的人相识日
        # 之前没有历史可细化，进度也不能先于实际计算推进。
        active = {
            session_id
            for session_id, start in starts.items()
            if start <= day
        }
        rows = history_rows(asof, day, only=active)
        store.record_score_history(day, rows)
        # 每天都写进度：崩溃/重启后只重算断点之后的天数。
        store.mark_daily_refined(list(active), day)
        points += len(rows)
    LOG.info(
        "逐日细化：%d 个联系人，%d 天网格，写入 %d 行，用时 %.1f 秒",
        len(pending),
        len(grid),
        points,
        time.monotonic() - started,
    )
    return points
