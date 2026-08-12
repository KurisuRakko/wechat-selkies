"""关系画像刷新：给候选联系人补上/刷新关系画像、异动解释与话题标签。

只在深度策略是 llm 时被调用。所有调用都是出站流量：聊天样本与异动列表
拼成一个 user 字符串后，整体过 masking.mask() 再交给 llm.chat——这是聊天
原文离开容器的唯一出口（与 classify 的采样调用并列，见 reading.transcript_lines）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from . import llm
from .constants import (
    LLM_MAX_CALLS_PER_RUN,
    LLM_REFRESH_DAYS,
    LLM_REFRESH_MESSAGES,
    LLM_SAMPLE_DAYS,
    LLM_SAMPLE_MAX_CHARS,
    TREND_BASELINE_DAYS,
    TREND_RECENT_DAYS,
)
from .depth import DepthStrategy
from .masking import mask
from .metrics import Metrics, day_key, day_span
from .reading import sample_transcript
from .scoring import anomalies_key, detect_anomalies, raw_metrics
from .storage import ContactRow, MetricsStore


LOG = logging.getLogger("wechat-insights")

SYSTEM_PROMPT = (
    "你是一个亲密关系分析助手。用户会给你他和一位朋友最近的聊天记录（已做隐私脱敏，"
    "部分词被替换成星号），可能还附有一份检测到的近期变化列表。只回复 JSON，不要任何"
    "其他文字，字段如下：\n"
    '"summary"：不超过 100 字的两三句话，概括你们主要聊什么话题、相处模式是什么样。'
    '用「你们」称呼双方，不要出现具体人名。\n'
    '"anomaly_note"：如果附了近期变化列表，基于聊天内容用一句话（不超过 50 字）推测'
    "变化最可能的原因；没有附变化列表则为 null。\n"
    '"tags"：2 到 4 个标签，每个 2–6 个字，概括你们的主要话题与相处方式'
    "（例如 游戏、深夜谈心、工作吐槽）；给不出就空数组。"
)


@dataclass(frozen=True, slots=True)
class PortraitReply:
    """一次画像调用解析出的三件套：关系画像 + 异动解释 + 话题标签。"""

    summary: str
    anomaly_note: str | None
    tags: list[str] | None


def parse_reply(reply: str) -> PortraitReply | None:
    """截第一个 JSON 块解析。summary 缺失或为空 → 返回 None（不落库，下轮重评）。

    画像行的唯一必需产物就是 summary；没有它这行没有任何展示价值。
    anomaly_note / tags 解析失败只降级成 None，不影响落库。tags 非 list
    （含缺失）归一成 None——「老缓存行没有 tags」与「模型给了空数组」是
    两回事：None 会触发下一轮重评补齐，[] 则不会。其余字段都是模型输出，
    长度硬截断防毒，且不把回复内容写进日志：日志里出现任何聊天相关文本
    都违背「原文不出容器」的原则。
    """

    start = reply.find("{")
    end = reply.rfind("}")
    if start < 0 or end <= start:
        LOG.warning("关系画像回复里没有 JSON 块")
        return None
    try:
        data = json.loads(reply[start : end + 1])
    except (ValueError, TypeError):
        LOG.warning("关系画像回复里解析不出 JSON")
        return None
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        LOG.warning("关系画像回复里没有可用的 summary")
        return None
    summary = summary.strip()[:200]
    note = data.get("anomaly_note")
    if not isinstance(note, str):
        note = None
    else:
        note = note.strip()[:100] or None
    tags = data.get("tags")
    if not isinstance(tags, list):
        tags = None
    else:
        cleaned: list[str] = []
        for item in tags:
            if not isinstance(item, str):
                continue
            tag = item.strip()[:8]
            if tag:
                cleaned.append(tag)
            if len(cleaned) >= 4:
                break
        tags = cleaned
    return PortraitReply(summary, note, tags)


def _window_bounds(moment: int) -> tuple[str, str, str, str]:
    """异动对比窗口的日键边界：(today, recent_start, baseline_end, baseline_start)。

    _recompute 与画像异动指纹共用这一份换算，两处不会各写各的窗口边界。
    """

    today = day_key(moment)
    recent_start = day_key(moment - TREND_RECENT_DAYS * 86400)
    baseline_end = day_key(moment - (TREND_RECENT_DAYS + 1) * 86400)
    baseline_start = day_key(
        moment - (TREND_RECENT_DAYS + TREND_BASELINE_DAYS) * 86400
    )
    return today, recent_start, baseline_end, baseline_start


def refresh_portraits(
    store: MetricsStore,
    reader,
    strategy: DepthStrategy,
    gap_seconds: int,
    moment: int,
    report_cb=None,
) -> int:
    """给候选联系人补上/刷新关系画像、异动解释与话题标签，返回本轮写入个数。

    候选 = 采样窗口内有消息，且（从未评过 / 过了保鲜期 / 评过后新增消息
    数达标 / 近期异动指纹变了）的联系人；从未评过的排最前，其余按最陈旧
    在前，截断到单轮调用上限，剩下的下一轮再评。异动指纹与 _recompute
    里的同一套计算重复一次（每轮多算几次 raw_metrics + detect_anomalies，
    增量一轮 16 秒可接受），换来 prompt 里的异动列表与缓存指纹自洽。
    """

    sample_start = moment - LLM_SAMPLE_DAYS * 86400
    today, recent_start, baseline_end, baseline_start = _window_bounds(moment)
    recent_windows = store.load_window(recent_start, today)
    baseline_windows = store.load_window(baseline_start, baseline_end)
    empty = Metrics()
    recent_span = day_span(recent_start, today)
    baseline_span = day_span(baseline_start, baseline_end)
    pending: list[
        tuple[int, int, str, ContactRow, list[dict[str, object]], str]
    ] = []
    for contact in store.all_contacts():
        if contact.last_message_at is None or contact.last_message_at < sample_start:
            continue
        cached = store.get_llm_depth(contact.session_id)
        anomalies = detect_anomalies(
            raw_metrics(
                recent_windows.get(contact.session_id, empty),
                strategy,
                recent_span,
            ),
            raw_metrics(
                baseline_windows.get(contact.session_id, empty),
                strategy,
                baseline_span,
            ),
            recent_windows.get(contact.session_id, empty),
            baseline_windows.get(contact.session_id, empty),
        )
        key = anomalies_key(anomalies)
        if not (
            cached is None
            or moment - cached.scored_at >= LLM_REFRESH_DAYS * 86400
            or contact.total_messages - cached.total_messages
            >= LLM_REFRESH_MESSAGES
            or cached.anomalies_key != key
            # 老缓存行没有 tags：还没经历过带 tags 字段的 prompt，重评一次
            # 补齐。单轮 40 次的上限兜底成本，多数部署两天内收敛。
            or cached.tags is None
        ):
            continue
        pending.append(
            (
                0 if cached is None else 1,
                cached.scored_at if cached is not None else 0,
                contact.session_id,
                contact,
                anomalies,
                key,
            )
        )

    selected = sorted(pending)[:LLM_MAX_CALLS_PER_RUN]
    if report_cb is not None:
        report_cb(phase="llm", done=0, total=len(selected), detail="")
    scored = skipped = failed = 0
    for done, (_, _, _, contact, anomalies, key) in enumerate(selected, start=1):
        if report_cb is not None:
            report_cb(
                phase="llm",
                done=done,
                total=len(selected),
                detail=contact.display_name,
            )
        sample = sample_transcript(
            reader,
            contact.session_id,
            contact.display_name,
            sample_start,
            gap_seconds,
            LLM_SAMPLE_MAX_CHARS,
        )
        if sample is None:
            skipped += 1
            continue
        user = sample
        if anomalies:
            user += "\n\n近期变化：\n" + "\n".join(
                f"- {item['label']}：{item['before']} → {item['after']}"
                for item in anomalies
            )
        reply = llm.chat(SYSTEM_PROMPT, mask(user))
        parsed = parse_reply(reply) if reply is not None else None
        if parsed is None:
            failed += 1
            continue
        store.set_llm_depth(
            contact.session_id,
            moment,
            contact.total_messages,
            parsed.summary,
            parsed.anomaly_note,
            key,
            parsed.tags,
        )
        scored += 1
    LOG.info(
        "LLM 关系画像：写入 %d 个，跳过 %d 个，失败 %d 个", scored, skipped, failed
    )
    return scored
