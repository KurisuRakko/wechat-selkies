#!/usr/bin/env python3
"""Regression tests for the TCP_NODELAY patch appended to
patches/upload-and-stats-fixes.py (fix 4).

selkies.py cannot be imported standalone for testing (it pulls in pixelflux/
pcmflux/websockets and performs real capture-pipeline setup at class
construction in places), so -- like this file's own existing three fixes --
this drives the real patch script against a synthetic fixture, then loads the
*patched* fixture's source with exec() to assert the new code's runtime
behaviour with fake objects standing in for the asyncio transport.

The fixture reproduces all four anchors the script's patch() calls look for
(fixes 1-3 already in the file, plus this candidate's fix 4), because the
script applies all of its patches, in order, in one run, and fails fast on
the first missing anchor -- a partial fixture would never reach fix 4.
"""

from __future__ import annotations

import ast
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


PATCH = Path(__file__).with_name("upload-and-stats-fixes.py")

FIXTURE = '''import asyncio

async def _send_stats_periodically_ws(websocket, shared_data, interval_seconds=5):
    pass


class SelkiesServer:
    async def run_server(self):
        while True:
            try:
                async with ws_async.serve(
                    self.ws_handler,
                    "0.0.0.0",
                    self.port,
                    compression=None,
                    ping_interval=20,
                    ping_timeout=20,
                ) as server_obj:
                    pass
            except Exception:
                pass

    async def ws_handler(self, websocket):
        if self.is_secure_mode:
            await config_gate.wait()
        return "handled"

    async def handle_message(self, websocket):
        async for message in websocket:
            if True:
                if True:
                    if True:
                        if True:
                            try:
                                active_uploads_by_path_conn[
                                    active_upload_target_path_conn
                                ].write(payload)
                            except Exception:
                                pass
'''


def make_site(source: str) -> tuple[Path, Path]:
    site = Path(tempfile.mkdtemp(prefix="selkies-tcp-nodelay-"))
    target = site / "selkies.py"
    target.write_text(source, encoding="utf-8")
    return site, target


def run(site: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PATCH), str(site)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )


# 1. a clean file gets all four patches applied, including TCP_NODELAY.
site, target = make_site(FIXTURE)
result = run(site)
assert result.returncode == 0, result.stderr
assert "4 patch(es) applied" in result.stdout, result.stdout
assert "TCP_NODELAY" in result.stdout

patched = target.read_text(encoding="utf-8")
ast.parse(patched)
assert "TCP_NODELAY" in patched
assert 'websocket.transport.get_extra_info("socket")' in patched
assert patched.index("setsockopt") < patched.index("if self.is_secure_mode:"), (
    "TCP_NODELAY must be set before any other handler logic runs"
)

# 2. behavioural check: exec the patched source and call ws_handler with a
#    fake transport, confirming setsockopt(TCP_NODELAY) actually fires with
#    the right arguments and the original handler body still runs afterward.
namespace: dict = {}
exec(compile(patched, "<patched-fixture>", "exec"), namespace)

calls = []


class FakeSocket:
    def setsockopt(self, level, opt, value):
        calls.append((level, opt, value))


class FakeTransport:
    def get_extra_info(self, key):
        return FakeSocket() if key == "socket" else None


class FakeWebsocket:
    transport = FakeTransport()


instance = namespace["SelkiesServer"]()
instance.is_secure_mode = False
outcome = asyncio.run(instance.ws_handler(FakeWebsocket()))
assert calls == [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)], calls
assert outcome == "handled", "the original handler body must still execute"

# 3. a transport with no socket (get_extra_info returns None) must not raise.
class NoSocketTransport:
    def get_extra_info(self, key):
        return None


class NoSocketWebsocket:
    transport = NoSocketTransport()


instance2 = namespace["SelkiesServer"]()
instance2.is_secure_mode = False
outcome2 = asyncio.run(instance2.ws_handler(NoSocketWebsocket()))
assert outcome2 == "handled", "a missing socket must degrade silently, not raise"

# 4. re-running the patch script must fail loudly (anchor consumed), matching
#    this file's existing non-idempotent patch() contract for its other three
#    fixes -- it must never silently re-wrap the handler a second time.
second = run(site)
assert second.returncode != 0, "a second run must fail, not double-patch"
assert target.read_text(encoding="utf-8") == patched, "a failed second run must not touch the file"

print("tcp-nodelay patch tests passed")
