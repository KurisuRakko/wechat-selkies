#!/usr/bin/env python3
"""构建期断言：本项目"副屏窗口管家"依赖的上游多显示器机制仍然成立。

root/scripts/second_display 完全建立在 Selkies 上游已有的多显示器能力之上
（不修改 selkies.py/selkies-core.js），具体依赖三条无稳定性承诺的上游内部
实现细节：

  * settings.py 里 second_screen 设置项仍然注册、且默认仍是 True——否则
    浏览器端第二块显示器这条能力本身可能已被默认关闭；
  * selkies.py 的 reconfigure_displays() 仍然把逻辑显示器命名成
    f"selkies-{display_id}"——x11.py 的 list_monitors() 靠这个前缀识别哪些
    RandR 显示器是 selkies 自己建出来的；
  * selkies-core.js 仍然用 .startsWith("#display2") 解析 URL hash 判断
    "这是副屏窗口"——patches/wechat-second-display.js 的 #display2 被动模式
    判断和它是同一套约定，一旦上游改了 hash 格式，两边就会各说各话。

这是纯只读断言（fail closed），逐字检查上述 token 是否还在。这些都是本项目
锁定的基础镜像 tag 里实测存在的原文（构建 wechat-selkies:latest 时用
--entrypoint bash 直接从镜像里 grep 出来核对过），未来基础镜像迁移可能无声
地改掉其中任何一处——任何一个 token 缺失，构建立即失败，而不是带着一个
"看起来能用、实际上副屏机制已经跑偏"的镜像静默出镜像。

用法：verify-second-display-support.py [selkies 包目录] [dashboard 根目录]
两个参数都省略时按真实镜像路径（/lsiopy/lib、/usr/share/selkies）定位；
测试脚本用假目录覆盖。本脚本只读，绝不修改任何文件。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


# selkies.py 里落地逻辑显示器命名的那一行，与 x11.py 的 MONITOR_NAME_PREFIX
# 依赖的正是这同一个前缀。
TOKENS_SELKIES_PY = ('monitor_name = f"selkies-{display_id}"',)

# settings.py 里 second_screen 设置项的注册：名字、类型、默认值一起断言，
# 默认值一旦从 True 变成 False，副屏功能对新部署就会默认不可用。
TOKENS_SETTINGS_PY = ("'name': 'second_screen', 'type': 'bool', 'default': True",)

# 活动 dashboard 的 core bundle 里解析 #display2 URL hash 的判断。
TOKENS_CORE_JS = ('.startsWith("#display2")',)


def _force_utf8() -> None:
    # 构建期环境可能是 C locale：PEP 538 的 coercion 依赖系统提供 C.UTF-8，
    # 这里显式重配置 stdout/stderr，保证中文输出不会因编码问题把构建搞挂。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def site_packages() -> str:
    """定位 selkies 包：与 verify-microphone-support.py 完全相同的扫描方式。

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
        raise SystemExit("verify-second-display-support: could not locate the selkies package")
    return candidates[0]


def find_active_dashboard(selkies_root: Path) -> Path:
    """在 selkies_root 下消歧出活动 dashboard。

    与 verify-microphone-support.py 的 find_active_dashboard() 同一手段：
    枚举 */index.html，取唯一同时带有 src/selkies-audio-unlock.js 的目
    录——那是本项目 Dockerfile 已打的标记，用于区分 selkies-dashboard 与
    其它候选目录。
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
            "verify-second-display-support: 无法读取 %s（%s）：%s" % (label, path, error),
            file=sys.stderr,
        )
        return 1
    missing = [token for token in tokens if token not in source]
    if missing:
        print(
            "verify-second-display-support: %s（%s）缺失以下上游 token："
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
        print("verify-second-display-support: %s" % error, file=sys.stderr)
        return 1
    failed += check_file(
        dashboard / "src" / "selkies-core.js", TOKENS_CORE_JS, "src/selkies-core.js"
    )
    if failed:
        return 1
    print(
        "verify-second-display-support: 上游多显示器机制（second_screen 设置、"
        "selkies-{display_id} 命名、#display2 hash 解析）仍然成立，"
        "纯只读检查通过，未做任何修改"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
