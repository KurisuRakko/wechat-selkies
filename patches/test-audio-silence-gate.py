#!/usr/bin/env python3
"""Regression tests for patches/audio-silence-gate.py."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path


PATCH = Path(__file__).with_name("audio-silence-gate.py")

FIXTURE = '''
def start_audio(capture_settings, data_logger):
        if True:
            capture_settings.use_silence_gate = False
            data_logger.info("pcmflux starting")
        return capture_settings
'''


def make_site(source: str) -> tuple[Path, Path]:
    site = Path(tempfile.mkdtemp(prefix="selkies-silence-gate-"))
    target = site / "selkies.py"
    target.write_text(source, encoding="utf-8")
    return site, target


def run(site: Path) -> subprocess.CompletedProcess[str]:
    # Pin the encoding on both sides: the child writes errors through sys.stderr
    # using the console encoding, so on a cp936 box an unpinned pipe can fail to
    # decode and leave stderr as None.
    return subprocess.run(
        [sys.executable, str(PATCH), str(site)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )


# 1. a clean file gets the gate turned on.
site, target = make_site(FIXTURE)
first = run(site)
assert first.returncode == 0, first.stderr
assert "patched" in first.stdout, first.stdout

patched = target.read_text(encoding="utf-8")
ast.parse(patched)
assert "capture_settings.use_silence_gate = True" in patched
assert "use_silence_gate = False" not in patched, "the old value must be gone"
# Indentation has to survive or the patched file would not parse; ast.parse above
# already proves that, but assert the literal too so a reflow is caught.
assert "            capture_settings.use_silence_gate = True\n" in patched

# 2. re-running is a no-op, not a double patch.
second = run(site)
assert second.returncode == 0, second.stderr
assert "already applied" in second.stdout, second.stdout
assert target.read_text(encoding="utf-8") == patched, "rerun changed the file"

# 3. losing the upstream line is a hard build failure.
missing = FIXTURE.replace(
    "            capture_settings.use_silence_gate = False\n", ""
)
assert missing != FIXTURE
bad_site, _ = make_site(missing)
bad = run(bad_site)
assert bad.returncode != 0, "a missing target must fail the build"
assert "drop silent PCMFlux chunks" in bad.stderr, bad.stderr
assert "original=0" in bad.stderr, bad.stderr

# 4. two copies are equally fatal — patching only the first would leave one path
#    still streaming silence.
doubled = FIXTURE + FIXTURE
dup_site, _ = make_site(doubled)
dup = run(dup_site)
assert dup.returncode != 0, "duplicate targets must fail the build"
assert "original=2" in dup.stderr, dup.stderr

print("audio-silence-gate patch tests passed")
