from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_history.errors import HistoryError
from wechat_history.reply import CommandRunner, ReplyPreparer, find_wechat_window


class FakeReader:
    def require_reply_account_active(self) -> None:
        pass

    def reply_session(self, session_id: str) -> dict:
        return {
            "session_id": session_id,
            "display_name": "文件传输助手",
            "ui_query": "文件传输助手",
            "kind": "direct",
            "alias": "",
        }


class FakeRunner(CommandRunner):
    def __init__(self):
        self.clipboard = b"original clipboard"
        self.input_text = ""
        self.paste_count = 0
        self.commands: list[list[str]] = []

    def output(self, arguments: list[str], *, input_data: bytes | None = None) -> bytes:
        self.commands.append(arguments)
        if arguments[:4] == ["xdotool", "search", "--onlyvisible", "--class"]:
            return b"123\n"
        if arguments[:2] == ["xdotool", "getwindowgeometry"]:
            return b"WINDOW=123\nX=0\nY=0\nWIDTH=560\nHEIGHT=760\n"
        if arguments[:2] == ["xdotool", "getwindowfocus"]:
            return b"123\n"
        if "TARGETS" in arguments:
            return b"TARGETS\nUTF8_STRING\n"
        return self.clipboard

    def action(self, arguments: list[str], *, input_data: bytes | None = None) -> None:
        self.commands.append(arguments)
        if arguments and arguments[0] == "xclip" and input_data is not None:
            self.clipboard = input_data
            return
        if arguments[:2] == ["xdotool", "key"]:
            key = arguments[-1]
            if key == "ctrl+v":
                self.paste_count += 1
                if self.paste_count == 2:
                    self.input_text = self.clipboard.decode("utf-8")
            elif key == "ctrl+c" and self.input_text:
                self.clipboard = self.input_text.encode("utf-8")
            elif key == "ctrl+z":
                self.input_text = ""


class ReplyTests(unittest.TestCase):
    def test_fills_but_never_sends_and_restores_clipboard(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as temporary, patch(
            "wechat_history.reply.CACHE_ROOT", Path(temporary)
        ):
            preparer = ReplyPreparer(FakeReader(), runner=runner, sleep=lambda _: None)
            result = preparer.prepare("filehelper", "测试草稿\n第二行")
        self.assertEqual(result["status"], "draft_prepared")
        self.assertFalse(result["sent"])
        self.assertEqual(runner.input_text, "测试草稿\n第二行")
        self.assertEqual(runner.clipboard, b"original clipboard")
        flattened = [part.casefold() for command in runner.commands for part in command]
        self.assertNotIn("return", flattened)
        self.assertNotIn("kp_enter", flattened)
        self.assertNotIn("enter", flattened)

    def test_focus_mismatch_stops_before_reply_paste(self) -> None:
        runner = FakeRunner()
        original_output = runner.output

        def wrong_focus(arguments: list[str], *, input_data: bytes | None = None) -> bytes:
            if arguments[:2] == ["xdotool", "getwindowfocus"]:
                runner.commands.append(arguments)
                return b"999\n"
            return original_output(arguments, input_data=input_data)

        runner.output = wrong_focus  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temporary, patch(
            "wechat_history.reply.CACHE_ROOT", Path(temporary)
        ):
            preparer = ReplyPreparer(FakeReader(), runner=runner, sleep=lambda _: None)
            with self.assertRaises(HistoryError) as raised:
                preparer.prepare("filehelper", "不得粘贴")
        self.assertEqual(raised.exception.code, "FOCUS_NOT_WECHAT")
        self.assertEqual(runner.paste_count, 0)

    def test_visible_wechat_dialog_stops_before_reply_paste(self) -> None:
        runner = FakeRunner()
        original_output = runner.output

        def extra_dialog(arguments: list[str], *, input_data: bytes | None = None) -> bytes:
            if arguments[:4] == ["xdotool", "search", "--onlyvisible", "--class"]:
                runner.commands.append(arguments)
                return b"123\n456\n"
            if arguments[:2] == ["xdotool", "getwindowgeometry"] and arguments[-1] == "456":
                runner.commands.append(arguments)
                return b"WINDOW=456\nX=0\nY=0\nWIDTH=300\nHEIGHT=200\n"
            return original_output(arguments, input_data=input_data)

        runner.output = extra_dialog  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temporary, patch(
            "wechat_history.reply.CACHE_ROOT", Path(temporary)
        ):
            preparer = ReplyPreparer(FakeReader(), runner=runner, sleep=lambda _: None)
            with self.assertRaises(HistoryError) as raised:
                preparer.prepare("filehelper", "不得粘贴")
        self.assertEqual(raised.exception.code, "WECHAT_DIALOG_VISIBLE")
        self.assertEqual(runner.paste_count, 0)

    def test_systray_icon_is_not_mistaken_for_a_dialog(self) -> None:
        # The live container always has a 24x24 override-redirect window with
        # WM_CLASS "wechat" alongside the main one, so counting every visible
        # window made the dialog check fire on every single request.
        runner = FakeRunner()
        original_output = runner.output

        def with_tray_icon(arguments: list[str], *, input_data: bytes | None = None) -> bytes:
            if arguments[:4] == ["xdotool", "search", "--onlyvisible", "--class"]:
                runner.commands.append(arguments)
                return b"123\n456\n"
            if arguments[:2] == ["xdotool", "getwindowgeometry"] and arguments[-1] == "456":
                runner.commands.append(arguments)
                return b"WINDOW=456\nX=4\nY=4\nWIDTH=24\nHEIGHT=24\n"
            return original_output(arguments, input_data=input_data)

        runner.output = with_tray_icon  # type: ignore[method-assign]
        window = find_wechat_window(runner)
        self.assertEqual(window.window_id, "123")


if __name__ == "__main__":
    unittest.main()
