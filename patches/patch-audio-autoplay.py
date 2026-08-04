#!/usr/bin/env python3
"""Defer Selkies playback AudioContext creation until a browser user gesture."""

from __future__ import annotations

import re
import sys
from pathlib import Path


SCRIPT_REL = "src/selkies-audio-unlock.js"
SCRIPT_TAG = f'<script src="{SCRIPT_REL}"></script>'
WORKER_WARNING = 'console.warn("AudioDecoderWorker not ready. Attempting to initialize audio pipeline.")'
GATED_WORKER_WARNING = f"window.wechatAudioGate.isUnlocked()&&{WORKER_WARNING}"

CONSTRUCTOR = re.compile(
    r"(?P<variable>[A-Za-z_$][\w$]*)="
    r"new\(window\.AudioContext\|\|window\.webkitAudioContext\)"
    r"\((?P<options>[A-Za-z_$][\w$]*)\)"
)
GATED_CONSTRUCTOR = re.compile(
    r"(?P<variable>[A-Za-z_$][\w$]*)=await window\.wechatAudioGate\.create\(\(\)=>"
    r"new\(window\.AudioContext\|\|window\.webkitAudioContext\)"
    r"\((?P<options>[A-Za-z_$][\w$]*)\)\)"
)
RESUME = re.compile(
    r"(?P<variable>[A-Za-z_$][\w$]*)&&(?P=variable)\.state!==\"running\"&&"
    r"(?P=variable)\.resume\(\)\.catch\((?P<error>[A-Za-z_$][\w$]*)=>"
    r"console\.error\(\"Error resuming audio context\",(?P=error)\)\)"
)
GATED_RESUME = re.compile(
    r"window\.wechatAudioGate\.resume\((?P<variable>[A-Za-z_$][\w$]*)\)"
)


def fail(message: str) -> None:
    raise RuntimeError(f"patch-audio-autoplay: {message}")


def patch_javascript(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    original = source

    constructors = list(CONSTRUCTOR.finditer(source))
    gated_constructors = list(GATED_CONSTRUCTOR.finditer(source))
    if len(constructors) == 1 and not gated_constructors:
        source, count = CONSTRUCTOR.subn(
            lambda match: (
                f'{match.group("variable")}=await window.wechatAudioGate.create(()=>'
                "new(window.AudioContext||window.webkitAudioContext)"
                f'({match.group("options")}))'
            ),
            source,
        )
        if count != 1:
            fail(f"constructor replacement count was {count} in {path}")
    elif constructors or len(gated_constructors) != 1:
        fail(
            f"expected one original or gated playback constructor in {path}; "
            f"found original={len(constructors)}, gated={len(gated_constructors)}"
        )

    resumes = list(RESUME.finditer(source))
    gated_resumes = list(GATED_RESUME.finditer(source))
    if len(resumes) == 1 and not gated_resumes:
        source, count = RESUME.subn(
            lambda match: f'window.wechatAudioGate.resume({match.group("variable")})',
            source,
        )
        if count != 1:
            fail(f"resume replacement count was {count} in {path}")
    elif resumes or len(gated_resumes) != 1:
        fail(
            f"expected one original or gated playback resume in {path}; "
            f"found original={len(resumes)}, gated={len(gated_resumes)}"
        )

    gated_warnings = source.count(GATED_WORKER_WARNING)
    raw_warnings = source.count(WORKER_WARNING) - gated_warnings
    if raw_warnings == 1 and gated_warnings == 0:
        source = source.replace(WORKER_WARNING, GATED_WORKER_WARNING, 1)
    elif raw_warnings != 0 or gated_warnings != 1:
        fail(
            f"expected one original or gated worker warning in {path}; "
            f"found original={raw_warnings}, gated={gated_warnings}"
        )

    if source.count("window.wechatAudioGate.create") != 1:
        fail(f"playback constructor gate validation failed in {path}")
    if source.count("window.wechatAudioGate.resume") != 1:
        fail(f"playback resume gate validation failed in {path}")

    if source != original:
        path.write_text(source, encoding="utf-8")
        print(f"patch-audio-autoplay: patched {path}")
    else:
        print(f"patch-audio-autoplay: already patched {path}")


def patch_dashboard(dashboard: Path) -> None:
    html = dashboard / "index.html"
    helper = dashboard / SCRIPT_REL
    core = dashboard / "src" / "selkies-core.js"
    assets = sorted((dashboard / "assets").glob("index-*.js"))

    if not helper.is_file():
        fail(f"missing audio gate helper {helper}")
    if not core.is_file():
        fail(f"missing Selkies source bundle {core}")
    if len(assets) != 1:
        fail(f"expected exactly one built dashboard bundle in {dashboard}, found {len(assets)}")

    patch_javascript(core)
    patch_javascript(assets[0])

    page = html.read_text(encoding="utf-8")
    tag_count = page.count(SCRIPT_TAG)
    if tag_count > 1:
        fail(f"duplicate audio gate script tags in {html}")
    if tag_count == 0:
        if page.count("</body>") != 1:
            fail(f"expected exactly one </body> in {html}")
        page = page.replace("</body>", f"  {SCRIPT_TAG}\n</body>")
        html.write_text(page, encoding="utf-8")
        print(f"patch-audio-autoplay: injected helper into {html}")
    else:
        print(f"patch-audio-autoplay: helper already injected into {html}")

    if html.read_text(encoding="utf-8").count(SCRIPT_TAG) != 1:
        fail(f"audio gate script injection validation failed in {html}")


def patch_root(root: Path) -> None:
    dashboards = []
    for html in root.glob("*/index.html"):
        dashboard = html.parent
        if (dashboard / SCRIPT_REL).is_file():
            dashboards.append(dashboard)

    if len(dashboards) != 1:
        fail(f"expected exactly one dashboard carrying {SCRIPT_REL}, found {len(dashboards)}")
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
