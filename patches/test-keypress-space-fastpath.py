#!/usr/bin/env python3
"""Regression tests for the space-key pynput fast-path appended to
patches/input-and-backpressure-fixes.py (fix 5).

input_handler.py cannot be imported standalone: its module-level `import
pynput` opens a real X connection and raises ImportError without one
(verified against the actual base image). selkies.py has the same problem
for unrelated reasons (pixelflux/pcmflux/websockets setup). So, like this
file's own existing fixes, this drives the real patch script against
synthetic fixtures for both files it touches, then execs the *patched*
input_handler.py fixture to assert the new branch's runtime behaviour
directly -- no real X11/pynput required for that half either, since the
fixture's own send_x11_keypress_printable() only exercises the plain-Python
branching this patch changes, not the pynput call itself.

The fixtures reproduce every anchor the script's seven patch() calls look for
(fixes 1, 2a, 2b, 3, 4 already in the file, plus this candidate's fixes 5
and 6),
because the script applies all of its patches, across both target files, in
one run, and fails fast on the first missing anchor.
"""

from __future__ import annotations

import ast
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path


PATCH = Path(__file__).with_name("input-and-backpressure-fixes.py")

INPUT_HANDLER_FIXTURE = '''import asyncio
import subprocess


class WebRTCInput:
    async def send_x11_keypress(self, keysym, down=True):
        action = "keydown" if down else "keyup"
        command = ["xdotool", action, "X"]

        if command:
            try:
                process = await subprocess.create_subprocess_exec(
                    *command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                await asyncio.wait_for(process.communicate(), timeout=0.5)
                if process.returncode == 0:
                    return
            except Exception:
                pass

        if use_pynput_for_printable or not command:
            try:
                if not self.keyboard:
                    await self._xdotool_fallback(keysym, down)
                    return

                pynput_key = pynput.keyboard.KeyCode.from_vk(keysym)
                if down:
                    self.keyboard.press(pynput_key)
                else:
                    self.keyboard.release(pynput_key)
            except Exception:
                await self._xdotool_fallback(keysym, down)

    async def send_x11_keypress_printable(self, keysym, down=True):
        is_printable = (0x20 <= keysym <= 0xFF) or ((keysym & 0xFF000000) == 0x01000000)
        action = "keydown" if down else "keyup"
        command = None
        use_pynput_for_printable = False
        if is_printable:
            unicode_codepoint = keysym & 0x00FFFFFF if (keysym & 0xFF000000) == 0x01000000 else keysym
            try:
                char = chr(unicode_codepoint)
                if char.isalpha():
                    use_pynput_for_printable = True
                else:
                    xdotool_arg = f"U{unicode_codepoint:04X}"
                    if not self.active_shortcut_modifiers:
                        command = ["xdotool", action, "--clearmodifiers", xdotool_arg]
                    else:
                        command = ["xdotool", action, xdotool_arg]
            except ValueError:
                use_pynput_for_printable = True
        return command, use_pynput_for_printable

    async def _inject_unicode_via_clipboard(self, text_to_type):
        async with self.clipboard_injection_lock:
            self.clipboard_paused = True
            KEY_SHIFT_L = 0xFFE1
            KEY_INSERT  = 0xFF63

            currently_active_mods = list(self.active_modifiers)

            try:
                for mod_keysym in currently_active_mods:
                    await self.send_x11_keypress(mod_keysym, down=False)

                old_data, old_mime = await self.read_clipboard(use_binary=True)

                mime_to_use = "UTF8_STRING" if not self.is_wayland else "text/plain"
                await self.write_clipboard(text_to_type, mime_type=mime_to_use)
                await asyncio.sleep(0.02)

                await self.send_x11_keypress(KEY_SHIFT_L, down=True)
                await self.send_x11_keypress(KEY_INSERT, down=True)
                await self.send_x11_keypress(KEY_INSERT, down=False)
                await self.send_x11_keypress(KEY_SHIFT_L, down=False)
                await asyncio.sleep(0.05)

                if old_data is not None:
                    await self.write_clipboard(old_data, mime_type=old_mime or "text/plain")
                elif self.is_wayland:
                    try:
                        proc = await subprocess.create_subprocess_exec(
                            "wl-copy", "--clear",
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=self._get_wl_env()
                        )
                        await asyncio.wait_for(proc.communicate(), timeout=1.0)
                    except Exception:
                        pass

            except Exception as e:
                logger_webrtc_input.error(f"Error during clipboard injection: {e}", exc_info=True)
            finally:
                for mod_keysym in currently_active_mods:
                    if mod_keysym in self.active_modifiers:
                        await self.send_x11_keypress(mod_keysym, down=True)

                self.clipboard_paused = False

    async def co_end(self, text_to_type):
        if True:
            if True:
                if True:
                    cmd = ["xdotool", "type", text_to_type]
                    process = await subprocess.create_subprocess_exec(
                        *cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    await asyncio.wait_for(process.communicate(), timeout=0.5)
'''

SELKIES_FIXTURE = '''import time

SENT_FRAME_TIMESTAMP_HISTORY_SIZE = 1000
MAX_UINT16_FRAME_ID = 65535


class SelkiesServer:
    async def _broadcast_primary(self, primary_viewers, frame_id, data_chunk):
                    now = time.monotonic()
                    for client_ws in primary_viewers:
                        for primary_client_info in self.display_clients.values():
                            if primary_client_info.get('ws') is client_ws:
                                if primary_client_info.get('backpressure_enabled', True):
                                    primary_client_info['sent_timestamps'][frame_id] = now
                                    primary_client_info['last_sent_frame_id'] = frame_id
                                    if len(primary_client_info['sent_timestamps']) > SENT_FRAME_TIMESTAMP_HISTORY_SIZE:
                                        primary_client_info['sent_timestamps'].popitem(last=False)
                                break
                    try:
                        websockets.broadcast(primary_viewers, data_chunk)
                        self._bytes_sent_in_interval += len(data_chunk) * len(primary_viewers)
                    except Exception as e:
                        data_logger.error(f"Error during primary broadcast: {e}")

    async def _desync(self, server_id, client_id):
                frame_desync = (server_id - client_id) if server_id >= client_id else ((MAX_UINT16_FRAME_ID - client_id) + server_id + 1)
'''


def make_site() -> tuple[Path, Path]:
    site = Path(tempfile.mkdtemp(prefix="selkies-space-fastpath-"))
    (site / "input_handler.py").write_text(INPUT_HANDLER_FIXTURE, encoding="utf-8")
    (site / "selkies.py").write_text(SELKIES_FIXTURE, encoding="utf-8")
    return site, site / "input_handler.py"


def run(site: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PATCH), str(site)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )


# 1. a clean site gets all seven patches applied, including the space fast-path
#    and the clipboard-restore retry.
site, input_handler_target = make_site()
result = run(site)
assert result.returncode == 0, result.stderr
assert "7 patch(es) applied" in result.stdout, result.stdout
assert "route space through the in-process pynput path" in result.stdout
assert "retry clipboard restore once before giving up" in result.stdout

patched = input_handler_target.read_text(encoding="utf-8")
ast.parse(patched)
assert 'char.isalpha() or char == " "' in patched
assert "if char.isalpha():\n" not in patched, "the old, space-excluding condition must be gone"

# 2. behavioural check: exec the patched input_handler.py fixture and confirm
#    space now behaves exactly like a letter (pynput branch, no xdotool
#    command built), while a digit is unaffected (still xdotool).
namespace: dict = {}
exec(compile(patched, "<patched-fixture>", "exec"), namespace)

instance = namespace["WebRTCInput"]()
instance.active_shortcut_modifiers = set()

command, use_pynput = asyncio.run(instance.send_x11_keypress_printable(0x20, down=True))
assert use_pynput is True, "space must take the pynput branch"
assert command is None, "space must not build an xdotool command anymore"

command, use_pynput = asyncio.run(instance.send_x11_keypress_printable(ord("a"), down=True))
assert use_pynput is True
assert command is None

command, use_pynput = asyncio.run(instance.send_x11_keypress_printable(ord("5"), down=True))
assert use_pynput is False, "digits are deliberately out of scope for this candidate"
assert command == ["xdotool", "keydown", "--clearmodifiers", "U0035"]

# 3. re-running the patch script must fail loudly (anchors consumed), matching
#    this file's existing non-idempotent patch() contract for its other five
#    fixes.
second = run(site)
assert second.returncode != 0, "a second run must fail, not double-patch"
assert input_handler_target.read_text(encoding="utf-8") == patched, (
    "a failed second run must not touch the file"
)

print("space-key pynput fast-path tests passed")
