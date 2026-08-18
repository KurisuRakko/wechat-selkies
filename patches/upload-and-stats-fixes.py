#!/usr/bin/env python3
"""Server-side companions to patches/patch-upload-client.py.

Same house style as input-and-backpressure-fixes.py: exact-string replacement
with a count assertion and an ast.parse check, so a base-image bump fails the
build instead of silently shipping the unpatched behaviour.

Four unrelated single-socket problems, all in selkies/selkies.py:

  1. The 0x01 upload-chunk handler calls a plain blocking file.write() straight
     on the asyncio event loop. With the stock 1 MiB chunk that is a
     multi-millisecond stall of *everything* — input injection, frame
     broadcast, ping/pong — once per chunk. Hand it to the default executor
     and await it: the await keeps per-connection ordering (this runs inside
     `async for message in websocket`), so END/ERROR still cannot overtake a
     pending write and close the handle underneath it.

  2. websockets' server default is max_size=1 MiB. Anything larger is answered
     with close code 1009 and the whole session dies — which is exactly what
     happened to the old dragdrop image paste (a single base64 `cb,image/png,`
     message). Workstream A removes that sender, but an explicit 4 MiB ceiling
     keeps a large clipboard write from killing the video stream too, while
     still bounding what one message can allocate.

  3. Stats are collected every 2 s but only forwarded every 5 s, so the
     connection-status pill in the top bar could not tell "the link is fine but
     the push is slow" from "the link is gone". Forwarding every 1 s makes a
     missing update meaningful; collection itself is untouched, so this costs
     one extra no-op wakeup per second and no extra measurement.
"""
import ast
import io
import os
import sys


def site_packages():
    for base in sorted(
        p for p in (
            os.path.join("/lsiopy/lib", d, "site-packages", "selkies")
            for d in os.listdir("/lsiopy/lib")
        ) if os.path.isdir(p)
    ):
        return base
    sys.exit("upload-and-stats-fixes: could not locate the selkies package")


SITE = sys.argv[1] if len(sys.argv) > 1 else site_packages()


def patch(filename, label, old, new):
    path = os.path.join(SITE, filename)
    with io.open(path, encoding="utf-8") as fh:
        src = fh.read()

    hits = src.count(old)
    if hits != 1:
        sys.exit(
            "upload-and-stats-fixes: %s — expected exactly 1 occurrence in %s, found %d. "
            "Upstream changed this code; re-derive the patch." % (label, path, hits)
        )

    src = src.replace(old, new, 1)
    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit("upload-and-stats-fixes: %s produced invalid Python: %s" % (label, e))

    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    print("upload-and-stats-fixes: %s -> %s" % (label, path))


# --------------------------------------------------------------------------- 1
# Move the upload chunk write off the event loop.

patch(
    "selkies.py",
    "upload chunk write runs in a thread, not on the event loop",
    """                            try:
                                active_uploads_by_path_conn[
                                    active_upload_target_path_conn
                                ].write(payload)
""",
    """                            try:
                                # Blocking disk I/O on the event loop stalls
                                # input injection, frame broadcast and the
                                # ping/pong keepalive for the duration of every
                                # chunk. Awaiting the executor keeps this
                                # connection's chunks strictly ordered, so the
                                # END/ERROR branches below still cannot close
                                # the handle under an in-flight write.
                                await asyncio.get_running_loop().run_in_executor(
                                    None,
                                    active_uploads_by_path_conn[
                                        active_upload_target_path_conn
                                    ].write,
                                    payload,
                                )
""",
)


# --------------------------------------------------------------------------- 2
# An oversized frame must not take the session down with it.

patch(
    "selkies.py",
    "explicit websocket max_size",
    """                async with ws_async.serve(
                    self.ws_handler,
                    "0.0.0.0",
                    self.port,
                    compression=None,
                    ping_interval=20,
                    ping_timeout=20,
                ) as server_obj:
""",
    """                async with ws_async.serve(
                    self.ws_handler,
                    "0.0.0.0",
                    self.port,
                    compression=None,
                    # websockets defaults to 1 MiB and answers anything larger
                    # with close code 1009, killing video, audio, input and the
                    # in-flight upload along with the offending message.
                    max_size=4 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                ) as server_obj:
""",
)


# --------------------------------------------------------------------------- 3
# Push the already-collected stats often enough to be a liveness signal.

patch(
    "selkies.py",
    "forward stats every second",
    "async def _send_stats_periodically_ws(websocket, shared_data, interval_seconds=5):",
    "async def _send_stats_periodically_ws(websocket, shared_data, interval_seconds=1):",
)


# --------------------------------------------------------------------------- 4
# TCP_NODELAY on the data websocket connection.

patch(
    "selkies.py",
    "TCP_NODELAY on the data websocket connection",
    """    async def ws_handler(self, websocket):
        if self.is_secure_mode:
""",
    """    async def ws_handler(self, websocket):
        # This one socket carries video, audio, input, clipboard and upload
        # bytes. websockets' own sync implementation sets TCP_NODELAY on every
        # accepted connection (websockets/sync/server.py); its asyncio
        # implementation never does, so Nagle's algorithm can sit on small
        # outgoing writes (frame ACK replies, pings, stats pushes, tiny
        # paint-over stripes) until more data queues up or a peer ACK
        # arrives -- tens of milliseconds of avoidable latency on exactly the
        # small, frequent traffic that makes typing/click/scroll feel
        # responsive. Every write on this connection is already a complete,
        # pre-assembled application message, so disabling Nagle has no
        # downside here.
        try:
            sock = websocket.transport.get_extra_info("socket")
            if sock is not None:
                import socket as _socket
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        except OSError:
            pass
        if self.is_secure_mode:
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

print("upload-and-stats-fixes: 4 patch(es) applied")
