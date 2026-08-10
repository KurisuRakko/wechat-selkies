#!/usr/bin/env python3
"""Fixture tests for the build-time /files JSON-listing patch.

Same style as test-frame-assembly-patch.py: embed a simplified copy of
/defaults/default.conf's two server blocks (both carrying the files location
block, plus one unrelated location block that must be left alone), run the
real patch script against it, and check what it produced.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


PATCHER = Path(__file__).with_name("files-json-index.py")

FILES_BLOCK = """  location SUBFOLDERfiles {
    fancyindex on;
    fancyindex_footer SUBFOLDERnginx/footer.html;
    fancyindex_header SUBFOLDERnginx/header.html;
    alias REPLACE_DOWNLOADS_PATH/;
    if (-f $request_filename) {
        add_header Content-Disposition "attachment";
        add_header X-Content-Type-Options "nosniff";
    }
  }
"""

# 两个 server 块（明文 3000 / TLS 3001）各含一份 files 块，另外放一个
# 与 files 无关的 location 块作为「不该被动」的对照。
FIXTURE = (
    "server {\n"
    "  listen 3000;\n"
    + FILES_BLOCK
    + """  location SUBFOLDERwechat-notifications {
    proxy_pass http://127.0.0.1:8765;
  }
}
server {
  listen 3001 ssl;
"""
    + FILES_BLOCK
    + "}\n"
)


def run(conf: Path, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(PATCHER), str(conf)],
        check=False,
        capture_output=True,
        text=True,
    )
    if expect_success and result.returncode != 0:
        raise AssertionError(result.stderr)
    if not expect_success and result.returncode == 0:
        raise AssertionError("malformed fixture unexpectedly passed")
    return result


def assert_patched(text: str) -> None:
    assert text.count("autoindex_format json;") == 2, "both files blocks are JSON"
    assert "fancyindex" not in text, "fancyindex is gone"
    assert text.count("index .selkies-no-index;") == 2, "no-index guard in both blocks"
    assert text.count("location SUBFOLDERfiles {") == 2, "block count unchanged"
    assert text.count("add_header Content-Disposition") == 2, "attachment header kept"
    # 对照 location 必须原样保留。
    assert "proxy_pass http://127.0.0.1:8765;" in text, "unrelated block untouched"
    assert text.count("location SUBFOLDERwechat-notifications {") == 1


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    conf = root / "default.conf"
    conf.write_text(FIXTURE, encoding="utf-8")

    first = run(conf)
    assert "patched" in first.stdout, "first run reports patched"
    patched = conf.read_text(encoding="utf-8")
    assert_patched(patched)

    # 二次运行必须幂等：退出码 0、内容不变、输出带 already。
    second = run(conf)
    assert "already patched" in second.stdout, "second run is idempotent"
    assert conf.read_text(encoding="utf-8") == patched, "second run changes nothing"

with tempfile.TemporaryDirectory() as temporary:
    # 上游模板被改动（一处 fancyindex 行被删）→ 必须非零退出且文件未被修改。
    root = Path(temporary)
    conf = root / "default.conf"
    changed = FIXTURE.replace(
        "    fancyindex on;\n    fancyindex_footer SUBFOLDERnginx/footer.html;\n",
        "    fancyindex on;\n",
    )
    conf.write_text(changed, encoding="utf-8")
    before = conf.read_text(encoding="utf-8")
    result = run(conf, expect_success=False)
    assert "re-derive" in result.stderr, "upstream-change failure names the cause"
    assert conf.read_text(encoding="utf-8") == before, "failed run leaves file untouched"

with tempfile.TemporaryDirectory() as temporary:
    # 混合状态：一处已打补丁、一处未打 → 必须非零退出。
    root = Path(temporary)
    conf = root / "default.conf"
    patched_block = FILES_BLOCK.replace(
        "    fancyindex on;\n    fancyindex_footer SUBFOLDERnginx/footer.html;\n"
        "    fancyindex_header SUBFOLDERnginx/header.html;\n"
        "    alias REPLACE_DOWNLOADS_PATH/;",
        "    index .selkies-no-index;\n    autoindex on;\n"
        "    autoindex_format json;\n    alias REPLACE_DOWNLOADS_PATH/;",
    )
    first = FIXTURE.index(FILES_BLOCK)
    second = FIXTURE.index(FILES_BLOCK, first + len(FILES_BLOCK))
    mixed = FIXTURE[:second] + patched_block + FIXTURE[second + len(FILES_BLOCK):]
    conf.write_text(mixed, encoding="utf-8")
    result = run(conf, expect_success=False)
    assert "original=1" in result.stderr, "mixed state reports original=1"

print("files-json-index patch tests: OK (4 cases)")
