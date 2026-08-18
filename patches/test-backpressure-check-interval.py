#!/usr/bin/env python3
"""Regression tests for patches/backpressure-check-interval.sh.

This script's sed call uses GNU sed's -i syntax (no backup-suffix argument),
which BSD/macOS sed parses differently and fails on -- exactly like its
sibling patches/damage-block-duration.sh. Run this test inside a Linux
environment (a `debian:stable-slim` container is enough; no other package is
required beyond python3 itself, which the container installs on demand):

    docker run --rm -v "$PWD":/repo -w /repo debian:stable-slim sh -c \
        'apt-get update -qq && apt-get install -y -qq python3 >/dev/null && \
         python3 patches/test-backpressure-check-interval.py'

Running it directly on macOS will fail with a sed parse error, not a useful
pass/fail signal -- that is expected, not a bug in the patch.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("backpressure-check-interval.sh")

FIXTURE = """# Constants
BACKPRESSURE_ALLOWED_DESYNC_MS = 2000
BACKPRESSURE_LATENCY_THRESHOLD_MS = 50
BACKPRESSURE_CHECK_INTERVAL_S = 0.5
MAX_UINT16_FRAME_ID = 65535
"""


def make_site(source: str) -> tuple[Path, Path]:
    site = Path(tempfile.mkdtemp(prefix="selkies-backpressure-interval-"))
    target = site / "selkies.py"
    target.write_text(source, encoding="utf-8")
    return site, target


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


# 1. the documented default (0.2) applies cleanly.
site, target = make_site(FIXTURE)
result = run("0.2", str(site))
assert result.returncode == 0, result.stderr
assert "BACKPRESSURE_CHECK_INTERVAL_S = 0.2" in result.stdout, result.stdout
assert target.read_text(encoding="utf-8").count("BACKPRESSURE_CHECK_INTERVAL_S = 0.2") == 1
assert "BACKPRESSURE_CHECK_INTERVAL_S = 0.5" not in target.read_text(encoding="utf-8")

# 2. re-running on the same (now-patched) file fails loudly instead of
#    silently re-writing or double-patching -- same fail-fast contract as
#    damage-block-duration.sh.
second = run("0.2", str(site))
assert second.returncode != 0, "a second run against an already-patched file must fail"

# 3. a non-numeric value is rejected before anything is touched.
bad_site, bad_target = make_site(FIXTURE)
bad = run("not-a-number", str(bad_site))
assert bad.returncode != 0
assert bad_target.read_text(encoding="utf-8") == FIXTURE, "a rejected value must not touch the file"

# 4. an out-of-range value (outside the sane 0-5s band) is also rejected.
range_site, range_target = make_site(FIXTURE)
ranged = run("10", str(range_site))
assert ranged.returncode != 0
assert range_target.read_text(encoding="utf-8") == FIXTURE

# 5. a missing upstream anchor is a hard build failure, matching every other
#    patch script in this repo (upstream drifted -> fail loudly, not silently
#    ship the unpatched value).
missing_site, missing_target = make_site(FIXTURE.replace("BACKPRESSURE_CHECK_INTERVAL_S = 0.5", "BACKPRESSURE_CHECK_INTERVAL_S = 1.0"))
missing = run("0.2", str(missing_site))
assert missing.returncode != 0, "a missing/changed anchor must fail the build"

print("backpressure-check-interval patch tests passed")
