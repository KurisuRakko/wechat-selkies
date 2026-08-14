"""绝交检测：消化用户的右键绝交标记，核实后封顶压低该联系人的综合分。

证据分两层：冷断由 stats_daily 确定性判定（标记日之后双方消息合计不超过
阈值且已过足够天数），吵架由 LLM 从标记日前后一周的窗口采样判定；两者都
给不出结论时，按标记的置信度决定直接按用户断言生效还是留到下一轮再核。
出站聊天文本必须过 masking.mask()。
"""

from __future__ import annotations

import json
import logging
import time

from . import llm
from .constants import (
    BREAKUP_MAX_PER_RUN,
    BREAKUP_MIN_ELAPSED_DAYS,
    BREAKUP_SAMPLE_CHARS,
    BREAKUP_SILENCE_MAX_MSGS,
    BREAKUP_WINDOW_DAYS,
)
from .depth import DepthStrategy
from .masking import mask
from .reading import sample_transcript_between
from .storage import ContactRow, MetricsStore


LOG = logging.getLogger("wechat-insights")

SYSTEM_PROMPT = (
    "你是一个亲密关系分析助手。用户认为自己和某位朋友在某个日期前后「绝交」了。"
    "你会收到该日期前后一周的聊天采样（已脱敏，部分词被替换成星号），以及该日期"
    "之后双方的消息统计。请判断这段关系在该日期前后是否发生了破裂。判定标准：\n"
    "- 双方发生激烈争吵、互相指责、说狠话、明确表达关系终结 → 破裂(quarrel)\n"
    "- 聊天在该日期附近戛然而止，之后几乎不再往来 → 破裂(silence)\n"
    "- 该日期之后双方仍在正常、友好地聊天 → 未破裂\n"
    "- 采样太少或看不出明显迹象 → 不确定\n"
    "只回复 JSON，不要任何其他文字：\n"
    '{"verdict": "broken" 或 "normal" 或 "unclear", "kind": "quarrel" 或 "silence", '
    '"note": "不超过 40 字的一句话依据"}\n'
    "verdict 是 normal 或 unclear 时 kind 可以省略。"
)


def parse_reply(reply: str) -> dict | None:
    """截第一个 JSON 块解析；verdict 只认 broken/normal/unclear。

    kind 只在 broken 时读取、非法值归 quarrel；note 截 40 字、非 str 归 ''。
    不把回复内容写进日志：日志里出现任何聊天相关文本都违背「原文不出容器」。
    """

    start = reply.find("{")
    end = reply.rfind("}")
    if start < 0 or end <= start:
        LOG.warning("绝交核实回复里没有 JSON 块")
        return None
    try:
        data = json.loads(reply[start : end + 1])
    except (ValueError, TypeError):
        LOG.warning("绝交核实回复里解析不出 JSON")
        return None
    if not isinstance(data, dict):
        return None
    verdict = data.get("verdict")
    if verdict not in ("broken", "normal", "unclear"):
        return None
    note = data.get("note")
    note = note.strip()[:40] if isinstance(note, str) else ""
    parsed: dict = {"verdict": verdict, "note": note}
    if verdict == "broken":
        kind = data.get("kind")
        parsed["kind"] = kind if kind in ("quarrel", "silence") else "quarrel"
    return parsed


def messages_after(store: MetricsStore, session_id: str, day: str) -> int:
    """标记日之后（不含当天）双方消息合计条数。

    stats_daily 的日键是 YYYY-MM-DD 字符串，直接按字典序比较即可。
    """

    return int(
        sum(
            metrics.messages_total()
            for key, metrics in store.load_days(session_id)
            if key > day
        )
    )


def _pending_at(contact: ContactRow) -> int:
    """候选排序键：标记时刻 epoch 秒；读不出时归 0（排最前）。"""

    try:
        return int((contact.breakup_pending_data() or {}).get("at") or 0)
    except (TypeError, ValueError):
        return 0


def refresh_breakups(
    store: MetricsStore,
    reader,
    strategy: DepthStrategy,
    gap_seconds: int,
    moment: int,
    report_cb=None,
) -> int:
    """核实绝交标记，写入结论并封顶压低综合分，返回本轮写入结论的联系人数。

    候选 = breakup_pending 非空且能解析出 date/certainty 的联系人，按标记
    时刻升序截断到单轮上限。证据按优先级：LLM 判 broken → 确认；否则冷断
    统计达标 → 确认；否则 LLM 判 normal → 否决；再否则按置信度决定直接
    生效（certain）或保留标记下一轮再核（suspected）。没有打过分的候选人
    无从核实：清除标记、跳过。progress phase = "breakup"。
    """

    candidates: list[ContactRow] = []
    for contact in store.all_contacts():
        if not contact.breakup_pending:
            continue
        data = contact.breakup_pending_data()
        date = data.get("date") if data is not None else None
        certainty = data.get("certainty") if data is not None else None
        valid = isinstance(date, str) and certainty in ("certain", "suspected")
        if valid:
            try:
                time.strptime(date, "%Y-%m-%d")
            except ValueError:
                valid = False
        if not valid:
            # 解析不出的脏标记直接清空：留着永远也消化不了。
            store.set_contact_breakup_pending(contact.session_id, "")
            continue
        candidates.append(contact)
    selected = sorted(candidates, key=_pending_at)[:BREAKUP_MAX_PER_RUN]
    if report_cb is not None:
        report_cb(phase="breakup", done=0, total=len(selected), detail="")
    written = confirmed = rejected = skipped = 0
    for done, contact in enumerate(selected, start=1):
        if report_cb is not None:
            report_cb(
                phase="breakup",
                done=done,
                total=len(selected),
                detail=contact.display_name,
            )
        pending = contact.breakup_pending_data() or {}
        date = str(pending["date"])
        certainty = str(pending["certainty"])
        prev = store.score_by_hash(contact.hash)
        if prev is None or not prev.get("scored"):
            store.set_contact_breakup_pending(contact.session_id, "")
            LOG.info("绝交核实跳过 %s：尚无打分，标记已清除", contact.display_name)
            skipped += 1
            continue
        date_ts = int(time.mktime(time.strptime(date, "%Y-%m-%d")))
        # 确定性证据：标记日之后的消息合计与距今天数。
        after = messages_after(store, contact.session_id, date)
        elapsed_days = (moment - date_ts) // 86400
        silence_ok = (
            after <= BREAKUP_SILENCE_MAX_MSGS
            and elapsed_days >= BREAKUP_MIN_ELAPSED_DAYS
        )
        llm_verdict = None
        if strategy.name == "llm":
            sample = sample_transcript_between(
                reader,
                contact.session_id,
                contact.display_name,
                date_ts - BREAKUP_WINDOW_DAYS * 86400,
                date_ts + BREAKUP_WINDOW_DAYS * 86400,
                gap_seconds,
                BREAKUP_SAMPLE_CHARS,
            )
            if sample is not None:
                user = (
                    sample
                    + "\n\n"
                    + f"用户标记的绝交日期：{date}\n"
                    + f"该日期之后双方消息合计：{after} 条（距今 {elapsed_days} 天）"
                )
                reply = llm.chat(SYSTEM_PROMPT, mask(user))
                llm_verdict = parse_reply(reply) if reply is not None else None
        # 决策，按优先级：LLM 判破裂 > 冷断统计 > LLM 判未破裂 > 用户断言。
        if llm_verdict is not None and llm_verdict.get("verdict") == "broken":
            verdict, kind = "confirmed", llm_verdict.get("kind") or "quarrel"
            source, note = "llm", llm_verdict.get("note") or ""
        elif silence_ok:
            verdict, kind = "confirmed", "silence"
            source = "stats"
            note = f"标记日后 {elapsed_days} 天仅 {after} 条往来"
        elif llm_verdict is not None and llm_verdict.get("verdict") == "normal":
            verdict, kind = "rejected", ""
            source, note = "llm", llm_verdict.get("note") or ""
        elif certainty == "certain":
            verdict, kind = "confirmed", "asserted"
            source, note = "asserted", "按用户标记生效，未经数据复核"
        else:
            # 存疑标记 + 证据不足：保留 pending 下一轮再核，沉默证据会随
            # 时间成熟；本轮不写结论。
            skipped += 1
            continue
        store.set_contact_breakup(
            contact.session_id,
            json.dumps(
                {
                    "verdict": verdict,
                    "kind": kind,
                    "date": date,
                    "certainty": certainty,
                    "note": note,
                    "decided_at": moment,
                    "source": source,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        store.set_contact_breakup_pending(contact.session_id, "")
        written += 1
        if verdict == "confirmed":
            confirmed += 1
        else:
            rejected += 1
    # 只记数量，不记内容：结论与理由不进日志。
    LOG.info(
        "绝交核实：写入 %d 个（确认 %d、否决 %d），跳过 %d 个",
        written,
        confirmed,
        rejected,
        skipped,
    )
    return written
