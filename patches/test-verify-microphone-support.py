#!/usr/bin/env python3
"""Regression tests for patches/verify-microphone-support.py."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


PATCH = Path(__file__).with_name("verify-microphone-support.py")

# 假 fixture：内容只需“包含”各自文件应断言的 token，无需是真实上游源码。
SELKIES_PY = '''
# 假 selkies.py fixture
settings.microphone_enabled[0]
virtual_source_name = "SelkiesVirtualMic"
master_monitor = "input.monitor"
pulse.load_module("module-virtual-source")
pasimple.PA_STREAM_PLAYBACK
'''

SETTINGS_PY = '''
# 假 settings.py fixture
microphone = [
    {'name': 'microphone_enabled', 'default': True},
]
'''

CORE_JS = '''
// 假 selkies-core.js fixture
const mic = {"microphone": true};
'''

# 每个必需 token 及其所在文件（脚本视角的“逻辑文件名”）：
REQUIRED = (
    ("selkies.py", 'virtual_source_name = "SelkiesVirtualMic"'),
    ("selkies.py", 'master_monitor = "input.monitor"'),
    ("selkies.py", '"module-virtual-source"'),
    ("selkies.py", "pasimple.PA_STREAM_PLAYBACK"),
    ("selkies.py", "settings.microphone_enabled[0]"),
    ("settings.py", "'name': 'microphone_enabled'"),
    ("selkies-core.js", '"microphone"'),
)

FIXTURES = {
    "selkies.py": SELKIES_PY,
    "settings.py": SETTINGS_PY,
    "selkies-core.js": CORE_JS,
}


def make_fixture(overrides: dict[str, str] | None = None) -> tuple[Path, Path, Path]:
    """造假的 selkies 包与假 dashboard，返回 (假包目录, 假 dashboard 根, dashboard 目录)。

    假包目录直接作为 argv[1] 喂给脚本——与 test-audio-silence-gate.py 的机制
    相同：site_packages() 在真实镜像里定位的是
    /lsiopy/lib/<版本>/site-packages/selkies 那一层，这里用临时目录代替。
    argv[2] 是 dashboard 根目录，脚本靠 src/selkies-audio-unlock.js 标记消歧。
    """
    if overrides is None:
        overrides = {}
    site = Path(tempfile.mkdtemp(prefix="selkies-mic-verify-"))
    (site / "selkies.py").write_text(
        overrides.get("selkies.py", SELKIES_PY), encoding="utf-8"
    )
    (site / "settings.py").write_text(
        overrides.get("settings.py", SETTINGS_PY), encoding="utf-8"
    )

    root = Path(tempfile.mkdtemp(prefix="selkies-dashboard-verify-"))
    dashboard = root / "selkies-dashboard"
    (dashboard / "src").mkdir(parents=True)
    (dashboard / "index.html").write_text(
        "<!doctype html><html><body></body></html>", encoding="utf-8"
    )
    # Dockerfile 注入的消歧标记，find_active_dashboard 靠它选中这个目录。
    (dashboard / "src" / "selkies-audio-unlock.js").write_text(
        "// audio gate marker", encoding="utf-8"
    )
    (dashboard / "src" / "selkies-core.js").write_text(
        overrides.get("selkies-core.js", CORE_JS), encoding="utf-8"
    )
    return site, root, dashboard


def run(site: Path, root: Path) -> subprocess.CompletedProcess[str]:
    # 与 test-audio-silence-gate.py 相同：两端都钉死 UTF-8，避免子进程在
    # cp936 控制台下用系统编码写 stderr 导致解码失败。
    return subprocess.run(
        [sys.executable, str(PATCH), str(site), str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )


# 1. 全部 token 齐备：退出 0、stdout 含「仍然成立」，且三个文件都未被改写
#    （脚本必须是纯只读的）。
site, root, dashboard = make_fixture()
site_py_before = (site / "selkies.py").read_text(encoding="utf-8")
settings_before = (site / "settings.py").read_text(encoding="utf-8")
core_before = (dashboard / "src" / "selkies-core.js").read_text(encoding="utf-8")

ok = run(site, root)
assert ok.returncode == 0, ok.stderr
assert "仍然成立" in ok.stdout, ok.stdout
assert (site / "selkies.py").read_text(encoding="utf-8") == site_py_before, \
    "断言脚本不得改写 selkies.py"
assert (site / "settings.py").read_text(encoding="utf-8") == settings_before, \
    "断言脚本不得改写 settings.py"
assert (dashboard / "src" / "selkies-core.js").read_text(encoding="utf-8") == core_before, \
    "断言脚本不得改写 selkies-core.js"

# 2. 逐个删除每个必需 token（每次从干净 fixture 出发）：退出非 0，且 stderr
#    点名该具体缺失 token。
for filename, token in REQUIRED:
    missing_source = FIXTURES[filename].replace(token, "", 1)
    assert missing_source != FIXTURES[filename], "fixture 里找不到 token: %r" % token
    site, root, _ = make_fixture({filename: missing_source})
    bad = run(site, root)
    assert bad.returncode != 0, "缺失 token %r 必须让构建失败" % token
    assert token in bad.stderr, \
        "stderr 必须点名缺失的 token %r，实际输出：%s" % (token, bad.stderr)

print("verify-microphone-support patch tests passed")
