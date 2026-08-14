"""绝交检测：消化用户的右键绝交标记，核实后封顶压低该联系人的综合分。

证据分两层：冷断由 stats_daily 确定性判定（标记日之后双方消息合计不超过
阈值且已过足够天数），吵架由 LLM 从标记日前后一周的窗口采样判定；两者都
给不出结论时，按标记的置信度决定直接按用户断言生效还是留到下一轮再核。
出站聊天文本必须过 masking.mask()。

日期填「不知道」时，guess_breakup_dates 先从 stats_daily 的活跃度断崖
推算候选日期，写回 pending 交给 refresh_breakups 走同一套判定；推算不出
候选就落 verdict="unknown" 的终态失败结论，不再排队重算，用户可以清除
或手动改选具体日期重新标记。推算算法本身只对 stats_daily 做算术，不
接触聊天原文、不调用 LLM——"LLM 确认"这一步由候选流入 refresh_breakups
之后的既有核实流程提供，不重复造一遍。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, timedelta

from . import llm
from .constants import (
    BREAKUP_CANDIDATE_MIN_TRAILING_MSGS,
    BREAKUP_CANDIDATE_TRAILING_DAYS,
    BREAKUP_GUESS_MAX_PER_RUN,
    BREAKUP_MAX_PER_RUN,
    BREAKUP_MIN_ELAPSED_DAYS,
    BREAKUP_SAMPLE_CHARS,
    BREAKUP_SILENCE_MAX_MSGS,
    BREAKUP_WINDOW_DAYS,
)
from .depth import DepthStrategy
from .masking import mask
from .metrics import day_moment
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


def find_breakup_candidate(
    store: MetricsStore, session_id: str, moment: int
) -> str | None:
    """从活跃度断崖里找绝交候选日：取最早满足三个条件的活跃日。

    1. 断崖后几乎不再往来：该日之后（不含当天）消息合计
       ≤ BREAKUP_SILENCE_MAX_MSGS，且已过 BREAKUP_MIN_ELAPSED_DAYS 天——
       与 refresh_breakups 的冷断判定同一把尺子。
    2. 断崖前关系有分量：该日之前 BREAKUP_CANDIDATE_TRAILING_DAYS 天内
       （含当天）消息合计 ≥ BREAKUP_CANDIDATE_MIN_TRAILING_MSGS，排除
       本来就偶尔联系、从未真正熟络过的联系人。

    条件 1 定义的合格集合天然是「后缀」（越晚的活跃日，之后剩下的消息
    越少），取满足两个条件里最早的一个，正是「之后彻底安静下来」的转折
    点本身：长期缓慢衰减的联系人两个条件此消彼长、永远凑不到一起，长
    空窗后又恢复聊天的联系人假转折点会被「之后还有一大波消息」的条件 1
    自然排除，只有真正的最后一次转折会留下来。

    stats_daily 的日键升序、O(n) 单趟扫描：后缀和算「断崖后」合计，双
    指针滑动窗口算「断崖前」合计，不重复调用 messages_after()。
    """

    days = store.load_days(session_id)
    if not days:
        return None
    totals = [metrics.messages_total() for _, metrics in days]
    keys = [day for day, _ in days]
    n = len(days)

    # suffix[i] = 严格晚于 keys[i] 的消息合计（含 keys[i] 自身那天已被
    # 排除，与 messages_after 的「不含当天」语义一致）。
    suffix = [0.0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix[i] = suffix[i + 1] + totals[i]

    window_left = 0  # 滑动窗口左边界，随 i 单调右移，不回退。
    for i in range(n):
        day = keys[i]
        if suffix[i + 1] > BREAKUP_SILENCE_MAX_MSGS:
            continue
        elapsed_days = (moment - day_moment(day)) // 86400
        if elapsed_days < BREAKUP_MIN_ELAPSED_DAYS:
            continue
        window_start = (
            date.fromisoformat(day) - timedelta(days=BREAKUP_CANDIDATE_TRAILING_DAYS)
        ).isoformat()
        while window_left < n and keys[window_left] < window_start:
            window_left += 1
        trailing = sum(totals[window_left : i + 1])  # 含当天。
        if trailing < BREAKUP_CANDIDATE_MIN_TRAILING_MSGS:
            continue
        return day  # 最早满足者即答案，提前返回。
    return None


def guess_breakup_dates(
    store: MetricsStore, moment: int, report_cb=None
) -> int:
    """消化「不知道」标记：推算候选日写回 pending，交给 refresh_breakups
    走同一套核实；推算不出候选就落终态失败结论。返回本轮处理的联系人数
    （推算出 + 推算失败，不计跳过）。

    候选 = breakup_pending 非空、date 为 None、certainty 合法的联系人，
    按标记时刻升序截断到单轮上限（复用既有 _pending_at 排序键）。这是
    stats_daily 上的纯算术，不需要 reader/strategy/gap_seconds，参数表
    如实反映（classify_contacts 按需精简签名是既有先例）。
    progress phase = "breakup_guess"，排在 refresh_breakups 的 "breakup"
    之前——本轮推算出的日期要能在同一轮里被后面的核实消化掉。
    """

    candidates: list[ContactRow] = []
    for contact in store.all_contacts():
        if not contact.breakup_pending:
            continue
        data = contact.breakup_pending_data()
        if data is None or data.get("date") is not None:
            continue
        if data.get("certainty") not in ("certain", "suspected"):
            continue
        candidates.append(contact)
    selected = sorted(candidates, key=_pending_at)[:BREAKUP_GUESS_MAX_PER_RUN]
    if report_cb is not None:
        report_cb(phase="breakup_guess", done=0, total=len(selected), detail="")
    resolved = failed = skipped = 0
    for done, contact in enumerate(selected, start=1):
        if report_cb is not None:
            report_cb(
                phase="breakup_guess",
                done=done,
                total=len(selected),
                detail=contact.display_name,
            )
        pending = contact.breakup_pending_data() or {}
        certainty = pending.get("certainty")
        prev = store.score_by_hash(contact.hash)
        if prev is None or not prev.get("scored"):
            # 未打过分：没有 stats_daily 意味着没有真正的往来可言，无从
            # 推算候选，清标记、跳过——与 refresh_breakups 的既有处理一致。
            store.set_contact_breakup_pending(contact.session_id, "")
            skipped += 1
            continue
        candidate = find_breakup_candidate(store, contact.session_id, moment)
        if candidate is not None:
            # 候选写回 pending、保留原始 at：排队顺序仍按最初标记时间算，
            # 不因为推算过而插队；date_source 标记来源，refresh_breakups
            # 写结论时原样透传。
            store.set_contact_breakup_pending(
                contact.session_id,
                json.dumps(
                    {
                        "date": candidate,
                        "certainty": certainty,
                        "at": int(pending.get("at") or moment),
                        "date_source": "guessed",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            resolved += 1
        else:
            # 推算不出候选：终态失败，不再排队重算；用户可以清除，也可以
            # 手动选一个具体日期重新标记。
            store.set_contact_breakup(
                contact.session_id,
                json.dumps(
                    {
                        "verdict": "unknown",
                        "kind": "",
                        "date": None,
                        "certainty": certainty,
                        "note": (
                            "聊天记录里找不到明显的往来断崖，无法自动判定"
                            "绝交日期；如果确定发生过，可以手动选一个日期"
                            "重新标记。"
                        ),
                        "decided_at": moment,
                        "source": "guess_failed",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            store.set_contact_breakup_pending(contact.session_id, "")
            failed += 1
    # 只记数量，不记内容：候选日期与失败原因不进日志。
    LOG.info(
        "绝交日期推算：推算出 %d 个、推算失败 %d 个，跳过 %d 个",
        resolved,
        failed,
        skipped,
    )
    return resolved + failed


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
    时刻升序截断到单轮上限。候选也可能来自 guess_breakup_dates 的推算
    （date_source="guessed"），走的是完全同一套判定，不额外分支。证据
    按优先级：LLM 判 broken → 确认；否则冷断统计达标 → 确认；否则 LLM
    判 normal → 否决；再否则按置信度决定直接生效（certain）或保留标记
    下一轮再核（suspected）。没有打过分的候选人无从核实：清除标记、跳过。
    progress phase = "breakup"。
    """

    candidates: list[ContactRow] = []
    for contact in store.all_contacts():
        if not contact.breakup_pending:
            continue
        data = contact.breakup_pending_data()
        date = data.get("date") if data is not None else None
        certainty = data.get("certainty") if data is not None else None
        if date is None and certainty in ("certain", "suspected"):
            # 日期待推算：guess_breakup_dates 的地盘。本轮如果轮到它，
            # 要么已经把 date 补上、要么已经写终态失败并清空 pending；
            # 如果还留在这里，说明推算阶段本轮预算没轮到它，下一轮再说，
            # 不是脏数据，不能清。
            continue
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
                    # 溯源：候选来自推算就是 "guessed"，否则是用户手填的
                    # 具体日期；旧数据没有这个键，缺省按 "user" 处理。
                    "date_source": pending.get("date_source", "user"),
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


# —— 展示裁剪：详情页温度曲线只显示绝交日之前的点 ——


def truncate_history_at_breakup(
    rows: list[dict], row: ContactRow
) -> tuple[list[dict], dict | None]:
    """确认绝交的联系人只显示绝交日（含）之前的温度曲线。

    截断只发生在下发时：score_history 的行一条不动——曲线是客观记录，
    清除绝交标记后完整曲线要能立刻回来，不必重算全史。没有真的截掉
    东西时不返回 cutoff，免得前端在数据范围外画标记线撑坏 time 轴。
    """

    data = row.breakup_data()
    if data is None or data.get("verdict") != "confirmed":
        return rows, None
    cutoff_day = data.get("date")
    if not isinstance(cutoff_day, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", cutoff_day
    ):
        # 脏数据防御：日期形状不对就不截断，绝交结论照常展示。
        return rows, None
    kept = [point for point in rows if point["day"] <= cutoff_day]
    if len(kept) == len(rows):
        # 绝交日晚于最后一个采样点：曲线本来就结束了，截断没有意义。
        return rows, None
    return kept, {
        "day": cutoff_day,
        "kind": data.get("kind"),
        "certainty": data.get("certainty"),
        "date_source": data.get("date_source", "user"),
    }
