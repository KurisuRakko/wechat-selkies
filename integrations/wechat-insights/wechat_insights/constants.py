"""可配置常量与指标列定义。

所有阈值都集中在这里，方便调参；环境变量只在容器启动时读一次。
"""

from __future__ import annotations

import os
from pathlib import Path


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    """读取整数环境变量，越界或非法时回退到默认值。"""

    try:
        value = int(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default
    return default if value < minimum or value > maximum else value


# —— 运行时路径与网络 ——
DB_PATH = Path(os.environ.get("INSIGHTS_DB_PATH", "/data/metrics.db"))
BIND_HOST = os.environ.get("INSIGHTS_BIND_HOST", "0.0.0.0")
BIND_PORT = _int_env("INSIGHTS_BIND_PORT", 8300, 1, 65535)
# 未设置则不鉴权，启动时会打印一条醒目警告。
AUTH_TOKEN = os.environ.get("INSIGHTS_AUTH_TOKEN", "")
AUTH_COOKIE = "wechat_insights_token"

# 每天固定时刻跑一轮增量分析，格式 HH:MM（容器本地时区）。
ANALYZE_TIME = os.environ.get("INSIGHTS_ANALYZE_TIME", "04:30")
# 容器启动后如果从未成功分析过，立即补跑一轮（首轮全量回填）。
RUN_ON_START = os.environ.get("INSIGHTS_RUN_ON_START", "true").lower() != "false"

# —— 对话切分与消息判定 ——
# 相邻消息间隔超过该秒数即视为两段独立对话。
SESSION_GAP_SECONDS = _int_env("INSIGHTS_SESSION_GAP_SECONDS", 6 * 3600, 300, 86400)
# 回复延迟小于该秒数记为「秒回」。
FAST_REPLY_SECONDS = 60
# 文本超过该字数记为「长消息」。
LONG_MESSAGE_CHARS = 50
# 对话轮次（同向连续块数）超过该值记为「长对话」。
LONG_CONVERSATION_TURNS = 20
# 深夜窗口 23:00–01:59:59（小时 23/0/1）：落在这个集合里即算深夜。
NIGHT_HOURS = frozenset({23, 0, 1})
# 「聊到最晚的一次」只在凌晨窗口里比较，避免 23:59 永远压过 02:00。
LATE_NIGHT_MAX_HOUR = 6

# —— 打分窗口 ——
SCORE_WINDOW_DAYS = _int_env("INSIGHTS_SCORE_WINDOW_DAYS", 730, 7, 3650)
# 打分窗口内每个天桶按 0.5^(天龄/半衰期) 加权：两年窗口 + 90 天半衰期下，
# 两年前的一天权重约 0.4%。「很久没聊」会自然滑向归零，不需要单独的截断补丁。
DECAY_HALF_LIFE_DAYS = _int_env("INSIGHTS_DECAY_HALF_LIFE_DAYS", 90, 7, 3650)
TREND_RECENT_DAYS = _int_env("INSIGHTS_TREND_RECENT_DAYS", 30, 3, 365)
# 趋势基线取「近期窗口之前」的这么多天。
TREND_BASELINE_DAYS = _int_env("INSIGHTS_TREND_BASELINE_DAYS", 90, 7, 3650)
# 打分窗口内往来消息少于该值的联系人不打分，只显示「数据不足」。
MIN_SCORE_MESSAGES = _int_env("INSIGHTS_MIN_SCORE_MESSAGES", 50, 1, 100000)
# 回复延迟中位数至少要有这么多个回复样本才可信。
MIN_REPLY_SAMPLES = 3
# 「近期异动」判定：原始值相对变化超过该倍数，且样本量达标。
ANOMALY_MIN_RATIO = 2.0
ANOMALY_MIN_SAMPLES = 10

# —— 「正在淡出」提醒 ——
# 已打分联系人沉默达到该天数、且综合分还够高时，进列表页的提醒卡：
# 在归零之前抓住正在滑落的高分关系，让看板从观赏变成行动。
FADE_MIN_GAP_DAYS = _int_env("INSIGHTS_FADE_MIN_GAP_DAYS", 14, 3, 365)
# 综合分低于该值的联系人不提醒——分数已经见底，不构成「正在淡出」。
FADE_MIN_OVERALL = _int_env("INSIGHTS_FADE_MIN_OVERALL", 40, 0, 100)
# 提醒名单最多取这么多位，按综合分降序。
FADE_LIST_LIMIT = _int_env("INSIGHTS_FADE_LIST_LIMIT", 8, 1, 50)

# —— 投入维度的类型成本权重 ——
# 语音/通话比一条文字贵得多，直接按「等价条数」加权。
COST_WEIGHTS = {
    "call": 8.0,
    "voice": 3.0,
    "video": 3.0,
    "image": 1.5,
    "sticker": 1.0,
    "location": 1.0,
    "link": 1.0,
    "file": 1.5,
    "contact_card": 1.0,
}
# 文字消息按字数折算成本：每这么多字算一个成本单位。
TEXT_CHARS_PER_COST_UNIT = 20.0

# —— 大模型深度打分（可选，默认关闭）——
# 为空 = 禁用 LLM 深度打分，完全离线；给出发送端点才启用，例如
# http://localhost:11434/v1（OpenAI 兼容）。末尾斜杠在 llm.py 里容错。
INSIGHTS_LLM_BASE_URL = os.environ.get("INSIGHTS_LLM_BASE_URL", "")
INSIGHTS_LLM_API_KEY = os.environ.get("INSIGHTS_LLM_API_KEY", "")
INSIGHTS_LLM_MODEL = os.environ.get("INSIGHTS_LLM_MODEL", "")
# 单次 LLM 请求的超时（秒）；超时按失败处理并重试一次。
INSIGHTS_LLM_TIMEOUT_SECONDS = _int_env("INSIGHTS_LLM_TIMEOUT_SECONDS", 30, 5, 300)

# —— 出站敏感词屏蔽 ——
# 可选用户词表文件（UTF-8，每行一词，# 开头为注释、空行忽略），与内置种子
# 词表合并去重；读不到时记 WARNING 并只用种子词表。任何要离开容器的聊天
# 文本都必须过 masking.mask()。
INSIGHTS_MASK_WORDS_FILE = os.environ.get("INSIGHTS_MASK_WORDS_FILE", "")

# —— LLM 深度打分采样与刷新 ——
# 采样回看天数：候选联系人在这么多天内有消息才会送评。
LLM_SAMPLE_DAYS = _int_env("INSIGHTS_LLM_SAMPLE_DAYS", 60, 7, 730)
# 分数保鲜期：超过这么多天没有重新评过分就重评。
LLM_REFRESH_DAYS = _int_env("INSIGHTS_LLM_REFRESH_DAYS", 30, 1, 365)
# 新增消息门槛：联系人累计消息数比打分时多出这么多条就重评。
LLM_REFRESH_MESSAGES = _int_env("INSIGHTS_LLM_REFRESH_MESSAGES", 200, 10, 100000)
# 单轮分析里最多调用多少次 LLM（成本控制上限，超出部分下一轮再评）。
LLM_MAX_CALLS_PER_RUN = _int_env("INSIGHTS_LLM_MAX_CALLS_PER_RUN", 40, 1, 1000)
# 一次采样最多送多少字的聊天文本（越长越贵；达到即停，不足就送全部）。
LLM_SAMPLE_MAX_CHARS = _int_env("INSIGHTS_LLM_SAMPLE_MAX_CHARS", 4000, 500, 20000)

# —— 增量读取 ——
# 单批读取的消息条数上限；首轮回填靠多批循环推进。
BACKFILL_BATCH = _int_env("INSIGHTS_BACKFILL_BATCH", 5000, 100, 200000)
# 明文缓存上限不在这里配：它是 wechat_history 的旋钮
# （WECHAT_HISTORY_MAX_CACHE_BYTES），本容器与主容器读的是同一批库、需要同一个
# 上限，各配一份只会让人改了一处以为生效了。

# —— 指标列 ——
# 与 wechat_history.formatting.message_kind 的返回值一一对应。
MESSAGE_KINDS = (
    "text",
    "image",
    "voice",
    "video",
    "sticker",
    "location",
    "link",
    "call",
    "file",
    "contact_card",
    "system",
    "recalled",
    "unknown",
)

# them = 联系人发来的，me = 我发出去的。
_SIDES = ("them", "me")

_BASE_METRICS = (
    "msgs_them",
    "msgs_me",
    "chars_them",
    "chars_me",
    "questions_them",
    "questions_me",
    "long_msgs_them",
    "long_msgs_me",
    "night_msgs_them",
    "night_msgs_me",
    "weekend_msgs_them",
    "weekend_msgs_me",
    "conversations",
    "conv_started_them",
    "conv_started_me",
    "conv_ended_them",
    "conv_ended_me",
    "turns_total",
    "long_convs",
    "runs_them",
    "runs_them_multi",
    "runs_me",
    "runs_me_multi",
    "replies_them",
    "replies_me",
    "fast_replies_them",
    "fast_replies_me",
)

KIND_METRICS = tuple(
    f"kind_{kind}_{side}" for kind in MESSAGE_KINDS for side in _SIDES
)

#: stats_daily 的全部数值列，建表与聚合语句都由它生成。
METRIC_COLUMNS = _BASE_METRICS + KIND_METRICS

#: 回复延迟直方图列（JSON 数组文本，跨天可直接逐桶相加）。
HISTOGRAM_COLUMNS = ("reply_hist_them", "reply_hist_me")

# 对数直方图桶数：桶 i 覆盖 [2^i, 2^(i+1)) 秒，最后一桶约 97 天封顶。
HISTOGRAM_BUCKETS = 24

SCHEMA_VERSION = 2
