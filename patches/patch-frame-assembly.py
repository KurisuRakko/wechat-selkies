#!/usr/bin/env python3
"""Wire the minified Selkies bundle's striped-video path to window.wechatFrameAssembler.

Context: patches/wechat-frame-assembler.js (injected by
inject-frame-assembler-script.sh) is a FIFO-per-stripe state machine that
recovers the true vncFrameID for every decoded stripe and only lets the
render loop paint a frame once every stripe belonging to it has arrived —
fixing the horizontal tearing described in patches/wechat-frame-assembler.js's
header comment. This script is the microsurgery that calls it from the
minified bundle, which the assembler alone cannot do.

Five sites, found in both src/selkies-core.js and the built assets/index-*.js
(same two-copy rule as patch-upload-client.py — a base-image bump that only
updates one would otherwise silently ship half a fix):

  A  the decoder-output callback that pushes {yPos,frame,vncFrameID} into the
     paint queue — vncFrameID here is bound at decoder-*creation* time and
     goes stale the moment a decoder is reused, which is the root cause of
     the tearing. Routed through wechatFrameAssembler.entry() to recover the
     id the FIFO actually observed.
  B  the direct decoder.decode() call and its catch — bracketed with
     submitted()/submitFailed() so the FIFO tracks every chunk actually
     handed to the decoder.
  C  the pendingChunks enqueue path (decoder not configured yet) and its
     flush-time catch — submitted() at enqueue time (preserves arrival
     order), submitFailed() if the later decode() throws.
  D  the render loop's unconditional "paint everything queued" loop — this is
     the actual tear: stripes from different frames drawn in the same pass.
     Replaced with wechatFrameAssembler.drain(), which withholds incomplete
     frames.
  E  the two places a stripe decoder is torn down/recreated (old-decoder
     close, config failure) — resetStripe() so the FIFO does not leak counts
     for stripes that will never arrive on the new decoder.

Every site is guarded with `window.wechatFrameAssembler&&` (site D also
guards, via an if/else) so a build that somehow lost the injected script
degrades to the original unpatched behavior instead of throwing.

Each site's regex is anchored on structure and string literals that survive
minification (the upstream identifiers used in this file's comments, like H
or Ai, are from one snapshot and are NOT matched literally — every real
pattern below uses \\w+ so a renamed build still matches).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


CORE_REL = "src/selkies-core.js"
MARKER_REL = "src/wechat-dragdrop.js"

ID = r"[A-Za-z_$][\w$]*"


def fail(message: str) -> None:
    raise RuntimeError(f"patch-frame-assembly: {message}")


# --------------------------------------------------------------------- A ---
# function Ai(o,r,h){At.push({yPos:o,frame:h,vncFrameID:r})}
#   o=yPos r=vncFrameID(stale, bound at decoder creation) h=VideoFrame
SITE_A_ORIGINAL = re.compile(
    rf"function (?P<fn>{ID})\((?P<y>{ID}),(?P<id>{ID}),(?P<frame>{ID})\)\{{"
    rf"(?P<q>{ID})\.push\(\{{yPos:(?P=y),frame:(?P=frame),vncFrameID:(?P=id)\}}\)\}}"
)
SITE_A_PATCHED = re.compile(
    rf"function (?P<fn>{ID})\((?P<y>{ID}),(?P<id>{ID}),(?P<frame>{ID})\)\{{"
    rf"(?P<q>{ID})\.push\(window\.wechatFrameAssembler\?window\.wechatFrameAssembler\.entry\("
    rf"(?P=y),(?P=id),(?P=frame)\):\{{yPos:(?P=y),frame:(?P=frame),vncFrameID:(?P=id)\}}\)\}}"
)


def site_a(source: str) -> str:
    originals = list(SITE_A_ORIGINAL.finditer(source))
    patched = list(SITE_A_PATCHED.finditer(source))
    if len(patched) == 1 and not originals:
        return source
    if len(originals) != 1 or patched:
        fail(
            f"site A: expected one original or one patched decoder-output "
            f"callback, found original={len(originals)}, patched={len(patched)}"
        )
    m = originals[0]
    replacement = (
        f'function {m["fn"]}({m["y"]},{m["id"]},{m["frame"]}){{{m["q"]}.push('
        f'window.wechatFrameAssembler?window.wechatFrameAssembler.entry('
        f'{m["y"]},{m["id"]},{m["frame"]}):'
        f'{{yPos:{m["y"]},frame:{m["frame"]},vncFrameID:{m["id"]}}})}}'
    )
    return source[: m.start()] + replacement + source[m.end() :]


# ------------------------------------------------------------------ E1/id --
# try{ae.decoder.close()}catch(ct){console.warn("Error closing old VNC stripe
# decoder:",ct)}const lt=new VideoDecoder({output:Ai.bind(null,H,W),...
#   H=yPos W=vncFrameID(current) — this is also where H/W's names are learned
#   for sites B and C, which have no local vncFrameID literal of their own.
SITE_E1_ORIGINAL = re.compile(
    rf"try\{{(?P<ae>{ID})\.decoder\.close\(\)\}}catch\((?P<ct>{ID})\)\{{"
    rf'console\.warn\("Error closing old VNC stripe decoder:",(?P=ct)\)\}}'
    rf"const (?P<lt>{ID})=new VideoDecoder\(\{{output:(?P<fn>{ID})\.bind\(null,(?P<y>{ID}),(?P<id>{ID})\)"
)
SITE_E1_PATCHED = re.compile(
    rf"try\{{(?P<ae>{ID})\.decoder\.close\(\)\}}catch\((?P<ct>{ID})\)\{{"
    rf'console\.warn\("Error closing old VNC stripe decoder:",(?P=ct)\)\}}'
    rf"window\.wechatFrameAssembler&&window\.wechatFrameAssembler\.resetStripe\((?P<y>{ID})\);"
    rf"const (?P<lt>{ID})=new VideoDecoder\(\{{output:(?P<fn>{ID})\.bind\(null,(?P=y),(?P<id>{ID})\)"
)


def site_e1(source: str) -> tuple[str, str, str]:
    """Returns (patched_source, y_name, id_name)."""
    originals = list(SITE_E1_ORIGINAL.finditer(source))
    patched = list(SITE_E1_PATCHED.finditer(source))
    if len(patched) == 1 and not originals:
        m = patched[0]
        return source, m["y"], m["id"]
    if len(originals) != 1 or patched:
        fail(
            f"site E1: expected one original or one patched old-decoder-close "
            f"site, found original={len(originals)}, patched={len(patched)}"
        )
    m = originals[0]
    replacement = (
        f'try{{{m["ae"]}.decoder.close()}}catch({m["ct"]}){{'
        f'console.warn("Error closing old VNC stripe decoder:",{m["ct"]})}}'
        f'window.wechatFrameAssembler&&window.wechatFrameAssembler.resetStripe({m["y"]});'
        f'const {m["lt"]}=new VideoDecoder({{output:{m["fn"]}.bind(null,{m["y"]},{m["id"]})'
    )
    patched_source = source[: m.start()] + replacement + source[m.end() :]
    return patched_source, m["y"], m["id"]


# --------------------------------------------------------------------- B ---
# try{ae.decoder.decode(Dt)}catch(ct){It(ct,`stripe_decode_Y=${H}`)}
def site_b(source: str, y_name: str, id_name: str) -> str:
    y = re.escape(y_name)
    original = re.compile(
        rf"try\{{(?P<dec>{ID})\.decoder\.decode\((?P<chunk>{ID})\)\}}"
        rf"catch\((?P<err>{ID})\)\{{(?P<errfn>{ID})\((?P=err),`stripe_decode_Y=\$\{{{y}\}}`\)\}}"
    )
    patched = re.compile(
        rf"window\.wechatFrameAssembler&&window\.wechatFrameAssembler\.submitted\({y},{re.escape(id_name)}\);"
        rf"try\{{(?P<dec>{ID})\.decoder\.decode\((?P<chunk>{ID})\)\}}"
        rf"catch\((?P<err>{ID})\)\{{"
        rf"window\.wechatFrameAssembler&&window\.wechatFrameAssembler\.submitFailed\({y}\);"
        rf"(?P<errfn>{ID})\((?P=err),`stripe_decode_Y=\$\{{{y}\}}`\)\}}"
    )
    originals = list(original.finditer(source))
    patcheds = list(patched.finditer(source))
    if len(patcheds) == 1 and not originals:
        return source
    if len(originals) != 1 or patcheds:
        fail(
            f"site B: expected one original or one patched direct-decode site "
            f"for Y={y_name}, found original={len(originals)}, patched={len(patcheds)}"
        )
    m = originals[0]
    replacement = (
        f'window.wechatFrameAssembler&&window.wechatFrameAssembler.submitted({y_name},{id_name});'
        f'try{{{m["dec"]}.decoder.decode({m["chunk"]})}}'
        f'catch({m["err"]}){{'
        f'window.wechatFrameAssembler&&window.wechatFrameAssembler.submitFailed({y_name});'
        f'{m["errfn"]}({m["err"]},`stripe_decode_Y=${{{y_name}}}`)}}'
    )
    return source[: m.start()] + replacement + source[m.end() :]


# --------------------------------------------------------------------- C ---
# ...?ae.pendingChunks.push(ci):console.warn(...)   — the enqueue-while-
# configuring branch of a ternary, NOT a statement: injecting the submitted()
# call needs the comma operator, a `;` here would be a syntax error. Anchored
# on the surrounding `?`/`:` so the original pattern cannot re-match its own
# patched (parenthesised, comma-joined) output on a second run.
SITE_C_PUSH_ORIGINAL = re.compile(rf"\?(?P<obj>{ID})\.pendingChunks\.push\((?P<arg>{ID})\):")
SITE_C_PUSH_PATCHED = re.compile(
    rf"\?\(window\.wechatFrameAssembler&&window\.wechatFrameAssembler\.submitted\("
    rf"(?P<y>{ID}),(?P<id>{ID})\),(?P<obj>{ID})\.pendingChunks\.push\((?P<arg>{ID})\)\):"
)


def site_c_push(source: str, y_name: str, id_name: str) -> str:
    originals = list(SITE_C_PUSH_ORIGINAL.finditer(source))
    patcheds = list(SITE_C_PUSH_PATCHED.finditer(source))
    if len(patcheds) == 1 and not originals:
        return source
    if len(originals) != 1 or patcheds:
        fail(
            f"site C (enqueue): expected one original or one patched "
            f"pendingChunks.push, found original={len(originals)}, patched={len(patcheds)}"
        )
    m = originals[0]
    replacement = (
        f'?(window.wechatFrameAssembler&&window.wechatFrameAssembler.submitted({y_name},{id_name}),'
        f'{m["obj"]}.pendingChunks.push({m["arg"]})):'
    )
    return source[: m.start()] + replacement + source[m.end() :]


# catch(v){console.error(`Error decoding pending chunk for stripe Y=${o}:`,v,x)}
SITE_C_FLUSH_ORIGINAL = re.compile(
    rf"catch\((?P<err>{ID})\)\{{console\.error\("
    rf"`Error decoding pending chunk for stripe Y=\$\{{(?P<y>{ID})\}}:`,(?P=err),(?P<x>{ID})\)\}}"
)
SITE_C_FLUSH_PATCHED = re.compile(
    rf"catch\((?P<err>{ID})\)\{{"
    rf"window\.wechatFrameAssembler&&window\.wechatFrameAssembler\.submitFailed\((?P<y>{ID})\);"
    rf"console\.error\(`Error decoding pending chunk for stripe Y=\$\{{(?P=y)\}}:`,(?P=err),(?P<x>{ID})\)\}}"
)


def site_c_flush(source: str) -> str:
    originals = list(SITE_C_FLUSH_ORIGINAL.finditer(source))
    patcheds = list(SITE_C_FLUSH_PATCHED.finditer(source))
    if len(patcheds) == 1 and not originals:
        return source
    if len(originals) != 1 or patcheds:
        fail(
            f"site C (flush catch): expected one original or one patched "
            f"pending-chunk decode catch, found original={len(originals)}, "
            f"patched={len(patcheds)}"
        )
    m = originals[0]
    replacement = (
        f'catch({m["err"]}){{'
        f'window.wechatFrameAssembler&&window.wechatFrameAssembler.submitFailed({m["y"]});'
        f'console.error(`Error decoding pending chunk for stripe Y=${{{m["y"]}}}:`,{m["err"]},{m["x"]})}}'
    )
    return source[: m.start()] + replacement + source[m.end() :]


# --------------------------------------------------------------------- D ---
# let m=!1;for(const M of At)i.width>0&&i.height>0&&f.drawImage(M.frame,0,M.yPos),
# M.frame.close(),m=!0;At=[],m&&!rt&&ai()
SITE_D_ORIGINAL = re.compile(
    rf"let (?P<m>{ID})=!1;for\(const (?P<item>{ID}) of (?P<q>{ID})\)"
    rf"(?P<canvas>{ID})\.width>0&&(?P=canvas)\.height>0&&(?P<ctx>{ID})\.drawImage\("
    rf"(?P=item)\.frame,0,(?P=item)\.yPos\),(?P=item)\.frame\.close\(\),(?P=m)=!0;"
    rf"(?P=q)=\[\],(?P=m)&&!(?P<started>{ID})&&(?P<start>{ID})\(\)"
)
SITE_D_PATCHED = re.compile(
    rf"let (?P<m>{ID})=!1;if\(window\.wechatFrameAssembler\)\{{"
    rf"(?P<q>{ID})=window\.wechatFrameAssembler\.drain\((?P=q),(?P<ctx>{ID}),(?P<canvas>{ID})\);"
    rf"(?P=m)=window\.wechatFrameAssembler\.painted\}}else\{{"
    rf"for\(const (?P<item>{ID}) of (?P=q)\)"
    rf"(?P=canvas)\.width>0&&(?P=canvas)\.height>0&&(?P=ctx)\.drawImage\("
    rf"(?P=item)\.frame,0,(?P=item)\.yPos\),(?P=item)\.frame\.close\(\),(?P=m)=!0;"
    rf"(?P=q)=\[\]\}}(?P=m)&&!(?P<started>{ID})&&(?P<start>{ID})\(\)"
)


def site_d(source: str) -> str:
    originals = list(SITE_D_ORIGINAL.finditer(source))
    patcheds = list(SITE_D_PATCHED.finditer(source))
    if len(patcheds) == 1 and not originals:
        return source
    if len(originals) != 1 or patcheds:
        fail(
            f"site D: expected one original or one patched paint-queue drain "
            f"loop, found original={len(originals)}, patched={len(patcheds)}"
        )
    m = originals[0]
    replacement = (
        f'let {m["m"]}=!1;if(window.wechatFrameAssembler){{'
        f'{m["q"]}=window.wechatFrameAssembler.drain({m["q"]},{m["ctx"]},{m["canvas"]});'
        f'{m["m"]}=window.wechatFrameAssembler.painted}}else{{'
        f'for(const {m["item"]} of {m["q"]}){m["canvas"]}.width>0&&{m["canvas"]}.height>0&&'
        f'{m["ctx"]}.drawImage({m["item"]}.frame,0,{m["item"]}.yPos),{m["item"]}.frame.close(),{m["m"]}=!0;'
        f'{m["q"]}=[]}}{m["m"]}&&!{m["started"]}&&{m["start"]}()'
    )
    return source[: m.start()] + replacement + source[m.end() :]


# --------------------------------------------------------------------- E2 --
# .catch(ct=>{if(console.error(`Error configuring VNC stripe decoder Y=${H}:`,ct),
# se[H]&&se[H].decoder===lt){try{lt.state!=="closed"&&lt.close()}catch{}delete se[H]}})
SITE_E2_ORIGINAL = re.compile(
    rf"console\.error\(`Error configuring VNC stripe decoder Y=\$\{{(?P<y>{ID})\}}:`,(?P<err>{ID})\),"
    rf"(?P<se>{ID})\[(?P=y)\]&&(?P=se)\[(?P=y)\]\.decoder===(?P<lt>{ID})\)\{{"
    rf'try\{{(?P=lt)\.state!=="closed"&&(?P=lt)\.close\(\)\}}catch\{{\}}delete (?P=se)\[(?P=y)\]\}}'
)
SITE_E2_PATCHED = re.compile(
    rf"console\.error\(`Error configuring VNC stripe decoder Y=\$\{{(?P<y>{ID})\}}:`,(?P<err>{ID})\),"
    rf"(?P<se>{ID})\[(?P=y)\]&&(?P=se)\[(?P=y)\]\.decoder===(?P<lt>{ID})\)\{{"
    rf'try\{{(?P=lt)\.state!=="closed"&&(?P=lt)\.close\(\)\}}catch\{{\}}delete (?P=se)\[(?P=y)\];'
    rf"window\.wechatFrameAssembler&&window\.wechatFrameAssembler\.resetStripe\((?P=y)\)\}}"
)


def site_e2(source: str) -> str:
    originals = list(SITE_E2_ORIGINAL.finditer(source))
    patcheds = list(SITE_E2_PATCHED.finditer(source))
    if len(patcheds) == 1 and not originals:
        return source
    if len(originals) != 1 or patcheds:
        fail(
            f"site E2: expected one original or one patched config-failure "
            f"catch, found original={len(originals)}, patched={len(patcheds)}"
        )
    m = originals[0]
    replacement = (
        f'console.error(`Error configuring VNC stripe decoder Y=${{{m["y"]}}}:`,{m["err"]}),'
        f'{m["se"]}[{m["y"]}]&&{m["se"]}[{m["y"]}].decoder==={m["lt"]}){{'
        f'try{{{m["lt"]}.state!=="closed"&&{m["lt"]}.close()}}catch{{}}delete {m["se"]}[{m["y"]}];'
        f'window.wechatFrameAssembler&&window.wechatFrameAssembler.resetStripe({m["y"]})}}'
    )
    return source[: m.start()] + replacement + source[m.end() :]


def patch_javascript(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    original = source

    source = site_a(source)
    source, y_name, id_name = site_e1(source)
    source = site_b(source, y_name, id_name)
    source = site_c_push(source, y_name, id_name)
    source = site_c_flush(source)
    source = site_d(source)
    source = site_e2(source)

    if source != original:
        path.write_text(source, encoding="utf-8")
        print(f"patch-frame-assembly: patched {path}")
    else:
        print(f"patch-frame-assembly: already patched {path}")


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
