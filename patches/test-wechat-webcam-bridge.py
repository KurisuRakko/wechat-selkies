#!/usr/bin/env python3
"""Regression tests for root/scripts/webcam/wechat-webcam-bridge.py.

两段式验证（镜像 test-audio-silence-gate.py 的 subprocess 风格）：

  1. 直接以子进程跑 --self-test：不碰真实设备/网络，只验证解码逻辑，
     returncode 必须为 0 且 stdout 含 "self-test passed"。
  2. importlib 直接加载脚本（不执行 __main__），单独覆盖
     decode_frame_to_rgb 的三个边界：尺寸相符透传、尺寸不符触发 resize、
     非法 JPEG 抛可预期异常。

若本机缺 numpy/Pillow，可在验证环节改用 docker 构建产物内的
/lsiopy/bin/python3 运行本测试并注明。
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

BRIDGE = (Path(__file__).resolve().parent.parent /
          "root/scripts/webcam/wechat-webcam-bridge.py")


def run_self_test() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BRIDGE), "--self-test"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


# 1. --self-test 子进程：全过则退出码 0 且打印 "self-test passed"。
result = run_self_test()
assert result.returncode == 0, result.stderr
assert "self-test passed" in result.stdout, result.stdout

# 2. importlib 直接加载脚本，逐边界覆盖 decode_frame_to_rgb。
spec = importlib.util.spec_from_file_location("wechat_webcam_bridge", BRIDGE)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

# 现造一张 320x240 的渐变图编码成 JPEG（纯色图在 resize 时可能被插值出
# 边缘差异，渐变图能顺便验证像素内容真的被保留了）。
source = np.zeros((240, 320, 3), dtype=np.uint8)
source[..., 0] = np.linspace(0, 255, 320, dtype=np.uint8)[None, :]
source[..., 1] = 77
source[..., 2] = 200
buffer = io.BytesIO()
Image.fromarray(source, "RGB").save(buffer, format="JPEG", quality=90)
jpeg = buffer.getvalue()

# 2a. 尺寸相符：直接透传，shape/dtype/内容都对（JPEG 有损，留容差）。
frame = module.decode_frame_to_rgb(jpeg, 320, 240)
assert frame.shape == (240, 320, 3), frame.shape
assert frame.dtype == np.uint8, frame.dtype
assert abs(int(frame[0, 0, 0]) - 0) <= 8, "R 渐变起点"
assert abs(int(frame[0, -1, 0]) - 255) <= 8, "R 渐变终点被保留"

# 2b. 尺寸不符：被 resize 成目标尺寸，而不是抛异常。
resized = module.decode_frame_to_rgb(jpeg, 640, 480)
assert resized.shape == (480, 640, 3), resized.shape

# 2c. 非法 JPEG：抛可预期异常（OSError，PIL.UnidentifiedImageError 的子类）。
try:
    module.decode_frame_to_rgb(b"\x00\x01\x02not a jpeg", 640, 480)
except OSError:
    pass
else:
    raise AssertionError("invalid JPEG must raise OSError")

print("wechat-webcam-bridge tests passed")
