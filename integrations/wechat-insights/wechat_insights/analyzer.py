"""离线分析器：增量读取私聊消息 → 按天聚合 → 预计算看板数据。

解密、快照、账户白名单、私聊过滤全部复用 wechat_history；这里只负责游标推进与
统计。消息原文读进内存做完词法统计就丢弃，不写入 metrics.db。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from wechat_history.reader import HistoryReader, _read_connection
from wechat_history.sessions import scan_direct_rows

from . import llm
from .constants import (
    BACKFILL_BATCH,
    DECAY_HALF_LIFE_DAYS,
    LLM_MAX_CALLS_PER_RUN,
    LLM_REFRESH_DAYS,
    LLM_REFRESH_MESSAGES,
    LLM_SAMPLE_DAYS,
    LLM_SAMPLE_MAX_CHARS,
    MIN_SCORE_MESSAGES,
    SCORE_WINDOW_DAYS,
    SESSION_GAP_SECONDS,
    TREND_BASELINE_DAYS,
    TREND_RECENT_DAYS,
)
from .conversation import ME, Message, split_conversations
from .masking import mask
from .depth import DepthStrategy, get_depth_strategy
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
from .scoring import (
    DIMENSION_NAMES,
    detect_anomalies,
    median,
    raw_metrics,
    score_cohort,
)
from .storage import ContactRow, MetricsStore, WindowStats


LOG = logging.getLogger("wechat-insights")


@dataclass(frozen=True, slots=True)
class Cursor:
    """已提交的最后一条消息位置；(0, "", -1) 表示还没读过任何消息。

    local_id 只在单个分片内唯一，必须带上分片路径才能构成全局唯一的全序；
    空分片排在一切真实分片之前。排序键与恢复谓词都用这个三元组。
    """

    timestamp: int
    shard: str
    local_id: int


@dataclass(slots=True)
class AnalysisResult:
    started_at: int
    duration_seconds: float
    sessions: int = 0
    messages_read: int = 0
    scored: int = 0
    llm_scored: int = 0
    per_session: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MessageBatch:
    """一批读到的消息，以及推进游标需要的元信息。"""

    #: 已剔除无方向系统噪声的消息，按时间升序。
    messages: list[Message]
    #: 与 messages 一一对应的游标位置；对话被截断时按前缀取最后一条。
    positions: list[Cursor]
    #: 本批最后一行（含被剔除的行）的位置；一行都没有时为 None。
    last: Cursor | None
    #: 数据库里还有更多行没读。
    has_more: bool


def _resume_where(cursor: Cursor, shard: str) -> tuple[str, tuple[object, ...]]:
    """按 (时间戳, 分片, local_id) 全序生成单个分片的恢复谓词。

    分片之间共享 local_id，游标必须带上分片，否则同刻同号的两行会互相
    吞掉。游标分片之后的分片，同一时间戳下的行都在游标之后，全部要读；
    游标分片之前的分片则整片已读完，只有更晚的时间戳才有效。
    """

    if shard > cursor.shard:
        return "create_time >= ?", (cursor.timestamp,)
    if shard == cursor.shard:
        return (
            "create_time > ? OR (create_time = ? AND local_id > ?)",
            (cursor.timestamp, cursor.timestamp, cursor.local_id),
        )
    return "create_time > ?", (cursor.timestamp,)


def read_messages_after(
    reader: HistoryReader,
    session_id: str,
    display_name: str,
    contacts: dict[str, dict],
    cursor: Cursor,
    limit: int,
) -> MessageBatch:
    """读取游标之后最多 limit 条消息，按时间升序。

    查询模式照抄 notifications.read_new_messages 的游标窗口，区别是不设上界：
    分析器不需要和会话行对齐，读到哪算哪，剩下的留给下一批。
    """

    table = reader._message_table(session_id)
    session = {
        "session_id": session_id,
        "display_name": display_name,
        "alias": "",
        "kind": "direct",
    }
    collected: list[tuple[tuple[int, str, int], Message | None]] = []
    for relative_db in reader._message_database_keys():
        database = reader.cache.get(relative_db)
        with _read_connection(database) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            names = reader._name_map(connection)
            where, parameters = _resume_where(cursor, relative_db)
            rows = connection.execute(
                f"""
                SELECT local_id, local_type, create_time, real_sender_id,
                       message_content, WCDB_CT_message_content
                FROM [{table}]
                WHERE {where}
                ORDER BY create_time ASC, local_id ASC
                LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()
            for row in rows:
                item = reader._message_item(row, relative_db, session, contacts, names)
                timestamp = int(row["create_time"] or 0)
                local_id = int(row["local_id"] or 0)
                # 没有发送者的系统噪声参与不了「谁回谁」的判定，占位成 None：
                # 它不进统计，但游标仍要跨过它，否则每轮都会重复读到。
                message = (
                    None
                    if item["direction"] == "unknown"
                    else Message(
                        timestamp=timestamp,
                        local_id=local_id,
                        direction=str(item["direction"]),
                        kind=str(item["type"]),
                        text=str(item["text"] or ""),
                    )
                )
                collected.append(((timestamp, relative_db, local_id), message))

    collected.sort(key=lambda entry: entry[0])
    window = collected[:limit]
    kept = [entry for entry in window if entry[1] is not None]
    return MessageBatch(
        messages=[entry[1] for entry in kept],
        positions=[Cursor(*entry[0]) for entry in kept],
        last=Cursor(*window[-1][0]) if window else None,
        has_more=len(collected) > limit,
    )


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


_LLM_SYSTEM_PROMPT = (
    "你是一个亲密关系分析助手。用户会给你他和一位朋友最近的聊天记录（已做隐私脱敏，"
    "部分词被替换成星号）。请从话题实质性、情感袒露程度、相互追问与回应的质量三个方面，"
    "评估这段对话关系的深度，给出 0 到 100 的整数分：日常寒暄、斗图、纯事务性沟通在 "
    "30 以下；有实质话题讨论在 30–60；有情感袒露、深度交流在 60 以上。只回复 JSON，"
    "格式 {\"score\": 整数}，不要任何其他文字。"
)


def _parse_llm_score(reply: str) -> int | None:
    """从模型回复里截取第一个 JSON 块解析整数分并夹到 [0, 100]；失败返回 None。

    不把回复内容写进日志：回复虽然是模型生成的，但日志里出现任何聊天相关
    文本都违背「原文不出容器」的原则。
    """

    start = reply.find("{")
    end = reply.rfind("}")
    if start < 0 or end <= start:
        LOG.warning("LLM 深度分回复里没有 JSON 块")
        return None
    try:
        data = json.loads(reply[start : end + 1])
        score = int(data["score"])
    except (ValueError, KeyError, TypeError):
        LOG.warning("LLM 深度分回复里解析不出 score")
        return None
    return max(0, min(100, score))


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
    ):
        self.store = store
        self.reader_factory = reader_factory
        self.strategy = strategy or get_depth_strategy()
        self.gap_seconds = gap_seconds
        self.batch_size = batch_size

    def run(self, now: int | None = None) -> AnalysisResult:
        moment = int(time.time()) if now is None else int(now)
        started = time.monotonic()
        result = AnalysisResult(started_at=moment, duration_seconds=0.0)

        reader = self.reader_factory()
        try:
            sessions = scan_direct_rows(reader)
            contacts = reader._load_contacts()
            result.sessions = len(sessions)
            for session_id in sorted(sessions):
                session = sessions[session_id]
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
            # LLM 深度打分要在 reader 关闭前做（它还要从快照里读采样文本）。
            # 它是可选的加分项：出任何意外都不能拖垮整轮打分，隔离处理。
            if self.strategy.name == "llm":
                try:
                    result.llm_scored = self._refresh_llm_depth(reader, moment)
                except Exception:
                    LOG.exception("LLM 深度打分失败，本轮跳过")
        finally:
            reader.close()

        result.scored = self._recompute(moment)
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

    def _refresh_llm_depth(self, reader: HistoryReader, moment: int) -> int:
        """给候选联系人补上/刷新 LLM 深度分，写进 llm_depth 缓存表。

        只在深度策略是 llm 时被调用。所有调用都是出站流量：样本必须先
        整体过 masking.mask() 再交给 llm.chat——这是聊天原文离开容器的
        唯一出口。

        候选 = 采样窗口内有消息，且（从未评过 / 分数过了保鲜期 / 打分后
        新增消息数达标）的联系人；从未评过的排最前，其余按最陈旧在前，
        截断到单轮调用上限，剩下的下一轮再评。
        """

        sample_start = moment - LLM_SAMPLE_DAYS * 86400
        pending: list[tuple[int, int, str, ContactRow]] = []
        for contact in self.store.all_contacts():
            if contact.last_message_at is None or contact.last_message_at < sample_start:
                continue
            cached = self.store.get_llm_depth(contact.session_id)
            if not (
                cached is None
                or moment - cached.scored_at >= LLM_REFRESH_DAYS * 86400
                or contact.total_messages - cached.total_messages >= LLM_REFRESH_MESSAGES
            ):
                continue
            pending.append(
                (
                    0 if cached is None else 1,
                    cached.scored_at if cached is not None else 0,
                    contact.session_id,
                    contact,
                )
            )

        scored = skipped = failed = 0
        for _, _, _, contact in sorted(pending)[:LLM_MAX_CALLS_PER_RUN]:
            sample = self._llm_sample(
                reader, contact.session_id, contact.display_name, sample_start
            )
            if sample is None:
                skipped += 1
                continue
            reply = llm.chat(_LLM_SYSTEM_PROMPT, mask(sample))
            score = _parse_llm_score(reply) if reply is not None else None
            if score is None:
                failed += 1
                continue
            self.store.set_llm_depth(
                contact.session_id, score, moment, contact.total_messages
            )
            scored += 1
        LOG.info(
            "LLM 深度打分：评分 %d 个，跳过 %d 个，失败 %d 个", scored, skipped, failed
        )
        return scored

    def _llm_sample(
        self,
        reader: HistoryReader,
        session_id: str,
        display_name: str,
        sample_start: int,
    ) -> str | None:
        """从采样窗口读一批消息，从最晚往前拼最近几段对话；没有 text 返回 None。

        只取 text 类消息，每行「我: 内容 / TA: 内容」，段与段之间空行分隔，
        总字数达到 LLM_SAMPLE_MAX_CHARS 就停。窗口内消息超过一个批次时只
        覆盖最旧的那一批（读接口只支持从游标向前读），采样质量足够。
        """

        batch = read_messages_after(
            reader,
            session_id,
            display_name,
            {},
            Cursor(sample_start, "", -1),
            BACKFILL_BATCH,
        )
        blocks: list[str] = []
        total = 0
        for conversation in reversed(
            split_conversations(batch.messages, self.gap_seconds)
        ):
            lines = [
                f"{'我' if message.direction == ME else 'TA'}: {message.text}"
                for message in conversation
                if message.kind == "text" and message.text
            ]
            if not lines:
                continue
            blocks.append("\n".join(lines))
            total += sum(len(line) for line in lines)
            if total >= LLM_SAMPLE_MAX_CHARS:
                break
        return None if not blocks else "\n\n".join(blocks)

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

    def _recompute(self, moment: int) -> int:
        """重算所有联系人的七维分、趋势与异动，整体替换 scores 表。"""

        # 窗口起点用「N 天前的同一时刻」折算，跨 DST 切换时本地时间会偏移
        # 一小时、日键偶尔差一天（生产容器在澳大利亚/悉尼，有夏令时），
        # 目前按秒数近似，不做逐天修正。
        today = day_key(moment)
        score_start = day_key(moment - SCORE_WINDOW_DAYS * 86400)
        score_start_ts = moment - SCORE_WINDOW_DAYS * 86400
        recent_start = day_key(moment - TREND_RECENT_DAYS * 86400)
        baseline_end = day_key(moment - (TREND_RECENT_DAYS + 1) * 86400)
        baseline_start = day_key(
            moment - (TREND_RECENT_DAYS + TREND_BASELINE_DAYS) * 86400
        )

        score_stats = self.store.load_window_stats(
            score_start, today, lambda day: self._weight_of(today, day)
        )
        recent_windows = self.store.load_window(recent_start, today)
        baseline_windows = self.store.load_window(baseline_start, baseline_end)
        contacts_by_id = {
            contact.session_id: contact for contact in self.store.all_contacts()
        }

        # 门槛只看未加权的原始消息数：衰减是「远记忆变淡」，不是「远消息作废」。
        eligible = sorted(
            session_id
            for session_id, stats in score_stats.items()
            if stats.raw.messages_total() >= MIN_SCORE_MESSAGES
        )

        empty = Metrics()
        # 打分窗口的日均除数用 decayed_span 的「等效天数」：均匀活跃的人加权前后
        # 日均一致，两年的衰减窗口与近 30 天窗口的日均仍可直接比较。
        equivalent_days = decayed_span(day_span(score_start, today), DECAY_HALF_LIFE_DAYS)
        llm_scores = self.store.all_llm_depth() if self.strategy.name == "llm" else {}
        raw_score: dict[str, dict[str, float | None]] = {}
        for session_id in eligible:
            stats = score_stats[session_id]
            contact = contacts_by_id[session_id]
            extras: dict[str, float | None] = {
                "active_day_rate": stats.active_weight / equivalent_days,
                "current_gap_days": (
                    (moment - contact.last_message_at) / 86400
                    if contact.last_message_at is not None
                    else None
                ),
                "longest_gap_days": self._longest_gap(
                    stats, contact, score_start, score_start_ts
                ),
            }
            if self.strategy.name == "llm":
                # LLM 分可能还没评出来（None）：缺值权重回流给词法三项，
                # 深度维度自动退化成纯词法，不需要特判。
                extras["llm_depth_score"] = llm_scores.get(session_id)
            raw_score[session_id] = raw_metrics(
                stats.weighted, self.strategy, equivalent_days, extras
            )
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

        scores = score_cohort(raw_score, self.strategy)
        # 趋势 = 同一批联系人内，近期窗口的百分位 减去 基线窗口的百分位。
        recent_scores = score_cohort(raw_recent, self.strategy)
        baseline_scores = score_cohort(raw_baseline, self.strategy)

        payloads: list[tuple[str, dict[str, object]]] = []
        for contact in contacts_by_id.values():
            session_id = contact.session_id
            stats = score_stats.get(session_id)
            if stats is None or stats.raw.messages_total() <= 0:
                # 打分窗口里一条消息都没有（或根本没有天桶）：两年内没有往来，
                # 全部归零；有消息但不够门槛的仍走下面的「数据不足」。
                payload: dict[str, object] = {
                    "hash": contact.hash,
                    "display_name": contact.display_name,
                    "scored": False,
                    "zeroed": True,
                    "overall": 0,
                    "dimensions": {name: 0.0 for name in DIMENSION_NAMES},
                    "trends": None,
                    "recent_messages": recent_windows.get(
                        session_id, empty
                    ).messages_total(),
                    "window_messages": 0,
                    "last_message_at": contact.last_message_at,
                    "sample_note": "两年内没有往来",
                    "anomalies": [],
                }
                payloads.append((session_id, payload))
                continue
            scored = session_id in scores
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
                "anomalies": (
                    detect_anomalies(
                        raw_recent[session_id],
                        raw_baseline[session_id],
                        recent_windows.get(session_id, empty),
                        baseline_windows.get(session_id, empty),
                    )
                    if scored
                    else []
                ),
            }
            payloads.append((session_id, payload))

        self.store.save_scores(payloads)
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
