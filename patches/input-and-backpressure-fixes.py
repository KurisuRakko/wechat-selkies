#!/usr/bin/env python3
"""Build-time patches for three ways this Selkies build loses keystrokes.

Applied with exact-string replacement rather than sed: all three targets are
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


# --------------------------------------------------------------------------- 2
# co,end truncates long text and orphans its child.
#
# This path carries every IME phrase commit (see atomic-ime-commit.sh) and, per
# on_message, every modifier-free non-alphabetic printable — so all digits and
# punctuation. xdotool's default inter-key delay is 12 ms, so the fixed 0.5 s
# ceiling truncates anything past roughly 40 characters, and the exception is
# swallowed with a warning.
#
# The delay is deliberately left at xdotool's default rather than set to 0:
# upstream issue #257 reports that an inter-key delay below ~10 ms makes Selkies
# lose individual letters, so racing the target application would trade one
# failure mode for another. Scale the timeout instead.

patch(
    "input_handler.py",
    "co,end timeout scaling and orphan kill",
    """                    cmd = ["xdotool", "type", text_to_type]
                    process = await subprocess.create_subprocess_exec(
                        *cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    await asyncio.wait_for(process.communicate(), timeout=0.5)
""",
    """                    cmd = ["xdotool", "type", text_to_type]
                    process = await subprocess.create_subprocess_exec(
                        *cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    # 20 ms per character, comfortably above xdotool's 12 ms
                    # default inter-key delay, so a long IME commit is not cut off
                    # partway through. The old fixed 0.5 s truncated anything past
                    # roughly 40 characters.
                    co_end_timeout = 0.5 + 0.02 * len(text_to_type)
                    try:
                        await asyncio.wait_for(process.communicate(), timeout=co_end_timeout)
                    except asyncio.TimeoutError:
                        # Kill it: wait_for cancels communicate() but leaves the
                        # child typing, which interleaves with later keystrokes and
                        # scrambles them.
                        process.kill()
                        await process.wait()
                        raise
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


# The bundled .pyc files would otherwise be consulted first. Python invalidates
# them on the source mtime, which we just changed, but drop them so nothing can
# shadow the patched source.
cache = os.path.join(SITE, "__pycache__")
if os.path.isdir(cache):
    for name in os.listdir(cache):
        os.remove(os.path.join(cache, name))
    os.rmdir(cache)

print("input-and-backpressure-fixes: 3 patch(es) applied")
