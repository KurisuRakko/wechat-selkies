"""详情页与年报页用的派生视图：把天桶合成月度序列、类型构成与年度总结。

详情页的视图是单个联系人、走 (session_id, day) 主键的小范围聚合；年报是
整库范围的窗口聚合，同样不碰消息原文——只有统计数字。
"""

from __future__ import annotations

import calendar
from datetime import datetime

from . import llm
from .constants import MESSAGE_KINDS
from .masking import mask
from .metrics import Metrics, day_key
from .scoring import reply_median


def monthly_series(days: list[tuple[str, Metrics]]) -> list[dict[str, object]]:
    """按自然月合成消息量与双方回复延迟中位数，按月份升序。"""

    months: dict[str, Metrics] = {}
    for day, metrics in days:
        month = day[:7]
        bucket = months.get(month)
        if bucket is None:
            bucket = Metrics()
            months[month] = bucket
        bucket.merge(metrics)

    series: list[dict[str, object]] = []
    for month in sorted(months):
        window = months[month]
        series.append(
            {
                "month": month,
                "in": window.get("msgs_them"),
                "out": window.get("msgs_me"),
                "reply_median_them": reply_median(
                    window.reply_hist_them, window.get("replies_them")
                ),
                "reply_median_me": reply_median(
                    window.reply_hist_me, window.get("replies_me")
                ),
            }
        )
    return series


def type_composition(total: Metrics) -> list[dict[str, object]]:
    """双向合计的消息类型构成，去掉计数为零的类型，按数量降序。"""

    items = [
        {
            "kind": kind,
            "count": total.get(f"kind_{kind}_them") + total.get(f"kind_{kind}_me"),
        }
        for kind in MESSAGE_KINDS
    ]
    items = [item for item in items if int(item["count"]) > 0]
    items.sort(key=lambda item: -int(item["count"]))
    return items


def total_metrics(days: list[tuple[str, Metrics]]) -> Metrics:
    total = Metrics()
    for _, metrics in days:
        total.merge(metrics)
    return total


# —— 友谊年报 ——
# 「淡出的人」判定：上一年往来不少于该条数、且当年不足上一年该比例的
# 联系人才算在淡出——阈值太低会把本来就不常聊的人也算进去。
REPORT_FADE_MIN_PREVIOUS_MESSAGES = 100
REPORT_FADE_DROP_RATIO = 0.1
# 各榜单的人数上限。
REPORT_TOP_LIMIT = 5
REPORT_NIGHT_LIMIT = 3
REPORT_WEEKEND_LIMIT = 3
REPORT_NEW_FRIENDS_LIMIT = 8
REPORT_FADED_LIMIT = 8
# 叙事正文的防御性长度上限（prompt 已要求 150 字以内，上限只防模型抽风）。
REPORT_NARRATIVE_MAX_CHARS = 500


def yearly_report(store, year: int, now: int) -> dict:
    """合成一整年的年报统计。

    窗口 = 当年 1/1 到 min(12/31, 今天)，当年与上一年各 load_window 一次
    （「淡出的人」要跨年对比）。纯本地聚合，不调用任何外部服务；「新朋友」
    （first_message_at 落在当年）与「哈哈哈之王」（max_laugh_run）来自
    contacts 表的全时段里程碑。
    """

    today = day_key(now)
    start_day = f"{year:04d}-01-01"
    end_day = min(f"{year:04d}-12-31", today)
    previous_start = f"{year - 1:04d}-01-01"
    previous_end = f"{year - 1:04d}-12-31"

    windows = store.load_window(start_day, end_day)
    previous = store.load_window(previous_start, previous_end)
    contacts = {contact.session_id: contact for contact in store.all_contacts()}

    incoming = int(sum(window.get("msgs_them") for window in windows.values()))
    outgoing = int(sum(window.get("msgs_me") for window in windows.values()))

    def rank(key, limit: int) -> list[dict]:
        """按 key(metrics) 在当年窗口内排序取前 limit 名，只留计数大于 0 的。"""

        rows = []
        for session_id, metrics in windows.items():
            count = int(key(metrics))
            if count <= 0:
                continue
            contact = contacts.get(session_id)
            rows.append(
                {
                    "display_name": (
                        contact.display_name if contact is not None else session_id
                    ),
                    "hash": contact.hash if contact is not None else "",
                    "messages": count,
                }
            )
        rows.sort(key=lambda row: -row["messages"])
        return rows[:limit]

    def totals(metrics: Metrics, name: str) -> int:
        return int(metrics.get(f"{name}_them") + metrics.get(f"{name}_me"))

    # 新朋友：first_message_at 落在当年（[当年 1/1 00:00, 次年 1/1 00:00)），
    # 按当年消息量降序，至多 8 个。
    year_start_ts = int(datetime(year, 1, 1).timestamp())
    year_end_ts = int(datetime(year + 1, 1, 1).timestamp())
    new_friends = []
    for session_id, contact in contacts.items():
        first = contact.first_message_at
        if first is None or not year_start_ts <= first < year_end_ts:
            continue
        window = windows.get(session_id)
        if window is None:
            continue
        new_friends.append(
            {
                "display_name": contact.display_name,
                "hash": contact.hash,
                "messages": int(window.messages_total()),
            }
        )
    new_friends.sort(key=lambda row: -row["messages"])
    new_friends = new_friends[:REPORT_NEW_FRIENDS_LIMIT]

    # 淡出的人：上一年往来 ≥100 条、当年不足上一年 10%（当年 0 条也算）。
    # 完全沉寂的旧友只有上一年的天桶，不在当年窗口里，所以要遍历上一年。
    faded = []
    empty = Metrics()
    for session_id, metrics in previous.items():
        previous_total = int(metrics.messages_total())
        if previous_total < REPORT_FADE_MIN_PREVIOUS_MESSAGES:
            continue
        this_total = int(windows.get(session_id, empty).messages_total())
        if this_total >= previous_total * REPORT_FADE_DROP_RATIO:
            continue
        contact = contacts.get(session_id)
        faded.append(
            {
                "display_name": (
                    contact.display_name if contact is not None else session_id
                ),
                "hash": contact.hash if contact is not None else "",
                "previous_messages": previous_total,
                "messages": this_total,
            }
        )
    faded.sort(key=lambda row: -row["previous_messages"])
    faded = faded[:REPORT_FADED_LIMIT]

    # 哈哈哈之王：全时段成就，max_laugh_run 跨年累计，与年份无关。
    haha_king = None
    king = max(
        (contact for contact in contacts.values() if contact.max_laugh_run > 0),
        key=lambda contact: contact.max_laugh_run,
        default=None,
    )
    if king is not None:
        haha_king = {
            "display_name": king.display_name,
            "hash": king.hash,
            "max_laugh_run": king.max_laugh_run,
        }

    return {
        "year": year,
        "window": {"start": start_day, "end": end_day},
        "overview": {
            "messages": incoming + outgoing,
            "contacts": len(windows),
            "incoming": incoming,
            "outgoing": outgoing,
        },
        "top": rank(lambda metrics: metrics.messages_total(), REPORT_TOP_LIMIT),
        "night": rank(
            lambda metrics: totals(metrics, "night_msgs"), REPORT_NIGHT_LIMIT
        ),
        "weekend": rank(
            lambda metrics: totals(metrics, "weekend_msgs"), REPORT_WEEKEND_LIMIT
        ),
        "new_friends": new_friends,
        "faded": faded,
        "haha_king": haha_king,
        "monthly": _monthly_message_counts(store, year, start_day, end_day),
    }


def _monthly_message_counts(
    store, year: int, start_day: str, end_day: str
) -> list[dict]:
    """当年 1–12 月的消息总量，固定 12 格；窗口外的月份计数自然为 0。"""

    months = []
    for month in range(1, 13):
        first = f"{year:04d}-{month:02d}-01"
        last = f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]}"
        total = int(
            sum(
                window.messages_total()
                for window in store.load_window(first, last).values()
            )
        )
        months.append({"month": f"{year:04d}-{month:02d}", "count": total})
    return months


REPORT_NARRATIVE_SYSTEM_PROMPT = (
    "你是一个温暖但不腻的年度报告撰稿人。根据用户这一年的聊天统计数字，写一段 "
    "150 字以内的年度总结，第二人称，指出模式与变化，不要报菜名式罗列数字，"
    "不要提到任何人名。只回复正文。"
)


def narrative_user_text(report: dict) -> str:
    """年报叙事的输入：只含匿名聚合数字。

    这里**故意**不含任何联系人名字与聊天文本——这是设计约束，不是疏忽：
    叙事模型只能看到总量、月度分布与榜单数字，无法据此识别任何人。调用方
    仍须把拼接结果整体过 mask()（出站扼流点纪律）。
    """

    overview = report["overview"]
    return "\n".join(
        [
            f"今年（{report['year']} 年）的聊天统计：",
            f"总消息数 {overview['messages']} 条，覆盖 {overview['contacts']} 位联系人；"
            f"对方发来 {overview['incoming']} 条，我发出 {overview['outgoing']} 条。",
            f"聊得最多的前 {len(report['top'])} 位联系人，消息量分别是 "
            f"{_counts(report['top'])} 条。",
            f"深夜（23 点至凌晨 2 点）消息最多的前 {len(report['night'])} 位，"
            f"分别是 {_counts(report['night'])} 条。",
            f"周末消息最多的前 {len(report['weekend'])} 位，分别是 "
            f"{_counts(report['weekend'])} 条。",
            f"这一年新认识了 {len(report['new_friends'])} 位联系人，"
            f"有 {len(report['faded'])} 位上一年很热络的老朋友明显淡出。",
            f"每月消息量依次是：{_counts(report['monthly'], key='count')}。",
        ]
    )


def _counts(rows: list[dict], key: str = "messages") -> str:
    """把一行行的数字用「、」连起来，例如「1234、987、654」。"""

    return "、".join(str(row[key]) for row in rows)


def generate_narrative(report: dict) -> str | None:
    """用配置的 LLM 生成年报叙事正文；失败返回 None（调用方降级为无叙事）。

    只把匿名聚合数字（narrative_user_text）发出去，整体过 mask() 后才出站。
    """

    reply = llm.chat(
        REPORT_NARRATIVE_SYSTEM_PROMPT, mask(narrative_user_text(report))
    )
    if not reply:
        return None
    return reply.strip()[:REPORT_NARRATIVE_MAX_CHARS]
