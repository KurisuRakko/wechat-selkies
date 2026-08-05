"""Put an already-uploaded file into the WeChat input box. Never sends it.

The browser drops a file on the stream, Selkies' own uploader writes it to
UPLOAD_ROOT over the shared WebSocket, and then the page asks this module to
paste it into whatever conversation WeChat currently has open. Doing the paste
here rather than in the page is what makes it reliable: the page can only blind
-fire Ctrl+V at the stream and hope WeChat's input box has focus, while this
runs next to WeChat and can activate the window, verify focus, click the input
box and verify focus again before touching the clipboard.

Differences from ReplyPreparer, which this deliberately reuses the primitives
of (CommandRunner, Clipboard, find_wechat_window, and the same flock so a
draft and an attachment can never interleave):

  * No session search. Whatever conversation the user is looking at is the
    target — the drop already happened in that context.
  * No Ctrl+A emptiness check. A draft the user is halfway through typing must
    survive; the attachment is pasted at the caret and appended to it.
  * No read-back verification. The pasted payload is an image or a file URI,
    not text, so there is nothing to compare against.
  * The flock is waited for rather than failed on, because two files dropped
    together arrive as two back-to-back requests.

Like reply.py, this module contains no Return/KP_Enter path at all: _key
refuses those keys outright, so a bug here cannot send a message.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from .constants import CACHE_ROOT
from .errors import HistoryError, fail
from .reply import Clipboard, CommandRunner, WeChatWindow, find_wechat_window


# Selkies' uploader writes dropped files here, keeping the dropped name
# verbatim. nginx also publishes the same directory at /files.
UPLOAD_ROOT = Path(os.environ.get("WECHAT_ATTACH_UPLOAD_ROOT", "/config/Desktop"))

MAX_FILENAME_CHARS = 255
# Above this, inlining the bytes through xclip costs more than it is worth and
# the file URI form is used instead.
MAX_INLINE_IMAGE_BYTES = 20 * 1024 * 1024

STABLE_POLL_SECONDS = 0.25
STABLE_CONFIRMATIONS = 2
# The client reports "end" the moment it hands the last chunk to the socket, so
# the bytes are still in flight. Wait for the file to reach the announced size
# and hold it for two polls. nginx's proxy_read_timeout is 60 s and the lock
# wait plus the UI work below can add ~17 s, so this ceiling has to stay well
# under that.
MIN_WAIT_SECONDS = 10.0
MAX_WAIT_SECONDS = 40.0
WAIT_BYTES_PER_SECOND = 256 * 1024

LOCK_TIMEOUT_SECONDS = 15.0
LOCK_POLL_SECONDS = 0.25

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

# WeChat is a Qt application. Qt maps an image/png selection target onto
# application/x-qt-image, which is what QMimeData::hasImage() consumes, so
# Ctrl+V inlines the picture. text/uri-list surfaces via QMimeData::hasUrls()
# and attaches the file instead. Announcing the wrong target for the bytes
# pastes garbage, so the target is chosen from the file's magic number and
# never from its extension or the browser-reported MIME type.
IMAGE_PNG = "image/png"
IMAGE_JPEG = "image/jpeg"
URI_LIST = "text/uri-list"

ALLOWED_KINDS = ("image", "file")


def resolve_upload(file_name: object) -> Path:
    """Map a browser-reported name onto exactly one file directly in UPLOAD_ROOT."""

    if not isinstance(file_name, str) or not file_name:
        raise fail("INVALID_FILENAME", "文件名不合法")
    if len(file_name) > MAX_FILENAME_CHARS:
        raise fail("INVALID_FILENAME", "文件名不合法")
    if file_name in (".", ".."):
        raise fail("INVALID_FILENAME", "文件名不合法")
    if any(character in file_name for character in ("/", "\\", "\x00")):
        raise fail("INVALID_FILENAME", "文件名不合法")

    root = UPLOAD_ROOT.resolve()
    # resolve() also follows symlinks, so a link planted in the upload
    # directory cannot point the paste at something outside it.
    candidate = (UPLOAD_ROOT / file_name).resolve()
    if candidate.parent != root or candidate == root:
        raise fail("INVALID_FILENAME", "文件名不合法")
    return candidate


def _file_uri(path: Path) -> bytes:
    return b"file://" + quote(str(path)).encode("ascii") + b"\r\n"


def _clipboard_payload(path: Path, kind: str, size: int) -> tuple[str, bytes]:
    if kind == "image" and size <= MAX_INLINE_IMAGE_BYTES:
        try:
            with path.open("rb") as handle:
                header = handle.read(len(PNG_MAGIC))
                if header.startswith(PNG_MAGIC):
                    handle.seek(0)
                    return IMAGE_PNG, handle.read()
                if header.startswith(JPEG_MAGIC):
                    handle.seek(0)
                    return IMAGE_JPEG, handle.read()
        except OSError as exc:
            raise fail("UPLOAD_UNREADABLE", "无法读取已上传的文件") from exc
    return URI_LIST, _file_uri(path)


class AttachClipboard(Clipboard):
    # text/uri-list is added so that a snapshot taken right after a failed
    # attachment can still save and restore whatever is on the selection.
    _BINARY_TARGETS = (IMAGE_PNG, IMAGE_JPEG, URI_LIST)


class AttachPreparer:
    def __init__(
        self,
        runner: CommandRunner | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self.runner = runner or CommandRunner()
        self.clipboard = AttachClipboard(self.runner)
        self.sleep = sleep
        self.clock = clock

    # ----------------------------------------------------------- primitives
    # Copied from ReplyPreparer rather than shared, so the "no send key" rule
    # is enforced independently in both modules.

    def _key(self, window_id: str, key: str) -> None:
        # Intentionally no Return/KP_Enter path exists in this module.
        if key.casefold() in {"return", "kp_enter", "enter"}:
            raise fail("SEND_BLOCKED", "附件工具禁止模拟发送按键")
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
            raise fail("FOCUS_NOT_WECHAT", "微信主窗口未保持焦点，未放入附件") from exc

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

    # ---------------------------------------------------------------- waits

    def wait_for_stable_file(self, path: Path, expected_size: int) -> int:
        budget = min(
            MAX_WAIT_SECONDS,
            MIN_WAIT_SECONDS + expected_size / WAIT_BYTES_PER_SECOND,
        )
        deadline = self.clock() + budget
        confirmations = 0
        while True:
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            if size == expected_size:
                confirmations += 1
                if confirmations >= STABLE_CONFIRMATIONS:
                    return size
            else:
                confirmations = 0
            if self.clock() >= deadline:
                raise fail("UPLOAD_INCOMPLETE", "等待上传完成超时")
            self.sleep(STABLE_POLL_SECONDS)

    def _acquire(self, lock) -> None:
        deadline = self.clock() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError as exc:
                if self.clock() >= deadline:
                    raise fail("REPLY_BUSY", "另一个草稿操作正在进行") from exc
                self.sleep(LOCK_POLL_SECONDS)

    # ----------------------------------------------------------------- main

    def attach(self, file_name: object, size: object, kind: object) -> dict:
        if kind not in ALLOWED_KINDS:
            raise fail("INVALID_KIND", "不支持的附件类型")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise fail("INVALID_SIZE", "文件大小不合法")
        path = resolve_upload(file_name)
        actual_size = self.wait_for_stable_file(path, size)
        window = find_wechat_window(self.runner)

        CACHE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = CACHE_ROOT / "reply.lock"
        with lock_path.open("a+b") as lock:
            if os.name == "posix":
                os.chmod(lock_path, 0o600)
            self._acquire(lock)
            return self._attach_locked(window, path, str(kind), actual_size)

    def _attach_locked(
        self, window: WeChatWindow, path: Path, kind: str, size: int
    ) -> dict:
        target, data = _clipboard_payload(path, kind, size)
        original_clipboard = self.clipboard.snapshot()
        action_error: Exception | None = None
        try:
            self.runner.action(
                ["xdotool", "windowactivate", "--sync", window.window_id]
            )
            self._require_focus(window.window_id)

            # Same input-box coordinates ReplyPreparer uses against this exact
            # WeChat build. Clicking places the caret; an existing draft is kept
            # and the attachment lands after it.
            input_x = int(window.width * 0.72)
            input_y = int(window.height * 0.86)
            self._click(window.window_id, input_x, input_y)
            self.sleep(0.15)
            self._require_focus(window.window_id)

            self.clipboard.set_target(target, data)
            self._key(window.window_id, "ctrl+v")
            # X11 paste is pull-based: WeChat converts the selection after the
            # keystroke, so restoring the old clipboard immediately would make
            # it paste the previous contents instead.
            self.sleep(0.3)
        except HistoryError as exc:
            action_error = exc
        except (OSError, subprocess.SubprocessError):
            action_error = fail("UI_AUTOMATION_FAILED", "微信界面操作失败，未放入附件")
        finally:
            try:
                self.clipboard.restore(original_clipboard)
            except HistoryError as restore_error:
                if action_error is None:
                    action_error = restore_error
        if action_error is not None:
            raise action_error
        return {
            "ok": True,
            "attached": "image" if target.startswith("image/") else "file",
            "target": target,
            "fileName": path.name,
            "size": size,
            "sent": False,
        }
