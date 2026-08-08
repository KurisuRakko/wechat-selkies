"""详情页用的派生视图：把天桶合成月度序列与类型构成。

这些都是单个联系人、走 (session_id, day) 主键的小范围聚合，不是全表扫描。
"""

from __future__ import annotations

from .constants import MESSAGE_KINDS
from .metrics import Metrics
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
