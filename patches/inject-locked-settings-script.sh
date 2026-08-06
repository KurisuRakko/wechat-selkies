#!/bin/sh
# 构建期补丁：把 patches/wechat-locked-settings.js 注入 dashboard 的 HTML。
# 幂等逻辑与 inject-quality-presets-script.sh 完全一致。
set -e

ROOT="${1:-/usr/share/selkies}"
SCRIPT_REL="src/wechat-locked-settings.js"
TAG="<script src=\"$SCRIPT_REL\"></script>"

found=0
for html in "$ROOT"/*/index.html; do
    [ -f "$html" ] || continue
    dir=$(dirname "$html")
    bundle="$dir/src/selkies-core.js"

    [ -f "$dir/$SCRIPT_REL" ] || continue

    if [ ! -f "$bundle" ]; then
        echo "inject-locked-settings-script: missing Selkies bundle beside $html" >&2
        exit 1
    fi

    # 脚本写入的每个键都必须存在于 core bundle 中。键名在压缩后可能带引号
    # 也可能不带，因此统一按纯字符串检查，避免把实际存在的 API 误判为缺失。
    for api in \
        'use_css_scaling' \
        'useCssScaling' \
        'force_aligned_resolution' \
        'antiAliasingEnabled' \
        'use_browser_cursors' \
        'scaling_dpi' \
        'encoder' \
        'rate_control_mode' \
        'use_paint_over_quality' \
        'h264_streaming_mode' \
        'use_cpu' \
        'h264_crf' \
        'h264_paintover_crf' \
        'video_bitrate' \
        'x264enc-striped'; do
        if ! grep -Fq "$api" "$bundle"; then
            echo "inject-locked-settings-script: required API $api missing from $bundle" >&2
            exit 1
        fi
    done

    tag_count=$(grep -Fo "$TAG" "$html" | wc -l | tr -d ' ')
    if [ "$tag_count" -gt 1 ]; then
        echo "inject-locked-settings-script: duplicate script tags in $html" >&2
        exit 1
    fi

    if [ "$tag_count" -eq 1 ]; then
        echo "inject-locked-settings-script: already present in $html"
        found=$((found + 1))
        continue
    fi

    if ! grep -q "</body>" "$html"; then
        echo "inject-locked-settings-script: no </body> in $html" >&2
        exit 1
    fi

    sed -i.bak "s|</body>|  $TAG\n</body>|" "$html"
    rm -f "$html.bak"

    tag_count=$(grep -Fo "$TAG" "$html" | wc -l | tr -d ' ')
    [ "$tag_count" -eq 1 ] || {
        echo "inject-locked-settings-script: FAILED to inject into $html" >&2
        exit 1
    }
    echo "inject-locked-settings-script: injected into $html"
    found=$((found + 1))
done

if [ "$found" -eq 0 ]; then
    echo "inject-locked-settings-script: found no dashboard with both index.html and $SCRIPT_REL" >&2
    exit 1
fi

echo "inject-locked-settings-script: $found dashboard(s) wired up"
