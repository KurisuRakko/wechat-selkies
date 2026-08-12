"""好感度校准：消化用户的右键反馈标记，产出七维偏移并落库。

方向永远由用户标记决定，LLM 只负责把幅度分配到七个维度上（输出非负幅度，
符号由代码套用）；LLM 不可用或失败时回退为全维统一幅度。出站聊天文本
必须过 masking.mask()。
"""

from __future__ import annotations

import json
import logging

from . import llm
from .constants import (
    CALIBRATE_FALLBACK_STEP,
    CALIBRATE_MAX_PER_RUN,
    CALIBRATE_SAMPLE_CHARS,
    CALIBRATE_STEP_MAX,
    CALIBRATE_TOTAL_MAX,
    LLM_SAMPLE_DAYS,
)
from .depth import DepthStrategy
from .masking import mask
from .reading import sample_transcript
from .scoring import DIMENSION_NAMES
from .storage import MetricsStore


LOG = logging.getLogger("wechat-insights")

#: 七维的中文名与含义，prompt 与「当前七维分」展示共用同一份措辞。
_DIMENSIONS = (
    ("responsiveness", "响应", "TA 回复你的速度（延迟中位数与秒回率）"),
    ("initiative", "主动", "谁在主动维系关系（发起对话、连发追加、说最后一句）"),
    ("investment", "投入", "TA 每天在你身上投入的成本（通话、语音、文字量、表情包）"),
    ("rhythm", "节奏", "聊天形态（深夜、周末占比、对话轮次与长度）"),
    ("depth", "深度", "聊天内容的分量（消息长度、疑问句、长消息）"),
    ("constancy", "恒常", "联系是否细水长流（活跃天数占比、沉默天数）"),
    ("reciprocity", "对等", "关系是不是双向的（双方投入与消息量的均衡度）"),
)

SYSTEM_PROMPT = (
    "你是一个亲密关系分析助手。用户觉得看板对某位朋友的好感度评分「偏低」"
    "（用户希望更高）或「偏高」（用户希望更低）。你会收到这位朋友最近的聊天"
    "采样（已脱敏，部分词被替换成星号）与当前七个维度的分数（0-100 的相对"
    "百分位）。请判断哪些维度最该调整以符合用户感受，只回复 JSON，不要任何"
    "其他文字：\n"
    '{"dims": {"responsiveness": 幅度, ..., "reciprocity": 幅度}, '
    '"note": "不超过 40 字的一句话理由"}\n'
    "幅度是 0 到 6 的绝对值，不要带正负号；不想动的维度给 0。维度含义：\n"
    + "\n".join(f"- {name}（{label}）：{desc}" for name, label, desc in _DIMENSIONS)
)


def parse_reply(reply: str) -> tuple[dict[str, float], str] | None:
    """截第一个 JSON 块解析；dims 只收七维键、幅度夹到 [0, CALIBRATE_STEP_MAX]。

    全部缺失或全 0 → 返回 None（让调用方走回退）；note 截 40 字、非 str 归 ''。
    不把回复内容写进日志：日志里出现任何聊天相关文本都违背「原文不出容器」。
    """

    start = reply.find("{")
    end = reply.rfind("}")
    if start < 0 or end <= start:
        LOG.warning("好感度校准回复里没有 JSON 块")
        return None
    try:
        data = json.loads(reply[start : end + 1])
    except (ValueError, TypeError):
        LOG.warning("好感度校准回复里解析不出 JSON")
        return None
    raw_dims = data.get("dims")
    if not isinstance(raw_dims, dict):
        return None
    dims: dict[str, float] = {}
    for name in DIMENSION_NAMES:
        value = raw_dims.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        magnitude = min(CALIBRATE_STEP_MAX, max(0.0, float(value)))
        if magnitude:
            dims[name] = round(magnitude, 1)
    if not dims:
        return None
    note = data.get("note")
    note = note.strip()[:40] if isinstance(note, str) else ""
    return dims, note


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def refresh_calibrations(
    store: MetricsStore,
    reader,
    strategy: DepthStrategy,
    gap_seconds: int,
    moment: int,
    report_cb=None,
) -> int:
    """消化好感度标记，产出七维偏移并落库，返回本轮写入校准的联系人数。

    候选 = feedback_pending 为 up/down 的联系人，按标记时刻升序截断到单轮
    上限，剩下的下一轮再消化。方向由标记决定，LLM 只分配幅度；LLM 不可用
    或解析失败时回退为全维统一幅度。没有打过分的候选人无从校准：清除标记、
    跳过。progress phase = "calibrate"。
    """

    candidates = [
        contact
        for contact in store.all_contacts()
        if contact.feedback_pending in ("up", "down")
    ]
    selected = sorted(
        candidates, key=lambda contact: int(contact.feedback_pending_at or 0)
    )[:CALIBRATE_MAX_PER_RUN]
    if report_cb is not None:
        report_cb(phase="calibrate", done=0, total=len(selected), detail="")
    written = skipped = fell_back = 0
    for done, contact in enumerate(selected, start=1):
        if report_cb is not None:
            report_cb(
                phase="calibrate",
                done=done,
                total=len(selected),
                detail=contact.display_name,
            )
        prev = store.score_by_hash(contact.hash)
        if prev is None or not prev.get("scored"):
            store.set_contact_feedback(contact.session_id, "", "")
            LOG.info("好感度校准跳过 %s：尚无打分，标记已清除", contact.display_name)
            skipped += 1
            continue
        sign = 1.0 if contact.feedback_pending == "up" else -1.0
        dims: dict[str, float] = {}
        note = ""
        source = "fallback"
        if strategy.name == "llm":
            sample = sample_transcript(
                reader,
                contact.session_id,
                contact.display_name,
                moment - LLM_SAMPLE_DAYS * 86400,
                gap_seconds,
                CALIBRATE_SAMPLE_CHARS,
            )
            if sample is not None:
                scores = prev.get("dimensions") or {}
                user = (
                    sample
                    + "\n\n当前七维分：\n"
                    + "\n".join(
                        f"{label} {scores.get(name, 50.0)}"
                        for name, label, _desc in _DIMENSIONS
                    )
                    + (
                        "\n用户反馈：评分偏低，希望更高"
                        if sign > 0
                        else "\n用户反馈：评分偏高，希望更低"
                    )
                )
                reply = llm.chat(SYSTEM_PROMPT, mask(user))
                parsed = parse_reply(reply) if reply is not None else None
                if parsed is not None:
                    dims, note = parsed
                    source = "llm"
        if source == "fallback":
            dims = {name: CALIBRATE_FALLBACK_STEP for name in DIMENSION_NAMES}
            fell_back += 1
        old = (contact.calibration_data() or {}).get("dims")
        old_dims = old if isinstance(old, dict) else {}
        new_dims: dict[str, float] = {}
        # 键并集：本次没建议的维度原样保留旧偏移，否则累计偏移会在
        # 一轮只动个别维度的校准中静默丢失。
        for name in dict.fromkeys((*old_dims, *dims)):
            if name in dims:
                value = _clamp(
                    float(old_dims.get(name, 0.0)) + sign * float(dims[name]),
                    -CALIBRATE_TOTAL_MAX,
                    CALIBRATE_TOTAL_MAX,
                )
            else:
                # 保留旧值也夹界/取整一次，防历史数据越界。
                value = _clamp(
                    float(old_dims.get(name, 0.0)),
                    -CALIBRATE_TOTAL_MAX,
                    CALIBRATE_TOTAL_MAX,
                )
            value = round(value, 1)
            if value:
                new_dims[name] = value
        if new_dims:
            store.set_contact_calibration(
                contact.session_id,
                json.dumps(
                    {
                        "dims": new_dims,
                        "updated_at": moment,
                        "source": source,
                        "note": note,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        else:
            # 校准净值归零：清空校准列，相当于没校准过。
            store.set_contact_calibration(contact.session_id, "")
        store.set_contact_feedback(contact.session_id, "", "")
        written += 1
    LOG.info("好感度校准：写入 %d 个（回退 %d），跳过 %d 个", written, fell_back, skipped)
    return written
