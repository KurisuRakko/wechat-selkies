"""关系温度升温回归：旧口径（A）vs 新口径（B）vs 新口径+合成时段分（C）。

用法：
  cd integrations/wechat-insights
  PYTHONPATH=../wechat-history:. .venv/bin/python scripts/regress_warming.py \
      --db /tmp/metrics-snap.db --session <目标联系人的 session_id>
可选：--from 2025-06-01 --to 2026-03-01

三条曲线在同一进程、同一时区、同一份只读快照上计算，彼此可比（不受
「快照晚于生产回放时刻」造成的漂移影响）：
  A 旧口径 — 脚本内嵌 v2 的 reciprocity 组成（balance_msgs 0.4 /
             balance_chars 0.3 / balance_started 0.3）与不含
             mutual_cost_per_day 的 raw 包装，monkeypatch 后计算；
  B 新口径 — 仓库当前代码原样计算，PeriodIndex({}) 表示没有 LLM 时段分；
  C 新口径 — 同 B，注入内存 PeriodIndex（合成时段分）。

合成时段分的铁律是离散度：全员同一个常数值会让百分位退化成 0/100 的
阶跃，曲线失去意义。目标联系人按固定升温序列（2025-10 起逐月走高），
其余联系人按 sha256(sid+period) 伪随机分布。

只读：MetricsStore(read_only=True) 打开快照，绝不写库；不导入 analyzer、
不产生任何出站请求。退出码 0 = 全部断言通过，1 = 有失败，2 = 用法/库错。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from wechat_insights.constants import (
    DECAY_HALF_LIFE_DAYS,
    MIN_SCORE_MESSAGES,
    SCORE_WINDOW_DAYS,
)
from wechat_insights.depth import Component, LLMDepth
from wechat_insights.metrics import (
    day_key,
    day_moment,
    day_span,
    decayed_span,
    decayed_weight,
)
from wechat_insights.periods import PeriodIndex, PeriodRow, month_last_day
from wechat_insights.scoring import dimensions, median, raw_metrics, score_cohort
from wechat_insights.storage import ContactRow, MetricsStore, WindowStats

#: 三条曲线共用的深度策略。B/C 里 LLM 项经 extras 注入；A 的旧深度组成
#: 里 llm_depth_score 缺值时按 score_cohort 的机制回流词法三项，与「没评过
#: 分」的生产行为一致，不需要特判。
STRATEGY = LLMDepth()

#: 当前口径的七维组成（B/C 用）。模块级捕获一次，避免在 monkeypatch
#: legacy_dimensions 期间回调 scoring.dimensions 造成递归。
CURRENT_DIMENSIONS = dimensions(STRATEGY)

#: v2 旧口径的两个被替换的维度。对等维只有 balance 三项（没有
#: mutual_cost_per_day 与 llm_mutuality_score），深度维是单项 LLM 分 + 词法
#: 三项减半（与旧 depth.py 的 LLMDepth 逐字一致）。
LEGACY_RECIPROCITY = (
    Component("balance_msgs", 0.4, True),
    Component("balance_chars", 0.3, True),
    Component("balance_started", 0.3, True),
)
LEGACY_DEPTH = (
    Component("avg_len_them", 0.4 * 0.5, True),
    Component("question_rate_them", 0.3 * 0.5, True),
    Component("long_msg_rate_them", 0.3 * 0.5, True),
    Component("llm_depth_score", 0.5, True),
)

#: 目标联系人的固定升温序列；不在表里的月份一律 (35, 35, 50)。
TARGET_MONTHS = {
    "2025-10": (55, 65, 60),
    "2025-11": (70, 80, 70),
    "2025-12": (75, 85, 75),
    "2026-01": (75, 85, 75),
    "2026-02": (72, 82, 72),
}

#: R1–R8 依赖的关键日；它们都在「目标首条天桶日起每 7 天」的网格上
#: （目标 2022-10-26 起：09-03 = 第 149 周、11-26 = 161、12-24 = 165、
#: 02-25 = 174），插入网格只是兜底，防止自定义 --session 破坏对齐。
ANCHOR_DAYS = ("2025-09-03", "2025-11-26", "2025-12-24", "2026-02-25")


def legacy_dimensions(_strategy) -> tuple[tuple[str, tuple[Component, ...]], ...]:
    """v2 旧口径的七维组成：其余六维照搬当前定义，只换对等与深度两维。"""

    return tuple(
        (name, LEGACY_RECIPROCITY if name == "reciprocity" else LEGACY_DEPTH
         if name == "depth" else components)
        for name, components in CURRENT_DIMENSIONS
    )


def _balance(them: float, me: float) -> float | None:
    """双方量级之比：min/max，两边都没有时无定义（与仓库 scoring 同式）。"""

    if them <= 0 and me <= 0:
        return None
    return min(them, me) / max(them, me)


def _longest_gap(
    window: WindowStats, contact: ContactRow, score_start: str, score_start_ts: int
) -> int:
    """窗口内最大沉默间隔，认识早于窗口起点的纳入前导空档（抄 analyzer）。"""

    longest = window.longest_gap_days
    if (
        window.first_day is not None
        and contact.first_message_at is not None
        and contact.first_message_at < score_start_ts
    ):
        longest = max(longest, day_span(score_start, window.first_day) - 1)
    return longest


def base_raws(
    store: MetricsStore, contacts: dict[str, ContactRow], day: str
) -> tuple[dict[str, dict[str, float | None]], dict[str, dict[str, float | None]], list[str]]:
    """一个 asof 日的 eligible 联系人 raw 基础值（与 analyzer._scores_asof 同口径）。

    返回 (当前口径 raw, 旧口径 raw, eligible)。旧口径 raw 与当前只差
    「无 mutual_cost_per_day、多 balance_started」，一次扫描同时产出，
    避免为 A/B/C 各扫一遍窗口。
    """

    moment = day_moment(day)
    score_start = day_key(moment - SCORE_WINDOW_DAYS * 86400)
    score_start_ts = moment - SCORE_WINDOW_DAYS * 86400

    def weight_of(bucket_day: str) -> float:
        return decayed_weight(day_span(bucket_day, day) - 1, DECAY_HALF_LIFE_DAYS)

    stats = store.load_window_stats(score_start, day, weight_of)
    eligible = sorted(
        session_id
        for session_id, window in stats.items()
        if window.raw.messages_total() >= MIN_SCORE_MESSAGES
        and contacts[session_id].relation_kind() != "transactional"
    )
    equivalent_days = decayed_span(
        day_span(score_start, day), DECAY_HALF_LIFE_DAYS
    )
    raws: dict[str, dict[str, float | None]] = {}
    raw_legacy: dict[str, dict[str, float | None]] = {}
    for session_id in eligible:
        window = stats[session_id]
        contact = contacts[session_id]
        extras: dict[str, float | None] = {
            "active_day_rate": window.active_weight / equivalent_days,
            "current_gap_days": (
                day_span(window.last_day, day) - 1
                if window.last_day is not None
                else None
            ),
            "longest_gap_days": _longest_gap(
                window, contact, score_start, score_start_ts
            ),
        }
        values = raw_metrics(window.weighted, STRATEGY, equivalent_days, extras)
        legacy_values = {
            key: value
            for key, value in values.items()
            if key != "mutual_cost_per_day"
        }
        legacy_values["balance_started"] = _balance(
            window.weighted.get("conv_started_them"),
            window.weighted.get("conv_started_me"),
        )
        raws[session_id] = values
        raw_legacy[session_id] = legacy_values
    return raws, raw_legacy, eligible


def score_variants(
    raws: dict[str, dict[str, float | None]],
    raw_legacy: dict[str, dict[str, float | None]],
    day: str,
    period_index: PeriodIndex | None,
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """同一组 raw 换算成 A/B/C 三口径的分（percentile 在同一 eligible 集内取）。"""

    raw_c = raws
    if period_index is not None:
        # C：当前 raw 上注入时段化 LLM 三个原始值；可见性由 PeriodIndex 管。
        raw_c = {}
        for session_id, values in raws.items():
            enriched = dict(values)
            enriched.update(period_index.asof(session_id, day))
            raw_c[session_id] = enriched
    with patch("wechat_insights.scoring.dimensions", legacy_dimensions):
        scores_a = score_cohort(raw_legacy, STRATEGY)
    scores_b = score_cohort(raws, STRATEGY)
    scores_c = score_cohort(raw_c, STRATEGY)
    return scores_a, scores_b, scores_c


def weekly_grid(start_day: str, end_day: str) -> list[str]:
    """从 start_day 起每 7 天一个点直到 end_day（与生产周网格同构）。"""

    days = []
    cursor = start_day
    while cursor <= end_day:
        days.append(cursor)
        cursor = (date.fromisoformat(cursor) + timedelta(days=7)).isoformat()
    return days


def month_range(start: str, end: str) -> list[tuple[str, str]]:
    """[start, end] 之间的自然月，每项 (period, 该月月末日键)。"""

    months = []
    cursor = date.fromisoformat(start + "-01")
    end_first = date.fromisoformat(end + "-01")
    while cursor <= end_first:
        period = cursor.strftime("%Y-%m")
        months.append((period, month_last_day(period)))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def synthetic_index(
    store: MetricsStore,
    target: str,
    months: list[tuple[str, str]],
    override_2026_01: tuple[int, int, int] | None = None,
) -> PeriodIndex:
    """合成时段分：目标固定升温序列，其余联系人 sha256 离散分布。

    全员同一个常数的教训：百分位退化成 0/100 阶跃，R9/R10 的
    「整体平移」断言就失效了。override_2026_01 只改目标——R8 用它验证
    不可见时段不影响可见日。
    """

    rows: dict[str, list[PeriodRow]] = {}
    for contact in store.all_contacts():
        session_id = contact.session_id
        rows[session_id] = []
        for period, last_day in months:
            if session_id == target:
                triple = TARGET_MONTHS.get(period, (35, 35, 50))
                if override_2026_01 is not None and period == "2026-01":
                    triple = override_2026_01
            else:
                base = (
                    20
                    + int(
                        hashlib.sha256(f"{session_id}{period}".encode()).hexdigest()[
                            :8
                        ],
                        16,
                    )
                    % 61
                )
                triple = (base, (base + 7) % 101, (base + 13) % 101)
            depth, warmth, mutuality = triple
            rows[session_id].append(
                PeriodRow(
                    period, last_day, float(depth), float(warmth), float(mutuality)
                )
            )
    return PeriodIndex(rows)


def _row_values(scores: dict[str, dict[str, float]], session_id: str) -> tuple:
    if not scores:
        return ("  —", "  —", "  —")
    values = scores.get(session_id)
    if values is None:
        return ("  —", "  —", "  —")
    return (
        f"{values['overall']:6.1f}",
        f"{values['reciprocity']:6.1f}",
        f"{values['depth']:6.1f}",
    )


def run(db_path: str, session: str, from_day: str, to_day: str) -> int:
    """主流程：只读快照 → 三口径逐日曲线 → 对照表 → R1–R10。"""

    if not os.path.exists(db_path):
        print(f"快照不存在：{db_path}", file=sys.stderr)
        print("用部署前 VACUUM INTO 的 metrics.db 副本，勿传生产库路径。", file=sys.stderr)
        return 2
    store = MetricsStore(Path(db_path), read_only=True)

    row = store.connection.execute(
        "SELECT MIN(day) AS first, MAX(day) AS last "
        "FROM stats_daily WHERE session_id = ?",
        (session,),
    ).fetchone()
    if row["first"] is None:
        print(f"会话 {session} 在快照里没有任何天桶。", file=sys.stderr)
        return 2

    contacts = {contact.session_id: contact for contact in store.all_contacts()}
    grid = weekly_grid(str(row["first"]), str(row["last"]))
    for anchor in ANCHOR_DAYS:
        if anchor not in grid:
            grid.append(anchor)  # 兜底，保证断言日一定在网格上
    grid = sorted(grid)
    if from_day:
        grid = [day for day in grid if day >= from_day]
    if to_day:
        grid = [day for day in grid if day <= to_day]
    if not grid:
        print("窗口内没有任何网格日。", file=sys.stderr)
        return 2

    months = month_range("2025-01", "2026-02")
    index = synthetic_index(store, session, months)
    index_alt = synthetic_index(store, session, months, override_2026_01=(100, 100, 100))

    print(f"目标联系人：{session}（{store.get_contact(session).display_name if store.get_contact(session) else '?'}）")
    print(f"网格：{grid[0]} ~ {grid[-1]}，共 {len(grid)} 个点（每周一个）")
    print()
    print(
        "day          A      B      C    A对等  B对等  C对等  A深度  C深度"
    )
    print("------------" + "--------" * 8)

    a_by_day: dict[str, dict[str, dict[str, float]]] = {}
    b_by_day: dict[str, dict[str, dict[str, float]]] = {}
    c_by_day: dict[str, dict[str, dict[str, float]]] = {}
    base_cache: dict[str, tuple] = {}
    for day in grid:
        base = base_raws(store, contacts, day)
        base_cache[day] = base
        raws, raw_legacy, _ = base
        scores_a, scores_b, scores_c = score_variants(raws, raw_legacy, day, index)
        a_by_day[day] = scores_a
        b_by_day[day] = scores_b
        c_by_day[day] = scores_c
        a_ov, a_rec, a_dep = _row_values(scores_a, session)
        b_ov, b_rec, _ = _row_values(scores_b, session)
        c_ov, c_rec, c_dep = _row_values(scores_c, session)
        print(f"{day}  {a_ov} {b_ov} {c_ov} {a_rec} {b_rec} {c_rec} {a_dep} {c_dep}")

    failures: list[str] = []

    def check(no: str, label: str, ok: bool, detail: str) -> None:
        print(f"R{no}  {'PASS' if ok else 'FAIL'}  {label}  {detail}")
        if not ok:
            failures.append(f"R{no}")

    def target(day: str, variant: str) -> dict[str, float]:
        # 断言只关心目标联系人；缺日时返回空表由调用方判定 SKIP。
        return a_by_day[day].get(session, {}) if variant == "A" else (
            b_by_day[day].get(session, {}) if variant == "B" else c_by_day[day].get(session, {})
        )

    def same_day(day: str) -> bool:
        return day in grid and session in a_by_day[day]

    print()
    print("断言（阈值见 /tmp/insights-v3-plan.md 7.5）：")

    # R1/R2：升温期（2025-10..12）里新口径下限不再被旧口径压制。
    season = [day for day in grid if "2025-10-01" <= day <= "2025-12-31"]
    if season and all(same_day(day) for day in season):
        min_a = min(target(day, "A")["overall"] for day in season)
        min_b = min(target(day, "B")["overall"] for day in season)
        check("1", "min(B, 升温期) − min(A, 升温期) ≥ 3.0",
              min_b - min_a >= 3.0, f"{min_a:.2f} → {min_b:.2f}（Δ {min_b - min_a:+.2f}）")
        min_a_r = min(target(day, "A")["reciprocity"] for day in season)
        min_b_r = min(target(day, "B")["reciprocity"] for day in season)
        check("2", "min(B对等, 升温期) − min(A对等, 升温期) ≥ 15.0",
              min_b_r - min_a_r >= 15.0, f"{min_a_r:.2f} → {min_b_r:.2f}（Δ {min_b_r - min_a_r:+.2f}）")
    else:
        print("R1/R2  SKIP  窗口不含完整的 2025-10..12 升温期")

    if same_day("2025-12-24") and same_day("2025-09-03"):
        b_1224 = target("2025-12-24", "B")["overall"]
        b_0903 = target("2025-09-03", "B")["overall"]
        a_1224 = target("2025-12-24", "A")["overall"]
        c_1224 = target("2025-12-24", "C")["overall"]
        c_0903 = target("2025-09-03", "C")["overall"]
        check("3", "B(2025-12-24) − A(2025-12-24) ≥ 2.0",
              b_1224 - a_1224 >= 2.0, f"{a_1224:.2f} → {b_1224:.2f}（Δ {b_1224 - a_1224:+.2f}）")
        check("4", "B(2025-12-24) ≥ B(2025-09-03) − 1.5（升温期不再净倒扣）",
              b_1224 >= b_0903 - 1.5, f"{b_0903:.2f} → {b_1224:.2f}（Δ {b_1224 - b_0903:+.2f}）")
        check("5", "C(2025-12-24) − C(2025-09-03) ≥ 2.0（LLM 让升温期由跌转涨）",
              c_1224 - c_0903 >= 2.0, f"{c_0903:.2f} → {c_1224:.2f}（Δ {c_1224 - c_0903:+.2f}）")
    else:
        print("R3–R5  SKIP  网格缺 2025-09-03 或 2025-12-24")

    if same_day("2026-02-25"):
        c_0225 = target("2026-02-25", "C")["overall"]
        c_0225_depth = target("2026-02-25", "C")["depth"]
        if same_day("2025-09-03"):
            c_0903 = target("2025-09-03", "C")["overall"]
            c_0903_depth = target("2025-09-03", "C")["depth"]
            check("6", "C(2026-02-25) − C(2025-09-03) ≥ 8.0",
                  c_0225 - c_0903 >= 8.0, f"{c_0903:.2f} → {c_0225:.2f}（Δ {c_0225 - c_0903:+.2f}）")
            check("7", "C深度(2026-02-25) − C深度(2025-09-03) ≥ 30.0",
                  c_0225_depth - c_0903_depth >= 30.0,
                  f"{c_0903_depth:.2f} → {c_0225_depth:.2f}（Δ {c_0225_depth - c_0903_depth:+.2f}）")
        else:
            print("R6/R7  SKIP  网格缺 2025-09-03")
        # R8：改 2026-01 的时段分，2025-12-24 这一刻必须毫无变化——那个时段
        # 尚未收口、period_end 在 asof 之后，铁律 1 不容许看到未来。
        if same_day("2025-12-24"):
            raws, raw_legacy, _ = base_cache["2025-12-24"]
            _, _, c_alt = score_variants(raws, raw_legacy, "2025-12-24", index_alt)
            c_base = c_by_day["2025-12-24"][session]
            if c_alt:
                c_alt_values = c_alt.get(session, {})
                delta = abs(c_alt_values.get("overall", 0.0) - c_base["overall"])
                check("8", "改 2026-01 时段分不影响 C(2025-12-24)",
                      delta < 1e-9, f"Δ {delta:.3g}")
            else:
                print("R8  SKIP  2025-12-24 无目标评分")
        else:
            print("R8  SKIP  网格缺 2025-12-24")
    else:
        print("R6–R8  SKIP  网格缺 2026-02-25 或 2025-12-24")

    # R9/R10：防呆断言——权重写错或 mutual_cost_per_day 除数用错会让整个
    # cohort 一起平移，这两条会立刻炸；单日最大值取两个断言日里更大的那个。
    cohort_days = [day for day in ("2025-11-26", "2026-02-25") if day in grid]
    if cohort_days:
        max_shift = 0.0
        worst_day = ""
        for day in cohort_days:
            cohort_a = a_by_day[day]
            cohort_b = b_by_day[day]
            median_a = median([v["overall"] for v in cohort_a.values()])
            median_b = median([v["overall"] for v in cohort_b.values()])
            check("9", f"中位数位移 |median(B) − median(A)| ≤ 2.0（{day}）",
                  abs(median_b - median_a) <= 2.0, f"{median_a:.2f} → {median_b:.2f}（Δ {median_b - median_a:+.2f}）")
            shift = max(
                abs(cohort_b[sid]["overall"] - cohort_a[sid]["overall"])
                for sid in cohort_a
            )
            if shift > max_shift:
                max_shift = shift
                worst_day = day
        check("10", f"单人综合分最大位移 max|B − A| ≤ 8.0（{worst_day}）",
              max_shift <= 8.0, f"{max_shift:.2f}")
    else:
        print("R9/R10  SKIP  网格缺 2025-11-26 与 2026-02-25")

    print()
    if failures:
        print(f"失败：{', '.join(failures)}")
        return 1
    print("全部通过")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="关系温度升温回归（只读快照，零网络）")
    parser.add_argument("--db", required=True, help="metrics.db 只读快照路径")
    # 不给默认值：本仓库公开，真实 session_id 属于身份信息，不能落进源码。
    parser.add_argument("--session", required=True, help="目标联系人 session_id")
    parser.add_argument("--from", dest="from_day", help="网格起点（含）")
    parser.add_argument("--to", dest="to_day", help="网格终点（含）")
    args = parser.parse_args()
    return run(args.db, args.session, args.from_day, args.to_day)


if __name__ == "__main__":
    sys.exit(main())
