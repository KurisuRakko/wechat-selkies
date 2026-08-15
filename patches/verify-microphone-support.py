#!/usr/bin/env python3
"""构建期断言：浏览器→容器的麦克风转发仍是 Selkies 上游原生能力。

本项目**不实现**麦克风转发，也没有任何相关功能代码。完整链路在 Selkies
上游：侧边栏麦克风按钮 getUserMedia 采集 24kHz 单声道 PCM，经数据
WebSocket 以首字节 0x02 发给服务端；selkies.py 用 pulsectl 惰性加载
module-virtual-source，在 "input" sink（由基础镜像启动脚本无条件创建）之上
创建名为 SelkiesVirtualMic 的 PulseAudio 虚拟源；pulsectl/pasimple 依赖已随
Selkies 安装。

这个脚本是纯只读断言（fail closed）：逐字检查上游 selkies.py、settings.py
与活动 dashboard 的 src/selkies-core.js 里支撑上述链路的 token 是否还在。
这些是无稳定性承诺的上游内部实现，且本项目锁定的基础镜像 tag 已被上游冻结，
未来基础镜像迁移可能无声地弄丢该能力——任何一个 token 缺失，构建立即失败，
而不是带着坏掉的麦克风转发静默出镜像。

用法：verify-microphone-support.py [selkies 包目录] [dashboard 根目录]
两个参数都省略时按真实镜像路径（/lsiopy/lib、/usr/share/selkies）定位；
测试脚本用假目录覆盖。本脚本只读，绝不修改任何文件。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


# selkies.py 里支撑虚拟源创建与开关读取的 token，按链路逐一断言：
TOKENS_SELKIES_PY = (
    'virtual_source_name = "SelkiesVirtualMic"',  # 虚拟源名字，微信下拉列表里选它
    'master_monitor = "input.monitor"',           # 虚拟源挂在 "input" sink 的 monitor 上
    '"module-virtual-source"',                    # pulsectl 加载的 PulseAudio 模块
    "pasimple.PA_STREAM_PLAYBACK",                # pasimple 打开回放流的方式
    "settings.microphone_enabled[0]",             # 服务端对浏览器开关的读取点
)

# settings.py 里麦克风开关的注册名：
TOKENS_SETTINGS_PY = ("'name': 'microphone_enabled'",)

# 活动 dashboard 的 core bundle 里侧边栏麦克风按钮/通道的标识：
TOKENS_CORE_JS = ('"microphone"',)


def _force_utf8() -> None:
    # 构建期环境可能是 C locale：PEP 538 的 coercion 依赖系统提供 C.UTF-8，
    # 这里显式重配置 stdout/stderr，保证中文输出不会因编码问题把构建搞挂。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def site_packages() -> str:
    """定位 selkies 包：与 audio-silence-gate.py 完全相同的扫描方式。

    在 /lsiopy/lib 下按版本目录枚举 site-packages/selkies，取排序后第一个。
    """
    candidates = []
    lib_root = "/lsiopy/lib"
    if os.path.isdir(lib_root):
        for dirname in sorted(os.listdir(lib_root)):
            candidate = os.path.join(lib_root, dirname, "site-packages", "selkies")
            if os.path.isdir(candidate):
                candidates.append(candidate)
    if not candidates:
        raise SystemExit("verify-microphone-support: could not locate the selkies package")
    return candidates[0]


def find_active_dashboard(selkies_root: Path) -> Path:
    """在 selkies_root 下消歧出活动 dashboard。

    与 patch-audio-autoplay.py 的 patch_root() 同一手段：枚举 */index.html，
    取唯一同时带有 src/selkies-audio-unlock.js 的目录——那是本项目 Dockerfile
    已打的标记，用于区分 selkies-dashboard 与其它候选目录。
    """
    dashboards = []
    for html in selkies_root.glob("*/index.html"):
        dashboard = html.parent
        if (dashboard / "src" / "selkies-audio-unlock.js").is_file():
            dashboards.append(dashboard)
    if len(dashboards) != 1:
        raise RuntimeError(
            "expected exactly one dashboard carrying src/selkies-audio-unlock.js "
            "under %s, found %d" % (selkies_root, len(dashboards))
        )
    return dashboards[0]


def check_file(path: Path, tokens: tuple[str, ...], label: str) -> int:
    """断言 path 包含全部 token；任一缺失则点名打印到 stderr 并返回 1。"""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as error:
        print(
            "verify-microphone-support: 无法读取 %s（%s）：%s" % (label, path, error),
            file=sys.stderr,
        )
        return 1
    missing = [token for token in tokens if token not in source]
    if missing:
        print(
            "verify-microphone-support: %s（%s）缺失以下上游 token："
            % (label, path),
            file=sys.stderr,
        )
        for token in missing:
            print("  - %s" % token, file=sys.stderr)
        return 1
    return 0


def main() -> int:
    _force_utf8()
    site = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(site_packages())
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/usr/share/selkies")

    failed = 0
    failed += check_file(site / "selkies.py", TOKENS_SELKIES_PY, "selkies.py")
    failed += check_file(site / "settings.py", TOKENS_SETTINGS_PY, "settings.py")
    try:
        dashboard = find_active_dashboard(root)
    except (OSError, RuntimeError) as error:
        print("verify-microphone-support: %s" % error, file=sys.stderr)
        return 1
    failed += check_file(
        dashboard / "src" / "selkies-core.js", TOKENS_CORE_JS, "src/selkies-core.js"
    )
    if failed:
        return 1
    print(
        "verify-microphone-support: 上游麦克风转发链路（虚拟源/开关/按钮）"
        "仍然成立，纯只读检查通过，未做任何修改"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
