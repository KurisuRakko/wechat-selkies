from __future__ import annotations

import fcntl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from wechat_history.attach import AttachPreparer
from wechat_history.errors import HistoryError
from wechat_history.reply import CommandRunner


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake png body"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"fake jpeg body"
GIF_BYTES = b"GIF89a" + b"fake gif body"


class FakeClock:
    """Monotonic clock that only advances when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeRunner(CommandRunner):
    def __init__(self) -> None:
        self.clipboard_target = "UTF8_STRING"
        self.clipboard = b"original clipboard"
        self.commands: list[list[str]] = []
        self.pasted: list[tuple[str, bytes]] = []
        self.focus = b"123\n"

    def output(self, arguments: list[str], *, input_data: bytes | None = None) -> bytes:
        self.commands.append(arguments)
        if arguments[:4] == ["xdotool", "search", "--onlyvisible", "--class"]:
            return b"123\n"
        if arguments[:2] == ["xdotool", "getwindowgeometry"]:
            return b"WINDOW=123\nX=0\nY=0\nWIDTH=560\nHEIGHT=760\n"
        if arguments[:2] == ["xdotool", "getwindowfocus"]:
            return self.focus
        if "TARGETS" in arguments:
            return b"TARGETS\nUTF8_STRING\n"
        return self.clipboard

    def action(self, arguments: list[str], *, input_data: bytes | None = None) -> None:
        self.commands.append(arguments)
        if arguments and arguments[0] == "xclip" and input_data is not None:
            self.clipboard_target = arguments[arguments.index("-t") + 1]
            self.clipboard = input_data
            return
        if arguments[:2] == ["xdotool", "key"] and arguments[-1] == "ctrl+v":
            self.pasted.append((self.clipboard_target, self.clipboard))

    # convenience -----------------------------------------------------------
    @property
    def keys(self) -> list[str]:
        return [c[-1] for c in self.commands if c[:2] == ["xdotool", "key"]]

    def used(self, program: str) -> bool:
        return any(command and command[0] == program for command in self.commands)


class AttachTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.cache = self.root / "cache"
        self.clock = FakeClock()
        patches = [
            patch("wechat_history.attach.UPLOAD_ROOT", self.uploads),
            patch("wechat_history.attach.CACHE_ROOT", self.cache),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self._temporary.cleanup)

    def preparer(self, runner: FakeRunner) -> AttachPreparer:
        return AttachPreparer(runner=runner, sleep=self.clock.sleep, clock=self.clock)

    def write(self, name: str, data: bytes) -> int:
        (self.uploads / name).write_bytes(data)
        return len(data)


class AttachSuccessTests(AttachTestCase):
    def test_plain_file_pastes_percent_encoded_uri_once(self) -> None:
        size = self.write("report notes.txt", b"hello world")
        runner = FakeRunner()
        result = self.preparer(runner).attach("report notes.txt", size, "file")

        self.assertEqual(result["attached"], "file")
        self.assertEqual(result["target"], "text/uri-list")
        self.assertEqual(len(runner.pasted), 1)
        target, data = runner.pasted[0]
        self.assertEqual(target, "text/uri-list")
        expected = "file://" + quote(str(self.uploads / "report notes.txt")) + "\r\n"
        self.assertEqual(data.decode("ascii"), expected)
        self.assertIn("%20", data.decode("ascii"))
        self.assertNotIn(" ", data.decode("ascii"))
        self.assertEqual(runner.keys, ["ctrl+v"])
        self.assertEqual(runner.clipboard, b"original clipboard")
        flattened = [part.casefold() for command in runner.commands for part in command]
        self.assertNotIn("return", flattened)
        self.assertNotIn("kp_enter", flattened)
        self.assertNotIn("enter", flattened)

    def test_png_is_inlined_as_image_png(self) -> None:
        size = self.write("shot.png", PNG_BYTES)
        runner = FakeRunner()
        result = self.preparer(runner).attach("shot.png", size, "image")
        self.assertEqual(result["attached"], "image")
        self.assertEqual(runner.pasted, [("image/png", PNG_BYTES)])

    def test_jpeg_is_inlined_as_image_jpeg(self) -> None:
        size = self.write("photo.jpg", JPEG_BYTES)
        runner = FakeRunner()
        result = self.preparer(runner).attach("photo.jpg", size, "image")
        self.assertEqual(result["target"], "image/jpeg")
        self.assertEqual(runner.pasted, [("image/jpeg", JPEG_BYTES)])

    def test_unrecognised_magic_falls_back_to_uri_list(self) -> None:
        size = self.write("animation.gif", GIF_BYTES)
        runner = FakeRunner()
        result = self.preparer(runner).attach("animation.gif", size, "image")
        self.assertEqual(result["target"], "text/uri-list")
        self.assertEqual(result["attached"], "file")
        self.assertEqual(runner.pasted[0][0], "text/uri-list")

    def test_oversized_image_is_attached_as_a_file(self) -> None:
        size = self.write("huge.png", PNG_BYTES)
        runner = FakeRunner()
        with patch("wechat_history.attach.MAX_INLINE_IMAGE_BYTES", 1):
            result = self.preparer(runner).attach("huge.png", size, "image")
        self.assertEqual(result["target"], "text/uri-list")


class AttachRefusalTests(AttachTestCase):
    def test_missing_file_times_out_without_touching_the_desktop(self) -> None:
        runner = FakeRunner()
        with self.assertRaises(HistoryError) as raised:
            self.preparer(runner).attach("never-arrived.txt", 10, "file")
        self.assertEqual(raised.exception.code, "UPLOAD_INCOMPLETE")
        self.assertFalse(runner.used("xdotool"))
        self.assertFalse(runner.used("xclip"))

    def test_partial_upload_times_out(self) -> None:
        self.write("partial.bin", b"only the first bytes")
        runner = FakeRunner()
        with self.assertRaises(HistoryError) as raised:
            self.preparer(runner).attach("partial.bin", 999999, "file")
        self.assertEqual(raised.exception.code, "UPLOAD_INCOMPLETE")
        self.assertFalse(runner.used("xdotool"))

    def test_upload_that_finishes_late_still_attaches(self) -> None:
        target = self.uploads / "slow.bin"
        target.write_bytes(b"12345")
        runner = FakeRunner()
        preparer = self.preparer(runner)
        original_sleep = preparer.sleep

        def growing_sleep(seconds: float) -> None:
            original_sleep(seconds)
            if self.clock.now >= 1.0 and target.stat().st_size != 10:
                target.write_bytes(b"1234567890")

        preparer.sleep = growing_sleep
        result = preparer.attach("slow.bin", 10, "file")
        self.assertEqual(result["ok"], True)
        self.assertEqual(len(runner.pasted), 1)

    def test_traversal_and_separators_are_rejected(self) -> None:
        for name in ("../evil", "a/b", "a\\b", "", ".", "..", "x\x00y"):
            runner = FakeRunner()
            with self.assertRaises(HistoryError) as raised:
                self.preparer(runner).attach(name, 1, "file")
            self.assertEqual(raised.exception.code, "INVALID_FILENAME", name)
            self.assertFalse(runner.commands, name)

    def test_symlink_out_of_the_upload_root_is_rejected(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_bytes(b"secret")
        try:
            (self.uploads / "link.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable in this environment")
        runner = FakeRunner()
        with self.assertRaises(HistoryError) as raised:
            self.preparer(runner).attach("link.txt", 6, "file")
        self.assertEqual(raised.exception.code, "INVALID_FILENAME")

    def test_bad_kind_and_size_are_rejected(self) -> None:
        runner = FakeRunner()
        with self.assertRaises(HistoryError) as raised:
            self.preparer(runner).attach("x.txt", 1, "video")
        self.assertEqual(raised.exception.code, "INVALID_KIND")
        with self.assertRaises(HistoryError) as raised:
            self.preparer(runner).attach("x.txt", True, "file")
        self.assertEqual(raised.exception.code, "INVALID_SIZE")
        with self.assertRaises(HistoryError) as raised:
            self.preparer(runner).attach("x.txt", -1, "file")
        self.assertEqual(raised.exception.code, "INVALID_SIZE")

    def test_held_lock_reports_reply_busy(self) -> None:
        size = self.write("busy.txt", b"x")
        self.cache.mkdir(parents=True, exist_ok=True)
        lock_path = self.cache / "reply.lock"
        runner = FakeRunner()
        with lock_path.open("a+b") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(HistoryError) as raised:
                self.preparer(runner).attach("busy.txt", size, "file")
        self.assertEqual(raised.exception.code, "REPLY_BUSY")
        self.assertEqual(runner.pasted, [])

    def test_focus_mismatch_restores_the_clipboard_and_never_pastes(self) -> None:
        size = self.write("focus.txt", b"x")
        runner = FakeRunner()
        runner.focus = b"999\n"
        with self.assertRaises(HistoryError) as raised:
            self.preparer(runner).attach("focus.txt", size, "file")
        self.assertEqual(raised.exception.code, "FOCUS_NOT_WECHAT")
        self.assertEqual(runner.pasted, [])
        self.assertEqual(runner.clipboard, b"original clipboard")
        self.assertEqual(runner.clipboard_target, "UTF8_STRING")


if __name__ == "__main__":
    unittest.main()
