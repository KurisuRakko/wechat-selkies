#!/bin/sh
# Build-time patch: load patches/wechat-ime-anchor.js from the dashboard's HTML.
#
# Same pattern as inject-dragdrop-script.sh: the dashboard already loads plain
# (non-module) scripts next to the bundle, so the IME anchor script is added the
# same way instead of editing the minified bundle. It only touches #overlayInput
# styling and value, which the bundle creates by id.
#
# /usr/share/selkies/web is deleted and recreated from /usr/share/selkies/$DASHBOARD
# by init-nginx on every container start, so both the HTML and the JS have to live
# in the dashboard directory, not in web/.
set -e

ROOT="${1:-/usr/share/selkies}"
SCRIPT_REL="src/wechat-ime-anchor.js"
TAG="<script src=\"$SCRIPT_REL\"></script>"

found=0
for html in "$ROOT"/*/index.html; do
    [ -f "$html" ] || continue
    dir=$(dirname "$html")

    # Only dashboards that actually carry the streaming client.
    [ -f "$dir/$SCRIPT_REL" ] || continue

    if grep -q "wechat-ime-anchor.js" "$html"; then
        echo "inject-ime-anchor-script: already present in $html"
        found=$((found + 1))
        continue
    fi

    if ! grep -q "</body>" "$html"; then
        echo "inject-ime-anchor-script: no </body> in $html" >&2
        exit 1
    fi

    # Last thing before </body>, so the bundle has already created
    # #overlayInput (or the script's own poll will find it shortly after).
    sed -i "s|</body>|  $TAG\n</body>|" "$html"

    grep -q "wechat-ime-anchor.js" "$html" || {
        echo "inject-ime-anchor-script: FAILED to inject into $html" >&2
        exit 1
    }
    echo "inject-ime-anchor-script: injected into $html"
    found=$((found + 1))
done

if [ "$found" -eq 0 ]; then
    echo "inject-ime-anchor-script: found no dashboard with both index.html and $SCRIPT_REL" >&2
    exit 1
fi

echo "inject-ime-anchor-script: $found dashboard(s) wired up"
