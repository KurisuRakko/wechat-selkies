#!/usr/bin/env python3
"""Fixture tests for the build-time Selkies audio patch."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


PATCHER = Path(__file__).with_name("patch-audio-autoplay.py")
ORIGINAL = (
    'async function init(){const OPT={sampleRate:48e3};CTX='
    'new(window.AudioContext||window.webkitAudioContext)(OPT);'
    'MIC=new AudioContext({sampleRate:24e3})}'
    'CTX&&CTX.state!=="running"&&CTX.resume().catch(ERR=>'
    'console.error("Error resuming audio context",ERR));'
    'console.warn("AudioDecoderWorker not ready. Attempting to initialize audio pipeline.")'
)


def make_dashboard(root: Path, bundle_text: str = ORIGINAL) -> Path:
    dashboard = root / "selkies-dashboard"
    (dashboard / "src").mkdir(parents=True)
    (dashboard / "assets").mkdir()
    (dashboard / "index.html").write_text("<html><body></body></html>", encoding="utf-8")
    (dashboard / "src" / "selkies-audio-unlock.js").write_text("gate", encoding="utf-8")
    (dashboard / "src" / "selkies-core.js").write_text(bundle_text, encoding="utf-8")
    (dashboard / "assets" / "index-fixture.js").write_text(bundle_text, encoding="utf-8")
    return dashboard


def run(root: Path, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(PATCHER), str(root)],
        check=False,
        capture_output=True,
        text=True,
    )
    if expect_success and result.returncode != 0:
        raise AssertionError(result.stderr)
    if not expect_success and result.returncode == 0:
        raise AssertionError("malformed fixture unexpectedly passed")
    return result


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    dashboard = make_dashboard(root)
    run(root)
    run(root)  # The image build patch must be safe to validate a second time.

    html = (dashboard / "index.html").read_text(encoding="utf-8")
    assert html.count('<script src="src/selkies-audio-unlock.js"></script>') == 1
    for bundle in [dashboard / "src" / "selkies-core.js", dashboard / "assets" / "index-fixture.js"]:
        patched = bundle.read_text(encoding="utf-8")
        assert patched.count("window.wechatAudioGate.create") == 1
        assert patched.count("window.wechatAudioGate.resume") == 1
        assert patched.count("window.wechatAudioGate.isUnlocked") == 1
        assert patched.count("new AudioContext({sampleRate:24e3})") == 1

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    make_dashboard(root, "upstream changed")
    result = run(root, expect_success=False)
    assert "expected one original or gated playback constructor" in result.stderr

print("audio autoplay patch tests passed")
