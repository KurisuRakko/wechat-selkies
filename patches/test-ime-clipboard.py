#!/usr/bin/env python3
"""Regression tests for the Selkies IME clipboard patch.

Run this inside a built image so it exercises the generated input_handler.py,
not a duplicate of the implementation in the build-time patch.
"""

import asyncio
import unittest
from collections import deque
from unittest.mock import AsyncMock, call, patch

import selkies.input_handler as input_handler


class FakeProcess:
    def __init__(self, returncode=0, stderr=b"", communicate_error=None):
        self.returncode = returncode
        self.stderr = stderr
        self.communicate_error = communicate_error
        self.communicate_inputs = []
        self.killed = False
        self.waited = False

    async def communicate(self, input=None):
        self.communicate_inputs.append(input)
        if self.communicate_error is not None:
            raise self.communicate_error
        return b"", self.stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        return self.returncode


class ProcessFactory:
    def __init__(self, *processes):
        self.processes = deque(processes)
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self.processes:
            raise AssertionError(f"unexpected subprocess: {args!r}")
        return self.processes.popleft()


def make_input(*, old_data="old clipboard", old_mime="text/plain", writes=None):
    instance = object.__new__(input_handler.WebRTCInput)
    instance.clipboard_injection_lock = asyncio.Lock()
    instance.clipboard_paused = False
    instance.active_modifiers = {0xFFE3}
    instance.is_wayland = False
    instance.send_x11_keypress = AsyncMock()
    instance.read_clipboard = AsyncMock(return_value=(old_data, old_mime))
    instance.write_clipboard = AsyncMock(side_effect=writes)
    return instance


class ClipboardInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_restores_text_and_modifiers(self):
        instance = make_input(writes=[True, True])
        paste_proc = FakeProcess()
        factory = ProcessFactory(paste_proc)

        with patch.object(input_handler.subprocess, "create_subprocess_exec", factory):
            dispatched = await instance._inject_unicode_via_clipboard("中文")

        self.assertTrue(dispatched)
        self.assertFalse(instance.clipboard_paused)
        self.assertEqual(
            instance.write_clipboard.await_args_list,
            [
                call("中文", mime_type="UTF8_STRING"),
                call("old clipboard", mime_type="text/plain"),
            ],
        )
        self.assertEqual(
            instance.send_x11_keypress.await_args_list,
            [call(0xFFE3, down=False), call(0xFFE3, down=True)],
        )
        self.assertEqual(factory.calls[0][0], ("xdotool", "key", "--clearmodifiers", "ctrl+v"))

    async def test_success_restores_binary_clipboard(self):
        instance = make_input(
            old_data=b"\x89PNG test", old_mime="image/png", writes=[True, True]
        )
        factory = ProcessFactory(FakeProcess())

        with patch.object(input_handler.subprocess, "create_subprocess_exec", factory):
            dispatched = await instance._inject_unicode_via_clipboard("混合abc")

        self.assertTrue(dispatched)
        self.assertEqual(
            instance.write_clipboard.await_args_list[-1],
            call(b"\x89PNG test", mime_type="image/png"),
        )

    async def test_empty_clipboard_is_explicitly_cleared(self):
        instance = make_input(old_data=None, old_mime=None, writes=[True])
        paste_proc = FakeProcess()
        clear_proc = FakeProcess()
        factory = ProcessFactory(paste_proc, clear_proc)

        with patch.object(input_handler.subprocess, "create_subprocess_exec", factory):
            dispatched = await instance._inject_unicode_via_clipboard("空")

        self.assertTrue(dispatched)
        self.assertEqual(factory.calls[1][0][0], "xclip")
        self.assertEqual(clear_proc.communicate_inputs, [b""])
        self.assertFalse(instance.clipboard_paused)

    async def test_paste_failure_restores_then_reports_not_dispatched(self):
        instance = make_input(writes=[True, True])
        factory = ProcessFactory(FakeProcess(returncode=1, stderr=b"paste failed"))

        with patch.object(input_handler.subprocess, "create_subprocess_exec", factory):
            dispatched = await instance._inject_unicode_via_clipboard("失败")

        self.assertFalse(dispatched)
        self.assertEqual(instance.write_clipboard.await_count, 2)
        self.assertFalse(instance.clipboard_paused)

    async def test_paste_timeout_kills_child_and_restores(self):
        instance = make_input(writes=[True, True])
        paste_proc = FakeProcess(communicate_error=asyncio.TimeoutError())
        factory = ProcessFactory(paste_proc)

        with patch.object(input_handler.subprocess, "create_subprocess_exec", factory):
            dispatched = await instance._inject_unicode_via_clipboard("超时")

        self.assertFalse(dispatched)
        self.assertTrue(paste_proc.killed)
        self.assertTrue(paste_proc.waited)
        self.assertEqual(instance.write_clipboard.await_count, 2)
        self.assertFalse(instance.clipboard_paused)

    async def test_restore_failure_does_not_reclassify_successful_paste(self):
        # Fix 6 retries the restore once, so "failure" here means both attempts
        # failed (writes: paste ok, restore attempt 1 fails, retry fails).
        instance = make_input(writes=[True, False, False])
        factory = ProcessFactory(FakeProcess())

        with patch.object(input_handler.subprocess, "create_subprocess_exec", factory):
            dispatched = await instance._inject_unicode_via_clipboard("只粘贴一次")

        self.assertTrue(dispatched)
        self.assertEqual(instance.write_clipboard.await_count, 3)
        self.assertFalse(instance.clipboard_paused)

    async def test_restore_retries_once_before_giving_up(self):
        instance = make_input(writes=[True, False, True])
        factory = ProcessFactory(FakeProcess())

        with patch.object(input_handler.subprocess, "create_subprocess_exec", factory):
            dispatched = await instance._inject_unicode_via_clipboard("只粘贴一次")

        self.assertTrue(dispatched)
        self.assertEqual(instance.write_clipboard.await_count, 3)
        self.assertEqual(
            instance.write_clipboard.await_args_list[1],
            instance.write_clipboard.await_args_list[2],
            "retry passes the exact same payload and mime type",
        )
        self.assertFalse(instance.clipboard_paused)

    async def test_restore_failing_twice_still_reports_dispatch_result(self):
        instance = make_input(writes=[True, False, False])
        factory = ProcessFactory(FakeProcess())

        with patch.object(input_handler.subprocess, "create_subprocess_exec", factory):
            dispatched = await instance._inject_unicode_via_clipboard("只粘贴一次")

        self.assertTrue(dispatched, "a successful paste must not be reclassified by restore failure")
        self.assertEqual(instance.write_clipboard.await_count, 3)
        self.assertFalse(instance.clipboard_paused)

    async def test_concurrent_commits_do_not_interleave_clipboards(self):
        instance = make_input(writes=[True, True, True, True])
        instance.read_clipboard = AsyncMock(
            side_effect=[("old one", "text/plain"), ("old two", "text/plain")]
        )
        factory = ProcessFactory(FakeProcess(), FakeProcess())

        with patch.object(input_handler.subprocess, "create_subprocess_exec", factory):
            results = await asyncio.gather(
                instance._inject_unicode_via_clipboard("第一句"),
                instance._inject_unicode_via_clipboard("第二句"),
            )

        self.assertEqual(results, [True, True])
        payloads = [entry.args[0] for entry in instance.write_clipboard.await_args_list]
        self.assertIn(
            payloads,
            (["第一句", "old one", "第二句", "old two"],
             ["第二句", "old one", "第一句", "old two"]),
        )
        self.assertFalse(instance.clipboard_paused)


class CompositionCommitTests(unittest.IsolatedAsyncioTestCase):
    def make_bare_input(self):
        instance = object.__new__(input_handler.WebRTCInput)
        instance.is_wayland = False
        instance._inject_unicode_via_clipboard = AsyncMock(return_value=True)
        return instance

    async def test_long_ascii_commit_uses_scaled_xdotool_timeout(self):
        instance = self.make_bare_input()
        process = FakeProcess()
        factory = ProcessFactory(process)
        real_wait_for = asyncio.wait_for
        observed_timeouts = []

        async def tracking_wait_for(awaitable, timeout):
            observed_timeouts.append(timeout)
            return await real_wait_for(awaitable, timeout)

        text = "a" * 100
        with (
            patch.object(input_handler.subprocess, "create_subprocess_exec", factory),
            patch.object(input_handler.asyncio, "wait_for", tracking_wait_for),
        ):
            await instance.on_message(f"co,end,{text}")

        self.assertEqual(factory.calls[0][0], ("xdotool", "type", text))
        self.assertEqual(observed_timeouts, [2.5])
        instance._inject_unicode_via_clipboard.assert_not_awaited()

    async def test_ascii_timeout_kills_child(self):
        instance = self.make_bare_input()
        process = FakeProcess(communicate_error=asyncio.TimeoutError())
        factory = ProcessFactory(process)

        with patch.object(input_handler.subprocess, "create_subprocess_exec", factory):
            await instance.on_message("co,end," + "a" * 100)

        self.assertTrue(process.killed)
        self.assertTrue(process.waited)

    async def test_non_ascii_success_never_runs_xdotool_type_fallback(self):
        instance = self.make_bare_input()
        factory = ProcessFactory()

        with patch.object(input_handler.subprocess, "create_subprocess_exec", factory):
            await instance.on_message("co,end,中文abc")

        instance._inject_unicode_via_clipboard.assert_awaited_once_with("中文abc")
        self.assertEqual(factory.calls, [])

    async def test_non_ascii_predispatch_failure_runs_one_fallback(self):
        instance = self.make_bare_input()
        instance._inject_unicode_via_clipboard.return_value = False
        factory = ProcessFactory(FakeProcess())

        with patch.object(input_handler.subprocess, "create_subprocess_exec", factory):
            await instance.on_message("co,end,中文")

        self.assertEqual(
            factory.calls[0][0],
            ("xdotool", "type", "--clearmodifiers", "中文"),
        )
        self.assertEqual(len(factory.calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
