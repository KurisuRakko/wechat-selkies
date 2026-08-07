#!/bin/sh
# Build-time wiring for drag-out export: dashboard script tag, nginx location
# and the s6 longrun that owns the remote XDND receiver.
#
# Three pieces have to agree or the feature silently does nothing, so all three
# are installed and verified here rather than in three places:
#
#   * src/wechat-desktop-export.js loaded from the dashboard's index.html,
#     next to the preset bar it hides while a drag is running.
#   * an nginx location proxying to the helper's loopback API. The trailing
#     slash on proxy_pass strips the location prefix, so the helper always sees
#     /events and /file/<token> whatever SUBFOLDER the deployment uses.
#     Injected before "location SUBFOLDERfiles" so init-nginx's
#     "/files {/,/^  }/d" removal for HARDEN_DESKTOP cannot take it with it.
#   * svc-wechat-export, which runs as root because WeChat's attachment
#     directories are 0700 root and the desktop user cannot read them.
#
# init-nginx copies /defaults/default.conf over the live config on every
# container start, so patching the template here also fixes existing volumes.
set -eu

assets="${1:-/tmp/wechat-desktop-export}"
root="${2:-}"

dashboard="$root/usr/share/selkies/selkies-dashboard"
html="$dashboard/index.html"
bundle="$dashboard/src/selkies-core.js"
nginx_default="$root/defaults/default.conf"
s6_root="$root/etc/s6-overlay/s6-rc.d"
helper="$root/scripts/wechat/wechat-export-drop.py"
tag='<script src="src/wechat-desktop-export.js"></script>'

for required in \
    "$assets/wechat-desktop-export.js" \
    "$assets/s6/svc-wechat-export/run" \
    "$assets/s6/svc-wechat-export/type" \
    "$assets/s6/user/contents.d/svc-wechat-export" \
    "$helper" \
    "$html" \
    "$bundle" \
    "$nginx_default"; do
    if [ ! -f "$required" ]; then
        echo "install-wechat-desktop-export: missing $required" >&2
        exit 1
    fi
done

# The drop zone replaces this element while a drag runs; if the preset bar is
# ever renamed the zone would appear next to it instead of in its place.
if ! grep -Fq 'wechat-quality-presets' "$dashboard/src/wechat-quality-presets.js"; then
    echo "install-wechat-desktop-export: preset bar id missing from the preset script" >&2
    exit 1
fi

install -m 0644 "$assets/wechat-desktop-export.js" \
    "$dashboard/src/wechat-desktop-export.js"

HTML="$html" TAG="$tag" /lsiopy/bin/python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["HTML"])
tag = os.environ["TAG"]
text = path.read_text(encoding="utf-8")
count = text.count(tag)
if count > 1:
    raise SystemExit(f"install-wechat-desktop-export: duplicate tags in {path}")
if count == 0:
    if text.count("</body>") != 1:
        raise SystemExit(f"install-wechat-desktop-export: expected one </body> in {path}")
    text = text.replace("</body>", f"  {tag}\n</body>")
    path.write_text(text, encoding="utf-8")
if path.read_text(encoding="utf-8").count(tag) != 1:
    raise SystemExit(f"install-wechat-desktop-export: failed to inject {path}")
PY

NGINX_DEFAULT="$nginx_default" /lsiopy/bin/python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["NGINX_DEFAULT"])
text = path.read_text(encoding="utf-8")
marker = "  location SUBFOLDERfiles {"
location = """  location SUBFOLDERwechat-export/ {
    proxy_set_header        Host $http_host;
    proxy_set_header        X-Real-IP $remote_addr;
    proxy_set_header        X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header        X-Forwarded-Proto $scheme;
    proxy_http_version      1.1;
    proxy_buffering         off;
    proxy_read_timeout      3600s;
    proxy_pass              http://127.0.0.1:8766/;
  }
"""
count = text.count(location)
# Two server blocks (3000 plain, 3001 TLS); both or neither.
if count not in (0, 2):
    raise SystemExit(f"install-wechat-desktop-export: unexpected nginx location count {count}")
if count == 0:
    if text.count(marker) != 2:
        raise SystemExit("install-wechat-desktop-export: nginx server layout changed")
    text = text.replace(marker, location + marker)
    path.write_text(text, encoding="utf-8")
if path.read_text(encoding="utf-8").count(location) != 2:
    raise SystemExit("install-wechat-desktop-export: nginx injection failed")
PY

mkdir -p "$s6_root/svc-wechat-export/dependencies.d" "$s6_root/user/contents.d"
install -m 0755 "$assets/s6/svc-wechat-export/run" "$s6_root/svc-wechat-export/run"
install -m 0644 "$assets/s6/svc-wechat-export/type" "$s6_root/svc-wechat-export/type"
install -m 0644 "$assets/s6/svc-wechat-export/dependencies.d/init-services" \
    "$s6_root/svc-wechat-export/dependencies.d/init-services"
install -m 0644 "$assets/s6/svc-wechat-export/dependencies.d/svc-xorg" \
    "$s6_root/svc-wechat-export/dependencies.d/svc-xorg"
install -m 0644 "$assets/s6/user/contents.d/svc-wechat-export" \
    "$s6_root/user/contents.d/svc-wechat-export"

chmod 0755 "$helper"
# python-xlib is what the receiver window and the XFixes drag signal are built
# on; a missing binding would only surface at runtime as a dead feature.
/lsiopy/bin/python3 -c 'import Xlib.ext.xfixes'
/lsiopy/bin/python3 "$helper" --self-test
grep -Fq 'wechatDesktopExportInstalled' "$dashboard/src/wechat-desktop-export.js"

echo "install-wechat-desktop-export: dashboard, nginx and s6 integration wired up"
