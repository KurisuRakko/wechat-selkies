#!/usr/bin/env python3
"""Fixture tests for the build-time frame-assembly patch.

Same style as test-audio-autoplay-patch.py and test-upload-client-patch.py:
build a fake dashboard around a synthetic bundle, run the real patch script
against it, and check what it produced. Two identifier-naming styles are used
(one modeled on src/selkies-core.js, one on assets/index-*.js as they actually
minify), because the patch script's regexes are only useful if they survive a
base-image bump renaming every identifier.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


PATCHER = Path(__file__).with_name("patch-frame-assembly.py")

# Modeled on the real src/selkies-core.js (verified against a pulled base
# image): o/r/h/H/W/ae/lt/ct/It/se are the actual identifiers that build
# produces there.
CORE_STYLE = (
    'let At=[];function Ai(o,r,h){At.push({yPos:o,frame:h,vncFrameID:r})}'
    'try{ae.decoder.close()}catch(ct){console.warn("Error closing old VNC stripe decoder:",ct)}'
    'const lt=new VideoDecoder({output:Ai.bind(null,H,W),error:ct=>It(ct,`stripe_decoder_Y=${H}`)});'
    'if(ae.decoder.state==="configured"){const Dt=new EncodedVideoChunk(ci);'
    'try{ae.decoder.decode(Dt)}catch(ct){It(ct,`stripe_decode_Y=${H}`)}}'
    'else ae.decoder.state==="unconfigured"||ae.decoder.state==="configuring"?'
    'ae.pendingChunks.push(ci):console.warn(`VNC stripe decoder for Y=${H} in unexpected state: '
    '${ae.decoder.state}. Dropping chunk.`);'
    'try{r.decoder.decode(x)}catch(v){console.error(`Error decoding pending chunk for stripe Y=${o}:`,v,x)}'
    'let m=!1;for(const M of At)i.width>0&&i.height>0&&f.drawImage(M.frame,0,M.yPos),M.frame.close(),m=!0;'
    'At=[],m&&!rt&&ai();'
    'console.error(`Error configuring VNC stripe decoder Y=${H}:`,ct),se[H]&&se[H].decoder===lt){'
    'try{lt.state!=="closed"&&lt.close()}catch{}delete se[H]}'
)

# Modeled on the real assets/index-*.js vite bundle: entirely different
# identifiers for the same constructs.
VITE_STYLE = (
    'let en=[];function Za(S,x,P){en.push({yPos:S,frame:P,vncFrameID:x})}'
    'try{Pt.decoder.close()}catch(vi){console.warn("Error closing old VNC stripe decoder:",vi)}'
    'const wa=new VideoDecoder({output:Za.bind(null,Ue,He),error:vi=>Fi(vi,`stripe_decoder_Y=${Ue}`)});'
    'if(Pt.decoder.state==="configured"){const vi=new EncodedVideoChunk(an);'
    'try{Pt.decoder.decode(vi)}catch(Ta){Fi(Ta,`stripe_decode_Y=${Ue}`)}}'
    'else Pt.decoder.state==="unconfigured"||Pt.decoder.state==="configuring"?'
    'Pt.pendingChunks.push(an):console.warn(`VNC stripe decoder for Y=${Ue} in unexpected state: '
    '${Pt.decoder.state}. Dropping chunk.`);'
    'try{x.decoder.decode(oe)}catch(ie){console.error(`Error decoding pending chunk for stripe Y=${S}:`,ie,oe)}'
    'let Q=!1;for(const Ee of en)h.width>0&&h.height>0&&T.drawImage(Ee.frame,0,Ee.yPos),Ee.frame.close(),Q=!0;'
    'en=[],Q&&!Ia&&hn();'
    'console.error(`Error configuring VNC stripe decoder Y=${Ue}:`,vi),Qe[Ue]&&Qe[Ue].decoder===wa){'
    'try{wa.state!=="closed"&&wa.close()}catch{}delete Qe[Ue]}'
)


def make_dashboard(root: Path, core_text: str, asset_text: str) -> Path:
    dashboard = root / "selkies-dashboard"
    (dashboard / "src").mkdir(parents=True)
    (dashboard / "assets").mkdir()
    (dashboard / "index.html").write_text("<html><body></body></html>", encoding="utf-8")
    (dashboard / "src" / "wechat-dragdrop.js").write_text("marker", encoding="utf-8")
    (dashboard / "src" / "selkies-core.js").write_text(core_text, encoding="utf-8")
    (dashboard / "assets" / "index-fixture.js").write_text(asset_text, encoding="utf-8")
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


def assert_patched(bundle_text: str, y: str, id_: str, flush_y: str) -> None:
    assert bundle_text.count("window.wechatFrameAssembler.entry(") == 1, "site A"
    assert bundle_text.count("window.wechatFrameAssembler.resetStripe(") == 2, "sites E1+E2"
    assert bundle_text.count(f"window.wechatFrameAssembler&&window.wechatFrameAssembler.submitted({y},{id_})") == 2, (
        "sites B+C (direct decode and pendingChunks enqueue)"
    )
    # Site B's submitFailed uses the main handler's Y variable; site C's flush
    # catch is a different function with its own parameter name for the same
    # Y value (Fi(o){...} in the real bundle), so the two literals differ.
    assert bundle_text.count(f"window.wechatFrameAssembler.submitFailed({y})") == 1, "site B catch"
    assert bundle_text.count(f"window.wechatFrameAssembler.submitFailed({flush_y})") == 1, "site C flush catch"
    assert bundle_text.count("window.wechatFrameAssembler.drain(") == 1, "site D"
    assert bundle_text.count("window.wechatFrameAssembler.painted") == 1, "site D"
    # The pendingChunks enqueue is inside a ternary expression; the injected
    # call must use the comma operator, not a statement-terminating `;`,
    # or the patched bundle would be a syntax error.
    assert ".pendingChunks.push(" in bundle_text
    push_idx = bundle_text.index(".pendingChunks.push(")
    assert bundle_text[max(0, push_idx - 80):push_idx].count(",") >= 1
    assert ";" not in bundle_text[bundle_text.index("?(window.wechatFrameAssembler"):push_idx]


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    dashboard = make_dashboard(root, CORE_STYLE, VITE_STYLE)
    run(root)
    run(root)  # must be safe to run twice (image build re-runs patches idempotently)

    core_patched = (dashboard / "src" / "selkies-core.js").read_text(encoding="utf-8")
    vite_patched = (dashboard / "assets" / "index-fixture.js").read_text(encoding="utf-8")

    assert_patched(core_patched, "H", "W", "o")
    assert_patched(vite_patched, "Ue", "He", "S")

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    make_dashboard(root, "upstream changed", "upstream changed")
    result = run(root, expect_success=False)
    assert "site A" in result.stderr

with tempfile.TemporaryDirectory() as temporary:
    # Site A present, everything else missing: must fail on the *next* site,
    # not silently accept a half-patched bundle.
    root = Path(temporary)
    only_site_a = 'let At=[];function Ai(o,r,h){At.push({yPos:o,frame:h,vncFrameID:r})}'
    make_dashboard(root, only_site_a, only_site_a)
    result = run(root, expect_success=False)
    assert "site E1" in result.stderr

print("frame-assembly patch tests passed")
