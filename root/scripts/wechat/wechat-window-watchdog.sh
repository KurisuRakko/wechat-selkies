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
# The login/QR window is ~560x760 and dialogs ~564x516; requiring both axes above
# this means neither can ever be treated as the main window.
MIN_W=600
MIN_H=600
# Counts as maximized at this fraction of the screen. Not 100%: openbox runs with
# noStrut so a maximized window is full-screen, but leave slack for rounding.
MAX_W_PCT=95
MAX_H_PCT=92

LOG="${WECHAT_WATCHDOG_LOG:-/config/.wechat-watchdog.log}"
LOG_MAX_BYTES=262144

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG" 2>/dev/null
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

# Largest non-modal class=wechat window at or above the size floor.
# $1: "visible" restricts to mapped windows; "any" includes unmapped ones, which
#     is what tray recovery needs.
find_main_window() {
    local scope="$1" search_args wid w h area best best_area
    if [ "$scope" = "visible" ]; then
        search_args="--onlyvisible"
    else
        search_args=""
    fi
    best=""
    best_area=0
    for wid in $(xdotool search $search_args --class "$WECHAT_CLASS" 2>/dev/null); do
        is_modal "$wid" && continue
        set -- $(win_geom "$wid")
        w="$1"; h="$2"
        [ -n "$w" ] && [ -n "$h" ] || continue
        [ "$w" -ge "$MIN_W" ] && [ "$h" -ge "$MIN_H" ] || continue
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

log "watchdog started (interval ${INTERVAL}s, force_maximized=${FORCE_MAX}, class=${WECHAT_CLASS})"

while true; do
    sleep "$INTERVAL"

    if ! pgrep -x wechat >/dev/null 2>&1; then
        log "process: wechat is gone, relaunching"
        nohup /usr/bin/wechat >/dev/null 2>&1 &
        if [ "${ENABLE_WECHAT_AUTO_LOGIN:-true}" = "true" ]; then
            nohup /lsiopy/bin/python3 /scripts/wechat/wechat-auto-login.py >/dev/null 2>&1 &
        fi
        sleep 10   # give it time to map a window before judging it
        continue
    fi

    set -- $(screen_size)
    SW="$1"; SH="$2"
    if [ -z "$SW" ] || [ -z "$SH" ] || [ "$SW" -le 0 ]; then
        continue   # no display yet
    fi

    if wid=$(find_main_window visible); then
        if [ "$FORCE_MAX" = "true" ] && ! geometry_is_maximized "$wid" "$SW" "$SH"; then
            maximize "$wid" "$SW" "$SH"
        fi
        continue
    fi

    # No main window mapped. If any class=wechat window is visible it is the
    # login/QR screen — leave that alone, auto-login deals with it.
    if [ -n "$(xdotool search --onlyvisible --class "$WECHAT_CLASS" 2>/dev/null)" ]; then
        continue
    fi

    # Nothing visible at all: WeChat hid to the tray. Map the main window back.
    # The previous version never reached this branch — its "is any window up?"
    # check matched by title and so was satisfied by the unmapped ghost window.
    if hidden=$(find_main_window any); then
        log "window: nothing visible, mapping $hidden back from the tray"
        xdotool windowmap "$hidden" 2>/dev/null
        xdotool windowactivate "$hidden" 2>/dev/null
        sleep 1
        if [ "$FORCE_MAX" = "true" ] && ! geometry_is_maximized "$hidden" "$SW" "$SH"; then
            maximize "$hidden" "$SW" "$SH"
        fi
    fi
done
