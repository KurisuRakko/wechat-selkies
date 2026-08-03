#!/bin/bash
# Keep the WeChat main window present and maximized.
#
# Three distinct situations, which look the same to the user but need different
# handling:
#
#   1. The process is gone (crashed, or quit from the tray). Relaunch it, and
#      re-run auto-login so the session is restored without a manual click.
#   2. The process is alive but the main window is unmapped — WeChat hides to the
#      tray when its window is closed rather than exiting. Map it back.
#   3. The window is mapped but not maximized (WeChat remembers its own geometry
#      and openbox rules only apply when a window first appears). Maximize it.
#
# Deliberately does NOT touch anything but the main window: settings dialogs,
# image viewers and mini-program windows are all separate top-levels, and
# maximizing those would be actively annoying. They are excluded by matching the
# main window's title exactly.

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

# WeChat's main window title has changed across releases (微信(测试版), WeChat
# Beta, Weixin, and WeChat on 4.1.x), and the login window uses the same title as
# the main one while being much smaller. So match on title AND require the window
# to be larger than the login/QR window, which is under 600px in both axes.
MAIN_TITLES='^(WeChat|Weixin|微信|微信（测试版）|WeChat Beta)$'
MIN_W=600
MIN_H=600

echo "👁️ WeChat window watchdog started (interval ${INTERVAL}s, force_maximized=${FORCE_MAX})"

find_main_window() {
    # Echoes the window id of the main window, or nothing.
    local wid name geom w h
    for wid in $(xdotool search --onlyvisible --name '.' 2>/dev/null); do
        name=$(xdotool getwindowname "$wid" 2>/dev/null) || continue
        printf '%s' "$name" | grep -qE "$MAIN_TITLES" || continue
        geom=$(xdotool getwindowgeometry --shell "$wid" 2>/dev/null) || continue
        w=$(printf '%s\n' "$geom" | sed -n 's/^WIDTH=\([0-9]*\)$/\1/p')
        h=$(printf '%s\n' "$geom" | sed -n 's/^HEIGHT=\([0-9]*\)$/\1/p')
        [ -n "$w" ] && [ -n "$h" ] || continue
        if [ "$w" -ge "$MIN_W" ] && [ "$h" -ge "$MIN_H" ]; then
            printf '%s' "$wid"
            return 0
        fi
    done
    return 1
}

# Any window carrying the main title, regardless of size — used to tell "logged
# out, showing the small login window" apart from "hidden to tray".
find_any_window() {
    local wid name
    for wid in $(xdotool search --name '.' 2>/dev/null); do
        name=$(xdotool getwindowname "$wid" 2>/dev/null) || continue
        if printf '%s' "$name" | grep -qE "$MAIN_TITLES"; then
            printf '%s' "$wid"
            return 0
        fi
    done
    return 1
}

is_maximized() {
    xprop -id "$1" _NET_WM_STATE 2>/dev/null \
        | grep -q "_NET_WM_STATE_MAXIMIZED_HORZ.*_NET_WM_STATE_MAXIMIZED_VERT\|_NET_WM_STATE_MAXIMIZED_VERT.*_NET_WM_STATE_MAXIMIZED_HORZ"
}

while true; do
    sleep "$INTERVAL"

    if ! pgrep -x wechat >/dev/null 2>&1; then
        echo "⚠️ WeChat process is gone; relaunching"
        nohup /usr/bin/wechat >/dev/null 2>&1 &
        if [ "${ENABLE_WECHAT_AUTO_LOGIN:-true}" = "true" ]; then
            nohup /lsiopy/bin/python3 /scripts/wechat/wechat-auto-login.py >/dev/null 2>&1 &
        fi
        # Give it time to map a window before judging it again.
        sleep 10
        continue
    fi

    wid=$(find_main_window) || wid=""

    if [ -z "$wid" ]; then
        # Alive but no main window. If a small window with the same title exists
        # it is the login/QR screen — leave that alone, it is supposed to be
        # small and auto-login handles it. Otherwise WeChat hid to the tray.
        if any=$(find_any_window); then
            continue
        fi
        hidden=$(xdotool search --name "$MAIN_TITLES" 2>/dev/null | head -1)
        if [ -n "$hidden" ]; then
            echo "🔍 WeChat window is hidden; mapping it back"
            xdotool windowmap "$hidden" 2>/dev/null || true
            xdotool windowactivate "$hidden" 2>/dev/null || true
        fi
        continue
    fi

    if [ "$FORCE_MAX" = "true" ] && ! is_maximized "$wid"; then
        echo "🔲 Maximizing WeChat main window ($wid)"
        # wmctrl states are what openbox honours; fall back to a manual resize if
        # wmctrl is unavailable in the image.
        if command -v wmctrl >/dev/null 2>&1; then
            wmctrl -i -r "$wid" -b add,maximized_vert,maximized_horz 2>/dev/null || true
        else
            eval "$(xdpyinfo | sed -n 's/^  dimensions: *\([0-9]*\)x\([0-9]*\).*/SW=\1; SH=\2/p')"
            [ -n "$SW" ] && xdotool windowsize "$wid" "$SW" "$SH" 2>/dev/null || true
            xdotool windowmove "$wid" 0 0 2>/dev/null || true
        fi
    fi
done
