#!/usr/bin/env python3
"""Turn the /files directory listing into JSON instead of fancyindex HTML.

The sidebar "Files" panel used to embed an iframe pointing at /files/, which
nginx served as a fancyindex directory listing. That whole panel is blank in
practice, for two stacked reasons:

  * the nginx `location SUBFOLDERfiles` block has no `index` directive, so the
    built-in default `index index.html` applies — if the downloads directory
    (REPLACE_DOWNLOADS_PATH) contains a user file named index.html, a request
    for /files/ returns *that HTML* instead of the directory listing;
  * the same block adds `Content-Disposition: attachment` to any existing
    file, so the browser downloads the HTML instead of rendering it, and the
    iframe ends up empty.

The dashboard now draws its own Explorer-style file browser
(src/wechat-file-manager.js) and fetches the listing as JSON, so this patch
points `index` at a name that can never exist (nginx then falls through to
autoindex) and switches autoindex to its JSON format.

Deliberately NOT added: `autoindex_localtime on`. It only affects the HTML
listing; the JSON branch always reports mtime as RFC1123 GMT, and adding the
directive would just mislead. The trailing `if (-f ...)` attachment block is
left untouched — downloads must keep their attachment header.

The `location SUBFOLDERfiles` block appears twice in /defaults/default.conf
(once in the plaintext server, once in the TLS server), and init-nginx deletes
the whole block when SELKIES_FILE_TRANSFERS lacks `download` or when
HARDEN_DESKTOP is set, so this patch must stay inside that one block and never
introduce a second one that would dodge the upstream hardening.
"""

from __future__ import annotations

import sys
from pathlib import Path


ORIGINAL = """  location SUBFOLDERfiles {
    fancyindex on;
    fancyindex_footer SUBFOLDERnginx/footer.html;
    fancyindex_header SUBFOLDERnginx/header.html;
    alias REPLACE_DOWNLOADS_PATH/;"""

REPLACEMENT = """  location SUBFOLDERfiles {
    # 目录清单改成 JSON，由 dashboard 的文件管理器（src/wechat-file-manager.js）
    # 渲染，旧的 HTML 目录清单不再有消费者。
    #
    # index 指向一个不会存在的名字是这里的关键：nginx 内建默认 index index.html，
    # 下载目录里只要有一个 index.html，/files/ 的清单请求就会命中它，再叠上本块
    # 的 Content-Disposition: attachment——附件不渲染，旧版侧边栏「文件」面板整块
    # 空白就是这么来的。名字落不到实体文件时 nginx 会继续走 autoindex。
    index .selkies-no-index;
    autoindex on;
    autoindex_format json;
    alias REPLACE_DOWNLOADS_PATH/;"""


def fail(message: str) -> None:
    raise RuntimeError(f"files-json-index: {message}")


def validate(source: str, path: Path) -> None:
    # 收尾校验：JSON 清单两处就位、fancyindex 彻底消失、下载路径占位符与
    # location 块数量不变。
    if source.count("autoindex_format json;") != 2:
        fail(
            f"expected 2 autoindex_format json directives in {path}, "
            f"found {source.count('autoindex_format json;')}"
        )
    if "fancyindex" in source:
        fail(f"fancyindex directives still present in {path}")
    if source.count("REPLACE_DOWNLOADS_PATH") != 2:
        fail(
            f"expected 2 REPLACE_DOWNLOADS_PATH placeholders in {path}, "
            f"found {source.count('REPLACE_DOWNLOADS_PATH')}"
        )
    if source.count("location SUBFOLDERfiles {") != 2:
        fail(
            f"expected 2 location SUBFOLDERfiles blocks in {path}, "
            f"found {source.count('location SUBFOLDERfiles {')}"
        )


def patch_conf(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    original = source

    original_count = source.count(ORIGINAL)
    patched_count = source.count(REPLACEMENT)
    if original_count == 2 and patched_count == 0:
        source = source.replace(ORIGINAL, REPLACEMENT)
        if source.count(ORIGINAL) != 0:
            fail(f"replacement left an original block behind in {path}")
        if source.count(REPLACEMENT) != 2:
            fail(f"replacement produced {source.count(REPLACEMENT)} patched blocks in {path}")
        action = "patched"
    elif original_count == 0 and patched_count == 2:
        action = "already patched"
    else:
        fail(
            f"expected exactly 2 original files blocks and 0 patched ones in "
            f"{path}; found original={original_count}, patched={patched_count} — "
            f"the upstream template changed, re-derive the patch"
        )

    validate(source, path)

    if action == "patched":
        if source != original:
            path.write_text(source, encoding="utf-8")
            print(f"files-json-index: patched {path}")
    else:
        print(f"files-json-index: already patched {path}")


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
