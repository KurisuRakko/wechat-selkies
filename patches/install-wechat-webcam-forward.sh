#!/bin/sh
# 可选安装浏览器→容器虚拟摄像头桥接（INSTALL_WEBCAM_FORWARD=true 时启用）。
#
# 四条链路必须全部就位，缺一条功能就静默失效，所以都在这里安装并校验：
#
#   * src/wechat-webcam-forward.js 注入 dashboard 的 index.html（幂等），
#     页面顶部出现「摄像头」开关按钮。
#   * nginx 模板 /defaults/default.conf 的两个 server 块（3000 明文 /
#     3001 TLS）各插入一个 WebSocket 反代 location，把浏览器的
#     SUBFOLDERwechat-webcam/ 转发到 bridge 的 loopback 端口；插在
#     "location SUBFOLDERfiles" 之前，避免 init-nginx 的
#     "/files {/,/^  }/d" 删除规则把它一起删掉（与 install-wechat-
#     notifications.sh / install-wechat-desktop-export.sh 同一手法）。
#   * svc-wechat-webcam（s6 longrun）以 root 常驻运行 bridge 服务。
#   * bridge 依赖 pyfakewebcam/numpy（仅本开关开启时由 Dockerfile 安装），
#     构建期做 import 自检与 --self-test。
#
# init-nginx 每次容器启动都把 /defaults/default.conf 复制成在线配置，所以
# 构建期改模板也会覆盖已有 volume。
#
# 与 install-wechat-notifications.sh 同一约定：第一个参数是开关（非 true 时
# 直接退出），第二个参数是资源目录。资源由 Dockerfile 无条件 COPY，装不装
# 由这里决定。
set -eu

enabled="${1:-false}"
assets="${2:-/tmp/wechat-webcam}"
root="${3:-}"

if [ "$enabled" != "true" ]; then
    echo "install-wechat-webcam-forward: disabled"
    exit 0
fi

dashboard="$root/usr/share/selkies/selkies-dashboard"
html="$dashboard/index.html"
nginx_default="$root/defaults/default.conf"
s6_root="$root/etc/s6-overlay/s6-rc.d"
helper="$root/scripts/webcam/wechat-webcam-bridge.py"
tag='<script src="src/wechat-webcam-forward.js"></script>'

for required in \
    "$assets/wechat-webcam-forward.js" \
    "$assets/wechat-webcam-bridge.py" \
    "$assets/s6/svc-wechat-webcam/run" \
    "$assets/s6/svc-wechat-webcam/type" \
    "$assets/s6/svc-wechat-webcam/dependencies.d/init-services" \
    "$assets/s6/user/contents.d/svc-wechat-webcam" \
    "$html" \
    "$nginx_default"; do
    if [ ! -f "$required" ]; then
        echo "install-wechat-webcam-forward: missing $required" >&2
        exit 1
    fi
done

install -m 0644 "$assets/wechat-webcam-forward.js" \
    "$dashboard/src/wechat-webcam-forward.js"

# 幂等注入 <script> 标签：重复构建不叠加，缺一个 </body> 就失败。
HTML="$html" TAG="$tag" /lsiopy/bin/python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["HTML"])
tag = os.environ["TAG"]
text = path.read_text(encoding="utf-8")
count = text.count(tag)
if count > 1:
    raise SystemExit(f"install-wechat-webcam-forward: duplicate tags in {path}")
if count == 0:
    if text.count("</body>") != 1:
        raise SystemExit(f"install-wechat-webcam-forward: expected one </body> in {path}")
    text = text.replace("</body>", f"  {tag}\n</body>")
    path.write_text(text, encoding="utf-8")
if path.read_text(encoding="utf-8").count(tag) != 1:
    raise SystemExit(f"install-wechat-webcam-forward: failed to inject {path}")
PY

# 两个 server 块各插一次（count in (0,2) 校验，幂等）。反代必须带
# Upgrade/Connection upgrade 头（照抄模板里 Selkies 自己 websocket 块的写法），
# 否则 websockets 握手 400。proxy_pass 尾斜杠剥掉 SUBFOLDER 前缀，bridge 端
# 永远只见裸路径。
NGINX_DEFAULT="$nginx_default" /lsiopy/bin/python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["NGINX_DEFAULT"])
text = path.read_text(encoding="utf-8")
marker = "  location SUBFOLDERfiles {"
location = """  location SUBFOLDERwechat-webcam/ {
    proxy_set_header        Upgrade $http_upgrade;
    proxy_set_header        Connection "upgrade";
    proxy_set_header        Host $host;
    proxy_set_header        X-Real-IP $remote_addr;
    proxy_set_header        X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header        X-Forwarded-Proto $scheme;
    proxy_http_version      1.1;
    proxy_read_timeout      3600s;
    proxy_buffering         off;
    proxy_pass              http://127.0.0.1:8767/;
  }
"""
count = text.count(location)
if count not in (0, 2):
    raise SystemExit(f"install-wechat-webcam-forward: unexpected nginx location count {count}")
if count == 0:
    if text.count(marker) != 2:
        raise SystemExit("install-wechat-webcam-forward: nginx server layout changed")
    text = text.replace(marker, location + marker)
    path.write_text(text, encoding="utf-8")
if path.read_text(encoding="utf-8").count(location) != 2:
    raise SystemExit("install-wechat-webcam-forward: nginx injection failed")
PY

mkdir -p "$s6_root/svc-wechat-webcam/dependencies.d" "$s6_root/user/contents.d"
install -m 0755 "$assets/s6/svc-wechat-webcam/run" \
    "$s6_root/svc-wechat-webcam/run"
install -m 0644 "$assets/s6/svc-wechat-webcam/type" \
    "$s6_root/svc-wechat-webcam/type"
install -m 0644 "$assets/s6/svc-wechat-webcam/dependencies.d/init-services" \
    "$s6_root/svc-wechat-webcam/dependencies.d/init-services"
install -m 0644 "$assets/s6/user/contents.d/svc-wechat-webcam" \
    "$s6_root/user/contents.d/svc-wechat-webcam"

install -m 0755 "$assets/wechat-webcam-bridge.py" "$helper"

# 缺一个依赖都只在运行期以"虚拟摄像头打不开/服务起不来"的形式暴露，构建期
# 就断言清楚。
/lsiopy/bin/python3 -c 'import pyfakewebcam, numpy, PIL, websockets'
/lsiopy/bin/python3 "$helper" --self-test
grep -Fq 'wechat-webcam-forward' "$dashboard/src/wechat-webcam-forward.js"

echo "install-wechat-webcam-forward: dashboard, nginx and s6 integration wired up"
