#!/usr/bin/env python3
"""构建期补丁：往 /defaults/default.conf 的两个 server 块（明文 3000 / TLS
3001）各插入一个反代 location，把浏览器可见的 SUBFOLDERwechat-second-display/
转发给 second_display 守护进程的 loopback HTTP 端点（127.0.0.1:8768）。

proxy_pass 故意不带 URI 部分（没有尾斜杠）：nginx 因此把原始请求路径原样
转发给后端，而不是剥掉匹配到的 location 前缀——和
integrations/wechat-history/wechat_history/notifications.py 的
/wechat-notifications/api/... 路由是同一约定，
root/scripts/second_display/http_api.py 的路由也注册着同样带前缀的完整
路径 /wechat-second-display/api/status，两边必须保持一致，改一边要连带
改另一边。

新 location 插在两处 `location SUBFOLDERfiles {` 之前——这是
files-json-index.py 已经在用的既定锚点，本补丁只读它定位插入点、不改写它
的内容，与该补丁互不冲突。auth_basic 在 server 块级别声明，新 location
没有自己的 auth_basic/auth_basic_user_file，会自动继承（配置了 PASSWORD
时这个状态端点和其它页面一样受密码保护）。

init-nginx 每次容器启动都把这个模板复制成在线配置，所以构建期改模板也会
覆盖已有 volume。
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "  location SUBFOLDERfiles {"

LOCATION = """  location SUBFOLDERwechat-second-display/ {
    proxy_set_header Host $host;
    proxy_pass http://127.0.0.1:8768;
  }
"""


def fail(message: str) -> None:
    raise RuntimeError(f"patch-second-display-nginx: {message}")


def patch_conf(path: Path) -> None:
    source = path.read_text(encoding="utf-8")

    location_count = source.count(LOCATION)
    if location_count == 2:
        print(f"patch-second-display-nginx: already patched {path}")
        return
    if location_count != 0:
        fail(
            f"expected 0 or 2 existing location blocks in {path}, found "
            f"{location_count} — a previous patch left a partial insertion"
        )

    marker_count = source.count(MARKER)
    if marker_count != 2:
        fail(
            f"expected 2 occurrences of {MARKER!r} in {path}, found "
            f"{marker_count} — the upstream template changed, re-derive the anchor"
        )

    patched = source.replace(MARKER, LOCATION + MARKER)

    if patched.count(LOCATION) != 2:
        fail(
            f"insertion produced {patched.count(LOCATION)} location blocks "
            f"in {path}, expected 2"
        )
    if patched.count(MARKER) != 2:
        fail(f"insertion damaged the files location marker in {path}")

    path.write_text(patched, encoding="utf-8")
    print(f"patch-second-display-nginx: patched {path}")


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/defaults/default.conf")
    try:
        patch_conf(path)
    except (OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
