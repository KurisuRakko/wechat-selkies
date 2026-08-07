#!/usr/bin/env python3
"""把微信里拖出来的文件交给浏览器下载。

浏览器只是把鼠标事件转发给远端 X11 会话，所以整段拖拽自始至终都发生在容器
里：HTML5 的 dragover/drop 永远不会触发。真正"接住"文件的必须是远端桌面上的
一个 XDND 接收窗口，页面上的提示只是同步出来的视觉反馈。

三个已经在容器里验证过、并且决定了本文件写法的事实：

  * XFixes 的 XdndSelection owner 变更是可靠的"拖拽开始"信号。普通点击（哪怕
    按住 400ms 不放）不会触发它，每次新拖拽都会触发一次，即使 owner 窗口没变。
  * 拖拽期间源程序持有指针 grab，事件收不到；但 XQueryPointer 查的是按键状态而
    不是事件投递，grab 之下依然准确，所以"按键松开"就是可靠的"拖拽结束"。
  * InputOnly 窗口不会被拖拽源选中（Qt 的目标查找会跳过），必须是 InputOutput。
    但只要建窗时 background_pixmap=None，X 就永远不会绘制它：窗口能收 XDND，
    屏幕上一个像素都不变，视频流里也就不会多出一个方块。

助手必须以 root 运行：微信的 /config/xwechat_files/<账户>/ 是 0700 root，桌面
会话的 abc 用户读不到拖出来的那个文件。正因为如此，投放进来的路径一律不可信：
容器内任何以 abc 身份运行的 X 客户端都能伪造一次 XDND 投放，把 /etc/shadow 之类
只有 root 读得到的文件换成一个可下载的 token。所以只有 ALLOWED_ROOTS 白名单目录
下的文件才允许导出，见 allowed_source()。

文件复制到专用的导出目录（不是 /config/Desktop，避免和拖入上传的文件混在一起
形成回环），并按 mtime 只保留最近若干个。浏览器通过 nginx 反代到本进程的
loopback API：SSE 推 drag-start / drag-end / file-exported，下载走一次性 token，
所以导出目录本身不对外暴露，也没有任何路径拼接。
"""

from __future__ import annotations

import json
import os
import posixpath
import queue
import re
import select
import shutil
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

EXPORT_DIR = os.environ.get("WECHAT_EXPORT_DIR", "/config/.host-export")
# 滚动清理：只保留最近这么多个导出文件，避免长期运行把 /config 撑满。
KEEP_FILES = int(os.environ.get("WECHAT_EXPORT_KEEP", "20"))
HTTP_HOST = "127.0.0.1"
HTTP_PORT = int(os.environ.get("WECHAT_EXPORT_PORT", "8766"))

# 接收区按屏幕比例取，不用固定像素：Selkies 会把远端分辨率调成"浏览器视口 ×
# devicePixelRatio"，所以按比例算出来的区域换算回 CSS 像素后，在任何 DPR 下都是
# 差不多大的一块，两侧不需要知道对方的缩放比。
ZONE_W_RATIO, ZONE_H_RATIO = 0.20, 0.15
ZONE_MIN_W, ZONE_MAX_W = 280, 620
ZONE_MIN_H, ZONE_MAX_H = 140, 320
ZONE_MARGIN_RATIO = 0.005

# 拖拽刚开始的一瞬间按键一定还按着，但轮询和 XFixes 之间存在竞态；给一个最短存
# 活时间，避免接收窗口刚建好就被判定为"已松手"而闪一下。
MIN_DRAG_SECONDS = 0.4
# 万一按键状态永远读不到松开（客户端断线等），拖拽状态也不能一直挂着。
MAX_DRAG_SECONDS = 120.0
# 取选区内容的超时。超时就当这次投放失败，不阻塞后面的拖拽。
DROP_TIMEOUT_SECONDS = 10.0

SSE_HEARTBEAT_SECONDS = 15.0

ATOM_NAMES = (
    "XdndAware", "XdndSelection", "XdndEnter", "XdndPosition", "XdndStatus",
    "XdndLeave", "XdndDrop", "XdndFinished", "XdndActionCopy", "XdndTypeList",
    "text/uri-list", "WECHAT_EXPORT_DROP",
)

TAG = "[wechat-export-drop]"


def log(*parts: object) -> None:
    print(TAG, *parts, flush=True)


# --------------------------------------------------------------- uri-list 解析

def parse_uri_list(payload: bytes) -> list[str]:
    """把 text/uri-list 的内容解析成本地路径列表。

    只接受 file: URI。按 RFC 2483，以 # 开头的行是注释。
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = payload.decode("latin-1")

    paths: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = urlsplit(line)
        if parts.scheme != "file":
            log("ignoring non-file uri:", line)
            continue
        # file:///path 与 file://localhost/path 都合法，其它主机名不是本机文件。
        if parts.netloc and parts.netloc.lower() != "localhost":
            log("ignoring remote file uri:", line)
            continue
        path = unquote(parts.path)
        if path and path not in paths:
            paths.append(path)
    return paths


# ------------------------------------------------------------- 投放来源白名单

def parse_allowed_roots(value: str, real=os.path.realpath) -> tuple[str, ...]:
    """把冒号分隔的目录列表解析并 realpath 归一化，启动时算一次。"""
    roots: list[str] = []
    for entry in value.split(":"):
        entry = entry.strip()
        if not entry:
            continue
        resolved = real(entry)
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def allowed_source(path: str, roots: tuple[str, ...],
                   real=os.path.realpath) -> bool:
    """投放进来的路径是否落在白名单目录里。

    先 realpath 把符号链接和 ".." 都消掉，判断的是最终真实位置而不是字面量。
    比较时给 root 补一个分隔符：直接 startswith("/config") 会把 /configfoo
    也算成 /config 下面。
    """
    resolved = real(path)
    for root in roots:
        if resolved == root or resolved.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


# 微信的附件全在 /config/xwechat_files/ 下，默认一个 /config 就够。留空等于禁用
# 导出（fail closed），比放行整个文件系统安全。
ALLOWED_ROOTS = parse_allowed_roots(
    os.environ.get("WECHAT_EXPORT_ALLOWED_ROOTS", "/config"))


# --------------------------------------------------------------- 导出目录管理

_UNSAFE = re.compile(r"[\x00-\x1f/\\]")


def safe_name(name: str) -> str:
    """把投放进来的文件名收敛成一个能安全落盘的纯文件名。"""
    # NFC 归一化，避免 macOS 传来的分解形式在容器里长成另一个名字。
    name = unicodedata.normalize("NFC", name)
    # 分隔符换掉之后就已经不可能穿越目录了，剩下只需挡住 "." 和 ".." 这类空名字。
    name = _UNSAFE.sub("_", name).strip()
    if not name.strip("."):
        name = "file"
    return name[:180]


def unique_path(directory: str, name: str, exists=os.path.exists) -> str:
    """同名文件加序号：a.txt → a (2).txt → a (3).txt。"""
    candidate = os.path.join(directory, name)
    if not exists(candidate):
        return candidate
    stem, ext = os.path.splitext(name)
    for index in range(2, 1000):
        candidate = os.path.join(directory, "%s (%d)%s" % (stem, index, ext))
        if not exists(candidate):
            return candidate
    raise OSError("too many files named %r in %s" % (name, directory))


def config_owner() -> tuple[int, int]:
    """导出目录跟着 /config 的属主走，宿主机上的用户才读得到导出的文件。"""
    try:
        info = os.stat("/config")
        return info.st_uid, info.st_gid
    except OSError:
        return -1, -1


class ExportStore:
    """导出目录 + token 表。token 是唯一对外暴露的引用，没有任何路径拼接。"""

    def __init__(self, directory: str = EXPORT_DIR, keep: int = KEEP_FILES,
                 allowed_roots: tuple[str, ...] = ALLOWED_ROOTS):
        self.directory = directory
        self.keep = max(1, keep)
        self.allowed_roots = allowed_roots
        self.lock = threading.Lock()
        self.tokens: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
        uid, gid = config_owner()
        os.makedirs(self.directory, mode=0o755, exist_ok=True)
        if uid >= 0:
            try:
                os.chown(self.directory, uid, gid)
            except OSError as error:
                log("could not chown export dir:", error)
        self._uid, self._gid = uid, gid

    def add(self, source_path: str) -> tuple[str, str] | None:
        """把一个投放进来的文件复制进导出目录，返回 (token, 展示用文件名)。"""
        # 白名单检查放在 add 的入口而不是调用方：本进程是 root，凡是"把任意路径
        # 变成可下载 token"的入口都必须过这一关，以后多一条调用路径也自动被覆盖。
        if not allowed_source(source_path, self.allowed_roots):
            log("refusing to export outside %s: %s"
                % (":".join(self.allowed_roots) or "<empty allowlist>", source_path))
            return None
        if not os.path.isfile(source_path):
            log("dropped path is not a regular file:", source_path)
            return None
        name = safe_name(os.path.basename(source_path))
        with self.lock:
            target = unique_path(self.directory, name)
            # 用 copyfile 而不是 copy2：滚动清理按 mtime 排序，保留源文件的 mtime
            # 会让"只留最近 N 个"变成按"微信什么时候收到"来淘汰，同一个聊天文件
            # 导出两次甚至分不出先后。
            shutil.copyfile(source_path, target)
            os.chmod(target, 0o644)
            if self._uid >= 0:
                try:
                    os.chown(target, self._uid, self._gid)
                except OSError:
                    pass
            token = os.urandom(12).hex()
            self.tokens[token] = (target, os.path.basename(target))
            self._prune_locked()
        log("exported", source_path, "->", target)
        return token, os.path.basename(target)

    def resolve(self, token: str) -> tuple[str, str] | None:
        with self.lock:
            return self.tokens.get(token)

    def _prune_locked(self) -> None:
        try:
            entries = [
                os.path.join(self.directory, entry)
                for entry in os.listdir(self.directory)
            ]
            files = [path for path in entries if os.path.isfile(path)]
        except OSError as error:
            log("could not list export dir:", error)
            return
        files.sort(key=lambda path: os.path.getmtime(path), reverse=True)
        for stale in files[self.keep:]:
            try:
                os.remove(stale)
                log("pruned", stale)
            except OSError as error:
                log("could not prune", stale, error)
        alive = set(files[: self.keep])
        for token in [t for t, (path, _) in self.tokens.items() if path not in alive]:
            del self.tokens[token]


# ------------------------------------------------------------------ SSE 广播

class Broker:
    """给每个订阅者一条独立队列，X 线程只管往里塞，慢客户端不会拖住拖拽。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.subscribers: list[queue.Queue] = []
        self.state: dict = {"dragging": False, "zone": None, "screen": None}

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=64)
        with self.lock:
            self.subscribers.append(channel)
            # 拖拽中途才连上来的标签页也要能立刻画出投放区。hello 必须在锁内入队：
            # 放到锁外的话，并发的 publish 可能抢先把 drag-start 塞进这条队列，
            # 随后过期的 hello(dragging:false) 才到，浏览器就把刚画出来的投放区
            # 又藏掉了。刚建的有界队列必空，这个 put 不会阻塞。
            channel.put(("hello", dict(self.state)))
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self.lock:
            if channel in self.subscribers:
                self.subscribers.remove(channel)

    def publish(self, event: str, payload: dict) -> None:
        with self.lock:
            if event == "drag-start":
                self.state = {"dragging": True, "zone": payload.get("zone"),
                              "screen": payload.get("screen")}
            elif event == "drag-end":
                self.state = {"dragging": False, "zone": None, "screen": None}
            channels = list(self.subscribers)
        for channel in channels:
            try:
                channel.put_nowait((event, payload))
            except queue.Full:
                pass


# ------------------------------------------------------------------ 导出线程

class Exporter:
    """把复制文件这件事挪出 X 事件循环。

    一次投放可能是几百 MB 的视频，shutil.copyfile 是同步的：留在
    on_selection_notify 里会把指针轮询和 drag-end 一起卡住，投放区要多挂好几秒。

    一条工作线程就够，不必每次投放起一个：它顺序处理队列，多次投放的
    file-exported 事件因此不会交错，ExportStore 自己有锁也不需要额外同步。

    线程是 daemon，退出时不排空队列。s6 停服务时正在复制的那个文件会留下一个残
    片，但它还没拿到 token、浏览器也没收到过它的 file-exported，滚动清理下次就
    把它淘汰掉；反过来，为一次几百 MB 的复制拖住容器关机才是真的坏。
    """

    def __init__(self, store: ExportStore, broker: Broker):
        self.store = store
        self.broker = broker
        self.queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True,
                         name="wechat-export-copy").start()

    def submit(self, payload: bytes) -> None:
        self.queue.put(payload)

    def _run(self) -> None:
        while True:
            payload = self.queue.get()
            try:
                self._export(payload)
            except Exception as error:   # 工作线程一死，功能就静默失效
                log("export worker failed:", error)

    def _export(self, payload: bytes) -> None:
        for path in parse_uri_list(payload):
            try:
                added = self.store.add(path)
            except OSError as error:
                log("export failed for", path, error)
                continue
            if not added:
                continue
            token, name = added
            self.broker.publish("file-exported", {
                "name": name,
                "url": "wechat-export/file/" + token,
            })


# ------------------------------------------------------------------ HTTP API

def make_handler(broker: Broker, store: ExportStore):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "wechat-export/1.0"

        def log_message(self, fmt, *args):  # noqa: N802 - stdlib 接口
            pass  # 每次下载都往日志里刷一行没有意义

        def do_GET(self):  # noqa: N802 - stdlib 接口
            # nginx 用带斜杠的 proxy_pass 把前缀剥掉了，所以这里看到的永远是
            # /events、/file/<token>，与部署的 SUBFOLDER 无关。
            path = posixpath.normpath(urlsplit(self.path).path)
            if path == "/events":
                self.serve_events()
            elif path.startswith("/file/"):
                self.serve_file(path[len("/file/"):])
            elif path == "/health":
                self.send_json({"ok": True, "dragging": broker.state["dragging"]})
            else:
                self.send_error(404)

        def send_json(self, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def serve_events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            # 长度未知的流，靠关闭连接来定界；EventSource 会自己重连。
            self.send_header("Connection", "close")
            # 反代链路上任何一层缓冲都会毁掉 SSE 的实时性。
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            channel = broker.subscribe()
            try:
                while True:
                    try:
                        event, payload = channel.get(timeout=SSE_HEARTBEAT_SECONDS)
                        frame = "event: %s\ndata: %s\n\n" % (
                            event, json.dumps(payload, ensure_ascii=False))
                    except queue.Empty:
                        frame = ": ping\n\n"   # 注释帧，只为了让连接不被判死
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                broker.unsubscribe(channel)

        def serve_file(self, token: str) -> None:
            entry = store.resolve(unquote(token))
            if not entry:
                self.send_error(404)
                return
            path, name = entry
            try:
                size = os.path.getsize(path)
                handle = open(path, "rb")
            except OSError:
                self.send_error(404)
                return
            with handle:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                # 文件名基本都是中文，只有 RFC 5987 的 filename* 不会被打成乱码；
                # 朴素的 filename= 留给不认它的客户端。
                self.send_header(
                    "Content-Disposition",
                    "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
                        _ascii_fallback(name), quote(name, safe="")))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                shutil.copyfileobj(handle, self.wfile)

    return Handler


def _ascii_fallback(name: str) -> str:
    """给不认 filename* 的客户端留一个纯 ASCII 名字。"""
    return "".join(
        ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name
    ) or "export"


# ----------------------------------------------------------------- X11 主循环

class DropZone:
    """拖拽期间才存在的那个"看不见的接收窗口"。"""

    def __init__(self, display, atoms: dict, broker: Broker, exporter: Exporter):
        # Xlib 的导入全部延后到这里：--self-test 只验证解析和改名，必须能在没装
        # python-xlib 的机器上直接跑（比如构建前在开发机上跑一遍）。
        from Xlib import X, Xatom
        from Xlib.protocol import event as xevent

        self.X, self.Xatom, self.xevent = X, Xatom, xevent
        self.display = display
        self.root = display.screen().root
        self.atoms = atoms
        self.broker = broker
        self.exporter = exporter

        self.window = None
        self.rect = (0, 0, 0, 0)
        self.screen = (0, 0)
        self.started_at = 0.0
        self.source = None
        self.accepting = False
        self.pending_drop: tuple[object, float] | None = None

    # ---- 几何

    def compute_rect(self) -> tuple[tuple[int, int, int, int], tuple[int, int]]:
        geometry = self.root.get_geometry()
        width, height = int(geometry.width), int(geometry.height)
        zone_w = int(min(ZONE_MAX_W, max(ZONE_MIN_W, round(width * ZONE_W_RATIO))))
        zone_h = int(min(ZONE_MAX_H, max(ZONE_MIN_H, round(height * ZONE_H_RATIO))))
        zone_w, zone_h = min(zone_w, width), min(zone_h, height)
        margin = int(round(width * ZONE_MARGIN_RATIO))
        x = max(0, width - margin - zone_w)
        y = min(margin, max(0, height - zone_h))
        return (x, y, zone_w, zone_h), (width, height)

    # ---- 生命周期

    @property
    def active(self) -> bool:
        return self.window is not None

    def begin(self) -> None:
        if self.active:
            return
        self.rect, self.screen = self.compute_rect()
        x, y, width, height = self.rect
        # background_pixmap=None 是关键：窗口能被拖拽源找到，但 X 永远不绘制它，
        # 屏幕内容一个像素都不变，所以视频编码器也不会因此产生新的一帧。
        self.window = self.root.create_window(
            x, y, width, height, 0, self.display.screen().root_depth,
            self.X.InputOutput, self.X.CopyFromParent,
            background_pixmap=self.X.NONE,
            override_redirect=True,
            event_mask=self.X.StructureNotifyMask,
        )
        self.window.change_property(self.atoms["XdndAware"], self.Xatom.ATOM, 32, [5])
        self.window.map()
        self.window.configure(stack_mode=self.X.Above)
        self.display.sync()
        self.started_at = time.time()
        self.source = None
        self.accepting = False
        self.pending_drop = None
        self.broker.publish("drag-start", {
            "zone": {"x": x, "y": y, "w": width, "h": height},
            "screen": {"w": self.screen[0], "h": self.screen[1]},
        })
        log("drag started, receiver at %d,%d %dx%d on %dx%d screen"
            % (x, y, width, height, self.screen[0], self.screen[1]))

    def end(self, reason: str) -> None:
        if not self.active:
            return
        try:
            self.window.destroy()
            self.display.sync()
        except Exception as error:  # 拆窗口失败不该拖垮整个助手
            log("could not destroy receiver:", error)
        self.window = None
        self.source = None
        self.accepting = False
        self.pending_drop = None
        self.broker.publish("drag-end", {"reason": reason})
        log("drag ended:", reason)

    # ---- XDND 协议

    def offers_uri_list(self, event_data: list[int]) -> bool:
        wanted = self.atoms["text/uri-list"]
        if wanted in event_data[2:5]:
            return True
        if not event_data[1] & 1:      # 类型不超过三个，全在消息里了
            return False
        try:
            source = self.display.create_resource_object("window", event_data[0])
            prop = source.get_full_property(self.atoms["XdndTypeList"], self.Xatom.ATOM)
            return bool(prop) and wanted in list(prop.value)
        except Exception as error:
            log("could not read XdndTypeList:", error)
            return False

    def on_client_message(self, event) -> None:
        if not self.active or event.window.id != self.window.id:
            return
        data = event.data[1]
        kind = event.client_type
        if kind == self.atoms["XdndEnter"]:
            self.source = data[0]
            self.accepting = self.offers_uri_list(data)
            log("XdndEnter from 0x%x, uri-list %s"
                % (data[0], "offered" if self.accepting else "missing"))
        elif kind == self.atoms["XdndPosition"]:
            self.send_status(data[0])
        elif kind == self.atoms["XdndLeave"]:
            self.source = None
            self.accepting = False
        elif kind == self.atoms["XdndDrop"]:
            self.on_drop(data[0], data[2])

    def send_status(self, source_id: int) -> None:
        x, y, width, height = self.rect
        message = self.xevent.ClientMessage(
            window=source_id, client_type=self.atoms["XdndStatus"],
            data=(32, [
                self.window.id,
                1 if self.accepting else 0,
                (x << 16) | y,
                (width << 16) | height,
                self.atoms["XdndActionCopy"],
            ]))
        self.display.create_resource_object("window", source_id).send_event(
            message, event_mask=self.X.NoEventMask)
        self.display.flush()

    def on_drop(self, source_id: int, timestamp: int) -> None:
        if not self.accepting:
            self.send_finished(source_id, accepted=False)
            return
        self.window.convert_selection(
            self.atoms["XdndSelection"], self.atoms["text/uri-list"],
            self.atoms["WECHAT_EXPORT_DROP"], timestamp)
        self.display.flush()
        # 取选区是异步的：记下来，等 SelectionNotify 回到主循环再处理，别在这里
        # 嵌套等待把指针轮询也堵住。
        self.pending_drop = (source_id, time.time() + DROP_TIMEOUT_SECONDS)

    def on_selection_notify(self, event) -> None:
        if not self.pending_drop or not self.active:
            return
        source_id, _ = self.pending_drop
        self.pending_drop = None
        payload = b""
        if event.property:
            prop = self.window.get_full_property(
                self.atoms["WECHAT_EXPORT_DROP"], self.X.AnyPropertyType)
            if prop:
                value = prop.value
                payload = value if isinstance(value, bytes) else bytes(value)
            self.window.delete_property(self.atoms["WECHAT_EXPORT_DROP"])
        # XDND 的应答不能等复制：源程序在收到 XdndFinished 前一直是"拖拽中"。
        self.send_finished(source_id, accepted=bool(payload))
        if payload:
            self.exporter.submit(payload)

    def send_finished(self, source_id: int, accepted: bool) -> None:
        message = self.xevent.ClientMessage(
            window=source_id, client_type=self.atoms["XdndFinished"],
            data=(32, [self.window.id if self.active else 0,
                       1 if accepted else 0,
                       self.atoms["XdndActionCopy"] if accepted else 0, 0, 0]))
        try:
            self.display.create_resource_object("window", source_id).send_event(
                message, event_mask=self.X.NoEventMask)
            self.display.flush()
        except Exception as error:
            log("could not send XdndFinished:", error)

    def tick(self) -> None:
        """每轮检查拖拽是不是该结束了。"""
        if not self.active:
            return
        now = time.time()
        if self.pending_drop and now > self.pending_drop[1]:
            log("selection transfer timed out")
            self.send_finished(self.pending_drop[0], accepted=False)
            self.pending_drop = None
        if now - self.started_at > MAX_DRAG_SECONDS:
            self.end("timeout")
            return
        if now - self.started_at < MIN_DRAG_SECONDS:
            return
        if self.pending_drop:
            return          # 投放已经发生，等数据传完再收摊
        if not self.buttons_held():
            self.end("released")

    def buttons_held(self) -> bool:
        # 拖拽期间源程序抓着指针，事件收不到；但按键状态查询不受 grab 影响。
        mask = (self.X.Button1Mask | self.X.Button2Mask | self.X.Button3Mask)
        try:
            return bool(self.root.query_pointer().mask & mask)
        except Exception as error:
            log("query_pointer failed:", error)
            return False


def open_display(name: str):
    from Xlib import display as xdisplay

    delay = 1.0
    while True:
        try:
            return xdisplay.Display(name)
        except Exception as error:
            log("waiting for X display %s (%s)" % (name, error))
            time.sleep(delay)
            delay = min(delay * 2, 15.0)


def run() -> int:
    from Xlib import X

    display_name = os.environ.get("DISPLAY", ":1")
    display = open_display(display_name)
    atoms = {name: display.get_atom(name) for name in ATOM_NAMES}
    root = display.screen().root

    display.xfixes_query_version()
    xfixes_base = display.query_extension("XFIXES").first_event
    # 只关心 SetSelectionOwner；owner 被销毁/客户端退出不代表一次拖拽。
    display.xfixes_select_selection_input(root, atoms["XdndSelection"], 1)
    display.sync()

    broker = Broker()
    store = ExportStore()
    exporter = Exporter(store, broker)
    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), make_handler(broker, store))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True,
                     name="wechat-export-http").start()
    log("listening on http://%s:%d, exporting to %s (keep %d)"
        % (HTTP_HOST, HTTP_PORT, store.directory, store.keep))
    log("allowed drop sources:", ":".join(store.allowed_roots) or "<none, export disabled>")

    zone = DropZone(display, atoms, broker, exporter)
    log("watching XdndSelection on", display_name)

    while True:
        timeout = 0.05 if zone.active else 1.0
        try:
            select.select([display.fileno()], [], [], timeout)
        except (OSError, ValueError) as error:
            log("display select failed:", error)
            return 1
        while display.pending_events():
            event = display.next_event()
            if event.type == xfixes_base:
                zone.begin()
            elif event.type == X.ClientMessage:
                zone.on_client_message(event)
            elif event.type == X.SelectionNotify:
                zone.on_selection_notify(event)
        zone.tick()


def self_test() -> int:
    """不碰 X、不碰网络，只验证 uri-list 解析和改名逻辑。"""
    assert parse_uri_list(b"file:///tmp/a.txt\r\n") == ["/tmp/a.txt"]
    assert parse_uri_list(b"file:///tmp/a.txt\nfile:///tmp/b.txt\n") == \
        ["/tmp/a.txt", "/tmp/b.txt"]
    # 微信给的文件名带中文和空格，一律是 percent-encoded。
    assert parse_uri_list("file:///config/%E6%B5%8B%20%E8%AF%95.docx\r\n"
                          .encode()) == ["/config/测 试.docx"]
    assert parse_uri_list(b"file://localhost/tmp/a.txt\r\n") == ["/tmp/a.txt"]
    assert parse_uri_list(b"#comment\r\nfile:///tmp/a.txt\r\n") == ["/tmp/a.txt"]
    assert parse_uri_list(b"http://example.com/a.txt\r\n") == []
    assert parse_uri_list(b"file://other-host/tmp/a.txt\r\n") == []
    assert parse_uri_list(b"") == []
    # 同一个文件在一次投放里出现两次只导出一次。
    assert parse_uri_list(b"file:///tmp/a.txt\r\nfile:///tmp/a.txt\r\n") == ["/tmp/a.txt"]

    assert safe_name("a/b.txt") == "a_b.txt"
    assert safe_name("../../etc/passwd") == ".._.._etc_passwd"
    assert safe_name("") == "file"
    assert safe_name("..") == "file"
    assert safe_name("图 片.png") == "图 片.png"
    assert safe_name("a\x00b.txt") == "a_b.txt"

    # 白名单：注入假的 realpath，好在没有 /config 的开发机上也能跑。
    def same(path: str) -> str:
        return path

    roots = ("/config",)
    assert allowed_source("/config/xwechat_files/wxid_x/msg/a.docx", roots, real=same)
    assert allowed_source("/config", roots, real=same), "白名单目录本身也算通过"
    assert not allowed_source("/etc/shadow", roots, real=same)
    # 前缀误判：/configfoo 不在 /config 下面。
    assert not allowed_source("/configfoo/x", roots, real=same)
    assert not allowed_source("/configfoo/x", ("/config/",), real=same), "尾斜杠同理"
    # 符号链接指向白名单之外：判断依据是 realpath 的结果，不是路径字面量。
    assert not allowed_source("/config/evil-link", roots,
                              real=lambda path: "/etc/shadow")
    assert allowed_source("/config/ok-link", roots,
                          real=lambda path: "/config/real.docx")
    # ".." 穿越同样由 realpath 消掉。
    assert not allowed_source("/config/../etc/shadow", roots, real=os.path.normpath)

    assert parse_allowed_roots("/config", real=same) == ("/config",)
    assert parse_allowed_roots("/config:/srv/share", real=same) == \
        ("/config", "/srv/share")
    assert parse_allowed_roots(" /config : /srv/share ", real=same) == \
        ("/config", "/srv/share")
    assert parse_allowed_roots("/config::/config", real=same) == ("/config",)
    # 留空 = 什么都不许导出，而不是什么都放行。
    assert parse_allowed_roots("", real=same) == ()
    assert not allowed_source("/config/a.docx", (), real=same)

    taken = {"/e/a.txt", "/e/a (2).txt"}
    assert unique_path("/e", "a.txt", taken.__contains__) == "/e/a (3).txt"
    assert unique_path("/e", "b.txt", taken.__contains__) == "/e/b.txt"
    assert unique_path("/e", "a.tar.gz", taken.__contains__) == "/e/a.tar.gz"

    assert _ascii_fallback("测 试.docx") == "_ _.docx"
    assert _ascii_fallback("plain.txt") == "plain.txt"

    print("wechat-export-drop self-test passed")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        raise SystemExit(self_test())
    raise SystemExit(run())
