"""离线分析器：增量读取私聊消息 → 按天聚合 → 预计算看板数据。

解密、快照、账户白名单、私聊过滤全部复用 wechat_history；这里只负责游标推进与
统计。消息原文读进内存做完词法统计就丢弃，不写入 metrics.db。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from wechat_history.reader import HistoryReader
from wechat_history.sessions import scan_direct_rows

from .classify import classify_contacts
from .constants import (
    BACKFILL_BATCH,
    DECAY_HALF_LIFE_DAYS,
    FADE_LIST_LIMIT,
    FADE_MIN_GAP_DAYS,
    FADE_MIN_OVERALL,
    MIN_SCORE_MESSAGES,
    SCORE_WINDOW_DAYS,
    SESSION_GAP_SECONDS,
    TREND_BASELINE_DAYS,
    TREND_RECENT_DAYS,
)
from .conversation import Message, split_conversations
from .depth import DepthStrategy, get_depth_strategy
from .history import (
    apply_formula_reset,
    backfill_history,
    prune_pre_acquaintance,
    refine_daily_history,
)
from .metrics import (
    Aggregation,
    Metrics,
    aggregate,
    day_key,
    day_span,
    decayed_span,
    decayed_weight,
    late_night_offset,
)
from .periods import PeriodIndex, refresh_periods
from .portrait import refresh_portraits
from .reading import Cursor, read_messages_after
from .scoring import (
    DIMENSION_NAMES,
    anomalies_key,
    detect_anomalies,
    median,
    raw_metrics,
    score_cohort,
)
from .storage import ContactRow, MetricsStore, WindowStats


LOG = logging.getLogger("wechat-insights")


@dataclass(slots=True)
class AnalysisResult:
    started_at: int
    duration_seconds: float
    sessions: int = 0
    messages_read: int = 0
    scored: int = 0
    llm_scored: int = 0
    classified: int = 0
    # 本轮写入的时段化 LLM 分行数。
    llm_periods: int = 0
    # 本轮全史回放写入的采样点行数（关系温度曲线补上部署日之前的历史）。
    history_points: int = 0
    # 本轮逐日细化写入的采样点行数（每日粒度联系人从相识日逐日补点）。
    refined_points: int = 0
    per_session: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ScoresAsOf:
    """打分内核在「某一时刻」的输出：今日打分与全史回放共用，没有第二份真相。

    stats 是窗口统计（零消息判定、window_messages）；raw 是百分位之前的原始值
    （「正在淡出」的 current_gap_days 从这里读）；scores 是 score_cohort 的结果
    （{} 表示没有对照系，整体按未打分处理）；eligible 是参与 cohort 的会话名单
    （中位数等派生统计的基数）；contacts 是全部联系人。
    """

    stats: dict[str, WindowStats]
    raw: dict[str, dict[str, float | None]]
    scores: dict[str, dict[str, float]]
    eligible: list[str]
    contacts: dict[str, ContactRow]
    equivalent_days: float


def update_milestones(
    contact: ContactRow, messages: list[Message], aggregation: Aggregation
) -> None:
    """把一批已提交的消息折进联系人级里程碑。

    这些量只能顺序推进（最长沉默要看相邻两条的间隔），所以放在联系人行上增量维护。
    """

    previous = contact.last_message_at
    for message in messages:
        if previous is not None:
            silence = message.timestamp - previous
            if silence > contact.longest_silence_seconds:
                contact.longest_silence_seconds = silence
                contact.longest_silence_ended_at = message.timestamp
        previous = message.timestamp

        offset = late_night_offset(message.timestamp)
        if offset > contact.latest_night_offset:
            contact.latest_night_offset = offset
            contact.latest_night_at = message.timestamp

    first = messages[0].timestamp
    if contact.first_message_at is None or first < contact.first_message_at:
        contact.first_message_at = first
    contact.last_message_at = messages[-1].timestamp
    contact.total_messages += len(messages)
    contact.max_laugh_run = max(contact.max_laugh_run, aggregation.max_laugh_run)


class Analyzer:
    """一轮完整分析：拉新消息 → 聚合落库 → 重算全部看板数据。"""

    def __init__(
        self,
        store: MetricsStore,
        # 默认直接用 wechat_history 的读取器（含密钥、快照、缓存上限的全套默认
        # 配置）；测试从这里换成假读取器。
        reader_factory=HistoryReader,
        strategy: DepthStrategy | None = None,
        gap_seconds: int = SESSION_GAP_SECONDS,
        batch_size: int = BACKFILL_BATCH,
        # 进度上报回调：每完成一个步骤收一次 {phase, detail, done, total}。
        progress_cb: Callable[[dict], None] | None = None,
        # 时段分索引注入口：脚本与测试塞入内存索引用；None 时按需整表读。
        period_index: PeriodIndex | None = None,
    ):
        self.store = store
        self.reader_factory = reader_factory
        self.strategy = strategy or get_depth_strategy()
        self.gap_seconds = gap_seconds
        self.batch_size = batch_size
        self._progress_cb = progress_cb
        self._period_override = period_index
        self._period_index: PeriodIndex | None = None

    @property
    def period_index(self) -> PeriodIndex:
        """时段分索引：显式注入的永远优先；否则整表读一次、缓存到本轮结束。"""

        if self._period_override is not None:
            return self._period_override
        if self._period_index is None:
            self._period_index = PeriodIndex.load(self.store)
        return self._period_index

    def _report(self, **fields: object) -> None:
        """进度上报：cb 缺省时零开销；cb 抛异常只记调试日志，绝不能弄挂分析。"""

        if self._progress_cb is None:
            return
        try:
            self._progress_cb(fields)
        except Exception:
            LOG.debug("进度上报回调异常", exc_info=True)

    def run(self, now: int | None = None) -> AnalysisResult:
        moment = int(time.time()) if now is None else int(now)
        started = time.monotonic()
        result = AnalysisResult(started_at=moment, duration_seconds=0.0)

        reader = self.reader_factory()
        try:
            sessions = scan_direct_rows(reader)
            contacts = reader._load_contacts()
            result.sessions = len(sessions)
            self._report(phase="sync", done=0, total=len(sessions), detail="")
            for done, session_id in enumerate(sorted(sessions)):
                session = sessions[session_id]
                self._report(
                    phase="sync",
                    done=done,
                    total=len(sessions),
                    detail=session.display_name,
                )
                try:
                    read = self._sync_session(
                        reader, session_id, session.display_name, contacts, moment
                    )
                except Exception:
                    LOG.exception("跳过会话 %s：增量读取失败", session.display_name)
                    continue
                if read:
                    result.messages_read += read
                    result.per_session[session.display_name] = read
            # LLM 深度打分与关系分类要在 reader 关闭前做（它们还要从快照里
            # 读采样文本）。它们是可选的加分项：出任何意外都不能拖垮整轮
            # 打分，隔离处理。
            if self.strategy.name == "llm":
                try:
                    result.llm_scored = refresh_portraits(
                        self.store,
                        reader,
                        self.strategy,
                        self.gap_seconds,
                        moment,
                        self._report,
                    )
                except Exception:
                    LOG.exception("关系画像刷新失败，本轮跳过")
                # 关系类型分类同样要用 reader 读全时段样本；分类失败只影响
                # 本轮判定，下一轮再试，不拖垮打分。
                try:
                    result.classified = classify_contacts(
                        self.store, reader, self.gap_seconds, self._report
                    )
                except Exception:
                    LOG.exception("关系类型分类失败，本轮跳过")
                # 时段化 LLM 评分排在分类之后：本轮刚判成事务往来的联系人
                # 立刻被 _pending 排除，不浪费调用。可选项，出意外不拖垮打分。
                try:
                    result.llm_periods = refresh_periods(
                        self.store, reader, self.gap_seconds, moment, self._report
                    )
                except Exception:
                    LOG.exception("时段化 LLM 评分失败，本轮跳过")
        finally:
            reader.close()

        # 本轮刚写入的时段行必须被看见：缓存失效，下一处使用会整表重读。
        self._period_index = None
        self._report(phase="score", done=0, total=0, detail="")
        result.scored = self._recompute(moment)
        # 一次性迁移：清掉旧口径在相识之前铺下的前导 0 段（回放现在从第一条
        # 消息起画），meta 标记已写就不再跑，详见函数 docstring。
        prune_pre_acquaintance(self.store, moment)
        # 打分口径版本变了就清历史标记与逐日进度，让下面这行自动重放全史
        # （重置是一次性的，见 apply_formula_reset）。
        apply_formula_reset(self.store)
        # 关系温度全史回放：今日打分之后补上部署日之前的历史，与今日共用
        # 同一个打分内核（_scores_asof），没有第二份真相。
        result.history_points = backfill_history(
            self.store, self._scores_asof, moment, self._report
        )
        # 每日粒度的联系人从相识日逐日补点：进度每天落库，断点续跑。
        result.refined_points = refine_daily_history(
            self.store, self._scores_asof, moment, self._report
        )
        result.duration_seconds = time.monotonic() - started

        if result.messages_read:
            LOG.info(
                "本轮读取 %d 条新消息，覆盖 %d 个会话：%s",
                result.messages_read,
                len(result.per_session),
                "、".join(
                    f"{name} {count}"
                    for name, count in sorted(
                        result.per_session.items(), key=lambda item: -item[1]
                    )[:10]
                ),
            )
        else:
            LOG.info("本轮没有新消息，%d 个私聊会话游标保持不变", result.sessions)
        return result

    def _sync_session(
        self,
        reader: HistoryReader,
        session_id: str,
        display_name: str,
        contacts: dict[str, dict],
        moment: int,
    ) -> int:
        contact = self.store.ensure_contact(session_id, display_name)
        cursor = Cursor(
            contact.cursor_timestamp, contact.cursor_shard, contact.cursor_local_id
        )
        committed = 0

        while True:
            batch = read_messages_after(
                reader, session_id, display_name, contacts, cursor, self.batch_size
            )
            if batch.last is None:
                break

            if not batch.messages:
                # 整批都是无方向的系统噪声，没有可统计的内容，只推进游标。
                cursor = batch.last
                self._advance(contact, cursor, display_name, {})
                if not batch.has_more:
                    break
                continue

            conversations = split_conversations(batch.messages, self.gap_seconds)
            # 最后一段对话可能还没结束（后面还有消息，或者刚聊完不久），
            # 留到下一轮连同后续消息一起统计，游标也就停在它前面。
            still_open = (
                batch.has_more
                or moment - conversations[-1][-1].timestamp <= self.gap_seconds
            )
            held_tail = False
            if still_open and len(conversations) > 1:
                conversations = conversations[:-1]
                held_tail = True
            elif still_open and not batch.has_more:
                conversations = []
                held_tail = True
            # still_open 且只有一段又被截断：这段对话比一个批次还长，只能就地
            # 提交，否则游标永远推不动。代价是这段超长对话被从中间拆成两段，
            # 对话级指标（轮次、平均长度）按两段分别统计；6 小时会话间隔下
            # 日常睡眠就会切段，单段超过一个批次（默认 5000 条）实际不常见。

            if not conversations:
                break

            messages = [message for group in conversations for message in group]
            aggregation = aggregate(conversations)
            update_milestones(contact, messages, aggregation)

            # 留了尾巴就只能停在最后一条已统计的消息上；整批提交时可以直接跨到
            # 本批最后一行，把尾部被过滤掉的系统噪声一并跳过。对话是分批消息的
            # 连续切片，截断后已提交消息仍是 batch.messages 的前缀，所以最后一条
            # 的位置是 positions[len(messages) - 1]。
            cursor = (
                batch.positions[len(messages) - 1] if held_tail else batch.last
            )
            self._advance(contact, cursor, display_name, aggregation.buckets)
            committed += len(messages)

            if not batch.has_more:
                break
            LOG.info("回填 %s：已处理 %d 条", display_name, committed)

        return committed

    def _advance(
        self,
        contact: ContactRow,
        cursor: Cursor,
        display_name: str,
        buckets: dict[str, Metrics],
    ) -> None:
        """在一个事务里写下这一批的指标与新游标。"""

        contact.cursor_timestamp = cursor.timestamp
        contact.cursor_local_id = cursor.local_id
        contact.cursor_shard = cursor.shard
        contact.display_name = display_name
        self.store.commit_batch(contact.session_id, buckets, contact)

    @staticmethod
    def _window_bounds(moment: int) -> tuple[str, str, str, str]:
        """异动对比窗口的日键边界：(today, recent_start, baseline_end, baseline_start)。

        _recompute 与 LLM 异动指纹共用这一份换算，两处不会各写各的窗口边界。
        """

        today = day_key(moment)
        recent_start = day_key(moment - TREND_RECENT_DAYS * 86400)
        baseline_end = day_key(moment - (TREND_RECENT_DAYS + 1) * 86400)
        baseline_start = day_key(
            moment - (TREND_RECENT_DAYS + TREND_BASELINE_DAYS) * 86400
        )
        return today, recent_start, baseline_end, baseline_start

    def _weight_of(self, today: str, day: str) -> float:
        """打分窗口内一个天桶的衰减权重：天龄 = 距今天数（今天为 0）。"""

        age = day_span(day, today) - 1
        return decayed_weight(age, DECAY_HALF_LIFE_DAYS)

    @staticmethod
    def _longest_gap(
        stats: WindowStats, contact: ContactRow, score_start: str, score_start_ts: int
    ) -> int:
        """窗口内最大沉默间隔，认识早于窗口起点的纳入前导空档。

        首条消息在窗口内的新朋友不算前导空档——认识之前不存在沉默。
        尾部空档不算：那是 current_gap_days 的职责，分开避免双重计罚。
        """

        longest = stats.longest_gap_days
        if (
            stats.first_day is not None
            and contact.first_message_at is not None
            and contact.first_message_at < score_start_ts
        ):
            longest = max(longest, day_span(score_start, stats.first_day) - 1)
        return longest

    @staticmethod
    def _kind_source(contact: ContactRow) -> str:
        """关系类型来源：手动改判 > 自动判定 > 默认（未判定）。"""

        if contact.kind_manual:
            return "manual"
        if contact.kind_auto:
            return "auto"
        return "default"

    @staticmethod
    def _unscored_payload(
        contact: ContactRow,
        stats: WindowStats | None,
        recent_windows: dict[str, Metrics],
        empty: Metrics,
        kind: str,
        kind_source: str,
        sample_note: str,
        zeroed: bool = False,
    ) -> dict[str, object]:
        """未打分分支的公共 payload：事务往来 / 家人零消息 / 普通归零。

        zeroed 只在普通归零时为真：整体 0 分、七维全 0。事务往来与家人零
        消息是「不打分」（None），与「归零」是两回事，前端展示也不同。
        """

        payload = {
            "hash": contact.hash,
            "display_name": contact.display_name,
            "scored": False,
            "overall": 0.0 if zeroed else None,
            "dimensions": (
                {name: 0.0 for name in DIMENSION_NAMES}
                if zeroed
                else {name: None for name in DIMENSION_NAMES}
            ),
            "trends": None,
            "recent_messages": recent_windows.get(
                contact.session_id, empty
            ).messages_total(),
            "window_messages": (
                stats.raw.messages_total() if stats is not None else 0
            ),
            "last_message_at": contact.last_message_at,
            "sample_note": sample_note,
            "anomalies": [],
            "relation_kind": kind,
            "kind_source": kind_source,
        }
        if zeroed:
            payload["zeroed"] = True
        return payload

    def _scores_asof(
        self,
        moment: int,
        *,
        gap_override: dict[str, float | None] | None = None,
    ) -> ScoresAsOf:
        """打分内核：把「某一时刻」能看到的天桶换算成每个联系人的七维分与综合分。

        今日打分（_recompute）与全史回放（_backfill_history）共用这一份换算，
        历史回放不许复制出第二份真相。与今日路径的差异集中注释在这里：
        - current_gap_days 按「窗口内最后活跃日到 asof_day 的天数」推算——
          不能拿 contact.last_message_at 这种「现在」的知识穿越回过去；
          今日路径用 gap_override 传回 contact.last_message_at 口径。
        - 时段化 LLM 分按 period_end ≤ asof_day 可见、每个时段只取最新一张
          快照：回放永远不会看到未来（未收口当月的快照 period_end 记的是
          评分当天），重放出来的点与当初实时写下的值逐字一致。
        """

        asof_day = day_key(moment)
        # 窗口起点用「N 天前的同一时刻」折算，跨 DST 切换时本地时间会偏移
        # 一小时、日键偶尔差一天（生产容器在澳大利亚/悉尼，有夏令时），
        # 目前按秒数近似，不做逐天修正。
        score_start = day_key(moment - SCORE_WINDOW_DAYS * 86400)
        score_start_ts = moment - SCORE_WINDOW_DAYS * 86400

        # 窗口终点 = asof_day：回放到历史某天时只看得见那天之前的天桶，
        # 「未来」的消息不会泄漏进窗口。
        stats = self.store.load_window_stats(
            score_start, asof_day, lambda day: self._weight_of(asof_day, day)
        )
        contacts = {
            contact.session_id: contact for contact in self.store.all_contacts()
        }

        # 门槛只看未加权的原始消息数：衰减是「远记忆变淡」，不是「远消息作废」。
        # 事务往来整体剔除：不打分、不进百分位 cohort——订票、快递这类目的性
        # 沟通的消息量会污染参照系，把正常朋友的相对分压扁。
        eligible = sorted(
            session_id
            for session_id, window in stats.items()
            if window.raw.messages_total() >= MIN_SCORE_MESSAGES
            and contacts[session_id].relation_kind() != "transactional"
        )

        # 打分窗口的日均除数用 decayed_span 的「等效天数」：均匀活跃的人加权前后
        # 日均一致，两年的衰减窗口与近 30 天窗口的日均仍可直接比较。
        equivalent_days = decayed_span(
            day_span(score_start, asof_day), DECAY_HALF_LIFE_DAYS
        )
        raw_score: dict[str, dict[str, float | None]] = {}
        for session_id in eligible:
            window = stats[session_id]
            contact = contacts[session_id]
            current_gap = (
                day_span(window.last_day, asof_day) - 1
                if window.last_day is not None
                else None
            )
            if gap_override is not None and session_id in gap_override:
                # 今日路径：沉默可能发生在窗口末活跃日之后（最近一个月没聊），
                # 只有「现在」的 last_message_at 才知道，保持原口径。
                current_gap = gap_override[session_id]
            extras: dict[str, float | None] = {
                "active_day_rate": window.active_weight / equivalent_days,
                "current_gap_days": current_gap,
                "longest_gap_days": self._longest_gap(
                    window, contact, score_start, score_start_ts
                ),
            }
            # 时段化 LLM 分：今日打分与全史回放走同一条路径，只有「这一刻能
            # 看见哪些时段」不同。没有可见时段时三个值都是 None，缺值权重按
            # score_cohort 的既有机制回流给同维度其余项，不需要任何特判。
            extras.update(self.period_index.asof(session_id, asof_day))
            raw_score[session_id] = raw_metrics(
                window.weighted, self.strategy, equivalent_days, extras
            )
        scores = score_cohort(raw_score, self.strategy)
        return ScoresAsOf(
            stats=stats,
            raw=raw_score,
            scores=scores,
            eligible=eligible,
            contacts=contacts,
            equivalent_days=equivalent_days,
        )

    def _recompute(self, moment: int) -> int:
        """重算所有联系人的七维分、趋势与异动，整体替换 scores 表。

        分数本体由 _scores_asof 内核算出；这里叠加今日专属的部分：趋势/异动
        窗口、payload 组装、温度历史每日记点与「正在淡出」。
        """

        today, recent_start, baseline_end, baseline_start = self._window_bounds(moment)
        contacts = {contact.session_id: contact for contact in self.store.all_contacts()}
        asof = self._scores_asof(
            moment,
            gap_override={
                session_id: (
                    (moment - contact.last_message_at) / 86400
                    if contact.last_message_at is not None
                    else None
                )
                for session_id, contact in contacts.items()
            },
        )
        score_stats = asof.stats
        contacts_by_id = asof.contacts
        eligible = asof.eligible
        scores = asof.scores
        raw_score = asof.raw
        equivalent_days = asof.equivalent_days

        recent_windows = self.store.load_window(recent_start, today)
        baseline_windows = self.store.load_window(baseline_start, baseline_end)

        empty = Metrics()
        raw_recent = {
            session_id: raw_metrics(
                recent_windows.get(session_id, empty),
                self.strategy,
                day_span(recent_start, today),
            )
            for session_id in eligible
        }
        raw_baseline = {
            session_id: raw_metrics(
                baseline_windows.get(session_id, empty),
                self.strategy,
                day_span(baseline_start, baseline_end),
            )
            for session_id in eligible
        }

        # 趋势 = 同一批联系人内，近期窗口的百分位 减去 基线窗口的百分位。
        recent_scores = score_cohort(raw_recent, self.strategy)
        baseline_scores = score_cohort(raw_baseline, self.strategy)

        # 画像字段只属于今日详情页 payload，在 _scores_asof 之外单独读：
        # 打分内核不依赖「当前」时刻的缓存，历史回放才没有第二份真相。
        llm_scores = self.store.all_llm_depth() if self.strategy.name == "llm" else {}
        payloads: list[tuple[str, dict[str, object]]] = []
        for contact in contacts_by_id.values():
            session_id = contact.session_id
            kind = contact.relation_kind()
            kind_source = self._kind_source(contact)
            stats = score_stats.get(session_id)
            if kind == "transactional":
                # 事务往来不参与打分、不进百分位 cohort：目的性沟通的消息量
                # 会污染参照系。不归零、不记温度历史、不进「正在淡出」。
                payload = self._unscored_payload(
                    contact, stats, recent_windows, empty, kind, kind_source,
                    "事务往来，不参与打分",
                )
                payloads.append((session_id, payload))
                continue
            if stats is None or stats.raw.messages_total() <= 0:
                if kind == "family":
                    # 家人久不聊天不代表疏远：窗口内零消息不归零、不记 0 分
                    # 历史，只标注原因；有数据时照常打分记历史。
                    payload = self._unscored_payload(
                        contact, stats, recent_windows, empty, kind, kind_source,
                        "家人，久未聊天不代表疏远",
                    )
                else:
                    # 打分窗口里一条消息都没有（或根本没有天桶）：两年内没有往来，
                    # 全部归零；有消息但不够门槛的仍走下面的「数据不足」。
                    payload = self._unscored_payload(
                        contact, stats, recent_windows, empty, kind, kind_source,
                        "两年内没有往来", zeroed=True,
                    )
                payloads.append((session_id, payload))
                continue
            scored = session_id in scores
            anomalies = (
                detect_anomalies(
                    raw_recent[session_id],
                    raw_baseline[session_id],
                    recent_windows.get(session_id, empty),
                    baseline_windows.get(session_id, empty),
                )
                if scored
                else []
            )
            dimension_scores = scores.get(session_id)
            # 趋势要基线窗口样本足够才成立：基线窗口不足时基线分恒为 50，
            # 趋势退化成「近期百分位 − 50」的假数字。此时置 null 让前端显示
            # 数据不足，而不是 0——「没有变化」和「没有数据」是两种说法。
            # 近期窗口不设门槛：最近淡出的人近期分本身就是真实的低百分位。
            has_trend = (
                scored
                and baseline_windows.get(session_id, empty).messages_total()
                >= MIN_SCORE_MESSAGES
            )
            payload = {
                "hash": contact.hash,
                "display_name": contact.display_name,
                "scored": scored,
                "overall": round(dimension_scores["overall"], 1) if scored else None,
                "dimensions": {
                    name: round(dimension_scores[name], 1) if scored else None
                    for name in DIMENSION_NAMES
                },
                "trends": (
                    {
                        name: round(
                            recent_scores[session_id][name]
                            - baseline_scores[session_id][name],
                            1,
                        )
                        for name in (*DIMENSION_NAMES, "overall")
                    }
                    if has_trend
                    else None
                ),
                "recent_messages": recent_windows.get(session_id, empty).messages_total(),
                "window_messages": stats.raw.messages_total(),
                "last_message_at": contact.last_message_at,
                "sample_note": "" if scored else "数据不足",
                "anomalies": anomalies,
                # 关系类型与来源：前端 badge 与「事务往来/家人」差异化展示用。
                "relation_kind": kind,
                "kind_source": kind_source,
            }
            if scored and self.strategy.name == "llm":
                # 画像、话题标签与异动解释只在详情页展示；归零/数据不足不发，
                # 词法策略下不出现任何键（前端对缺省与 None 一视同仁）。
                llm_row = llm_scores.get(session_id)
                summary = llm_row.summary if llm_row is not None else None
                payload["llm_summary"] = summary if summary else None
                payload["llm_summary_at"] = llm_row.scored_at if summary else None
                payload["llm_tags"] = (
                    (llm_row.tags or []) if llm_row is not None else []
                )
                note = llm_row.anomaly_note if llm_row is not None else None
                # 指纹不匹配 = 解释针对的是旧异动集合，宁缺毋滥。
                payload["anomaly_note"] = (
                    note
                    if note
                    and anomalies
                    and anomalies_key(anomalies) == llm_row.anomalies_key
                    else None
                )
            payloads.append((session_id, payload))

        self.store.save_scores(payloads)
        # 关系温度历史：每天一个采样点，从部署日起积累。scored 与归零的
        # 联系人都记（归零也是曲线的一部分），数据不足的跳过——没有分数
        # 就没有温度。UPSERT 语义保证同一天多轮分析只留一个点。
        self.store.record_score_history(
            today,
            [
                (
                    session_id,
                    payload["overall"],
                    json.dumps(
                        payload["dimensions"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
                for session_id, payload in payloads
                if payload["overall"] is not None
            ],
        )
        # 「正在淡出」提醒：在归零之前抓住正在滑落的高分关系——已打分、
        # 当前沉默达到 FADE_MIN_GAP_DAYS 天、综合分还高于 FADE_MIN_OVERALL
        # 的联系人，按综合分降序取前 FADE_LIST_LIMIT 个，让看板从观赏变成
        # 行动。无命中也写空数组，避免上一轮的旧名单残留。
        fading = []
        for session_id, payload in payloads:
            if not payload["scored"]:
                continue
            if payload["relation_kind"] == "family":
                # 家人久不聊天不代表疏远，「正在淡出」只针对朋友关系。
                continue
            gap_days = raw_score[session_id]["current_gap_days"]
            if gap_days is None or gap_days < FADE_MIN_GAP_DAYS:
                continue
            if payload["overall"] < FADE_MIN_OVERALL:
                continue
            fading.append(
                {
                    "hash": payload["hash"],
                    "display_name": payload["display_name"],
                    "gap_days": int(round(gap_days)),
                    "overall": payload["overall"],
                }
            )
        fading.sort(key=lambda item: item["overall"], reverse=True)
        self.store.set_json("fading", fading[:FADE_LIST_LIMIT])
        # 中位数随分数一起落库；一个人没有参照系时不写任何中位数。
        self.store.set_json(
            "medians",
            (
                {
                    name: round(
                        median([scores[session_id][name] for session_id in eligible]),
                        1,
                    )
                    for name in DIMENSION_NAMES
                }
                if scores
                else {}
            ),
        )
        self.store.set_meta("last_analyzed_at", str(moment))
        LOG.info("完成打分：%d/%d 个联系人样本达标", len(eligible), len(payloads))
        return len(scores)

