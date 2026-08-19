#!/usr/bin/env python3
"""X11 集成测试：在一次性容器里验证 second_display 守护进程真的会按 RandR
状态搬动窗口，而不仅仅是 classify()/layout() 的纯函数逻辑自洽。

只对镜像 wechat-selkies:second-display-test（docker build -t 该标签 . 产物，
不是 :latest）生效——:latest 没有本次新增的功能。

裸 assert 风格，直接跑：

    python3 patches/test-second-display-x11.py

每个一次性容器都用 AUTO_START_WECHAT=false（不启动真微信）、
ENABLE_WECHAT_WINDOW_WATCHDOG=false（避免主窗口看门狗和本测试的假窗口
打架）、ENABLE_WECHAT_SECOND_DISPLAY=true（打开本功能）启动，轮询状态端点
等守护进程就绪，再由容器内一个常驻的 python-xlib 进程造假窗口（该进程必须
常驻：X11 语义下客户端断开连接会连带销毁它创建的窗口）。

两组场景各自独立起停一个容器（对窗口尺寸约束的要求互相冲突，分开互不
干扰更简单）：

  run_movable_scenarios()：造 1 个固定尺寸的主窗口 + 2 个 WeChatAppEx
    小程序窗，验证 MOVABLE 窗口的平铺进 display2/断开后收回 primary/
    滞留在所有已知显示器之外时的启动自愈，全程主窗口几何不受影响
    （除了自愈场景本身会人为构造出"主窗口也不在 primary 上"的状态，
    这时主窗口按 Bug 2 的抢回逻辑同样应该被拉回）。

  run_main_election_scenarios()：造 2 个都满足主窗口几何判据的 wechat
    类窗口（生产实测场景：图片/视频查看器 WM_CLASS 也是 wechat 且经常
    >=600x600），验证只有先创建者当选主窗口、留在 primary，后创建者
    退化成 MOVABLE 被平铺进 display2；再把当选主窗口挪去 display2 区域，
    验证守护进程能在数秒内把它抢回 primary（Bug 2）。

用 xrandr --setmonitor/--delmonitor 模拟 selkies-primary/selkies-display2
的出现/消失，用 xdotool getwindowgeometry --shell 的输出解析做几何断言
（允许 ±32px 误差——openbox 装饰可能造成几点偏移），docker rm -f 在
finally 块里清理，无论成败都执行。只用文本断言：上述 xdotool 输出解析、
HTTP JSON 响应字段。不做任何视觉/截图判断。
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


def make_window(x, y, w, h, wm_class, fixed_size=True):
    win = root.create_window(
        x, y, w, h, 0, screen.root_depth,
        X.InputOutput, X.CopyFromParent,
        background_pixel=screen.white_pixel,
        event_mask=X.StructureNotifyMask,
    )
    win.set_wm_class(wm_class, wm_class)
    win.set_wm_name(wm_class + "-test-window")
    if fixed_size:
        # 固定尺寸 hint：没有 WM_NORMAL_HINTS 时 openbox 会把窗口当成"没有
        # 尺寸偏好"处理，观察到的实际效果是直接铺满工作区，而不是保留
        # CreateWindow 请求的宽高。min==max==请求值能让 openbox 尊重这个
        # 尺寸——movable 窗口需要这个，好让断言比对的目标尺寸稳定不变。
        win.set_wm_normal_hints(
            flags=(Xutil.PPosition | Xutil.PSize | Xutil.PMinSize | Xutil.PMaxSize),
            min_width=w, min_height=h, max_width=w, max_height=h,
        )
    else:
        # 主窗口只给下限、不给上限：真实微信主窗口本来就能被最大化/被
        # restore_maximized_to() 改尺寸，固定 min==max 会让 Bug 2 的抢回
        # 场景测不出任何东西（尺寸请求会被直接钳制回原值）。
        win.set_wm_normal_hints(
            flags=(Xutil.PPosition | Xutil.PSize | Xutil.PMinSize),
            min_width=min(w, 600), min_height=min(h, 600),
        )
    win.map()
    disp.sync()
    return win.id


windows = {
    "main": make_window(50, 50, 700, 700, "wechat", fixed_size=False),
    "movable1": make_window(100, 100, 300, 300, "WeChatAppEx"),
    "movable2": make_window(500, 100, 300, 300, "WeChatAppEx"),
}

with open("/tmp/second_display_test_windows.json", "w") as f:
    json.dump(windows, f)

while True:
    time.sleep(3600)
"""

# 第二套场景专用的窗口制造者：两个都满足主窗口几何判据的 wechat 类窗口
# （生产实测的场景——图片/视频查看器 WM_CLASS 也是 wechat 且经常
# >=600x600），先后创建，验证选举只认一个当主窗口。
#
# candidate_a（当选者）只给 min size、不给 max size：真实微信主窗口本来就
# 可以被最大化/被 restore_maximized_to() 改尺寸，固定 min==max 的窗口测不出
# Bug 2 的抢回场景。
#
# candidate_b（落选者，之后会退化成 MOVABLE）同样要固定尺寸（min==max，
# 和 movable1/movable2 同一手法）：ICCCM 的固定尺寸约束能让断言比对的
# 目标尺寸稳定不变；wm_class 同样是 "wechat"，会被 openbox 的
# <application class="wechat"><maximized>yes</maximized></application>
# 规则自动最大化（状态位被置位，实际几何仍被 min==max 钳制住，不会真的
# 变成全屏）。
#
# 这正是本次修复的决定性验证场景：candidate_b 落选后被分类成 MOVABLE，
# daemon 会通过 move_resize(..., demaximize=True) 尝试把它搬进 display2
# ——一个仍带着陈旧 _NET_WM_STATE_MAXIMIZED_* 状态位的窗口本会无视普通的
# xdotool windowmove/windowsize 请求，daemon 必须自己先用 wmctrl 摘掉这两
# 个状态位才能真正搬动它。测试不再像之前那样手动摘位绕开这个场景，让
# candidate_b 带着 openbox 自动加上的最大化状态直接进入 daemon 的搬运
# 流程，能否落进 display2 矩形就是对 move_resize() 的 demaximize 参数
# 最直接的验证。
ELECTION_WINDOW_FACTORY_SCRIPT = """
import json
import time

from Xlib import X, Xutil, display

disp = display.Display()
screen = disp.screen()
root = screen.root


def make_elected_candidate(x, y, w, h):
    win = root.create_window(
        x, y, w, h, 0, screen.root_depth,
        X.InputOutput, X.CopyFromParent,
        background_pixel=screen.white_pixel,
        event_mask=X.StructureNotifyMask,
    )
    win.set_wm_class("wechat", "wechat")
    win.set_wm_name("wechat-election-test-window")
    win.set_wm_normal_hints(
        flags=(Xutil.PPosition | Xutil.PSize | Xutil.PMinSize),
        min_width=600, min_height=600,
    )
    win.map()
    disp.sync()
    return win.id


def make_loser_candidate(x, y, w, h):
    win = root.create_window(
        x, y, w, h, 0, screen.root_depth,
        X.InputOutput, X.CopyFromParent,
        background_pixel=screen.white_pixel,
        event_mask=X.StructureNotifyMask,
    )
    win.set_wm_class("wechat", "wechat")
    win.set_wm_name("wechat-election-test-window-loser")
    win.set_wm_normal_hints(
        flags=(Xutil.PPosition | Xutil.PSize | Xutil.PMinSize | Xutil.PMaxSize),
        min_width=w, min_height=h, max_width=w, max_height=h,
    )
    win.map()
    disp.sync()
    return win.id


windows = {"candidate_a": make_elected_candidate(50, 50, 700, 700)}
# 明显早于 candidate_b 出现，直接测"first_seen 更早的赢"这条规则，不必
# 只靠 window_id 决胜。
time.sleep(2)
# 600x600 是 is_main_candidate() 的最低门槛，也刚好能在 8px 间距下塞进
# 800x600 的 display2 矩形。
windows["candidate_b"] = make_loser_candidate(50, 50, 600, 600)

with open("/tmp/second_display_election_windows.json", "w") as f:
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


def launch_window_factory(
    script: str, manifest_path: str, remote_script_path: str, wait_s: float = 15
) -> dict[str, str]:
    """把 script 拷进容器、后台常驻运行，等它写出 manifest_path 里的窗口 id
    清单。-d：后台常驻，保持这条 X11 连接不断，窗口才不会随连接关闭被服务端
    按 X11 语义回收。"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
        handle.write(script)
        local_path = handle.name
    try:
        copy = run(["docker", "cp", local_path, f"{CONTAINER}:{remote_script_path}"], timeout=15)
        if copy.returncode != 0:
            raise TestFailure(f"docker cp failed: {copy.stderr}")
    finally:
        Path(local_path).unlink(missing_ok=True)

    launch = run(
        ["docker", "exec", "-d", CONTAINER, "env", "DISPLAY=:1",
         "/lsiopy/bin/python3", remote_script_path],
        timeout=15,
    )
    if launch.returncode != 0:
        raise TestFailure(f"failed to launch window factory: {launch.stderr}")

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        result = docker_exec("cat", manifest_path, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            windows = json.loads(result.stdout)
            return {name: str(window_id) for name, window_id in windows.items()}
        time.sleep(0.5)
    raise TestFailure(f"window factory never wrote out {manifest_path}")


def create_test_windows() -> dict[str, str]:
    return launch_window_factory(
        WINDOW_FACTORY_SCRIPT,
        "/tmp/second_display_test_windows.json",
        "/tmp/second_display_window_factory.py",
    )


def create_election_windows() -> dict[str, str]:
    # candidate_b 的创建被 factory 脚本自己 sleep(2) 延后，manifest 要等
    # 到那之后才会写出来，超时上限相应放宽。
    return launch_window_factory(
        ELECTION_WINDOW_FACTORY_SCRIPT,
        "/tmp/second_display_election_windows.json",
        "/tmp/second_display_election_factory.py",
        wait_s=20,
    )


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


def _geom(rect: tuple[int, int, int, int]) -> str:
    x, y, w, h = rect
    return f"{w}/0x{h}/0+{x}+{y}"


def ensure_framebuffer() -> None:
    # 已有一块足够大的既存 mode，直接拿来当 framebuffer，不必用 cvt/gtf 现造
    # 一个新 mode——探测确认过 15360x8640 在这个虚拟输出上本来就存在。
    result = docker_exec(
        "env", "DISPLAY=:1", "xrandr", "--fb", "15360x8640",
        "--output", "screen", "--mode", "15360x8640", timeout=10,
    )
    if result.returncode != 0:
        raise TestFailure(f"xrandr --fb setup failed: {result.stderr}")


def set_monitor(name: str, rect: tuple[int, int, int, int]) -> None:
    # 探测确认过：用 --setmonitor 原地重定义一个已存在的同名 monitor 到一个
    # 相去甚远的新位置会报 BadValue（RandR 似乎要求同名重定义时新旧位置不能
    # 差太远）；先删后建则不受此限——两种情况都先尝试删除（不存在也不报错），
    # 保证这个函数对"该 monitor 是否已存在"保持幂等/健壮。
    docker_exec("env", "DISPLAY=:1", "xrandr", "--delmonitor", name, timeout=10)
    result = docker_exec(
        "env", "DISPLAY=:1", "xrandr", "--setmonitor", name, _geom(rect), "screen",
        timeout=10,
    )
    if result.returncode != 0:
        raise TestFailure(f"xrandr --setmonitor {name} {_geom(rect)} failed: {result.stderr}")


def delete_monitor(name: str) -> None:
    result = docker_exec("env", "DISPLAY=:1", "xrandr", "--delmonitor", name, timeout=10)
    if result.returncode != 0:
        raise TestFailure(f"xrandr --delmonitor {name} failed: {result.stderr}")


def setup_display2(primary: tuple[int, int, int, int], display2: tuple[int, int, int, int]) -> None:
    ensure_framebuffer()
    set_monitor("selkies-primary", primary)
    set_monitor("selkies-display2", display2)


def teardown_display2() -> None:
    delete_monitor("selkies-display2")


def force_unmaximize(window_id: str) -> None:
    """摘掉一个窗口的 _NET_WM_STATE_MAXIMIZED_* 状态位，并轮询确认状态确实
    生效——一个仍带着这两个状态位的窗口会无视普通的 xdotool windowmove/
    windowsize 请求（openbox 认为它就该待在"最大化"的位置，不管请求的坐标
    是什么），必须先摘状态位才能真正移动它。

    现在只在 run_main_election_scenarios() 里给 candidate_a 用：测试需要
    用自己的 xdotool windowmove 模拟"openbox 把已最大化的主窗口甩到副屏"
    这个生产场景（真实场景里这一步是 openbox 自己做的，不经过任何 xdotool
    调用），而测试自己发起的这条 xdotool 命令同样会被 openbox 无视，所以
    要先摘状态位。这与 move_resize() 的 demaximize 参数摘的是同一种状态位，
    但目的不同：这里是测试在搭建前置场景，不是在绕开被测的生产代码路径
    ——candidate_b 那条真正的 MOVABLE 搬运路径已经不再需要（也不应该）用
    这个函数手动摘位，直接交给 move_resize(..., demaximize=True) 自己处理，
    见 ELECTION_WINDOW_FACTORY_SCRIPT 上方的说明。"""
    hex_id = hex(int(window_id))
    result = docker_exec(
        "env", "DISPLAY=:1", "wmctrl", "-i", "-r", hex_id,
        "-b", "remove,maximized_vert,maximized_horz", timeout=10,
    )
    assert result.returncode == 0, f"wmctrl remove maximized failed: {result.stderr}"

    def is_unmaximized() -> bool:
        state = docker_exec("env", "DISPLAY=:1", "xprop", "-id", hex_id, "_NET_WM_STATE", timeout=5)
        return state.returncode == 0 and "MAXIMIZED" not in state.stdout

    assert wait_until(is_unmaximized, 5, interval=0.2), f"window {window_id} never left the maximized state"


def wait_until(predicate, timeout: float, interval: float = POLL_INTERVAL_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()  # 最后再判一次，把最终状态用于失败信息


def run_movable_scenarios() -> None:
    """场景组 1：MOVABLE 窗口的平铺/收回/滞留自愈，以及"主窗口全程不被
    这些机制误伤"。"""
    start_container()
    try:
        wait_for_ready()
        windows = create_test_windows()

        main_geometry_before = get_geometry(windows["main"])
        assert main_geometry_before[2] >= 600 and main_geometry_before[3] >= 600, (
            f"test setup bug: main window is not big enough to satisfy the "
            f"main-window criterion: {main_geometry_before}"
        )

        # ------------------------------------------------------------------
        # 启动自愈：一个 MOVABLE 窗口滞留在所有已知显示器范围之外时（daemon
        # 在窗口位于副屏区域时被杀掉/重启，assigned 记忆丢失，上游又已经把
        # 副屏 xrandr 布局收回），下一轮 reconcile 必须把它 cascade 拉回
        # primary——这条自愈逻辑跑在每轮 reconcile 里，和是否真的重启过
        # daemon 进程无关，不需要真的杀掉容器里的进程，直接构造等价的
        # "窗口位置 vs. 已知显示器"错位状态即可。
        #
        # 实测过好几种"把窗口挪到不与任何 monitor 重叠的地方"的直接手法都
        # 会被 openbox 挡下来：不管是 xdotool windowmove 还是客户端自己发的
        # 原始 ConfigureWindow，只要目标位置会导致窗口和"当前唯一已知的
        # monitor"完全不重叠，openbox 都会把落点钳制回贴着那块 monitor 边缘
        # 几像素重叠的位置——即使先建一块很远的 selkies-primary 再让窗口在
        # 它已经存在之后才出现也一样，openbox 的窗口摆放策略只认"当前有没有
        # 至少一块 monitor 能沾上边"，不认哪个 monitor 叫什么名字。
        #
        # 绕过办法：额外建一块不带 selkies- 前缀、覆盖窗口原本自然落点的
        # "decoy" monitor——它满足 openbox"总得沾上点 monitor"的摆放需求，
        # 窗口因此完全不会被挪动；而 x11.list_monitors() 只认 selkies- 前缀，
        # decoy 对我们自己的守护进程形同不存在。selkies-primary 这时首次
        # 建在完全另一个角落，对守护进程来说"窗口位置"和"它认识的唯一显示器"
        # 就是货真价实地对不上——和窗口物理位置没变、只是 daemon 记忆丢失的
        # 真实重启场景等价。
        native_area = (0, 0, 1024, 768)
        # >= 600x600：main 窗口只设了 min_width=min_height=600（不设上限，
        # 见 make_window 的 fixed_size=False 分支），比这更小的目标矩形会
        # 被 ICCCM 的 min size 钳制卡住，main 永远挤不进去。
        stray_primary_rect = (2000, 2000, 700, 700)
        ensure_framebuffer()
        set_monitor("decoy", native_area)
        set_monitor("selkies-primary", stray_primary_rect)  # 首次建立，不是重定义

        def movable1_healed() -> bool:
            return within_rect(get_geometry(windows["movable1"]), stray_primary_rect)

        healed_ok = wait_until(movable1_healed, RECONCILE_TIMEOUT_S)
        movable1_after_heal = get_geometry(windows["movable1"])
        assert healed_ok, (
            "a movable window stranded outside every known display never got "
            f"cascaded back into the (relocated) primary rect {stray_primary_rect} "
            f"within {RECONCILE_TIMEOUT_S}s: movable1={movable1_after_heal}"
        )
        assert within_rect(movable1_after_heal, stray_primary_rect), movable1_after_heal

        # 主窗口在这个人为构造的场景里也"不在 primary 上"（primary 被临时
        # 挪去了一个和主窗口原本位置毫不相干的角落），所以主窗口抢回逻辑
        # （见下面 Bug 2 的专门场景）会同样把它拉进这块 primary——这正是
        # 期望的行为，两条自愈/抢回逻辑并不冲突。这里只需确认它确实落进了
        # 这块 primary，不再断言"完全不动"。
        def main_reclaimed() -> bool:
            return within_rect(get_geometry(windows["main"]), stray_primary_rect)

        main_reclaimed_ok = wait_until(main_reclaimed, RECONCILE_TIMEOUT_S)
        main_geometry_after_heal = get_geometry(windows["main"])
        assert main_reclaimed_ok, (
            "main window was not also reclaimed onto the relocated primary rect "
            f"{stray_primary_rect}: main={main_geometry_after_heal}"
        )

        # 收尾：删掉这两块临时 monitor，给后面的正常 display2 场景让路——
        # set_monitor()/setup_display2() 自己也会先删后建，这里显式删除只是
        # 让测试的每个阶段读起来彼此独立。
        delete_monitor("selkies-primary")
        delete_monitor("decoy")

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

        print("test-second-display-x11: movable scenarios passed")
    finally:
        run(["docker", "rm", "-f", CONTAINER], timeout=20)


def run_main_election_scenarios() -> None:
    """场景组 2：两个都满足主窗口几何判据的 wechat 类窗口（生产实测场景：
    图片/视频查看器 WM_CLASS 也是 wechat 且经常 >=600x600）——只有先创建
    的当选主窗口，落选者要能被正常搬去副屏；以及当选主窗口被挪去副屏区域
    后，守护进程能在数秒内把它抢回主屏（Bug 2）。

    独立开一个新容器跑，不和 run_movable_scenarios() 共用：那边的 main
    窗口是固定尺寸（专门用来断言"平铺/收回不会误伤主窗口的几何"），这里
    需要的是能被真正 resize 的候选窗口（Bug 2 的抢回配方要把窗口 resize
    回 primary 的尺寸），两种测试目的对窗口尺寸约束的要求互相冲突，分开
    互不干扰更简单。
    """
    start_container()
    try:
        wait_for_ready()

        # 先把 primary+display2 建好、稳定下来，再造窗口：openbox 对
        # "wechat" class 窗口的自动最大化在 RandR 拓扑变化时会重新触发一次
        # （不只是窗口初次出现时），如果先造窗口再改拓扑，任何这里手动摘掉
        # 的最大化状态都会被这次重新触发再次盖掉。窗口在拓扑已经稳定之后才
        # 出现，就只需要应付"初次出现时的自动最大化"这一次。
        primary_rect = (0, 0, 1024, 768)
        display2_rect = (1024, 0, 800, 600)
        setup_display2(primary_rect, display2_rect)

        windows = create_election_windows()

        # candidate_b 带着 openbox 自动加上的 _NET_WM_STATE_MAXIMIZED_*
        # 状态位直接进入下面的选举/分类断言——不再手动摘位，这正是本次
        # 修复的决定性验证：daemon 必须自己通过
        # move_resize(..., demaximize=True) 把它摘位并平铺进 display2，
        # 见 ELECTION_WINDOW_FACTORY_SCRIPT 上方的说明。

        # wm_class="wechat" 的窗口被 openbox 加上的装饰（标题栏）比
        # WeChatAppEx 类窗口明显更高——实测过 xdotool windowmove 请求的是
        # frame 的位置，getwindowgeometry 报的是 client 的位置，两者之间
        # 差着这条装饰的高度，movable1/movable2（WeChatAppEx，无装饰）从
        # 没出现过这个偏移，但 candidate_a/candidate_b（wechat）稳定出现
        # 五十多像素的纵向偏移。默认的 32px 容差不够用，这里按 Bug 3 同一套
        # "装饰偏移余量"的量级放宽到 64px，而不是继续加大全局默认容差去
        # 掩盖其它场景可能出现的真实偏差。
        WECHAT_DECORATION_TOLERANCE_PX = 64

        # a. 先创建者当选主窗口、留在 primary；落选的后创建者退化成
        #    MOVABLE，被正常平铺进 display2。
        def elected_on_primary_and_loser_on_display2() -> bool:
            return within_rect(
                get_geometry(windows["candidate_a"]), primary_rect, WECHAT_DECORATION_TOLERANCE_PX
            ) and within_rect(
                get_geometry(windows["candidate_b"]), display2_rect, WECHAT_DECORATION_TOLERANCE_PX
            )

        elected_ok = wait_until(elected_on_primary_and_loser_on_display2, RECONCILE_TIMEOUT_S)
        candidate_a_geometry = get_geometry(windows["candidate_a"])
        candidate_b_geometry = get_geometry(windows["candidate_b"])
        assert elected_ok, (
            "election did not settle within "
            f"{RECONCILE_TIMEOUT_S}s: candidate_a(elected, expected on primary "
            f"{primary_rect})={candidate_a_geometry}, candidate_b(loser, expected "
            f"tiled onto display2 {display2_rect})={candidate_b_geometry}"
        )
        assert within_rect(candidate_a_geometry, primary_rect, WECHAT_DECORATION_TOLERANCE_PX), candidate_a_geometry
        assert within_rect(candidate_b_geometry, display2_rect, WECHAT_DECORATION_TOLERANCE_PX), candidate_b_geometry

        # b. 把当选主窗口挪进 display2 矩形——这是有重叠的合法移动，不受
        #    openbox"零重叠钳制"影响，但窗口当前是最大化状态，直接移动会
        #    被 openbox 无视：要先摘掉 maximized 状态位，才能真正把它挪走，
        #    构造出"主窗口被 openbox 甩到副屏"的等价场景（生产里这一步是
        #    openbox 自己做的，这里手动做等价的事）。
        force_unmaximize(windows["candidate_a"])

        move = docker_exec(
            "env", "DISPLAY=:1", "xdotool", "windowmove", windows["candidate_a"], "1100", "50",
            timeout=10,
        )
        assert move.returncode == 0, f"xdotool windowmove failed: {move.stderr}"
        docker_exec(
            "env", "DISPLAY=:1", "xdotool", "windowsize", windows["candidate_a"], "700", "500",
            timeout=10,
        )

        def elected_reclaimed_to_primary() -> bool:
            return within_rect(
                get_geometry(windows["candidate_a"]), primary_rect, WECHAT_DECORATION_TOLERANCE_PX
            )

        reclaimed_ok = wait_until(elected_reclaimed_to_primary, RECONCILE_TIMEOUT_S)
        candidate_a_after_reclaim = get_geometry(windows["candidate_a"])
        assert reclaimed_ok, (
            "the elected main window was dragged onto the second display's rect "
            f"and never got reclaimed back onto primary {primary_rect} within "
            f"{RECONCILE_TIMEOUT_S}s: candidate_a={candidate_a_after_reclaim}"
        )
        assert within_rect(
            candidate_a_after_reclaim, primary_rect, WECHAT_DECORATION_TOLERANCE_PX
        ), candidate_a_after_reclaim

        print("test-second-display-x11: main-election/reclaim scenarios passed")
    finally:
        run(["docker", "rm", "-f", CONTAINER], timeout=20)


def main() -> int:
    require_image()
    run_movable_scenarios()
    run_main_election_scenarios()
    print("test-second-display-x11: all geometry assertions passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TestFailure as error:
        print(f"test-second-display-x11: FAILED: {error}", file=sys.stderr)
        raise SystemExit(1)
