"""aiohttp 接口测试的共享件：假分析器、payload 构造器与基础用例。

不匹配 test*.py，不会被 pytest 收集，与 tests/support.py 同类角色。
test_server.py 与 test_server_breakup.py 共用同一套联系人 fixture：
SESSION_ID="friend" 打过一次分、有相识日、有一天的 stats_daily 天桶。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from aiohttp.test_utils import AioHTTPTestCase

from wechat_insights.analyzer import AnalysisResult
from wechat_insights.metrics import Metrics
from wechat_insights.server import InsightsRuntime, create_app
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


class ApiTestCase(AioHTTPTestCase):
    """带一个已打分联系人的 aiohttp 测试基类。

    ApiTests（test_server.py）与 BreakupApiTests（test_server_breakup.py）
    共用这份 fixture，各自只放跟自己职责相关的断言方法。
    """

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
