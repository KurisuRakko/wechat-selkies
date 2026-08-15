#!/usr/bin/env python3
"""把浏览器送来的 JPEG 帧写入 v4l2loopback 虚拟摄像头（微信视频通话的数据源）。

浏览器端（patches/wechat-webcam-forward.js）用 getUserMedia 采集、离屏 canvas
按 15fps 编码成裸 JPEG 帧，经专用 WebSocket（nginx 反代，location
SUBFOLDERwechat-webcam/）推给本服务；本服务解码成 RGB ndarray 后交给
pyfakewebcam 的 FakeWebcam.schedule_frame()，写入宿主映射进来的 /dev/videoN。
这条链路与 Selkies 自己的数据通道完全独立，与上游没有任何关系。

本服务必须以 root 运行：/dev/videoN 设备节点的属主在不同宿主上不可预测
（v4l2loopback 创建的节点通常属 root，但也可能跟随创建者的 uid/gid），以 root
写入最稳；这与 wechat-export-drop.py 以 root 运行是同一个理由。容器默认用户
abc 读不到属主不匹配的设备节点，而 FakeWebcam 打开设备失败会直接抛异常。

pyfakewebcam 刻意延迟到 _camera_or_none() 内部才 import：--self-test 只验证
JPEG 解码逻辑，在没有该依赖的开发机上也能直接跑（与 wechat-export-drop.py 的
Xlib 延迟导入同一约定）。

环境变量：

  WECHAT_WEBCAM_DEVICE      虚拟摄像头设备节点，默认 /dev/video10
  WECHAT_WEBCAM_WIDTH       输出宽度，默认 640
  WECHAT_WEBCAM_HEIGHT      输出高度，默认 480
  WECHAT_WEBCAM_BRIDGE_PORT loopback 监听端口，默认 8767

整个功能是可选的（构建期 INSTALL_WEBCAM_FORWARD），且仅对已加载 v4l2loopback
的 Linux 宿主有意义：Windows + Docker Desktop（WSL2 backend）宿主无法加载
自定义内核模块，不支持此功能，详见 docs/webcam-forwarding.md。
"""

from __future__ import annotations

import asyncio
import io
import os
import sys

import numpy as np
from PIL import Image

# v4l2 的像素格式约定是 RGB24；pyfakewebcam 的 schedule_frame() 也要求 RGB。

DEVICE = os.environ.get("WECHAT_WEBCAM_DEVICE", "/dev/video10")
WIDTH = int(os.environ.get("WECHAT_WEBCAM_WIDTH", "640"))
HEIGHT = int(os.environ.get("WECHAT_WEBCAM_HEIGHT", "480"))
PORT = int(os.environ.get("WECHAT_WEBCAM_BRIDGE_PORT", "8767"))
# 640x480 的 JPEG 最大帧远小于这个值；2 MB 上限只是防呆，不让畸形帧撑爆内存。
MAX_MESSAGE_BYTES = 2_000_000

TAG = "[wechat-webcam-bridge]"


def log(*parts: object) -> None:
    print(TAG, *parts, flush=True)


def _websockets():
    """websockets 延迟导入：--self-test 只测解码逻辑，不需要它。

    与 pyfakewebcam 的延迟导入同一约定——让 --self-test 在没有完整依赖的
    开发机上也能直接跑。
    """
    import websockets  # noqa: PLC0415
    return websockets


# ------------------------------------------------------------------ 帧解码

def decode_frame_to_rgb(data: bytes, width: int, height: int) -> np.ndarray:
    """把一帧 JPEG 字节解码成定长 (height, width, 3) 的 uint8 RGB ndarray。

    尺寸与目标不符时用 Pillow resize 收敛，不拒收（浏览器侧 canvas 固定
    640x480，但窗口级调试钩子可能改了帧率/质量之外的缩放路径，宿主可能
    配置了别的 WECHAT_WEBCAM_WIDTH/HEIGHT）。非法 JPEG 抛 OSError
    （PIL.UnidentifiedImageError 是其子类），由调用方按帧丢弃。
    """
    image = Image.open(io.BytesIO(data))
    image.load()  # 真正触发解码：截断/损坏的 JPEG 在这里才抛异常
    if image.size != (width, height):
        image = image.resize((width, height), Image.BILINEAR)
    rgb = image.convert("RGB")
    frame = np.asarray(rgb, dtype=np.uint8)
    # 兜底整形：某些模式（如 P 模式转换）可能产生 (h, w, 3) 以外的形状。
    return frame.reshape(height, width, 3)


# ------------------------------------------------------------------ 桥接服务

class Bridge:
    """每连接一个 handler 实例共享同一个相机；打开失败不重试、不崩溃。"""

    def __init__(self) -> None:
        self._camera = None
        self._camera_failure_logged = False

    def _camera_or_none(self):
        """惰性打开 FakeWebcam；失败只记一次错误日志，之后安静丢帧。

        设备节点不存在（宿主没 modprobe v4l2loopback、或没把 /dev/videoN 传
        进容器）时，绝不能让 bridge 崩溃或反复刷日志把 s6 判定为 unhealthy。
        """
        if self._camera is not None:
            return self._camera
        try:
            # 延迟 import：--self-test 不依赖 pyfakewebcam。
            from pyfakewebcam import FakeWebcam
            self._camera = FakeWebcam(DEVICE, WIDTH, HEIGHT)
            log("virtual camera ready on", DEVICE, "%dx%d" % (WIDTH, HEIGHT))
        except Exception as error:
            if not self._camera_failure_logged:
                self._camera_failure_logged = True
                log("virtual camera unavailable on", DEVICE,
                    "- dropping frames:", error)
            return None
        return self._camera

    async def handle_client(self, websocket) -> None:
        log("client connected:", websocket.remote_address)
        try:
            async for message in websocket:
                # 只收二进制帧；浏览器端发的是裸 JPEG（无类型前缀字节）。
                if not isinstance(message, (bytes, bytearray)):
                    continue
                try:
                    frame = decode_frame_to_rgb(bytes(message), WIDTH, HEIGHT)
                    camera = self._camera_or_none()
                    if camera is not None:
                        camera.schedule_frame(frame)
                except Exception as error:
                    # 单帧损坏只丢这一帧，不影响后续帧和连接。
                    log("dropping bad frame:", error)
        except _websockets().exceptions.ConnectionClosed:
            pass
        finally:
            log("client disconnected")


# ------------------------------------------------------------------ 入口

async def main() -> int:
    websockets = _websockets()
    bridge = Bridge()
    log("listening on 127.0.0.1:%d, device %s (%dx%d)"
        % (PORT, DEVICE, WIDTH, HEIGHT))
    # 只绑定 loopback：对外暴露交给 nginx 反代（location SUBFOLDERwechat-webcam/）。
    async with websockets.serve(bridge.handle_client, "127.0.0.1", PORT,
                                max_size=MAX_MESSAGE_BYTES):
        await asyncio.Future()  # 永久运行，直到 s6 停服务


def self_test() -> int:
    """不碰真实设备、不碰网络：只用 Pillow 现造图验证解码逻辑。"""
    # 造一张 320x240 的纯色图，编码成 JPEG 后走 decode_frame_to_rgb。
    source = Image.new("RGB", (320, 240), (200, 30, 40))
    buffer = io.BytesIO()
    source.save(buffer, format="JPEG", quality=85)
    jpeg = buffer.getvalue()

    # 尺寸相符：直接透传，shape/dtype 与 RGB 通道顺序都要对。JPEG 有损，
    # 色值断言留容差（quality 85 下每通道偏移通常不超过 4）。
    frame = decode_frame_to_rgb(jpeg, 320, 240)
    assert frame.shape == (240, 320, 3), frame.shape
    assert frame.dtype == np.uint8, frame.dtype
    assert abs(int(frame[0, 0, 0]) - 200) <= 8, "R 通道在第一位"
    assert abs(int(frame[0, 0, 1]) - 30) <= 8
    assert abs(int(frame[0, 0, 2]) - 40) <= 8

    # 尺寸不符：被 resize 而不是抛异常。
    resized = decode_frame_to_rgb(jpeg, 640, 480)
    assert resized.shape == (480, 640, 3), resized.shape

    # 非法 JPEG：抛可预期的 OSError（PIL.UnidentifiedImageError 是其子类）。
    try:
        decode_frame_to_rgb(b"this is not a jpeg", 640, 480)
    except OSError:
        pass
    else:
        raise AssertionError("invalid JPEG must raise OSError")

    # 截断的 JPEG 同样必须是 OSError。
    truncated = jpeg[: len(jpeg) // 2]
    try:
        decode_frame_to_rgb(truncated, 320, 240)
    except OSError:
        pass
    else:
        raise AssertionError("truncated JPEG must raise OSError")

    print("self-test passed")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        raise SystemExit(self_test())
    raise SystemExit(asyncio.run(main()))
