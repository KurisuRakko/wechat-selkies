#!/usr/bin/env python3
"""Stop the browser uploader from starving input, ACKs and pings on one socket.

In --mode=websockets everything shares a single WebSocket: video frames out,
and keystrokes, mouse, clipboard, frame ACKs and pong replies in. The stock
uploader queues file bytes until `bufferedAmount` exceeds 10 MiB, in 1 MiB
chunks. On a link that cannot drain that fast, every input event and every
frame ACK the client produces is queued *behind* up to 11 MiB of file data:

  * the server sees no ACKs, decides the client has stalled and stops sending
    frames — the picture freezes for the whole upload;
  * pong replies are delayed past ping_timeout=20 and the server closes the
    connection with 1011 keepalive ping timeout, which the client can only
    recover from with a full page reload.

Dropping the high-water mark to 256 KiB and the chunk size to 256 KiB caps the
worst-case queue ahead of an interactive message at ~0.5 MiB (~0.4 s on
10 Mbps). Throughput is unchanged: the uploader only sleeps (50 ms, untouched)
while the buffer is above the mark, so a fast link still streams continuously.

Both copies of the client are patched, the same way patch-audio-autoplay.py
does it: src/selkies-core.js is what actually runs, assets/index-*.js is the
built bundle, and leaving either behind would make a future base-image bump
silently ship half a fix.

Caution: `10*1024*1024` appears twice per file. The second occurrence is the
WebRTC aux data channel's bufferedAmountLowThreshold and must not be touched,
so the high-water mark is matched through its full declaration instead.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


CORE_REL = "src/selkies-core.js"
# /usr/share/selkies holds more than one dashboard: the default
# `selkies-dashboard` and the stock alternate `selkies-dashboard-wish`, both
# carrying src/selkies-core.js and their own bundle. (`web/` is a third copy,
# but init-nginx deletes and recreates it from $DASHBOARD on every container
# start, so it does not exist at build time and must never be patched.)
#
# Only the dashboard this image customises is patched, identified the same way
# patch-audio-autoplay.py identifies it: by one of the scripts the Dockerfile
# has already copied into it. The alternate dashboard receives none of the
# WeChat integration — no dragdrop bridge, no IME anchor, no audio gate — so
# fixing its uploader alone would be pointless, and it is unreachable without
# setting DASHBOARD by hand.
MARKER_REL = "src/wechat-dragdrop.js"

# const T=10*1024*1024,D=50,$=new FileReader   (core)
# const be=10*1024*1024,Re=50,tt=new FileReader (vite bundle)
HIGH_WATER = re.compile(
    r"const (?P<hw>[A-Za-z_$][\w$]{0,8})=10\*1024\*1024,"
    r"(?P<iv>[A-Za-z_$][\w$]{0,8})=50,"
    r"(?P<rd>[A-Za-z_$][\w$]{0,8})=new FileReader"
)
PATCHED_HIGH_WATER = re.compile(
    r"const (?P<hw>[A-Za-z_$][\w$]{0,8})=256\*1024,"
    r"(?P<iv>[A-Za-z_$][\w$]{0,8})=50,"
    r"(?P<rd>[A-Za-z_$][\w$]{0,8})=new FileReader"
)

# const Ot=500,Nt=50,Bt=1024*1024-1,Pt=200   (core)
# const A=500,Ce=50,Ne=1024*1024-1,et=200    (vite bundle)
CHUNK = re.compile(
    r"=50,(?P<ck>[A-Za-z_$][\w$]{0,8})=1024\*1024-1,(?P<mx>[A-Za-z_$][\w$]{0,8})=200"
)
PATCHED_CHUNK = re.compile(
    r"=50,(?P<ck>[A-Za-z_$][\w$]{0,8})=256\*1024-1,(?P<mx>[A-Za-z_$][\w$]{0,8})=200"
)


def fail(message: str) -> None:
    raise RuntimeError(f"patch-upload-client: {message}")


def patch_javascript(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    original = source

    matches = list(HIGH_WATER.finditer(source))
    patched = list(PATCHED_HIGH_WATER.finditer(source))
    if len(matches) == 1 and not patched:
        if source.count("10*1024*1024") != 2:
            fail(
                f"expected two 10*1024*1024 literals in {path} "
                f"(uploader high-water mark plus the WebRTC aux channel "
                f"threshold), found {source.count('10*1024*1024')}"
            )
        source, count = HIGH_WATER.subn(
            lambda match: (
                f'const {match.group("hw")}=256*1024,'
                f'{match.group("iv")}=50,'
                f'{match.group("rd")}=new FileReader'
            ),
            source,
        )
        if count != 1:
            fail(f"high-water replacement count was {count} in {path}")
        if source.count("10*1024*1024") != 1:
            fail(
                f"high-water replacement disturbed the WebRTC aux channel "
                f"threshold in {path}"
            )
    elif matches or len(patched) != 1:
        fail(
            f"expected one original or one patched uploader high-water mark in "
            f"{path}; found original={len(matches)}, patched={len(patched)}"
        )

    chunks = list(CHUNK.finditer(source))
    patched_chunks = list(PATCHED_CHUNK.finditer(source))
    if len(chunks) == 1 and not patched_chunks:
        if source.count("1024*1024-1") != 1:
            fail(
                f"expected exactly one 1024*1024-1 chunk literal in {path}, "
                f"found {source.count('1024*1024-1')}"
            )
        source, count = CHUNK.subn(
            lambda match: (
                f'=50,{match.group("ck")}=256*1024-1,{match.group("mx")}=200'
            ),
            source,
        )
        if count != 1:
            fail(f"chunk replacement count was {count} in {path}")
    elif chunks or len(patched_chunks) != 1:
        fail(
            f"expected one original or one patched upload chunk size in {path}; "
            f"found original={len(chunks)}, patched={len(patched_chunks)}"
        )

    if len(PATCHED_HIGH_WATER.findall(source)) != 1:
        fail(f"high-water validation failed in {path}")
    if len(PATCHED_CHUNK.findall(source)) != 1:
        fail(f"chunk size validation failed in {path}")

    if source != original:
        path.write_text(source, encoding="utf-8")
        print(f"patch-upload-client: patched {path}")
    else:
        print(f"patch-upload-client: already patched {path}")


def patch_dashboard(dashboard: Path) -> None:
    core = dashboard / CORE_REL
    assets = sorted((dashboard / "assets").glob("index-*.js"))

    if not core.is_file():
        fail(f"missing Selkies source bundle {core}")
    if len(assets) != 1:
        fail(
            f"expected exactly one built dashboard bundle in {dashboard}, "
            f"found {len(assets)}"
        )

    patch_javascript(core)
    patch_javascript(assets[0])


def patch_root(root: Path) -> None:
    dashboards = []
    for html in root.glob("*/index.html"):
        dashboard = html.parent
        if (dashboard / CORE_REL).is_file() and (dashboard / MARKER_REL).is_file():
            dashboards.append(dashboard)

    if len(dashboards) != 1:
        fail(
            f"expected exactly one dashboard carrying both {CORE_REL} and "
            f"{MARKER_REL}, found {len(dashboards)}"
        )
    patch_dashboard(dashboards[0])


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/usr/share/selkies")
    try:
        patch_root(root)
    except (OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
