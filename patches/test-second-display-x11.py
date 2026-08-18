#!/usr/bin/env python3
"""X11 集成测试：在一次性容器里验证 second_display 守护进程真的会按 RandR
状态搬动窗口，而不仅仅是 classify()/layout() 的纯函数逻辑自洽。

只对镜像 wechat-selkies:second-display-test（docker build -t 该标签 . 产物，
不是 :latest）生效——:latest 没有本次新增的功能。

裸 assert 风格，直接跑：

    python3 patches/test-second-display-x11.py

流程：
  1. 起一个一次性容器（AUTO_START_WECHAT=false 不启动真微信，
     ENABLE_WECHAT_WINDOW_WATCHDOG=false 避免主窗口看门狗和本测试的假窗口
     打架，ENABLE_WECHAT_SECOND_DISPLAY=true 打开本功能）。
  2. 轮询状态端点等守护进程就绪。
  3. 容器内起一个常驻的 python-xlib 进程，造 3 个假窗口（1 个满足主窗口
     判据的 wechat 窗、2 个 WeChatAppEx 小程序窗），写出窗口 id 到文件。
     该进程必须常驻：X11 语义下客户端断开连接会连带销毁它创建的窗口。
  4. 用 xrandr --setmonitor 模拟 selkies-primary/selkies-display2 同时出现，
     轮询直到两个 WeChatAppEx 窗口的几何落进 display2 矩形（允许 ±32px
     误差——openbox 装饰可能造成几点偏移），同时主窗口几何全程不变。
  5. 用 xrandr --delmonitor selkies-display2 模拟副屏断开，轮询直到两个
     窗口的几何收回落进 primary 矩形。
  6. docker rm -f 清理（finally 块，无论成败都执行）。

只用文本断言：xdotool getwindowgeometry --shell 的输出解析、HTTP JSON
响应字段。不做任何视觉/截图判断。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

IMAGE = sys.argv[1] if len(sys.argv) > 1 else "wechat-selkies:second-display-test"
CONTAINER = "wechat-second-display-x11-test"
READY_TIMEOUT_S = 60
RECONCILE_TIMEOUT_S = 15
POLL_INTERVAL_S = 1.0
GEOMETRY_TOLERANCE_PX = 32

# 容器内常驻的窗口制造者：必须保持进程存活并持有同一条 X11 连接，否则
# 连接一断，服务端会按 X11 语义连带销毁这条连接创建的全部窗口。
WINDOW_FACTORY_SCRIPT = """
import json
import time

from Xlib import X, Xutil, display

disp = display.Display()
screen = disp.screen()
root = screen.root


def make_window(x, y, w, h, wm_class):
    win = root.create_window(
        x, y, w, h, 0, screen.root_depth,
        X.InputOutput, X.CopyFromParent,
        background_pixel=screen.white_pixel,
        event_mask=X.StructureNotifyMask,
    )
    win.set_wm_class(wm_class, wm_class)
    win.set_wm_name(wm_class + "-test-window")
    # 固定尺寸 hint：没有 WM_NORMAL_HINTS 时 openbox 会把窗口当成"没有尺寸
    # 偏好"处理，观察到的实际效果是直接铺满工作区，而不是保留 CreateWindow
    # 请求的宽高。min==max==请求值能让 openbox 尊重这个尺寸。
    win.set_wm_normal_hints(
        flags=(Xutil.PPosition | Xutil.PSize | Xutil.PMinSize | Xutil.PMaxSize),
        min_width=w, min_height=h, max_width=w, max_height=h,
    )
    win.map()
    disp.sync()
    return win.id


windows = {
    "main": make_window(50, 50, 700, 700, "wechat"),
    "movable1": make_window(100, 100, 300, 300, "WeChatAppEx"),
    "movable2": make_window(500, 100, 300, 300, "WeChatAppEx"),
}

with open("/tmp/second_display_test_windows.json", "w") as f:
    json.dump(windows, f)

while True:
    time.sleep(3600)
"""


class TestFailure(AssertionError):
    pass


def run(args: list[str], timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def docker_exec(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return run(["docker", "exec", CONTAINER, *args], timeout=timeout)


def require_image() -> None:
    result = run(["docker", "image", "inspect", IMAGE], timeout=15)
    if result.returncode != 0:
        raise TestFailure(
            f"image {IMAGE} not found — build it first: "
            f"docker build -t {IMAGE} ."
        )


def start_container() -> None:
    run(["docker", "rm", "-f", CONTAINER], timeout=15)  # 容错：清掉上次失败留下的同名容器
    result = run(
        [
            "docker", "run", "-d", "--rm", "--name", CONTAINER,
            "-e", "AUTO_START_WECHAT=false",
            "-e", "ENABLE_WECHAT_WINDOW_WATCHDOG=false",
            "-e", "ENABLE_WECHAT_SECOND_DISPLAY=true",
            "--tmpfs", "/config",
            IMAGE,
        ],
        timeout=30,
    )
    if result.returncode != 0:
        raise TestFailure(f"docker run failed: {result.stderr}")


def wait_for_ready() -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    last_error = ""
    while time.monotonic() < deadline:
        result = docker_exec(
            "curl", "-sf", "127.0.0.1:8768/wechat-second-display/api/status", timeout=5
        )
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            if payload.get("version") == 1:
                return
        last_error = result.stderr or result.stdout
        time.sleep(1)
    raise TestFailure(f"daemon status endpoint never became ready: {last_error}")


def create_test_windows() -> dict[str, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
        handle.write(WINDOW_FACTORY_SCRIPT)
        local_path = handle.name
    try:
        remote_path = "/tmp/second_display_window_factory.py"
        copy = run(["docker", "cp", local_path, f"{CONTAINER}:{remote_path}"], timeout=15)
        if copy.returncode != 0:
            raise TestFailure(f"docker cp failed: {copy.stderr}")
    finally:
        Path(local_path).unlink(missing_ok=True)

    # -d：后台常驻，保持这条 X11 连接不断，窗口才不会随连接关闭被服务端回收。
    launch = run(
        ["docker", "exec", "-d", CONTAINER, "env", "DISPLAY=:1",
         "/lsiopy/bin/python3", remote_path],
        timeout=15,
    )
    if launch.returncode != 0:
        raise TestFailure(f"failed to launch window factory: {launch.stderr}")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = docker_exec("cat", "/tmp/second_display_test_windows.json", timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            windows = json.loads(result.stdout)
            return {name: str(window_id) for name, window_id in windows.items()}
        time.sleep(0.5)
    raise TestFailure("window factory never wrote out its window id manifest")


def get_geometry(window_id: str) -> tuple[int, int, int, int]:
    result = docker_exec(
        "env", "DISPLAY=:1", "xdotool", "getwindowgeometry", "--shell", window_id,
        timeout=10,
    )
    if result.returncode != 0:
        raise TestFailure(f"getwindowgeometry failed for {window_id}: {result.stderr}")
    fields = dict(
        line.split("=", 1) for line in result.stdout.strip().splitlines() if "=" in line
    )
    return (int(fields["X"]), int(fields["Y"]), int(fields["WIDTH"]), int(fields["HEIGHT"]))


def within_rect(
    geometry: tuple[int, int, int, int],
    target: tuple[int, int, int, int],
    tolerance: int = GEOMETRY_TOLERANCE_PX,
) -> bool:
    """geometry 是否落在 target 矩形内，target 四边各放宽 tolerance 像素。"""
    x, y, w, h = geometry
    tx, ty, tw, th = target
    return (
        x >= tx - tolerance
        and y >= ty - tolerance
        and x + w <= tx + tw + tolerance
        and y + h <= ty + th + tolerance
    )


def close_to(a: tuple[int, int, int, int], b: tuple[int, int, int, int], tolerance: int = GEOMETRY_TOLERANCE_PX) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


def setup_display2(primary: tuple[int, int, int, int], display2: tuple[int, int, int, int]) -> None:
    def geom(rect: tuple[int, int, int, int]) -> str:
        x, y, w, h = rect
        return f"{w}/0x{h}/0+{x}+{y}"

    # 已有一块足够大的既存 mode，直接拿来当framebuffer，不必用 cvt/gtf 现造
    # 一个新 mode——探测确认过 15360x8640 在这个虚拟输出上本来就存在。
    steps = [
        ["xrandr", "--fb", "15360x8640", "--output", "screen", "--mode", "15360x8640"],
        ["xrandr", "--setmonitor", "selkies-primary", geom(primary), "screen"],
        ["xrandr", "--setmonitor", "selkies-display2", geom(display2), "screen"],
    ]
    for step in steps:
        result = docker_exec("env", "DISPLAY=:1", *step, timeout=10)
        if result.returncode != 0:
            raise TestFailure(f"{' '.join(step)} failed: {result.stderr}")


def teardown_display2() -> None:
    result = docker_exec(
        "env", "DISPLAY=:1", "xrandr", "--delmonitor", "selkies-display2", timeout=10
    )
    if result.returncode != 0:
        raise TestFailure(f"xrandr --delmonitor selkies-display2 failed: {result.stderr}")


def wait_until(predicate, timeout: float, interval: float = POLL_INTERVAL_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()  # 最后再判一次，把最终状态用于失败信息


def main() -> int:
    require_image()
    start_container()
    try:
        wait_for_ready()
        windows = create_test_windows()

        main_geometry_before = get_geometry(windows["main"])
        assert main_geometry_before[2] >= 600 and main_geometry_before[3] >= 600, (
            f"test setup bug: main window is not big enough to satisfy the "
            f"main-window criterion: {main_geometry_before}"
        )

        primary_rect = (0, 0, 1024, 768)
        display2_rect = (1024, 0, 800, 600)
        setup_display2(primary_rect, display2_rect)

        def movables_on_display2() -> bool:
            return all(
                within_rect(get_geometry(windows[name]), display2_rect)
                for name in ("movable1", "movable2")
            )

        connected_ok = wait_until(movables_on_display2, RECONCILE_TIMEOUT_S)
        movable1_after_connect = get_geometry(windows["movable1"])
        movable2_after_connect = get_geometry(windows["movable2"])
        assert connected_ok, (
            "movable windows never landed inside the second display's rect "
            f"{display2_rect} within {RECONCILE_TIMEOUT_S}s: "
            f"movable1={movable1_after_connect} movable2={movable2_after_connect}"
        )
        assert within_rect(movable1_after_connect, display2_rect), movable1_after_connect
        assert within_rect(movable2_after_connect, display2_rect), movable2_after_connect

        main_geometry_after_connect = get_geometry(windows["main"])
        assert close_to(main_geometry_before, main_geometry_after_connect), (
            "main window geometry changed after the second display connected: "
            f"before={main_geometry_before} after={main_geometry_after_connect}"
        )

        teardown_display2()

        def movables_back_on_primary() -> bool:
            return all(
                within_rect(get_geometry(windows[name]), primary_rect)
                for name in ("movable1", "movable2")
            )

        recalled_ok = wait_until(movables_back_on_primary, RECONCILE_TIMEOUT_S)
        movable1_after_disconnect = get_geometry(windows["movable1"])
        movable2_after_disconnect = get_geometry(windows["movable2"])
        assert recalled_ok, (
            "movable windows never returned inside the primary rect "
            f"{primary_rect} within {RECONCILE_TIMEOUT_S}s after the second "
            f"display disconnected: movable1={movable1_after_disconnect} "
            f"movable2={movable2_after_disconnect}"
        )
        assert within_rect(movable1_after_disconnect, primary_rect), movable1_after_disconnect
        assert within_rect(movable2_after_disconnect, primary_rect), movable2_after_disconnect

        main_geometry_after_disconnect = get_geometry(windows["main"])
        assert close_to(main_geometry_before, main_geometry_after_disconnect), (
            "main window geometry changed after the second display disconnected: "
            f"before={main_geometry_before} after={main_geometry_after_disconnect}"
        )

        print("test-second-display-x11: all geometry assertions passed")
        return 0
    finally:
        run(["docker", "rm", "-f", CONTAINER], timeout=20)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TestFailure as error:
        print(f"test-second-display-x11: FAILED: {error}", file=sys.stderr)
        raise SystemExit(1)
