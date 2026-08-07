# WeChat for Linux using Selkies baseimage
FROM ghcr.io/linuxserver/baseimage-selkies:ubuntunoble

# Metadata labels
LABEL org.opencontainers.image.title="WeChat Selkies"
LABEL org.opencontainers.image.description="WeChat Linux client in browser via Selkies WebRTC"
LABEL org.opencontainers.image.authors="nickrunning"
LABEL org.opencontainers.image.source="https://github.com/nickrunning/wechat-selkies"
LABEL org.opencontainers.image.documentation="https://github.com/nickrunning/wechat-selkies#readme"
LABEL org.opencontainers.image.vendor="WeChat Selkies Project"
LABEL org.opencontainers.image.licenses="GPL-3.0-only"

# Build arguments for multi-arch support
ARG TARGETPLATFORM
ARG BUILDPLATFORM
ARG INSTALL_QQ=true
ARG INSTALL_PCMANFM=true
ARG INSTALL_WECHAT_HISTORY=false
RUN echo "🏗️ Building WeChat-Selkies on $BUILDPLATFORM, targeting $TARGETPLATFORM"

RUN apt-get update && \
    apt-get install -y fonts-noto-cjk libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-render-util0 libxcb-xkb1 libxkbcommon-x11-0 \
    shared-mime-info desktop-file-utils libxcb1 libxcb-icccm4 libxcb-image0 \
    libxcb-keysyms1 libxcb-randr0 libxcb-render0 libxcb-render-util0 libxcb-shape0 \
    libxcb-shm0 libxcb-sync1 libxcb-util1 libxcb-xfixes0 libxcb-xkb1 libxcb-xinerama0 \
    libxcb-xkb1 libxcb-glx0 libatk1.0-0 libatk-bridge2.0-0 libc6 libcairo2 libcups2 \
    libdbus-1-3 libfontconfig1 libgbm1 libgcc1 libgdk-pixbuf2.0-0 libglib2.0-0 \
    libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 libpangocairo-1.0-0 libstdc++6 libx11-6 \
    libxcomposite1 libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 \
    libxss1 libxtst6 libatomic1 libxcomposite1 libxrender1 libxrandr2 libxkbcommon-x11-0 \
    libfontconfig1 libdbus-1-3 libnss3 libx11-xcb1 stalonetray inotify-tools \
    wmctrl

ARG INSTALL_PCMANFM
RUN if [ "$INSTALL_PCMANFM" = "true" ]; then \
        apt-get install -y --no-install-recommends pcmanfm; \
    fi

# Optional, private WeChat history integration. Keep this disabled in public
# images; only the local compose file enables it.
RUN if [ "$INSTALL_WECHAT_HISTORY" = "true" ]; then \
        apt-get install -y --no-install-recommends xdotool xclip; \
    fi

RUN pip install --no-cache-dir python-xlib

COPY integrations/wechat-history/requirements.lock /tmp/wechat-history-requirements.lock
RUN if [ "$INSTALL_WECHAT_HISTORY" = "true" ]; then \
        /lsiopy/bin/python3 -m pip install \
            --no-cache-dir \
            --only-binary=:all: \
            --require-hashes \
            --target /opt/wechat-history/site-packages \
            -r /tmp/wechat-history-requirements.lock; \
    fi && \
    rm -f /tmp/wechat-history-requirements.lock

# Install WeChat based on target architecture
RUN case "$TARGETPLATFORM" in \
    "linux/amd64") \
        WECHAT_URL="https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_x86_64.deb"; \
        WECHAT_ARCH="x86_64" ;; \
    "linux/arm64") \
        WECHAT_URL="https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_arm64.deb"; \
        WECHAT_ARCH="arm64" ;; \
    *) \
        echo "❌ Unsupported platform: $TARGETPLATFORM" >&2; \
        echo "Supported platforms: linux/amd64, linux/arm64" >&2; \
        exit 1 ;; \
    esac && \
    echo "📦 Downloading WeChat for $WECHAT_ARCH architecture..." && \
    curl -fsSL --retry 3 --retry-delay 10 --retry-all-errors -o wechat.deb "$WECHAT_URL" && \
    echo "🔧 Installing WeChat..." && \
    (dpkg -i wechat.deb || (apt-get update && apt-get install -f -y && dpkg -i wechat.deb)) && \
    rm -f wechat.deb && \
    echo "✅ WeChat installation completed for $WECHAT_ARCH"

# Install QQ based on target architecture (optional)
ARG INSTALL_QQ
ARG QQ_AMD64_URL="https://qqdl.gtimg.cn/qqfile/QQNT/9.9.32/release/c390e792/QQ_3.2.31_260710_amd64_01.deb"
ARG QQ_ARM64_URL="https://qqdl.gtimg.cn/qqfile/QQNT/9.9.32/release/c390e792/QQ_3.2.31_260710_arm64_01.deb"
RUN if [ "$INSTALL_QQ" = "true" ]; then \
        case "$TARGETPLATFORM" in \
        "linux/amd64") \
            QQ_URL="$QQ_AMD64_URL"; \
            QQ_ARCH="x86_64" ;; \
        "linux/arm64") \
            QQ_URL="$QQ_ARM64_URL"; \
            QQ_ARCH="arm64" ;; \
        *) \
            echo "❌ Unsupported platform: $TARGETPLATFORM" >&2; \
            exit 1 ;; \
        esac && \
        echo "📦 Downloading QQ for $QQ_ARCH architecture..." && \
        curl -fsSL --retry 3 --retry-delay 10 --retry-all-errors -o qq.deb "$QQ_URL" && \
        echo "🔧 Installing QQ..." && \
        (dpkg -i qq.deb || (apt-get update && apt-get install -f -y && dpkg -i qq.deb)) && \
        rm -f qq.deb && \
        echo "✅ QQ installation completed for $QQ_ARCH"; \
    else \
        echo "⏭️ Skipping QQ installation (INSTALL_QQ=$INSTALL_QQ)"; \
    fi

# Clean up
RUN apt-get purge -y --autoremove
RUN apt-get autoclean && \
    rm -rf \
        /config/.cache \
        /config/.npm \
        /var/lib/apt/lists/* \
        /var/tmp/* \
        /tmp/*

# configure openbox dock mode for stalonetray
RUN sed -i '/<dock>/,/<\/dock>/s/<noStrut>no<\/noStrut>/<noStrut>yes<\/noStrut>/' /etc/xdg/openbox/rc.xml

# commit host-IME text in one insert instead of one keystroke per character
COPY patches/atomic-ime-commit.sh /tmp/atomic-ime-commit.sh
RUN sh /tmp/atomic-ime-commit.sh && rm -f /tmp/atomic-ime-commit.sh

# let the browser choose its own video decoder instead of being forced onto software
COPY patches/decoder-no-preference.sh /tmp/decoder-no-preference.sh
RUN sh /tmp/decoder-no-preference.sh && rm -f /tmp/decoder-no-preference.sh

# shorten the wasted full-stripe encodes that trail the end of any motion.
# pixelflux keeps encoding a "busy" stripe every frame for this many frames
# without re-hashing it, so upstream's 20 leaves a tail of up to 40 frames after
# the screen has already gone static. Raise it back toward 20 if hashing ever
# shows up as the bottleneck instead.
ARG DAMAGE_BLOCK_DURATION=4
COPY patches/damage-block-duration.sh /tmp/damage-block-duration.sh
RUN sh /tmp/damage-block-duration.sh "$DAMAGE_BLOCK_DURATION" && rm -f /tmp/damage-block-duration.sh

# drag files from the host onto the stream and they are pasted into WeChat; drag
# them out of the sidebar file list and they download. Added as a plain script
# beside the bundle (like src/universalTouchGamepad.js) rather than as surgery
# inside 666 KB of minified JS — it only uses window.webrtcInput, which the
# bundle already exports.
COPY patches/wechat-dragdrop.js /usr/share/selkies/selkies-dashboard/src/wechat-dragdrop.js
COPY patches/inject-dragdrop-script.sh /tmp/inject-dragdrop-script.sh
RUN sh /tmp/inject-dragdrop-script.sh && rm -f /tmp/inject-dragdrop-script.sh

# anchor the host IME's candidate window with a click-local textarea while the
# original full-screen input remains the mouse/drag surface. Padding the large
# native input is not a reliable caret anchor on macOS Chromium.
COPY patches/wechat-ime-anchor.js /usr/share/selkies/selkies-dashboard/src/wechat-ime-anchor.js
COPY patches/inject-ime-anchor-script.sh /tmp/inject-ime-anchor-script.sh
RUN sh /tmp/inject-ime-anchor-script.sh && rm -f /tmp/inject-ime-anchor-script.sh

# show whether the stream is actually alive. A half-open websocket never fires
# close, so the bundle's own reload-on-close reconnect never runs: the picture
# just stops and the page looks fine. The pill ages the server's stats push
# locally (clock-skew proof) and, if it goes quiet for 20 s with no upload in
# flight and the tab visible, reloads — at most three times in ten minutes.
COPY patches/wechat-connection-status.js /usr/share/selkies/selkies-dashboard/src/wechat-connection-status.js
COPY patches/inject-connection-status-script.sh /tmp/inject-connection-status-script.sh
RUN sh /tmp/inject-connection-status-script.sh && rm -f /tmp/inject-connection-status-script.sh

# four named quality presets in the same top bar, instead of eleven encoder
# knobs. A browser that once stored "H264 (CPU) FullFrame with rate control
# off" re-imposes it on every reconnect; the presets pin x264enc-striped + CBR
# and seed the settings before the bundle boots, then apply live through the
# same postMessage the sidebar uses. The sidebar stays as the advanced view.
COPY patches/wechat-quality-presets.js /usr/share/selkies/selkies-dashboard/src/wechat-quality-presets.js
COPY patches/inject-quality-presets-script.sh /tmp/inject-quality-presets-script.sh
RUN sh /tmp/inject-quality-presets-script.sh && rm -f /tmp/inject-quality-presets-script.sh

# Dedicated 1 MiB same-origin payload for the automatic speed test. Random bytes
# prevent nginx/middleware from compressing it into a few KB and corrupting the
# measured bandwidth.
RUN dd if=/dev/urandom of=/usr/share/selkies/selkies-dashboard/wechat-speedtest.bin bs=1048576 count=1 && \
    test "$(wc -c < /usr/share/selkies/selkies-dashboard/wechat-speedtest.bin)" -eq 1048576

# 锁定部署所需的显示/编码设置，隐藏侧边栏“应用程序/共享”卡片，并统一画质
# 滑块方向（最左差、最右好）。浏览器在 bundle 启动前写入这些键，随后由
# MutationObserver 持续锁定后渲染出来的控件和面板。
COPY patches/wechat-locked-settings.js /usr/share/selkies/selkies-dashboard/src/wechat-locked-settings.js
COPY patches/inject-locked-settings-script.sh /tmp/inject-locked-settings-script.sh
RUN sh /tmp/inject-locked-settings-script.sh && rm -f /tmp/inject-locked-settings-script.sh

# Chromium blocks an AudioContext created during page initialization. Defer the
# playback context until the first trusted pointer/key/touch gesture, then use a
# single gate for later resume attempts so every audio packet cannot repeat the
# same autoplay-policy warning. The microphone context is deliberately left in
# its existing explicit user-action path.
COPY patches/selkies-audio-unlock.js /usr/share/selkies/selkies-dashboard/src/selkies-audio-unlock.js
COPY patches/patch-audio-autoplay.py /tmp/patch-audio-autoplay.py
RUN /lsiopy/bin/python3 /tmp/patch-audio-autoplay.py && rm -f /tmp/patch-audio-autoplay.py

# stop losing keystrokes: give the key injector a fallback when its xdotool child
# fails, paste CJK IME commits through the clipboard instead of racing xdotool's
# per-character keymap rebinding (Qt drops characters typed that way), stop
# truncating long ASCII commits, and make the primary display actually honour
# backpressure so a slow link sheds frames instead of killing the session
COPY patches/input-and-backpressure-fixes.py /tmp/input-and-backpressure-fixes.py
RUN /lsiopy/bin/python3 /tmp/input-and-backpressure-fixes.py && rm -f /tmp/input-and-backpressure-fixes.py

# stop an upload from freezing the picture and killing the session. In
# --mode=websockets video, audio, input, clipboard and file bytes all share one
# socket, and the stock uploader lets 10 MiB of file data queue ahead of every
# keystroke, frame ACK and pong. Cap the client-side queue at 256 KiB, take the
# blocking chunk write off the server's event loop, raise the 1 MiB frame
# ceiling that closes the connection with 1009, and forward the already
# collected stats once a second so the status pill can tell a slow link from a
# dead one.
COPY patches/patch-upload-client.py /tmp/patch-upload-client.py
RUN /lsiopy/bin/python3 /tmp/patch-upload-client.py && rm -f /tmp/patch-upload-client.py
COPY patches/upload-and-stats-fixes.py /tmp/upload-and-stats-fixes.py
RUN /lsiopy/bin/python3 /tmp/upload-and-stats-fixes.py && rm -f /tmp/upload-and-stats-fixes.py

# stop the constant idle audio stream. Upstream disables pcmflux's silence gate,
# so an idle desktop still emits an Opus packet every 20 ms; with the gate on,
# silent chunks never reach the callback and a quiet machine sends nothing.
# Speech and notification sounds are unaffected.
COPY patches/audio-silence-gate.py /tmp/audio-silence-gate.py
RUN /lsiopy/bin/python3 /tmp/audio-silence-gate.py && rm -f /tmp/audio-silence-gate.py

# stop horizontal tearing in the striped (x264enc-striped) video path. pixelflux
# cuts each frame into horizontal Y-stripes and the bundle runs one VideoDecoder
# per stripe, pushing every decoded output into one global paint queue that the
# render loop flushes unconditionally on every rAF — so frame N's top half and
# frame N+1's bottom half can land in the same paint pass. This tracks the real
# frame id per stripe (the bundle's own vncFrameID is bound stale at decoder
# creation) and only lets a frame paint once every stripe belonging to it has
# arrived, with a short timeout so an incomplete frame never freezes the picture.
COPY patches/wechat-frame-assembler.js /usr/share/selkies/selkies-dashboard/src/wechat-frame-assembler.js
COPY patches/inject-frame-assembler-script.sh /tmp/inject-frame-assembler-script.sh
RUN sh /tmp/inject-frame-assembler-script.sh && rm -f /tmp/inject-frame-assembler-script.sh
COPY patches/patch-frame-assembly.py /tmp/patch-frame-assembly.py
RUN /lsiopy/bin/python3 /tmp/patch-frame-assembly.py && rm -f /tmp/patch-frame-assembly.py

# open links from WeChat in the viewer's own browser. The container has no browser
# at all, so Qt's first choice — xdg-open on PATH — currently fails silently and a
# clicked link does nothing. /usr/local/bin precedes /usr/bin in the container
# PATH, so this shim intercepts; it queues URLs for the injected page script and
# delegates everything that is not a URL to the real /usr/bin/xdg-open.
COPY patches/xdg-open-forward.sh /usr/local/bin/xdg-open
RUN chmod 0755 /usr/local/bin/xdg-open
# Consulted by non-Qt callers, and by Qt after xdg-open; point it at the shim too.
ENV BROWSER="/usr/local/bin/xdg-open"

# selkies.py only forwards a client's rate_control_mode (cbr/crf) to the encoder
# when this is enabled; otherwise it silently drops the setting and always runs
# CRF, so the four quality presets' "cbr" ceiling never takes effect and the
# encoder can burst without bound.
ENV SELKIES_ENABLE_RATE_CONTROL="true"

# WeChat has no use for gamepad input; drop the top-bar toggle button and the
# sidebar's "Gamepads" section so no gamepad UI ever renders.
ENV SELKIES_GAMEPAD_ENABLED="false"
ENV SELKIES_UI_SIDEBAR_SHOW_GAMEPADS="false"

# set app name
ENV TITLE="WeChat-Selkies"
ENV TZ="Asia/Shanghai"
ENV LC_ALL="zh_CN.UTF-8"
ENV AUTO_START_WECHAT="true"
ENV AUTO_START_QQ="false"
ENV ENABLE_WECHAT_NIGHTLY_RESTART="false"
ENV WECHAT_NIGHTLY_STOP_TIME="23:30"
ENV WECHAT_NIGHTLY_START_TIME="01:30"
ENV ENABLE_WECHAT_AUTO_LOGIN="true"
ENV AUTO_LOGIN_DELAY="3"
# Keep the main window maximized and present. openbox's <maximized> rule only
# fires when a window first appears and WeChat remembers its own geometry, so
# start.sh also runs a watcher that re-maximizes it, maps it back when WeChat
# hides to the tray, and relaunches the process if it exits.
ENV ENABLE_WECHAT_WINDOW_WATCHDOG="true"
ENV WECHAT_FORCE_MAXIMIZED="true"
ENV WECHAT_WINDOW_CHECK_INTERVAL="5"



# update favicon
RUN cp /usr/share/icons/hicolor/128x128/apps/wechat.png /usr/share/selkies/www/icon.png

# the baseimage's init-nginx writes manifest.json at startup declaring the icon
# as 180x180; the WeChat icon above is 128x128 and Chrome rejects a manifest
# icon whose declared size disagrees with the PNG
RUN sed -i 's/180x180/128x128/' /etc/s6-overlay/s6-rc.d/init-nginx/run && \
    grep -q '128x128' /etc/s6-overlay/s6-rc.d/init-nginx/run

# Chromium deprecated apple-mobile-web-app-capable and warns on every page load
# unless the standard tag is also present; keep both so iOS behaviour is unchanged
RUN sed -i 's|<meta name="apple-mobile-web-app-capable" content="yes">|<meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-capable" content="yes">|' \
        /usr/share/selkies/selkies-dashboard/index.html && \
    grep -q 'name="mobile-web-app-capable"' /usr/share/selkies/selkies-dashboard/index.html

# add local files
COPY /root /

# Source is harmless when the optional dependencies are disabled and keeping it
# separate from /root avoids copying tests/licenses into the container root.
COPY integrations/wechat-history /opt/wechat-history

# Web Push itself is private-history-only. http-ece is pure Python but publishes
# no wheel, so this small, separately hashed set is installed without dependency
# resolution after the main locked environment above.
RUN if [ "$INSTALL_WECHAT_HISTORY" = "true" ]; then \
        /lsiopy/bin/python3 -m pip install \
            --no-cache-dir \
            --no-build-isolation \
            --no-deps \
            --require-hashes \
            --target /opt/wechat-history/site-packages \
            -r /opt/wechat-history/webpush-requirements.lock; \
    fi

# Only a private history-enabled image receives the notification UI, root-scoped
# Service Worker, loopback API proxy and s6 longrun. The public default build is
# byte-for-byte unchanged at each of those runtime paths.
COPY patches/wechat-notifications.js /tmp/wechat-notifications/wechat-notifications.js
COPY patches/wechat-notification-sw.js /tmp/wechat-notifications/wechat-notification-sw.js
COPY patches/wechat-notifications-s6 /tmp/wechat-notifications/s6
COPY patches/install-wechat-notifications.sh /tmp/install-wechat-notifications.sh
RUN sh /tmp/install-wechat-notifications.sh \
        "$INSTALL_WECHAT_HISTORY" /tmp/wechat-notifications && \
    rm -rf /tmp/wechat-notifications /tmp/install-wechat-notifications.sh

# drag a file out of a WeChat chat and download it in the viewer's browser. The
# drag never leaves the remote X11 session — the browser only forwards pointer
# events, so HTML5 drop events cannot fire — which is why the file is caught by
# an XDND window the helper maps over the remote top-right corner for exactly as
# long as a drag lasts, and the page merely mirrors that as a drop zone in place
# of the quality preset bar. The helper runs as root because WeChat's attachment
# directories are 0700 root. Must come after "COPY /root /": the install script
# verifies and chmods /scripts/wechat/wechat-export-drop.py.
COPY patches/wechat-desktop-export.js /tmp/wechat-desktop-export/wechat-desktop-export.js
COPY patches/wechat-export-s6 /tmp/wechat-desktop-export/s6
COPY patches/install-wechat-desktop-export.sh /tmp/install-wechat-desktop-export.sh
RUN sh /tmp/install-wechat-desktop-export.sh /tmp/wechat-desktop-export && \
    rm -rf /tmp/wechat-desktop-export /tmp/install-wechat-desktop-export.sh
