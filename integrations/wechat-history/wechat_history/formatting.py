"""Privacy-preserving message decoding adapted from wechat-cli messages.py."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import zstandard as zstd


_ZSTD = zstd.ZstdDecompressor()
_UNSAFE_XML = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
_MAX_XML_CHARS = 20_000
_MAX_DECOMPRESSED_BYTES = 8 * 1024 * 1024


def split_message_type(value: object) -> tuple[int, int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0, 0
    if number > 0xFFFFFFFF:
        return number & 0xFFFFFFFF, number >> 32
    return number, 0


def message_kind(value: object) -> str:
    base, subtype = split_message_type(value)
    if base == 49 and subtype == 6:
        return "file"
    return {
        1: "text",
        3: "image",
        34: "voice",
        42: "contact_card",
        43: "video",
        47: "sticker",
        48: "location",
        49: "link",
        50: "call",
        10000: "system",
        10002: "recalled",
    }.get(base, "unknown")


def decompress_content(content: object, compression_type: object) -> str | None:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, bytes):
        return str(content)
    try:
        if int(compression_type or 0) == 4:
            raw = _ZSTD.decompress(content, max_output_size=_MAX_DECOMPRESSED_BYTES)
        else:
            raw = content
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return None


def _xml_root(content: str) -> ET.Element | None:
    if not content or len(content) > _MAX_XML_CHARS or _UNSAFE_XML.search(content):
        return None
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        return None


def _compact(text: str, limit: int = 4000) -> str:
    value = re.sub(r"[\t\r ]+", " ", text or "").strip()
    return value if len(value) <= limit else value[:limit] + "…"


def _app_message(content: str) -> tuple[str, dict]:
    root = _xml_root(content)
    if root is None:
        return "[链接/文件]", {}
    app = root.find(".//appmsg")
    if app is None:
        return "[链接/文件]", {}
    title = _compact(app.findtext("title") or "", 500)
    try:
        app_type = int((app.findtext("type") or "0").strip())
    except ValueError:
        app_type = 0
    metadata = {"title": title} if title else {}
    if app_type == 6:
        return (f"[文件] {title}" if title else "[文件]"), metadata
    if app_type == 5:
        return (f"[链接] {title}" if title else "[链接]"), metadata
    if app_type in (33, 36, 44):
        return (f"[小程序] {title}" if title else "[小程序]"), metadata
    if app_type == 57:
        quoted = app.find(".//refermsg")
        quoted_text = _compact(quoted.findtext("content") or "", 160) if quoted is not None else ""
        text = title or "[引用消息]"
        if quoted_text:
            text += f"\n↳ {quoted_text}"
        return text, metadata
    return (f"[链接/文件] {title}" if title else "[链接/文件]"), metadata


def _call_message(content: str) -> str:
    root = _xml_root(content)
    if root is None:
        return "[通话]"
    raw = _compact(root.findtext(".//msg") or "", 200)
    if raw.startswith("Duration:"):
        return f"[通话] 通话时长 {raw.split(':', 1)[1].strip()}"
    labels = {
        "Canceled": "已取消",
        "Line busy": "对方忙线",
        "Call not answered": "未接听",
        "Call wasn't answered": "未接听",
    }
    return f"[通话] {labels.get(raw, raw)}" if raw else "[通话]"


def format_message(
    local_id: object,
    local_type: object,
    content: str,
    is_group: bool,
) -> tuple[str, str, dict]:
    """Return (sender hint, safe text, metadata) without resolving media files."""

    sender_hint = ""
    body = content or ""
    if is_group and ":\n" in body:
        sender_hint, body = body.split(":\n", 1)
    kind = message_kind(local_type)
    metadata: dict = {}
    if kind == "text":
        text = _compact(body)
    elif kind == "image":
        text = "[图片]"
        metadata = {"local_id": str(local_id)}
    elif kind == "voice":
        text = "[语音]"
    elif kind == "video":
        text = "[视频]"
    elif kind == "sticker":
        text = "[表情]"
    elif kind == "contact_card":
        text = "[名片]"
    elif kind == "location":
        text = "[位置]"
    elif kind in ("link", "file"):
        text, metadata = _app_message(body)
    elif kind == "call":
        text = _call_message(body)
    elif kind == "recalled":
        text = "[撤回消息]"
    elif kind == "system":
        text = "[系统消息]"
    else:
        text = "[不支持的消息类型]"
    return sender_hint.strip(), text, metadata

