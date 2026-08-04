"""X11 reply drafting that deliberately has no send operation."""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .constants import CACHE_ROOT, MAX_REPLY_CHARS
from .errors import HistoryError, fail
from .reader import HistoryReader


@dataclass(frozen=True, slots=True)
class ClipboardSnapshot:
    target: str | None
    data: bytes


@dataclass(frozen=True, slots=True)
class WeChatWindow:
    window_id: str
    width: int
    height: int


class CommandRunner:
    """Small subprocess boundary that can be replaced in unit tests."""

    def __init__(self, display: str | None = None):
        self.environment = os.environ.copy()
        self.environment["DISPLAY"] = display or self.environment.get("DISPLAY", ":1")

    def output(self, arguments: list[str], *, input_data: bytes | None = None) -> bytes:
        result = subprocess.run(
            arguments,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self.environment,
            check=True,
            timeout=5,
        )
        return result.stdout

    def action(self, arguments: list[str], *, input_data: bytes | None = None) -> None:
        subprocess.run(
            arguments,
            input=input_data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self.environment,
            check=True,
            timeout=5,
        )


class Clipboard:
    _TEXT_TARGETS = (
        "UTF8_STRING",
        "text/plain;charset=utf-8",
        "text/plain",
        "STRING",
    )
    _BINARY_TARGETS = ("image/png", "image/jpeg")

    def __init__(self, runner: CommandRunner):
        self.runner = runner

    def snapshot(self) -> ClipboardSnapshot:
        try:
            targets = self.runner.output(
                ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]
            ).decode("ascii", errors="ignore").splitlines()
        except (OSError, subprocess.SubprocessError):
            return ClipboardSnapshot(None, b"")
        for target in (*self._TEXT_TARGETS, *self._BINARY_TARGETS):
            if target not in targets:
                continue
            try:
                data = self.runner.output(
                    ["xclip", "-selection", "clipboard", "-t", target, "-o"]
                )
                return ClipboardSnapshot(target, data)
            except (OSError, subprocess.SubprocessError):
                continue
        if targets:
            raise fail("CLIPBOARD_UNSUPPORTED", "当前剪贴板格式无法安全保存和恢复")
        return ClipboardSnapshot(None, b"")

    def restore(self, snapshot: ClipboardSnapshot) -> None:
        target = snapshot.target or "UTF8_STRING"
        try:
            self.runner.action(
                ["xclip", "-selection", "clipboard", "-t", target, "-i"],
                input_data=snapshot.data,
            )
        except (OSError, subprocess.SubprocessError):
            raise fail("CLIPBOARD_RESTORE_FAILED", "无法恢复原剪贴板内容")

    def set_text(self, text: str) -> None:
        try:
            self.runner.action(
                ["xclip", "-selection", "clipboard", "-t", "UTF8_STRING", "-i"],
                input_data=text.encode("utf-8"),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise fail("CLIPBOARD_FAILED", "无法设置 X11 剪贴板") from exc

    def get_text(self) -> str:
        try:
            return self.runner.output(
                ["xclip", "-selection", "clipboard", "-t", "UTF8_STRING", "-o"]
            ).decode("utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError) as exc:
            raise fail("CLIPBOARD_FAILED", "无法读取 X11 剪贴板") from exc


def find_wechat_window(runner: CommandRunner) -> WeChatWindow:
    try:
        raw_ids = runner.output(
            ["xdotool", "search", "--onlyvisible", "--class", "wechat"]
        ).decode("ascii", errors="ignore")
    except (OSError, subprocess.SubprocessError) as exc:
        raise fail("WECHAT_NOT_VISIBLE", "找不到可见的微信主窗口") from exc
    candidates: list[WeChatWindow] = []
    visible_ids: list[str] = []
    for window_id in (line.strip() for line in raw_ids.splitlines()):
        if not window_id.isdigit() or window_id in visible_ids:
            continue
        visible_ids.append(window_id)
        try:
            geometry = runner.output(
                ["xdotool", "getwindowgeometry", "--shell", window_id]
            ).decode("ascii", errors="ignore")
        except (OSError, subprocess.SubprocessError):
            continue
        values: dict[str, int] = {}
        for line in geometry.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if value.strip().lstrip("-").isdigit():
                values[key] = int(value)
        width, height = values.get("WIDTH", 0), values.get("HEIGHT", 0)
        if width >= 500 and height >= 600:
            candidates.append(WeChatWindow(window_id, width, height))
    if len(candidates) > 1:
        raise fail("WECHAT_WINDOW_AMBIGUOUS", "检测到多个微信主窗口，拒绝填写草稿")
    if len(visible_ids) > 1 or (visible_ids and not candidates):
        raise fail("WECHAT_DIALOG_VISIBLE", "检测到微信弹窗；请先在界面中处理后重试")
    if not candidates:
        raise fail("WECHAT_NOT_VISIBLE", "微信窗口尺寸不符合已验证的界面布局")
    return candidates[0]


def probe_wechat_window_status(runner: CommandRunner | None = None) -> str:
    try:
        find_wechat_window(runner or CommandRunner())
        return "visible"
    except HistoryError:
        return "not_visible"


class ReplyPreparer:
    def __init__(
        self,
        reader: HistoryReader,
        runner: CommandRunner | None = None,
        sleep=time.sleep,
    ):
        self.reader = reader
        self.runner = runner or CommandRunner()
        self.clipboard = Clipboard(self.runner)
        self.sleep = sleep

    def window_status(self) -> str:
        return probe_wechat_window_status(self.runner)

    def _find_window(self) -> WeChatWindow:
        return find_wechat_window(self.runner)

    def _key(self, window_id: str, key: str) -> None:
        # Intentionally no Return/KP_Enter path exists in this module.
        if key.casefold() in {"return", "kp_enter", "enter"}:
            raise fail("SEND_BLOCKED", "草稿工具禁止模拟发送按键")
        self.runner.action(
            ["xdotool", "key", "--window", window_id, "--clearmodifiers", key]
        )

    def _require_focus(self, window_id: str) -> None:
        try:
            focused = self.runner.output(["xdotool", "getwindowfocus"]).decode(
                "ascii", errors="ignore"
            ).strip()
            if not focused.isdigit() or int(focused) != int(window_id):
                raise ValueError
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise fail("FOCUS_NOT_WECHAT", "微信主窗口未保持焦点，未填写草稿") from exc

    def _click(self, window_id: str, x: int, y: int) -> None:
        self.runner.action(
            [
                "xdotool",
                "mousemove",
                "--window",
                window_id,
                str(x),
                str(y),
                "click",
                "1",
            ]
        )

    def prepare(self, session_id: str, text: str) -> dict:
        if not isinstance(text, str) or not text.strip():
            raise fail("INVALID_REPLY", "回复草稿不能为空")
        if len(text) > MAX_REPLY_CHARS:
            raise fail("INVALID_REPLY", f"回复草稿不能超过 {MAX_REPLY_CHARS} 个字符")
        if "\x00" in text:
            raise fail("INVALID_REPLY", "回复草稿不能包含 NUL 字符")

        self.reader.require_reply_account_active()
        session = self.reader.reply_session(session_id)
        window = self._find_window()
        CACHE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = CACHE_ROOT / "reply.lock"
        with lock_path.open("a+b") as lock:
            if os.name == "posix":
                os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise fail("REPLY_BUSY", "另一个草稿操作正在进行") from exc
            return self._prepare_locked(window, session, text)

    def _prepare_locked(self, window: WeChatWindow, session: dict, text: str) -> dict:
        original_clipboard = self.clipboard.snapshot()
        inserted = False
        action_error: Exception | None = None
        try:
            self.runner.action(
                ["xdotool", "windowactivate", "--sync", window.window_id]
            )
            self._require_focus(window.window_id)
            self._key(window.window_id, "ctrl+f")
            self.sleep(0.15)
            self._key(window.window_id, "ctrl+a")
            self.clipboard.set_text(session["ui_query"])
            self._key(window.window_id, "ctrl+v")
            self.sleep(0.8)

            result_x = max(90, min(int(window.width * 0.20), 170))
            result_y = max(95, min(int(window.height * 0.14), 140))
            self._click(window.window_id, result_x, result_y)
            self.sleep(0.6)

            input_x = int(window.width * 0.72)
            input_y = int(window.height * 0.86)
            self._click(window.window_id, input_x, input_y)
            self.sleep(0.15)
            self._require_focus(window.window_id)

            sentinel = f"__WECHAT_HISTORY_EMPTY_CHECK_{os.getpid()}__"
            self.clipboard.set_text(sentinel)
            self._key(window.window_id, "ctrl+a")
            self._key(window.window_id, "ctrl+c")
            self.sleep(0.1)
            existing = self.clipboard.get_text()
            if existing != sentinel and existing:
                self._key(window.window_id, "End")
                raise fail("DRAFT_NOT_EMPTY", "微信输入框已有内容，拒绝覆盖")

            self.clipboard.set_text(text)
            self._key(window.window_id, "ctrl+v")
            inserted = True
            self.sleep(0.2)
            self._key(window.window_id, "ctrl+a")
            self._key(window.window_id, "ctrl+c")
            self.sleep(0.1)
            verified = self.clipboard.get_text()
            if verified != text:
                self._key(window.window_id, "ctrl+z")
                inserted = False
                raise fail("DRAFT_VERIFY_FAILED", "无法确认草稿已进入微信输入框")
            self._key(window.window_id, "End")
        except HistoryError as exc:
            if inserted:
                try:
                    self._key(window.window_id, "ctrl+z")
                    inserted = False
                except Exception:
                    pass
            action_error = exc
        except (OSError, subprocess.SubprocessError) as exc:
            if inserted:
                try:
                    self._key(window.window_id, "ctrl+z")
                except Exception:
                    pass
            action_error = fail("UI_AUTOMATION_FAILED", "微信界面操作失败，未执行发送")
        finally:
            try:
                self.clipboard.restore(original_clipboard)
            except HistoryError as restore_error:
                if action_error is None:
                    action_error = restore_error
        if action_error is not None:
            raise action_error
        return {
            "status": "draft_prepared",
            "session_id": session["session_id"],
            "display_name": session["display_name"],
            "characters": len(text),
            "sent": False,
            "requires_manual_send": True,
            "instruction": "请在微信中核对账户、会话和草稿内容后手动发送",
        }
