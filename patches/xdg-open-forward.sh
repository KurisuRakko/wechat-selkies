#!/bin/sh
# Installed as /usr/local/bin/xdg-open — forwards URL opens to the viewer's own
# browser instead of trying to open them in the container.
#
# Why this works and why here:
#
#   * WeChat is Qt. QDesktopServices::openUrl goes through
#     QGenericUnixServices::detectWebBrowser(), whose FIRST candidate is
#     `xdg-open` looked up on PATH (the literal is stored UTF-16, which is why a
#     plain `strings | grep xdg-open` on the binary finds nothing). $BROWSER and
#     the hardcoded google-chrome/firefox/mozilla/opera list are only consulted
#     after that.
#   * The container has NO browser at all, so the real /usr/bin/xdg-open reaches
#     open_generic(), finds x-scheme-handler/http unset and every entry of its
#     built-in BROWSER list missing, and exits with "no method available for
#     opening". WeChat's stdout is /dev/null, so clicking a link did nothing at
#     all, silently. That is the behaviour this replaces.
#   * PATH inside the container is
#     /command:/lsiopy/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:...
#     so /usr/local/bin wins. The base image's init-selkies-config also runs
#     `chmod 755 $(which xdg-open)` on every start, and chmod 0000 when
#     DISABLE_OPEN_TOOLS=true — pointing at this shim, so that hardening switch
#     keeps working as documented.
#
# URLs are handed to the browser through a queue file under /config/Desktop,
# which nginx already publishes at /files (verified: no dotfile deny rule, ETag +
# Last-Modified present so a 1 Hz conditional GET is nearly free). The injected
# page script src/wechat-dragdrop.js polls it and opens each entry.
#
# Anything that is not an http/https/mailto URL is delegated to the real
# xdg-open, so opening a local file or folder still works in-container.
set -eu

QUEUE="${WECHAT_OPEN_QUEUE:-/config/Desktop/.wechat-open-urls}"
REAL_XDG_OPEN=/usr/bin/xdg-open
MAX_LINES=20
MAX_URL_BYTES=8192

target="${1:-}"

if [ -z "$target" ]; then
    exec "$REAL_XDG_OPEN" "$@"
fi

case "$target" in
    http://*|https://*|mailto:*) ;;
    *)
        # Not a URL we forward: local file, directory, other scheme.
        if [ -x "$REAL_XDG_OPEN" ]; then
            exec "$REAL_XDG_OPEN" "$@"
        fi
        exit 0
        ;;
esac

# A pathological URL should not be able to fill the volume.
if [ "$(printf '%s' "$target" | wc -c)" -gt "$MAX_URL_BYTES" ]; then
    exit 1
fi

mkdir -p "$(dirname "$QUEUE")" 2>/dev/null || true

# Records are "<epoch_ms> <url>". The page script ignores anything older than its
# staleness window, so a URL clicked while nobody is watching does not pop up
# hours later. Milliseconds because two clicks can land in the same second and
# the timestamp doubles as the de-duplication cursor.
now_ms=$(date +%s%3N 2>/dev/null || echo "$(date +%s)000")

# Written via a temp file in the same directory and renamed, so the poller can
# never read a half-written queue. 0644 because nginx runs as www-data.
tmp=$(mktemp "$(dirname "$QUEUE")/.wechat-open-urls.XXXXXX") || exit 1
trap 'rm -f "$tmp"' EXIT INT TERM

if [ -f "$QUEUE" ]; then
    tail -n "$((MAX_LINES - 1))" "$QUEUE" 2>/dev/null >> "$tmp" || true
fi
printf '%s %s\n' "$now_ms" "$target" >> "$tmp"

chmod 0644 "$tmp" 2>/dev/null || true
mv -f "$tmp" "$QUEUE"
trap - EXIT INT TERM

# Exit 0 so Qt does not fall through to probing for google-chrome/firefox/etc.
exit 0
