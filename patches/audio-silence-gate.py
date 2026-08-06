#!/usr/bin/env python3
"""Turn on pcmflux's silence gate so an idle desktop stops sending audio.

Upstream sets use_silence_gate = False, which makes pcmflux invoke its callback
for every 20 ms window whether or not there is any sound. The result is a
constant Opus stream from a machine where nothing is playing — on a chat client
that is almost all of the time.

With the gate on, pcmflux skips the callback entirely for a silent chunk, so an
idle desktop sends nothing. Speech and notification sounds are unaffected.

Deliberately one patch changing one line. This used to live in a larger
stream-efficiency-fixes.py alongside CSS-scaling changes and a frame-assembler
rework; that bundle introduced a regression that black-screened the stream, and
all of it was rolled back. The gate itself was never implicated and is entirely
independent of the video path, so it comes back on its own.

Fail closed: if upstream moves or renames this line, stop the image build rather
than silently shipping without the gate.
"""

from __future__ import annotations

import ast
import io
import os
import sys


TARGET = "selkies.py"
LABEL = "drop silent PCMFlux chunks"

OLD = "            capture_settings.use_silence_gate = False\n"
NEW = (
    "            # pcmflux skips the callback entirely for a silent chunk, so an\n"
    "            # idle desktop no longer emits an Opus packet every 20 ms.\n"
    "            capture_settings.use_silence_gate = True\n"
)


def site_packages() -> str:
    candidates = []
    lib_root = "/lsiopy/lib"
    if os.path.isdir(lib_root):
        for dirname in sorted(os.listdir(lib_root)):
            candidate = os.path.join(lib_root, dirname, "site-packages", "selkies")
            if os.path.isdir(candidate):
                candidates.append(candidate)
    if not candidates:
        raise SystemExit("audio-silence-gate: could not locate the selkies package")
    return candidates[0]


def main() -> int:
    site = sys.argv[1] if len(sys.argv) > 1 else site_packages()
    path = os.path.join(site, TARGET)

    with io.open(path, encoding="utf-8") as handle:
        source = handle.read()

    old_hits = source.count(OLD)
    new_hits = source.count(NEW)

    if new_hits == 1 and old_hits == 0:
        print(f"audio-silence-gate: already applied {LABEL}")
        return 0

    if old_hits != 1 or new_hits:
        # ASCII only: the interpreter writes this through sys.stderr using the
        # console encoding, and non-ASCII comes out mojibake in a cp936 console.
        print(
            "audio-silence-gate: %s: expected exactly one original and no "
            "patched target in %s, found original=%d patched=%d. Upstream "
            "changed this code; re-derive the patch." % (LABEL, path, old_hits, new_hits),
            file=sys.stderr,
        )
        return 1

    patched = source.replace(OLD, NEW, 1)
    try:
        ast.parse(patched)
    except SyntaxError as error:
        print(
            f"audio-silence-gate: {LABEL} produced invalid Python: {error}",
            file=sys.stderr,
        )
        return 1

    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(patched)
    print(f"audio-silence-gate: patched {LABEL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
