#!/usr/bin/env bash
# wechat-window-watchdog.sh 的 screen_size() 纯文本自测：不依赖真实 X
# 环境，用桩 xrandr / xdpyinfo 输出 canned 文本驱动，和
# tests/test-wechat-relogin.sh 同一套约定（PATH 桩、
# WECHAT_WATCHDOG_LIB_ONLY=1 source 脚本、父 shell 里断言）。
#
# 背景：副屏（second_display 功能）接入后，xdpyinfo 报的是
# primary+display2 合并后的 framebuffer 尺寸，主窗口正常最大化到 primary
# 时相对合并 fb 占比骤降，watchdog 的 maximize() 因此永远判"未最大化"、
# 每 5 秒重新执行一遍——这是"主屏画面一直闪"的主因。screen_size() 改为
# 优先取 xrandr --listmonitors 里 selkies-primary 这块逻辑显示器的真实
# 几何，没有才退回原来的 xdpyinfo 合并尺寸。
#
# 本机是 macOS 默认 bash 3.2，禁用 bash4 语法。

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
WATCHDOG="$SCRIPT_DIR/../root/scripts/wechat/wechat-window-watchdog.sh"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/wechat-screen-size.XXXXXX")
trap 'rm -rf "$tmp"' EXIT

# 两个桩各自的"canned 输出"文件，每个用例开始前重写，桩原样 cat 出去。
FAKE_LISTMONITORS="$tmp/listmonitors.txt"
FAKE_XDPYINFO="$tmp/xdpyinfo.txt"
: > "$FAKE_LISTMONITORS"
: > "$FAKE_XDPYINFO"
export FAKE_LISTMONITORS FAKE_XDPYINFO

STUB_DIR="$tmp/stubs"
mkdir -p "$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

cat > "$STUB_DIR/xrandr" <<'STUB'
#!/bin/sh
# 只关心 --listmonitors；其它子命令（本测试用不到）原样返回空，不报错。
cat "$FAKE_LISTMONITORS"
exit 0
STUB

cat > "$STUB_DIR/xdpyinfo" <<'STUB'
#!/bin/sh
cat "$FAKE_XDPYINFO"
exit 0
STUB

chmod +x "$STUB_DIR/xrandr" "$STUB_DIR/xdpyinfo"

failures=0

fail() {
    failures=$((failures + 1))
    echo "FAIL: $1" >&2
    shift
    for line in "$@"; do
        echo "  $line" >&2
    done
}

assert_eq() {   # <desc> <期望> <实际>
    local desc="$1" expect="$2" actual="$3"
    if [ "$expect" = "$actual" ]; then
        echo "ok - $desc"
    else
        fail "$desc" "期望: '$expect'" "实际: '$actual'"
    fi
}

call_screen_size() {
    (
        export WECHAT_WATCHDOG_LIB_ONLY=1
        . "$WATCHDOG"
        screen_size
    )
}

# 用例 1：xrandr --listmonitors 里有 selkies-primary —— 取它的真实几何，
# 不理会 xdpyinfo（xdpyinfo 特意写成一个明显不同的尺寸，用来证明它没被读）。
printf 'Monitors: 2\n 0: selkies-primary 1024/271x768/203+0+0  screen\n 1: selkies-display2 800/0x600/0+1024+0  screen\n' \
    > "$FAKE_LISTMONITORS"
printf '  dimensions:    7008x2048 pixels (1854x542 millimeters)\n' > "$FAKE_XDPYINFO"
assert_eq "case 1: 有 selkies-primary 时取它的几何" "1024 768" "$(call_screen_size)"

# 用例 2：xrandr --listmonitors 没有 selkies-primary（副屏功能关闭，或者
# RandR 还没建立）—— 退回 xdpyinfo 的合并尺寸，和引入本功能之前行为一致。
printf 'Monitors: 1\n 0: +screen 1024/271x768/203+0+0  screen\n' > "$FAKE_LISTMONITORS"
printf '  dimensions:    1024x768 pixels (271x203 millimeters)\n' > "$FAKE_XDPYINFO"
assert_eq "case 2: 无 selkies-primary 退回 xdpyinfo" "1024 768" "$(call_screen_size)"

# 用例 3：selkies-primary 名字前带 * 前缀（RandR 把它标记成 primary
# monitor 的写法）—— 依然要能正确解析出宽高。
printf 'Monitors: 2\n 0: *selkies-primary 4064/0x2624/0+0+0  screen\n 1: selkies-display2 2944/0x1952/0+4064+0  screen\n' \
    > "$FAKE_LISTMONITORS"
printf '  dimensions:    7008x2624 pixels (1854x694 millimeters)\n' > "$FAKE_XDPYINFO"
assert_eq "case 3: 名字带 * 前缀也能正确解析" "4064 2624" "$(call_screen_size)"

# 用例 4：生产实测的真实一行（不带任何前缀），确认宽高解析对应关系没有
# 錯位（宽在第一个 / 前，高在 x 之后、第二个 / 之前）。
printf 'Monitors: 2\n 0: selkies-primary 1024/271x768/203+0+0  screen\n 1: selkies-display2 800/0x600/0+1024+0  screen\n' \
    > "$FAKE_LISTMONITORS"
printf '' > "$FAKE_XDPYINFO"
assert_eq "case 4: 宽高顺序正确（宽 1024，高 768，不是反过来）" "1024 768" "$(call_screen_size)"

if [ "$failures" -gt 0 ]; then
    echo "$failures 个用例失败" >&2
    exit 1
fi

echo "wechat watchdog screen_size tests passed"
