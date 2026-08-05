#!/usr/bin/env python3
"""Build-time patches for three ways this Selkies build loses keystrokes,
plus the frame-id bookkeeping bug that freezes the picture instead.

Applied with exact-string replacement rather than sed: every target is
indentation-sensitive Python, where a regex that half-matches would produce a
file that still imports but behaves differently.

Every patch asserts its target appears exactly once and that the result parses.
If upstream reshapes any of them the build fails instead of silently shipping
the unpatched behaviour.

Context — the user's symptom is "typing loses characters, but only when the
network is slow". The chain, all confirmed in this source tree:

  * Video, audio, clipboard, uploads and input share ONE WebSocket.
  * The client's input sender discards a frame outright when the socket is not
    exactly OPEN (`readyState === WebSocket.OPEN ? send : console.warn`), with no
    queue and no retry, and "reconnect" is a full location.reload() polled every
    5 s — so every keystroke in that window is gone. That part is client-side and
    unchanged even on current upstream main; it is not patched here.
  * What IS patchable here is (1) why the connection dies on a slow link, and
    (2) two server-side paths that swallow a keystroke under load without a
    disconnect at all.

Upstream note: selkies fixed the stuck-key class of bug after the commit this
image pins (0d134b6e, 2026-05-17) — issue #145 was closed 2026-07-14 by PR #254,
which adds a client 'kh,' heartbeat for held keys plus a server stale-key sweep,
removes pynput entirely in favour of an in-process XTEST shim, and kills timed-out
subprocesses. That PR is API/ABI incompatible and has to move in lockstep with
pixelflux and pcmflux, so these local patches are the cheap subset.
"""
import ast
import io
import os
import re
import sys


def site_packages():
    for base in sorted(
        p for p in (
            os.path.join("/lsiopy/lib", d, "site-packages", "selkies")
            for d in os.listdir("/lsiopy/lib")
        ) if os.path.isdir(p)
    ):
        return base
    sys.exit("input-and-backpressure-fixes: could not locate the selkies package")


SITE = sys.argv[1] if len(sys.argv) > 1 else site_packages()


def patch(filename, label, old, new):
    path = os.path.join(SITE, filename)
    with io.open(path, encoding="utf-8") as fh:
        src = fh.read()

    hits = src.count(old)
    if hits != 1:
        sys.exit(
            "input-and-backpressure-fixes: %s — expected exactly 1 occurrence in %s, found %d. "
            "Upstream changed this code; re-derive the patch." % (label, path, hits)
        )

    src = src.replace(old, new, 1)
    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("input-and-backpressure-fixes: %s produced invalid Python: %s" % (label, e))

    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    print("input-and-backpressure-fixes: %s -> %s" % (label, path))


# --------------------------------------------------------------------------- 1
# send_x11_keypress silently drops a key when its xdotool child fails.
#
# Reaching the `if use_pynput_for_printable or not command:` line means the
# xdotool attempt was either skipped or has just failed. `or not command` is
# False precisely in the failure case — command is set — so the function returns
# having injected nothing, with no log at all.
#
# Every modifier lives in X11_KEYSYM_MAP, so this fires for modifier presses AND
# releases. on_message discards the keysym from active_modifiers *before*
# awaiting the injection, so a swallowed key-up leaves Ctrl physically held in
# Xvfb while the server believes it is released: subsequent letters become
# Ctrl+letter shortcuts that WeChat eats, and the user sees characters that never
# appear. No disconnect required — event-loop pressure is enough.

patch(
    "input_handler.py",
    "keypress fallback on xdotool failure",
    """        if command:
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
""",
    """        xdotool_injected = False
        if command:
            try:
                process = await subprocess.create_subprocess_exec(
                    *command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                try:
                    await asyncio.wait_for(process.communicate(), timeout=0.5)
                except asyncio.TimeoutError:
                    # wait_for only cancels communicate(); the child keeps running
                    # and an orphaned xdotool injects into whatever comes next.
                    process.kill()
                    await process.wait()
                    raise
                if process.returncode == 0:
                    xdotool_injected = True
                    return
                logger_webrtc_input.warning(
                    f"xdotool {action} returned {process.returncode} for keysym {keysym}; "
                    f"falling back to in-process injection"
                )
            except Exception as e:
                logger_webrtc_input.warning(
                    f"xdotool {action} failed for keysym {keysym} ({e!r}); "
                    f"falling back to in-process injection"
                )

        # `not xdotool_injected` is the fix: without it this branch is skipped in
        # exactly the case where the xdotool child was spawned and failed, which
        # silently discards the keystroke. Modifiers all take that path, and a
        # lost modifier key-up leaves the key held in X while active_modifiers
        # says otherwise.
        if use_pynput_for_printable or not command or not xdotool_injected:
""",
)


# --------------------------------------------------------------------------- 2a
# Make Selkies' existing Unicode clipboard injector transactional and let its
# caller know whether the paste keystroke was actually dispatched.
#
# Upstream restores the old selection only on its success path. A failed paste
# after the temporary text is installed therefore leaks the IME commit into the
# user's clipboard, and the method swallows the error so co,end cannot choose a
# fallback. Keep one implementation for both the existing single-codepoint
# fallback and phrase commits, but move restoration into finally and return a
# dispatch flag. A restore failure is deliberately logged without changing a
# successful result: falling back after Ctrl+V was sent would duplicate text.

patch(
    "input_handler.py",
    "transactional clipboard injection with dispatch result",
    """    async def _inject_unicode_via_clipboard(self, text_to_type):
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
""",
    """    async def _inject_unicode_via_clipboard(self, text_to_type):
        async with self.clipboard_injection_lock:
            self.clipboard_paused = True
            currently_active_mods = list(self.active_modifiers)
            old_data = None
            old_mime = None
            clipboard_replaced = False
            paste_dispatched = False

            try:
                for mod_keysym in currently_active_mods:
                    await self.send_x11_keypress(mod_keysym, down=False)

                old_data, old_mime = await self.read_clipboard(use_binary=True)
                mime_to_use = "UTF8_STRING" if not self.is_wayland else "text/plain"
                if not await self.write_clipboard(text_to_type, mime_type=mime_to_use):
                    raise RuntimeError("temporary clipboard write failed")
                clipboard_replaced = True

                # xclip/wl-copy forks an owner for the selection. Let it become
                # observable before asking the application to convert it.
                await asyncio.sleep(0.02)
                if self.is_wayland:
                    # Preserve upstream's Wayland-compatible Shift+Insert path.
                    key_shift_l = 0xFFE1
                    key_insert = 0xFF63
                    await self.send_x11_keypress(key_shift_l, down=True)
                    await self.send_x11_keypress(key_insert, down=True)
                    await self.send_x11_keypress(key_insert, down=False)
                    await self.send_x11_keypress(key_shift_l, down=False)
                    paste_dispatched = True
                else:
                    paste_proc = await subprocess.create_subprocess_exec(
                        "xdotool", "key", "--clearmodifiers", "ctrl+v",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    try:
                        _, paste_stderr = await asyncio.wait_for(
                            paste_proc.communicate(), timeout=2.0
                        )
                    except asyncio.TimeoutError:
                        try:
                            paste_proc.kill()
                        except ProcessLookupError:
                            pass
                        await paste_proc.wait()
                        raise
                    if paste_proc.returncode != 0:
                        raise RuntimeError(
                            f"paste keystroke failed with code {paste_proc.returncode}: "
                            f"{paste_stderr.decode(errors='replace').strip()}"
                        )
                    paste_dispatched = True

                # Selection conversion is pull-based. This matches the delay
                # proven by the local WeChat history integration and prevents
                # WeChat from fetching the restored (old) selection instead.
                await asyncio.sleep(0.2)
                return True
            except Exception as e:
                logger_webrtc_input.error(
                    f"Error during clipboard injection: {e}", exc_info=True
                )
                return paste_dispatched
            finally:
                try:
                    # Restoration must run even when the temporary write or
                    # paste command times out. If no prior payload existed,
                    # install an explicitly empty selection rather than leaking
                    # the IME text.
                    if clipboard_replaced:
                        try:
                            if old_data:
                                restored = await self.write_clipboard(
                                    old_data, mime_type=old_mime or "text/plain"
                                )
                                if not restored:
                                    raise RuntimeError("clipboard restore returned false")
                            elif self.is_wayland:
                                clear_proc = await subprocess.create_subprocess_exec(
                                    "wl-copy", "--clear",
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    env=self._get_wl_env(),
                                )
                                await asyncio.wait_for(
                                    clear_proc.communicate(), timeout=1.0
                                )
                                if clear_proc.returncode != 0:
                                    raise RuntimeError(
                                        f"wl-copy --clear returned {clear_proc.returncode}"
                                    )
                            else:
                                clear_proc = await subprocess.create_subprocess_exec(
                                    "xclip", "-selection", "clipboard", "-i", "-t", "UTF8_STRING",
                                    stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                )
                                await asyncio.wait_for(
                                    clear_proc.communicate(input=b""), timeout=1.0
                                )
                                if clear_proc.returncode != 0:
                                    raise RuntimeError(
                                        f"xclip clear returned {clear_proc.returncode}"
                                    )
                        except Exception as restore_exc:
                            logger_webrtc_input.error(
                                f"Clipboard restore after injection failed: {restore_exc}",
                                exc_info=True,
                            )

                    for mod_keysym in currently_active_mods:
                        if mod_keysym in self.active_modifiers:
                            try:
                                await self.send_x11_keypress(mod_keysym, down=True)
                            except Exception as modifier_exc:
                                logger_webrtc_input.error(
                                    f"Failed to restore modifier {mod_keysym}: {modifier_exc}",
                                    exc_info=True,
                                )
                finally:
                    self.clipboard_paused = False
""",
)


# --------------------------------------------------------------------------- 2b
# co,end drops CJK characters, truncates long text, and orphans its child.
#
# This path carries every IME phrase commit (see atomic-ime-commit.sh) and, per
# on_message, every modifier-free non-alphabetic printable — so all digits,
# punctuation, and any CJK the host IME commits.
#
# Two distinct failure modes, needing two different transports:
#
#   * ASCII is on the stock us layout, so `xdotool type` presses real keycodes —
#     race-free. Its only bug here was the fixed 0.5 s ceiling: at xdotool's
#     12 ms default inter-key delay anything past roughly 40 characters was
#     truncated, and the exception swallowed with a warning. Scale the timeout
#     instead. (The delay itself is left alone: upstream issue #257 reports that
#     under ~10 ms Selkies starts losing individual letters.)
#
#   * Everything off the layout — ALL CJK — makes xdotool rebind a spare keycode
#     per character on the fly (XChangeKeyboardMapping). The target app learns of
#     the rebinding asynchronously via MappingNotify, so under any load some
#     characters get resolved against the stale map and are silently dropped or
#     corrupted. Qt applications like WeChat are notoriously bad at this (same
#     bug family as xdotool #56/#49: typed output resolved against the wrong
#     map). No inter-key delay fixes a race; the transport is wrong. Paste the
#     commit through the clipboard instead — one atomic insert, no per-character
#     timing at all. This is the same mechanism upstream itself uses on Wayland
#     (_keyboard_worker -> _inject_unicode_via_clipboard) and the same
#     save/paste/restore dance the wechat-history integration has proven against
#     this exact WeChat build. clipboard_paused keeps the monitor from
#     broadcasting the transient clipboard contents to the browser, and the old
#     clipboard is restored only after WeChat has had time to pull the selection
#     (paste is pull-based; restoring immediately would paste the OLD content).

patch(
    "input_handler.py",
    "co,end: clipboard paste for CJK, scaled timeout for ASCII",
    """                    cmd = ["xdotool", "type", text_to_type]
                    process = await subprocess.create_subprocess_exec(
                        *cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    await asyncio.wait_for(process.communicate(), timeout=0.5)
""",
    """                    if text_to_type.isascii():
                        # On-layout characters: real keycodes, no remapping race.
                        cmd = ["xdotool", "type", text_to_type]
                        process = await subprocess.create_subprocess_exec(
                            *cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE
                        )
                        # 20 ms per character, comfortably above xdotool's 12 ms
                        # default inter-key delay, so a long commit is not cut
                        # off partway through.
                        co_end_timeout = 0.5 + 0.02 * len(text_to_type)
                        try:
                            _, type_stderr = await asyncio.wait_for(
                                process.communicate(), timeout=co_end_timeout
                            )
                        except asyncio.TimeoutError:
                            # Kill it: wait_for cancels communicate() but leaves
                            # the child typing, which interleaves with later
                            # keystrokes and scrambles them.
                            try:
                                process.kill()
                            except ProcessLookupError:
                                pass
                            await process.wait()
                            raise
                        if process.returncode != 0:
                            raise RuntimeError(
                                f"xdotool type failed with code {process.returncode}: "
                                f"{type_stderr.decode(errors='replace').strip()}"
                            )
                    else:
                        # Off-layout characters (all CJK): one clipboard insert.
                        # The helper returns True once the paste keystroke was
                        # dispatched, even if restoring the clipboard later
                        # fails, so the fallback can never duplicate text.
                        pasted = await self._inject_unicode_via_clipboard(text_to_type)
                        if not pasted:
                            # Last resort: the racy per-character path still beats
                            # dropping the whole commit.
                            process = await subprocess.create_subprocess_exec(
                                "xdotool", "type", "--clearmodifiers", text_to_type,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                            )
                            try:
                                _, type_stderr = await asyncio.wait_for(
                                    process.communicate(), timeout=0.5 + 0.02 * len(text_to_type)
                                )
                            except asyncio.TimeoutError:
                                try:
                                    process.kill()
                                except ProcessLookupError:
                                    pass
                                await process.wait()
                                raise
                            if process.returncode != 0:
                                raise RuntimeError(
                                    f"fallback xdotool type failed with code {process.returncode}: "
                                    f"{type_stderr.decode(errors='replace').strip()}"
                                )
""",
)


# --------------------------------------------------------------------------- 3
# Primary-display backpressure never sheds a frame.
#
# The backpressure_enabled check gates only the bookkeeping; websockets.broadcast
# sits outside it, so the ACK machinery has no effect on the primary display —
# the only one in use here. The secondary path a few lines below does `continue`
# correctly.
#
# The websockets library documents the consequence: broadcast "pushes the message
# synchronously to all connections even if their write buffers are overflowing.
# There's no backpressure... messages will pile up in its write buffer until the
# connection times out." That timeout (ping_timeout=20) is what kills the session
# on a slow link, and the client then discards every keystroke until its page
# reload completes.
#
# Safe by construction on a fast link: desync stays inside the budget so
# backpressure_enabled is never False and this is a no-op. When it does trigger,
# _run_frame_backpressure_logic lifts it again once desync falls back within
# budget, and force-lifts it if the frame-id gap exceeds
# FRAME_ID_SUSPICIOUS_GAP_THRESHOLD — so shedding cannot deadlock into a
# permanently frozen screen.

patch(
    "selkies.py",
    "primary display honours backpressure",
    """                    now = time.monotonic()
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
""",
    """                    now = time.monotonic()
                    ready_viewers = []
                    for client_ws in primary_viewers:
                        primary_client_info = None
                        for candidate in self.display_clients.values():
                            if candidate.get('ws') is client_ws:
                                primary_client_info = candidate
                                break
                        if primary_client_info is None:
                            # No backpressure state to consult for this viewer, so
                            # keep the previous behaviour and send.
                            ready_viewers.append(client_ws)
                            continue
                        if primary_client_info.get('backpressure_enabled', True):
                            primary_client_info['sent_timestamps'][frame_id] = now
                            primary_client_info['last_sent_frame_id'] = frame_id
                            if len(primary_client_info['sent_timestamps']) > SENT_FRAME_TIMESTAMP_HISTORY_SIZE:
                                primary_client_info['sent_timestamps'].popitem(last=False)
                            ready_viewers.append(client_ws)
                    if ready_viewers:
                        try:
                            websockets.broadcast(ready_viewers, data_chunk)
                            self._bytes_sent_in_interval += len(data_chunk) * len(ready_viewers)
                        except Exception as e:
                            data_logger.error(f"Error during primary broadcast: {e}")
""",
)


# --------------------------------------------------------------------------- 4
# A pipeline restart fakes a ~65000-frame desync and blacks the screen out.
#
# _reset_frame_ids_and_notify zeroes last_sent_frame_id whenever the capture
# pipeline restarts (a browser window resize is enough). A CLIENT_FRAME_ACK
# still in flight from the *old* pipeline then lands and refills
# acknowledged_frame_id with a high id, so this loop sees server_id < client_id
# with a small absolute gap — under FRAME_ID_SUSPICIOUS_GAP_THRESHOLD, so the
# rebase guard a few lines above does not fire — and falls into the uint16
# wraparound arm, which computes (65535 - client_id) + server_id + 1 ≈ 65000
# frames of desync. Backpressure engages permanently and the user sees a frozen
# or black screen until something forces it back off.
#
# The wraparound arm is unreachable in the case it was written for: a real
# uint16 wrap means client_id is near 65535 and server_id near 0, i.e. a gap far
# larger than the suspicious-gap threshold, which is caught and rebased above.
# So every value it ever produces here is wrong. Treat server_id < client_id as
# what it actually is — counters that no longer share an origin — and rebase the
# stall timer instead, exactly like the suspicious-gap guard does.

patch(
    "selkies.py",
    "frame-id reset does not trigger backpressure",
    """                frame_desync = (server_id - client_id) if server_id >= client_id else ((MAX_UINT16_FRAME_ID - client_id) + server_id + 1)
""",
    """                if server_id < client_id:
                    # Counters were reset out from under an in-flight ACK.
                    # Anything computed from these two ids is meaningless.
                    if not display_state.get('backpressure_enabled', True):
                        data_logger.info(f"Backpressure LIFTED for '{display_id}'. S:{server_id}, C:{client_id} (frame-id counters reset).")
                    display_state['backpressure_enabled'] = True
                    display_state['last_ack_update_time'] = time.monotonic()
                    continue

                frame_desync = server_id - client_id
""",
)


# The bundled .pyc files would otherwise be consulted first. Python invalidates
# them on the source mtime, which we just changed, but drop them so nothing can
# shadow the patched source.
cache = os.path.join(SITE, "__pycache__")
if os.path.isdir(cache):
    for name in os.listdir(cache):
        os.remove(os.path.join(cache, name))
    os.rmdir(cache)

print("input-and-backpressure-fixes: 5 patch(es) applied")
