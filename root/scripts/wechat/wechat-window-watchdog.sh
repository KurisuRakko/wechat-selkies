#!/bin/bash
# Keep the WeChat main window present and maximized, and relaunch WeChat if it
# exits.
#
# Three situations that look identical to the user but need different handling:
#
#   1. The process is gone (crashed, or quit from the tray). Relaunch it and
#      re-run auto-login so the session comes back without a manual click.
#   2. The process is alive but no main window is mapped — WeChat hides to the
#      tray when its window is closed rather than exiting. Map it back.
#   3. The main window is mapped but not filling the screen. Maximize it.
#
# Two properties of WeChat make the obvious implementations of 2 and 3 wrong.
# Both were observed on a live container:
#
#   * _NET_WM_STATE LIES. The openbox <maximized>yes</maximized> rule makes
#     openbox set _NET_WM_STATE_MAXIMIZED_VERT/HORZ as the window appears, but Qt
#     then resizes the window back to its own remembered geometry without the
#     atoms ever being cleared. A 560x760 window on a 3232x2048 screen was seen
#     carrying both maximized atoms. So maximization is judged from GEOMETRY, and
#     `wmctrl -b add,...` on its own is a no-op once the atoms are set — which is
#     exactly why the previous version of this script silently did nothing. The
#     state has to be removed and re-added.
#
#   * WeChat owns several confusable top-levels: an unmapped "ghost" window
#     titled 微信 with no WM_CLASS at all, a login/QR window (~560x760), modal
#     dialogs (~564x516, carrying _NET_WM_STATE_MODAL), and separate WeChatAppEx
#     windows for mini-programs whose WM_CLASS also contains "wechat". Window ids
#     are recreated across logins, so nothing may be cached. Matching on the
#     window TITLE — which the previous version did — is unreliable: it alternates
#     between "WeChat" and "微信" and the ghost window shares it.
#
# Selection is therefore: WM_CLASS exactly "wechat" (anchored, since unanchored
# also matches WeChatAppEx), visible, non-modal, largest by area, with an
# absolute size floor so the login window and dialogs can never be picked.
#
#   4. The process is alive, no main window is mapped, and a login window
#      (~560x760) is up, possibly covering a _NET_WM_STATE_MODAL dialog
#      (~564x516) that the server shows after a forced logout. WeChat will not
#      recover on its own, so the watchdog clicks the dialog away and the login
#      button (see handle_login_screen below).
#
# Why the auto-relogin clicks use XTEST instead of `xdotool key --window`:
# Qt ignores XSendEvent, so synthetic key events are dropped before they reach
# the widget; XTEST events are indistinguishable from real input.
#
# Why the modal check between the two dismissal steps matters: the dialog may
# close from the Return press, and then a coordinate click would land on the
# login window underneath instead.
#
# Why deferring to `pgrep wechat-auto-login.py` rather than a timestamp: the
# one-shot login helper covers the launch moment only, and observing the other
# clicker directly covers all four places that can raise the login screen.

if [ "${ENABLE_WECHAT_WINDOW_WATCHDOG:-true}" != "true" ]; then
    exit 0
fi

INTERVAL="${WECHAT_WINDOW_CHECK_INTERVAL:-5}"
FORCE_MAX="${WECHAT_FORCE_MAXIMIZED:-true}"
export DISPLAY="${DISPLAY:-:1}"

case "$INTERVAL" in
    ''|*[!0-9]*) INTERVAL=5 ;;
esac
[ "$INTERVAL" -lt 2 ] && INTERVAL=2

# Anchored so the WeChatAppEx mini-program runtime is not matched.
WECHAT_CLASS='^wechat$'
# Only the MAIN window is ever touched. The login/QR window is ~560x760 and modal
# dialogs ~564x516, so requiring both axes above this floor means neither they nor
# any other WeChat window can be picked up and resized.
MIN_W=600
MIN_H=600
# WeChat's systray icon is a 24x24 override-redirect window that also carries
# WM_CLASS "wechat", and xdotool --onlyvisible reports it as visible (it is
# IsViewable, reparented into stalonetray). Anything at or below this size is
# therefore chrome, not a window the user can see or interact with — used to tell
# "the login screen is up" apart from "everything is hidden in the tray".
MIN_REAL_W=200
MIN_REAL_H=200
# Counts as maximized at this fraction of the screen. Not 100%: openbox runs with
# noStrut so a maximized window is full-screen, but leave slack for rounding.
MAX_W_PCT=95
MAX_H_PCT=92

# 强制登出自愈。登录窗和提示弹窗都靠 MIN_REAL_* 这道下限与 24x24 托盘图标区分，
# 与主窗口识别共用同一套窗口选择逻辑。
AUTO_RELOGIN="${ENABLE_WECHAT_AUTO_RELOGIN:-true}"
RELOGIN_MAX_ATTEMPTS="${WECHAT_RELOGIN_MAX_ATTEMPTS:-3}"
RELOGIN_RETRY_DELAY="${WECHAT_RELOGIN_RETRY_DELAY:-30}"
RELOGIN_DRY_RUN="${WECHAT_RELOGIN_DRY_RUN:-false}"    # 排障用：只记日志不点击
case "$RELOGIN_MAX_ATTEMPTS" in ''|*[!0-9]*) RELOGIN_MAX_ATTEMPTS=3 ;; esac
case "$RELOGIN_RETRY_DELAY"  in ''|*[!0-9]*) RELOGIN_RETRY_DELAY=30 ;; esac

# 按钮相对位置（窗口宽高的百分比）。弹窗「我知道了」与登录窗「进入WeChat」都在
# 水平居中位置；70% 这个纵向比例沿用 wechat-auto-login.py 已验证的值。
MODAL_BTN_Y_PCT=78
LOGIN_BTN_Y_PCT=70
RELOGIN_STEP_DELAY=1     # 关掉弹窗到点登录按钮之间的间隔

RELOGIN_EPISODE=0        # 1 = 当前正处于一段登录屏
RELOGIN_ATTEMPTS=0
RELOGIN_LAST_TS=0
RELOGIN_DEFERRED=0       # 已经因 auto-login.py 在跑而记过一次日志

LOG="${WECHAT_WATCHDOG_LOG:-/config/.wechat-watchdog.log}"
LOG_MAX_BYTES=262144

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG" 2>/dev/null
    if [ "${LOG_ECHO:-0}" = "1" ]; then
        printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
    fi
    if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt "$LOG_MAX_BYTES" ]; then
        tail -n 500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" 2>/dev/null
    fi
}

screen_size() {
    xdpyinfo 2>/dev/null | sed -n 's/^  dimensions: *\([0-9]*\)x\([0-9]*\).*/\1 \2/p' | head -1
}

win_geom() {
    # echoes "W H "
    xdotool getwindowgeometry --shell "$1" 2>/dev/null \
        | sed -n -e 's/^WIDTH=\([0-9]*\)$/\1/p' -e 's/^HEIGHT=\([0-9]*\)$/\1/p' \
        | tr '\n' ' '
}

is_modal() {
    xprop -id "$1" _NET_WM_STATE 2>/dev/null | grep -q '_NET_WM_STATE_MODAL'
}

# Largest class=wechat window at or above a size floor.
# $1: "visible" restricts to mapped windows; "any" includes unmapped ones, which
#     is what tray recovery needs.
# $2/$3: minimum width/height, defaulting to the main-window floor.
# $4: "yes" inverts the modal test and returns only modal windows — used by the
#     auto-relogin path to find the forced-logout dialog.
find_window() {
    local scope="$1" min_w="${2:-$MIN_W}" min_h="${3:-$MIN_H}" want_modal="${4:-no}"
    local search_args wid w h area best best_area
    if [ "$scope" = "visible" ]; then
        search_args="--onlyvisible"
    else
        search_args=""
    fi
    best=""
    best_area=0
    for wid in $(xdotool search $search_args --class "$WECHAT_CLASS" 2>/dev/null); do
        if [ "$want_modal" = "yes" ]; then
            is_modal "$wid" || continue
        else
            is_modal "$wid" && continue
        fi
        set -- $(win_geom "$wid")
        w="$1"; h="$2"
        [ -n "$w" ] && [ -n "$h" ] || continue
        [ "$w" -ge "$min_w" ] && [ "$h" -ge "$min_h" ] || continue
        area=$((w * h))
        if [ "$area" -gt "$best_area" ]; then
            best_area="$area"
            best="$wid"
        fi
    done
    [ -n "$best" ] || return 1
    printf '%s' "$best"
}

geometry_is_maximized() {
    local wid="$1" sw="$2" sh="$3" w h
    set -- $(win_geom "$wid")
    w="$1"; h="$2"
    [ -n "$w" ] && [ -n "$h" ] || return 1
    [ "$((w * 100 / sw))" -ge "$MAX_W_PCT" ] && [ "$((h * 100 / sh))" -ge "$MAX_H_PCT" ]
}

maximize() {
    local wid="$1" sw="$2" sh="$3"
    log "maximize: window $wid is $(win_geom "$wid")on ${sw}x${sh}; toggling _NET_WM_STATE"

    # Remove before add: with the atoms already set a bare add does nothing.
    wmctrl -i -r "$wid" -b remove,maximized_vert,maximized_horz 2>/dev/null
    sleep 0.3
    wmctrl -i -r "$wid" -b add,maximized_vert,maximized_horz 2>/dev/null
    sleep 0.7

    if geometry_is_maximized "$wid" "$sw" "$sh"; then
        log "maximize: ok via wmctrl ($(win_geom "$wid"))"
        return 0
    fi

    # Qt ignored the window manager; resize it directly.
    log "maximize: wmctrl left it at $(win_geom "$wid"), forcing an explicit resize"
    xdotool windowmove "$wid" 0 0 2>/dev/null
    xdotool windowsize "$wid" "$sw" "$sh" 2>/dev/null
    sleep 0.5

    if geometry_is_maximized "$wid" "$sw" "$sh"; then
        log "maximize: ok via explicit resize ($(win_geom "$wid"))"
    else
        log "maximize: FAILED, still $(win_geom "$wid") — WeChat is overriding both paths"
    fi
}

window_is_viewable() {
    xwininfo -id "$1" 2>/dev/null | grep -q "IsViewable"
}

# 所有点击/按键的唯一出口：正常模式记录并执行，dry-run 只记录。
x_action() {
    if [ "$RELOGIN_DRY_RUN" = "true" ]; then
        log "relogin: [dry-run] $*"
        return 0
    fi
    log "relogin: $*"
    "$@" >/dev/null 2>&1
}

relogin_reset() {
    RELOGIN_EPISODE=0; RELOGIN_ATTEMPTS=0; RELOGIN_LAST_TS=0; RELOGIN_DEFERRED=0
}

# 主窗口回来了：把这段登录屏收尾，计数清零。
relogin_note_recovered() {
    [ "$RELOGIN_EPISODE" = "1" ] || return 0
    log "relogin: main window is back after ${RELOGIN_ATTEMPTS} attempt(s), state reset"
    relogin_reset
}

# 「我知道了」。Qt 丢弃 XSendEvent，所以先 activate + XTEST Return；没关掉再按
# 坐标点。两步之间必须校验存活，否则弹窗已关时坐标点击会穿透到下层登录窗。
dismiss_modal() {
    local wid="$1" w h
    set -- $(win_geom "$wid"); w="$1"; h="$2"
    [ -n "$w" ] && [ -n "$h" ] || { log "relogin: modal $wid has no geometry"; return 1; }
    x_action xdotool windowactivate "$wid"
    sleep 0.3
    x_action xdotool key --clearmodifiers Return
    sleep 0.4
    if ! window_is_viewable "$wid"; then
        log "relogin: modal $wid closed by Return"
        return 0
    fi
    x_action xdotool mousemove --window "$wid" $((w / 2)) $((h * MODAL_BTN_Y_PCT / 100)) click 1
    sleep 0.4
    if window_is_viewable "$wid"; then
        log "relogin: modal $wid still up after the click"   # 不致命，继续点登录窗
    else
        log "relogin: modal $wid closed by the click"
    fi
}

# 「进入WeChat」。顺序与 wechat-auto-login.py 一致。
click_login_button() {
    local wid="$1" w h
    set -- $(win_geom "$wid"); w="$1"; h="$2"
    [ -n "$w" ] && [ -n "$h" ] || { log "relogin: login window $wid has no geometry"; return 1; }
    x_action xdotool windowactivate "$wid"
    sleep 0.3
    x_action xdotool key --clearmodifiers Return
    x_action xdotool mousemove --window "$wid" $((w / 2)) $((h * LOGIN_BTN_Y_PCT / 100)) click 1
}

relogin_attempt() {
    local login_wid="$1" modal_wid
    if modal_wid=$(find_window visible "$MIN_REAL_W" "$MIN_REAL_H" yes); then
        dismiss_modal "$modal_wid"
        sleep "$RELOGIN_STEP_DELAY"
        # 弹窗关掉后窗口 id 可能重建，重新定位登录窗。
        login_wid=$(find_window visible "$MIN_REAL_W" "$MIN_REAL_H") || {
            log "relogin: login window is gone after dismissing the modal"
            return 0
        }
    fi
    click_login_button "$login_wid"
}

# 巡检循环里「主窗口不在、登录窗在场」时的处理。防抖、上限、让路都在这里。
handle_login_screen() {
    local wid="$1" now
    [ "$AUTO_RELOGIN" = "true" ] || return 0
    if [ "$RELOGIN_EPISODE" != "1" ]; then
        RELOGIN_EPISODE=1; RELOGIN_ATTEMPTS=0; RELOGIN_LAST_TS=0; RELOGIN_DEFERRED=0
        log "relogin: login screen is up (window $wid $(win_geom "$wid")), main window gone"
    fi
    # 一次性的 wechat-auto-login.py 也会点同一个窗口，让它先来。
    if pgrep -f 'wechat-auto-login\.py' >/dev/null 2>&1; then
        if [ "$RELOGIN_DEFERRED" != "1" ]; then
            RELOGIN_DEFERRED=1
            log "relogin: wechat-auto-login.py is running, deferring to it"
        fi
        return 0
    fi
    [ "$RELOGIN_ATTEMPTS" -ge "$RELOGIN_MAX_ATTEMPTS" ] && return 0
    now=$(date +%s)
    if [ "$RELOGIN_LAST_TS" -gt 0 ] && [ $((now - RELOGIN_LAST_TS)) -lt "$RELOGIN_RETRY_DELAY" ]; then
        return 0
    fi
    RELOGIN_ATTEMPTS=$((RELOGIN_ATTEMPTS + 1))
    RELOGIN_LAST_TS="$now"
    log "relogin: attempt ${RELOGIN_ATTEMPTS}/${RELOGIN_MAX_ATTEMPTS}"
    relogin_attempt "$wid"
    if [ "$RELOGIN_ATTEMPTS" -ge "$RELOGIN_MAX_ATTEMPTS" ]; then
        log "relogin: giving up after ${RELOGIN_MAX_ATTEMPTS} attempts; this is most likely the QR screen and needs a phone scan. No further clicks until the main window returns or WeChat is relaunched."
    fi
}

# 手动排障：/scripts/wechat/wechat-window-watchdog.sh --relogin-once
# 只对当前登录屏跑一次动作序列，不进循环；配合 WECHAT_RELOGIN_DRY_RUN=true
# 就只打印将要执行的命令。
if [ "${1:-}" = "--relogin-once" ]; then
    LOG_ECHO=1
    if wid=$(find_window visible "$MIN_REAL_W" "$MIN_REAL_H"); then
        handle_login_screen "$wid"
    else
        log "relogin: --relogin-once found no login window"
    fi
    exit 0
fi

# 纯文本自测用 WECHAT_WATCHDOG_LIB_ONLY=1 source 本文件，只取上面的函数。
if [ "${WECHAT_WATCHDOG_LIB_ONLY:-0}" = "1" ]; then
    return 0 2>/dev/null || exit 0
fi

log "watchdog started (interval ${INTERVAL}s, force_maximized=${FORCE_MAX}, auto_relogin=${AUTO_RELOGIN}, relogin_max=${RELOGIN_MAX_ATTEMPTS}, relogin_delay=${RELOGIN_RETRY_DELAY}s, dry_run=${RELOGIN_DRY_RUN}, class=${WECHAT_CLASS})"

while true; do
    sleep "$INTERVAL"

    if ! pgrep -x wechat >/dev/null 2>&1; then
        log "process: wechat is gone, relaunching"
        nohup /usr/bin/wechat >/dev/null 2>&1 &
        if [ "${ENABLE_WECHAT_AUTO_LOGIN:-true}" = "true" ]; then
            nohup /lsiopy/bin/python3 /scripts/wechat/wechat-auto-login.py >/dev/null 2>&1 &
        fi
        relogin_reset   # 全新进程，上一段登录屏的计数作废
        sleep 10   # give it time to map a window before judging it
        continue
    fi

    set -- $(screen_size)
    SW="$1"; SH="$2"
    if [ -z "$SW" ] || [ -z "$SH" ] || [ "$SW" -le 0 ]; then
        continue   # no display yet
    fi

    if wid=$(find_window visible); then
        relogin_note_recovered
        if [ "$FORCE_MAX" = "true" ] && ! geometry_is_maximized "$wid" "$SW" "$SH"; then
            maximize "$wid" "$SW" "$SH"
        fi
        continue
    fi

    # No main window mapped. If a real (non-tray-sized) WeChat window is visible,
    # that is the login/QR screen — handle_login_screen runs the auto-relogin
    # sequence for it. auto-login.py only covers the launch moment; self-healing
    # on a mid-session forced logout is this loop's job.
    #
    # The size floor here is load-bearing: WeChat's 24x24 systray icon also has
    # WM_CLASS "wechat" and xdotool --onlyvisible reports it as visible, so a bare
    # "is anything visible?" test is satisfied forever and the recovery below never
    # runs. That was the actual reason closing the window did not restore it.
    if login_wid=$(find_window visible "$MIN_REAL_W" "$MIN_REAL_H"); then
        handle_login_screen "$login_wid"
        continue
    fi

    # Only the tray icon is left: WeChat unmapped its main window rather than
    # exiting. Verified on a live container that the window survives as
    # IsUnMapped and that re-running /usr/bin/wechat does NOT bring it back, so
    # windowmap is the recovery.
    if hidden=$(find_window any); then
        log "window: only the tray icon is visible, mapping $hidden back"
        xdotool windowmap "$hidden" 2>/dev/null
        xdotool windowactivate "$hidden" 2>/dev/null
        sleep 1
        if xwininfo -id "$hidden" 2>/dev/null | grep -q "IsViewable"; then
            log "window: $hidden restored ($(win_geom "$hidden"))"
        else
            log "window: windowmap did not stick for $hidden"
        fi
        if [ "$FORCE_MAX" = "true" ] && ! geometry_is_maximized "$hidden" "$SW" "$SH"; then
            maximize "$hidden" "$SW" "$SH"
        fi
    else
        log "window: nothing visible and no hidden main window found"
    fi
done
