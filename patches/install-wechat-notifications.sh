#!/bin/sh
# Conditionally install the private history-backed Web Push integration.
set -eu

enabled="${1:-false}"
assets="${2:-/tmp/wechat-notifications}"
root="${3:-}"

if [ "$enabled" != "true" ]; then
    echo "install-wechat-notifications: disabled"
    exit 0
fi

dashboard="$root/usr/share/selkies/selkies-dashboard"
html="$dashboard/index.html"
nginx_default="$root/defaults/default.conf"
s6_root="$root/etc/s6-overlay/s6-rc.d"
tag='<script src="src/wechat-notifications.js"></script>'

for required in \
    "$assets/wechat-notifications.js" \
    "$assets/wechat-notification-sw.js" \
    "$assets/s6/svc-wechat-notifications/run" \
    "$assets/s6/svc-wechat-notifications/type" \
    "$html" \
    "$nginx_default"; do
    if [ ! -f "$required" ]; then
        echo "install-wechat-notifications: missing $required" >&2
        exit 1
    fi
done

install -m 0644 "$assets/wechat-notifications.js" \
    "$dashboard/src/wechat-notifications.js"
install -m 0644 "$assets/wechat-notification-sw.js" \
    "$dashboard/wechat-notification-sw.js"

HTML="$html" TAG="$tag" /lsiopy/bin/python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["HTML"])
tag = os.environ["TAG"]
text = path.read_text(encoding="utf-8")
count = text.count(tag)
if count > 1:
    raise SystemExit(f"install-wechat-notifications: duplicate tags in {path}")
if count == 0:
    if text.count("</body>") != 1:
        raise SystemExit(f"install-wechat-notifications: expected one </body> in {path}")
    text = text.replace("</body>", f"  {tag}\n</body>")
    path.write_text(text, encoding="utf-8")
if path.read_text(encoding="utf-8").count(tag) != 1:
    raise SystemExit(f"install-wechat-notifications: failed to inject {path}")
PY

NGINX_DEFAULT="$nginx_default" /lsiopy/bin/python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["NGINX_DEFAULT"])
text = path.read_text(encoding="utf-8")
marker = "  location SUBFOLDERfiles {"
location = """  location SUBFOLDERwechat-notifications/api/ {
    proxy_set_header        Host $http_host;
    proxy_set_header        X-Real-IP $remote_addr;
    proxy_set_header        X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header        X-Forwarded-Proto $scheme;
    proxy_http_version      1.1;
    proxy_buffering         off;
    client_max_body_size    16k;
    proxy_pass              http://127.0.0.1:8765;
  }
"""
count = text.count(location)
if count not in (0, 2):
    raise SystemExit(f"install-wechat-notifications: unexpected nginx location count {count}")
if count == 0:
    if text.count(marker) != 2:
        raise SystemExit("install-wechat-notifications: nginx server layout changed")
    text = text.replace(marker, location + marker)
    path.write_text(text, encoding="utf-8")
if path.read_text(encoding="utf-8").count(location) != 2:
    raise SystemExit("install-wechat-notifications: nginx injection failed")
PY

mkdir -p "$s6_root/svc-wechat-notifications/dependencies.d" \
    "$s6_root/user/contents.d"
install -m 0755 "$assets/s6/svc-wechat-notifications/run" \
    "$s6_root/svc-wechat-notifications/run"
install -m 0644 "$assets/s6/svc-wechat-notifications/type" \
    "$s6_root/svc-wechat-notifications/type"
install -m 0644 "$assets/s6/svc-wechat-notifications/dependencies.d/init-services" \
    "$s6_root/svc-wechat-notifications/dependencies.d/init-services"
install -m 0644 "$assets/s6/user/contents.d/svc-wechat-notifications" \
    "$s6_root/user/contents.d/svc-wechat-notifications"

PYTHONPATH="$root/opt/wechat-history/site-packages:$root/opt/wechat-history" \
    /lsiopy/bin/python3 -c \
    'import aiohttp, cryptography, http_ece, py_vapid, pywebpush, requests'
grep -Fq 'wechatNotificationsInstalled' "$dashboard/src/wechat-notifications.js"
grep -Fq 'pushsubscriptionchange' "$dashboard/wechat-notification-sw.js"

echo "install-wechat-notifications: browser, nginx and s6 integration installed"
