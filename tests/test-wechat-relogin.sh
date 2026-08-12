#!/usr/bin/env bash
# wechat-window-watchdog.sh 自动重登路径的纯文本自测：不依赖 X 环境，用
# 桩 xdotool / xprop / xwininfo / pgrep 加一个「世界」文件驱动。
#
# 机制：
#   * FAKE_WORLD 每行一个窗口：id width height modal viewable；
#   * 桩把每次调用原样追加到 FAKE_ACTIONS，并按世界文件应答；
#   * FAKE_CLOSE_MODAL_ON=key|click（默认 none）命中时，桩把世界里 modal
#     行的 viewable 改写为 0，模拟弹窗被关掉；
#   * sleep() 被覆盖为 no-op，全部用例毫秒级跑完；
#   * 每个用例在子 shell 里 export 环境变量后 source 脚本，直接调
#     handle_login_screen（或 find_window），再在父 shell 断言动作与日志。
#
# 本机是 macOS 默认 bash 3.2，禁用 bash4 语法。

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
WATCHDOG="$SCRIPT_DIR/../root/scripts/wechat/wechat-window-watchdog.sh"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/wechat-relogin.XXXXXX")
trap 'rm -rf "$tmp"' EXIT

FAKE_WORLD="$tmp/world"
FAKE_ACTIONS="$tmp/actions"
WATCH_LOG="$tmp/watch.log"
: > "$FAKE_WORLD"
: > "$FAKE_ACTIONS"
: > "$WATCH_LOG"

export FAKE_WORLD FAKE_ACTIONS
FAKE_CLOSE_MODAL_ON=none
FAKE_AUTOLOGIN_RUNNING=0

# 覆盖真 sleep：所有用例全部在毫秒级跑完
sleep() { :; }

STUB_DIR="$tmp/stubs"
mkdir -p "$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

cat > "$STUB_DIR/xdotool" <<'STUB'
#!/bin/sh
echo "xdotool $*" >> "$FAKE_ACTIONS"
cmd="$1"
shift
close_modal() {
    new=$(mktemp "${TMPDIR:-/tmp}/world.XXXXXX")
    while read -r id w h modal viewable; do
        [ -n "$id" ] || continue
        if [ "$modal" = "1" ]; then
            echo "$id $w $h 1 0" >> "$new"
        else
            echo "$id $w $h $modal $viewable" >> "$new"
        fi
    done < "$FAKE_WORLD"
    mv "$new" "$FAKE_WORLD"
}
case "$cmd" in
    search)
        onlyvisible=0
        for a in "$@"; do
            [ "$a" = "--onlyvisible" ] && onlyvisible=1
        done
        while read -r id w h modal viewable; do
            [ -n "$id" ] || continue
            if [ "$onlyvisible" = "1" ] && [ "$viewable" != "1" ]; then
                continue
            fi
            echo "$id"
        done < "$FAKE_WORLD"
        ;;
    getwindowgeometry)
        # xdotool getwindowgeometry --shell <id>，shift 后 $1=--shell $2=<id>
        id="${2:-}"
        while read -r wid w h modal viewable; do
            [ "$wid" = "$id" ] || continue
            echo "WIDTH=$w"
            echo "HEIGHT=$h"
        done < "$FAKE_WORLD"
        ;;
    key)
        [ "${FAKE_CLOSE_MODAL_ON:-none}" = "key" ] && close_modal
        ;;
    mousemove)
        [ "${FAKE_CLOSE_MODAL_ON:-none}" = "click" ] && close_modal
        ;;
esac
exit 0
STUB

cat > "$STUB_DIR/xprop" <<'STUB'
#!/bin/sh
echo "xprop $*" >> "$FAKE_ACTIONS"
# xprop -id <id> _NET_WM_STATE
id="${2:-}"
while read -r wid w h modal viewable; do
    [ "$wid" = "$id" ] || continue
    if [ "$modal" = "1" ]; then
        echo "_NET_WM_STATE(ATOM) = _NET_WM_STATE_MODAL"
    fi
done < "$FAKE_WORLD"
exit 0
STUB

cat > "$STUB_DIR/xwininfo" <<'STUB'
#!/bin/sh
echo "xwininfo $*" >> "$FAKE_ACTIONS"
# xwininfo -id <id>
id="${2:-}"
while read -r wid w h modal viewable; do
    [ "$wid" = "$id" ] || continue
    if [ "$viewable" = "1" ]; then
        echo "  Map State: IsViewable"
    fi
done < "$FAKE_WORLD"
exit 0
STUB

cat > "$STUB_DIR/pgrep" <<'STUB'
#!/bin/sh
echo "pgrep $*" >> "$FAKE_ACTIONS"
# pgrep -f 'wechat-auto-login\.py' 按 FAKE_AUTOLOGIN_RUNNING 应答，
# 其余（如 pgrep -x wechat）一律认为进程存在。
case "$*" in
    *wechat-auto-login*)
        [ "${FAKE_AUTOLOGIN_RUNNING:-0}" = "1" ] && exit 0
        exit 1 ;;
    *)
        exit 0 ;;
esac
STUB

chmod +x "$STUB_DIR/xdotool" "$STUB_DIR/xprop" "$STUB_DIR/xwininfo" "$STUB_DIR/pgrep"

failures=0

fail() {
    failures=$((failures + 1))
    echo "FAIL: $1" >&2
    shift
    for line in "$@"; do
        echo "  $line" >&2
    done
}

# 真正会被执行的操作行（xdotool windowactivate/key/mousemove）
action_lines() {
    grep -E '^xdotool (windowactivate|key |mousemove)' "$FAKE_ACTIONS"
}

assert_actions_eq() {   # <desc> <期望动作文件>
    local desc="$1" expect="$2"
    action_lines > "$tmp/actual-actions"
    if diff -u "$expect" "$tmp/actual-actions" >/dev/null 2>&1; then
        echo "ok - $desc"
    else
        fail "$desc" "期望: $(tr '\n' '|' < "$expect")" "实际: $(tr '\n' '|' < "$tmp/actual-actions")"
    fi
}

assert_no_actions() {   # <desc>
    local desc="$1"
    if action_lines | grep -q .; then
        fail "$desc" "不应有任何动作: $(tr '\n' '|' < <(action_lines))"
    else
        echo "ok - $desc"
    fi
}

assert_log_contains() { # <desc> <固定串>
    local desc="$1" pat="$2"
    if grep -Fq "$pat" "$WATCH_LOG" 2>/dev/null; then
        echo "ok - $desc"
    else
        fail "$desc" "日志缺 '$pat': $(tail -3 "$WATCH_LOG" 2>/dev/null | tr '\n' '|')"
    fi
}

assert_log_count() {    # <desc> <固定串> <期望行数>
    local desc="$1" pat="$2" want="$3" got
    got=$(grep -Fc "$pat" "$WATCH_LOG" 2>/dev/null)
    [ -n "$got" ] || got=0
    if [ "$got" = "$want" ]; then
        echo "ok - $desc"
    else
        fail "$desc" "日志里 '$pat' 应为 $want 行，实际 $got 行"
    fi
}

assert_actions_count() { # <desc> <固定串> <期望行数>
    local desc="$1" pat="$2" want="$3" got
    got=$(grep -Fc "$pat" "$FAKE_ACTIONS" 2>/dev/null)
    [ -n "$got" ] || got=0
    if [ "$got" = "$want" ]; then
        echo "ok - $desc"
    else
        fail "$desc" "动作里 '$pat' 应为 $want 行，实际 $got 行"
    fi
}

begin_case() {
    : > "$FAKE_WORLD"
    : > "$FAKE_ACTIONS"
    : > "$WATCH_LOG"
}

# 每个用例清掉可能从外部环境带进来的 relogin 变量，再按需覆盖
unset_relogin_env() {
    unset ENABLE_WECHAT_AUTO_RELOGIN WECHAT_RELOGIN_MAX_ATTEMPTS \
          WECHAT_RELOGIN_RETRY_DELAY WECHAT_RELOGIN_DRY_RUN
}

# 用例 1：弹窗 + 登录窗都在，弹窗点不掉 → 全序列动作，日志说明弹窗仍在
begin_case
printf '100 560 760 0 1\n200 564 516 1 1\n' > "$FAKE_WORLD"
(
    export ENABLE_WECHAT_WINDOW_WATCHDOG=true WECHAT_WATCHDOG_LIB_ONLY=1
    export WECHAT_WATCHDOG_LOG="$WATCH_LOG" FAKE_CLOSE_MODAL_ON=none
    unset_relogin_env
    . "$WATCHDOG"
    handle_login_screen 100
)
printf '%s\n' \
    'xdotool windowactivate 200' \
    'xdotool key --clearmodifiers Return' \
    'xdotool mousemove --window 200 282 402 click 1' \
    'xdotool windowactivate 100' \
    'xdotool key --clearmodifiers Return' \
    'xdotool mousemove --window 100 280 532 click 1' > "$tmp/expect"
assert_actions_eq "case 1: 弹窗在时按 弹窗→登录窗 顺序动作" "$tmp/expect"
assert_log_contains "case 1: 弹窗坐标点击后仍在" 'modal 200 still up after the click'

# 用例 2：Return 关掉弹窗 → 不再坐标点击弹窗，登录窗序列照常
begin_case
printf '100 560 760 0 1\n200 564 516 1 1\n' > "$FAKE_WORLD"
(
    export ENABLE_WECHAT_WINDOW_WATCHDOG=true WECHAT_WATCHDOG_LIB_ONLY=1
    export WECHAT_WATCHDOG_LOG="$WATCH_LOG" FAKE_CLOSE_MODAL_ON=key
    unset_relogin_env
    . "$WATCHDOG"
    handle_login_screen 100
)
printf '%s\n' \
    'xdotool windowactivate 200' \
    'xdotool key --clearmodifiers Return' \
    'xdotool windowactivate 100' \
    'xdotool key --clearmodifiers Return' \
    'xdotool mousemove --window 100 280 532 click 1' > "$tmp/expect"
assert_actions_eq "case 2: Return 关掉弹窗后不再坐标点击" "$tmp/expect"
assert_log_contains "case 2: 弹窗被 Return 关闭" 'modal 200 closed by Return'

# 用例 3：坐标点击关掉弹窗
begin_case
printf '100 560 760 0 1\n200 564 516 1 1\n' > "$FAKE_WORLD"
(
    export ENABLE_WECHAT_WINDOW_WATCHDOG=true WECHAT_WATCHDOG_LIB_ONLY=1
    export WECHAT_WATCHDOG_LOG="$WATCH_LOG" FAKE_CLOSE_MODAL_ON=click
    unset_relogin_env
    . "$WATCHDOG"
    handle_login_screen 100
)
assert_log_contains "case 3: 弹窗被坐标点击关闭" 'modal 200 closed by the click'
if action_lines | grep -Fq 'mousemove --window 200 282 402'; then
    echo "ok - case 3: 含弹窗坐标点击"
else
    fail "case 3: 缺弹窗坐标点击" "实际: $(tr '\n' '|' < <(action_lines))"
fi

# 用例 4：只有登录窗没有弹窗 → 不出现任何 200 的动作
begin_case
printf '100 560 760 0 1\n' > "$FAKE_WORLD"
(
    export ENABLE_WECHAT_WINDOW_WATCHDOG=true WECHAT_WATCHDOG_LIB_ONLY=1
    export WECHAT_WATCHDOG_LOG="$WATCH_LOG" FAKE_CLOSE_MODAL_ON=none
    unset_relogin_env
    . "$WATCHDOG"
    handle_login_screen 100
)
printf '%s\n' \
    'xdotool windowactivate 100' \
    'xdotool key --clearmodifiers Return' \
    'xdotool mousemove --window 100 280 532 click 1' > "$tmp/expect"
assert_actions_eq "case 4: 无弹窗只点登录窗" "$tmp/expect"

# 用例 5：重试上限 3，连调 5 次 → attempt 恰 3 行、giving up 恰 1 行、点击恰 3 次
begin_case
printf '100 560 760 0 1\n' > "$FAKE_WORLD"
(
    export ENABLE_WECHAT_WINDOW_WATCHDOG=true WECHAT_WATCHDOG_LIB_ONLY=1
    unset_relogin_env
    export WECHAT_WATCHDOG_LOG="$WATCH_LOG" WECHAT_RELOGIN_RETRY_DELAY=0
    export WECHAT_RELOGIN_MAX_ATTEMPTS=3
    . "$WATCHDOG"
    handle_login_screen 100
    handle_login_screen 100
    handle_login_screen 100
    handle_login_screen 100
    handle_login_screen 100
)
assert_log_count "case 5: 重试恰 3 次" 'relogin: attempt ' 3
assert_log_count "case 5: giving up 恰 1 次" 'relogin: giving up' 1
assert_actions_count "case 5: 登录窗点击恰 3 次" 'mousemove --window 100 280 532 click 1' 3

# 用例 6：防抖，RETRY_DELAY=999 连调 3 次 → attempt 恰 1 行
begin_case
printf '100 560 760 0 1\n' > "$FAKE_WORLD"
(
    export ENABLE_WECHAT_WINDOW_WATCHDOG=true WECHAT_WATCHDOG_LIB_ONLY=1
    unset_relogin_env
    export WECHAT_WATCHDOG_LOG="$WATCH_LOG" WECHAT_RELOGIN_RETRY_DELAY=999
    . "$WATCHDOG"
    handle_login_screen 100
    handle_login_screen 100
    handle_login_screen 100
)
assert_log_count "case 6: 防抖只试 1 次" 'relogin: attempt ' 1

# 用例 7：auto-login.py 在跑 → 让路，无任何点击，deferring 只记 1 次
begin_case
printf '100 560 760 0 1\n' > "$FAKE_WORLD"
(
    export ENABLE_WECHAT_WINDOW_WATCHDOG=true WECHAT_WATCHDOG_LIB_ONLY=1
    export WECHAT_WATCHDOG_LOG="$WATCH_LOG" FAKE_AUTOLOGIN_RUNNING=1
    unset_relogin_env
    . "$WATCHDOG"
    handle_login_screen 100
    handle_login_screen 100
    handle_login_screen 100
)
assert_no_actions "case 7: 让路时无任何点击"
assert_log_count "case 7: deferring 只记 1 次" 'deferring to it' 1

# 用例 8：关闭开关 → 无动作、日志无 relogin 行
begin_case
printf '100 560 760 0 1\n' > "$FAKE_WORLD"
(
    export ENABLE_WECHAT_WINDOW_WATCHDOG=true WECHAT_WATCHDOG_LIB_ONLY=1
    unset_relogin_env
    export WECHAT_WATCHDOG_LOG="$WATCH_LOG" ENABLE_WECHAT_AUTO_RELOGIN=false
    . "$WATCHDOG"
    handle_login_screen 100
)
assert_no_actions "case 8: 关闭开关后无动作"
if grep -Fq 'relogin:' "$WATCH_LOG" 2>/dev/null; then
    fail "case 8: 日志不应有 relogin 行" "$(cat "$WATCH_LOG")"
else
    echo "ok - case 8: 日志无 relogin 行"
fi

# 用例 9：dry-run 只记日志不执行动作（只读探测如 search/xwininfo 仍会跑）
begin_case
printf '100 560 760 0 1\n200 564 516 1 1\n' > "$FAKE_WORLD"
(
    export ENABLE_WECHAT_WINDOW_WATCHDOG=true WECHAT_WATCHDOG_LIB_ONLY=1
    unset_relogin_env
    export WECHAT_WATCHDOG_LOG="$WATCH_LOG" WECHAT_RELOGIN_DRY_RUN=true
    . "$WATCHDOG"
    handle_login_screen 100
)
assert_no_actions "case 9: dry-run 不产生任何动作"
assert_log_contains "case 9: dry-run 打印弹窗点击" '[dry-run] xdotool mousemove --window 200 282 402 click 1'
assert_log_contains "case 9: dry-run 打印登录窗点击" '[dry-run] xdotool mousemove --window 100 280 532 click 1'

# 用例 10：giving up 后主窗口回来 → 状态重置，再出现 attempt 1/3
begin_case
printf '100 560 760 0 1\n' > "$FAKE_WORLD"
(
    export ENABLE_WECHAT_WINDOW_WATCHDOG=true WECHAT_WATCHDOG_LIB_ONLY=1
    unset_relogin_env
    export WECHAT_WATCHDOG_LOG="$WATCH_LOG" WECHAT_RELOGIN_RETRY_DELAY=0
    export WECHAT_RELOGIN_MAX_ATTEMPTS=3
    . "$WATCHDOG"
    handle_login_screen 100
    handle_login_screen 100
    handle_login_screen 100
    relogin_note_recovered
    handle_login_screen 100
)
assert_log_contains "case 10: 恢复日志" 'main window is back after 3 attempt(s)'
n_rec=$(grep -Fn 'main window is back after 3 attempt(s)' "$WATCH_LOG" | head -1 | cut -d: -f1)
n_att=$(grep -Fn 'relogin: attempt 1/3' "$WATCH_LOG" | tail -1 | cut -d: -f1)
if [ -n "$n_rec" ] && [ -n "$n_att" ] && [ "$n_rec" -lt "$n_att" ]; then
    echo "ok - case 10: 恢复后重新开始 attempt 1/3"
else
    fail "case 10: 恢复后没有重新开始尝试" "恢复行号=$n_rec 最后 attempt 行号=$n_att"
fi

# 用例 11：find_window 兼容性 —— 默认跳过 modal，want_modal=yes 选中
begin_case
printf '200 564 516 1 1\n' > "$FAKE_WORLD"
(
    unset_relogin_env
    export ENABLE_WECHAT_WINDOW_WATCHDOG=true WECHAT_WATCHDOG_LIB_ONLY=1
    export WECHAT_WATCHDOG_LOG="$WATCH_LOG"
    unset FAKE_CLOSE_MODAL_ON FAKE_AUTOLOGIN_RUNNING
    . "$WATCHDOG"
    if find_window visible 200 200 >/dev/null 2>&1; then
        echo found > "$tmp/fw-nonmodal"
    else
        echo notfound > "$tmp/fw-nonmodal"
    fi
    find_window visible 200 200 yes > "$tmp/fw-modal"
)
if grep -q '^notfound$' "$tmp/fw-nonmodal"; then
    echo "ok - case 11: 默认不选 modal 窗口"
else
    fail "case 11: 默认把 modal 窗口当普通窗口选出来了" "结果: $(cat "$tmp/fw-nonmodal")"
fi
if [ "$(cat "$tmp/fw-modal")" = "200" ]; then
    echo "ok - case 11: want_modal=yes 选中 modal 窗口"
else
    fail "case 11: want_modal=yes 结果应为 200" "结果: $(cat "$tmp/fw-modal")"
fi

if [ "$failures" -gt 0 ]; then
    echo "$failures 个用例失败" >&2
    exit 1
fi

echo "wechat auto-relogin tests passed"
