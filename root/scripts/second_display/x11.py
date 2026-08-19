"""X11 读写封装：把纯 X 协议细节隔离在这一个模块里，daemon.py 只调用这里的
函数，不直接碰 Xlib/xdotool。不脱离 X 服务器就无法工作，因此没有配套的
host 端 pytest，只在 test-second-display-x11.py 的一次性容器里被间接验证。

设计上的两个关键取舍：

  * 读用 python-xlib（几何/属性/RandR），写用 xdotool 子进程
    （windowmove + windowsize）。这不是随意的双轨制：Qt 应用对 Xlib 原生的
    ConfigureWindow 请求有时不理睬，xdotool 这条命令路径是
    wechat-window-watchdog.sh 的 maximize() 已经在生产验证过的、对微信窗口
    确认有效的写入方式，这里直接复用，不重新造一遍容易踩同样的坑。

  * 事件监听（watch()）另开一条独立的 Xlib 连接，只负责"阻塞等下一个事件、
    叫醒调用方"，不在这条连接上做任何读写窗口属性的操作。python-xlib 的
    Display 对象不是线程安全的；daemon.run_forever() 所在的线程独占
    reconcile() 用的那条连接，watch() 自己的线程独占它自己开的连接，两条
    连接之间除了一个 threading.Event 之外不共享任何状态，因此不需要为
    "谁在用这条 Xlib 连接"加锁。
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Callable

from Xlib import X, error
from Xlib import display as xdisplay
from Xlib.ext import randr

from . import classify

LOG = logging.getLogger("wechat-second-display.x11")

# reconfigure_displays() 里逻辑显示器统一命名为 f"selkies-{display_id}"
# （selkies.py），只保留这个前缀的 RandR 显示器，天然排除任何非 selkies
# 建立的显示器。
MONITOR_NAME_PREFIX = "selkies-"

# query_tree 递归查找"真正客户端窗口"的深度上限：openbox 的重新父化通常只
# 有 root -> frame -> client 一层，留出充分余量应付 Qt 偶尔多包一层的情况，
# 同时避免异常的窗口树导致无限递归。
MAX_TREE_DEPTH = 6

XDOTOOL_TIMEOUT_S = 5.0


def open_display() -> xdisplay.Display:
    """打开一条新的 Xlib 连接。"""
    return xdisplay.Display()


def _absolute_geometry(root, window) -> tuple[int, int, int, int]:
    """窗口在根坐标系（屏幕坐标）下的 (x, y, width, height)。

    get_geometry() 返回的 x/y 是相对直接父窗口的坐标——openbox 一类的
    reparenting 窗口管理器会把每个客户端窗口套进自己创建的装饰 frame，这时
    get_geometry() 的 x/y 只是"相对 frame 内边距"的几个像素，不是屏幕坐标。
    用 TranslateCoordinates 把窗口自身坐标系里的原点 (0,0) 翻译到根窗口坐标
    系，才是真正能拿去和 RandR 显示器矩形比较重叠、也能拿去和 xdotool
    getwindowgeometry 的输出对比的绝对坐标。

    注意 translate_coords 的参数方向：调用方式是 dst.translate_coords(src,
    x, y)，翻译"src 坐标系里的 (x,y)"到"dst（self）坐标系"，因此这里必须由
    root 发起调用、把 window 当 src，而不是反过来。
    """
    geom = window.get_geometry()
    translated = root.translate_coords(window, 0, 0)
    return translated.x, translated.y, geom.width, geom.height


def _net_wm_state_atoms(disp, window) -> frozenset:
    """读取窗口 _NET_WM_STATE 属性里当前设置的全部原子；读取失败或属性为空
    时返回空集合。

    是原来 _is_modal() 的泛化版本：以前这里只单独判断
    _NET_WM_STATE_MODAL 一个原子，现在 _describe() 还要用同一份原子集合
    判断 _NET_WM_STATE_MAXIMIZED_VERT/_HORZ（窗口是否已被最大化，决定
    move_resize() 搬它之前要不要先摘状态位）——把"读属性"和"判断具体是哪个
    状态"拆开，调用方对同一个窗口只需发一次 GetProperty 请求，就能回答
    任意多个"是不是含有某个状态位"的问题。

    注意这不是 Xlib Window.get_wm_state()——那个方法读的是 ICCCM 的
    WM_STATE（Normal/Iconic），语义完全不同，不能用来判断模态或最大化。
    """
    state_atom = disp.get_atom("_NET_WM_STATE")
    try:
        prop = window.get_full_property(state_atom, X.AnyPropertyType)
    except error.XError:
        return frozenset()
    if prop is None or not prop.value:
        return frozenset()
    return frozenset(prop.value)


def _describe(disp, root, window) -> classify.WindowInfo | None:
    """把一个原始 Xlib Window 对象读成 WindowInfo。

    窗口在枚举期间消失是正常竞态（BadWindow 一类的 XError）：返回 None，
    调用方直接跳过这个窗口即可，下一轮 reconcile 自然不会再看到它。
    """
    try:
        attrs = window.get_attributes()
        wm_class = window.get_wm_class()
        x, y, width, height = _absolute_geometry(root, window)
        transient = window.get_wm_transient_for()
        title = window.get_wm_name() or ""
        state_atoms = _net_wm_state_atoms(disp, window)
    except error.XError:
        return None

    is_modal = disp.get_atom("_NET_WM_STATE_MODAL") in state_atoms
    is_maximized = (
        disp.get_atom("_NET_WM_STATE_MAXIMIZED_VERT") in state_atoms
        or disp.get_atom("_NET_WM_STATE_MAXIMIZED_HORZ") in state_atoms
    )

    return classify.WindowInfo(
        window_id=window.id,
        wm_class=(wm_class[1] if wm_class else ""),
        title=title,
        width=width,
        height=height,
        x=x,
        y=y,
        mapped=(attrs.map_state == X.IsViewable),
        override_redirect=bool(attrs.override_redirect),
        is_modal=is_modal,
        is_maximized=is_maximized,
        transient_for=(transient.id if transient is not None else None),
    )


def _iter_candidate_windows(window, depth: int = 0):
    """递归 query_tree，收集"真正的客户端窗口"。

    openbox 这类 reparenting 窗口管理器会把每个应用窗口套进自己创建的装饰
    frame 里，所以顶层窗口（root 的直接子窗口）大多数只是这些 frame，没有
    WM_CLASS。真正的客户端窗口带 WM_CLASS，是 frame 的子窗口——一旦命中就
    不必再往它自己的子树递归（微信不会把客户端窗口再嵌一层）。
    override-redirect 窗口（系统托盘图标一类）从不被重新父化，直接挂在
    root 下，一遇到就收，不需要判断 WM_CLASS。

    既没有 WM_CLASS 也不是 override-redirect 的窗口（例如纯装饰 frame，或
    WeChat 那个无 WM_CLASS 的"幽灵"窗口）不会被当成候选，也不会被归类/搬
    动——枚举不到的窗口天然不会被误判成可搬运，这是最安全的默认行为。
    """
    try:
        children = window.query_tree().children
    except error.XError:
        return
    for child in children:
        try:
            attrs = child.get_attributes()
        except error.XError:
            continue
        if attrs.override_redirect:
            yield child
            continue
        try:
            wm_class = child.get_wm_class()
        except error.XError:
            wm_class = None
        if wm_class:
            yield child
            continue
        if depth < MAX_TREE_DEPTH:
            yield from _iter_candidate_windows(child, depth + 1)


def list_windows(disp, root) -> list[classify.WindowInfo]:
    """枚举当前所有候选顶层窗口（含未 map 的），转换成 WindowInfo 列表。"""
    windows = []
    for candidate in _iter_candidate_windows(root):
        info = _describe(disp, root, candidate)
        if info is not None:
            windows.append(info)
    return windows


def list_monitors(disp, root) -> dict[str, tuple[int, int, int, int]]:
    """读取 selkies- 前缀的 RandR 逻辑显示器，键还原成 displayId（去掉前缀，
    如 "primary"/"display2"），值是 (x, y, w, h)。

    RandR 扩展不可用或没有任何 selkies- 前缀的显示器时返回空字典——这在
    容器刚启动、selkies 还没跑过一次 reconfigure_displays() 时是正常状态。
    """
    result: dict[str, tuple[int, int, int, int]] = {}
    try:
        reply = randr.get_monitors(root)
    except error.XError:
        return result
    for monitor in reply.monitors:
        try:
            name = disp.get_atom_name(monitor.name)
        except error.XError:
            continue
        if not name.startswith(MONITOR_NAME_PREFIX):
            continue
        display_id = name[len(MONITOR_NAME_PREFIX):]
        # 注意字段名是 width_in_pixels/height_in_pixels，不是 width/height
        # ——MonitorInfo 结构体里没有后者，实测过（RandR 1.5 的
        # GetMonitors 回复本身按毫米/像素两套单位分别命名宽高）。
        result[display_id] = (
            monitor.x, monitor.y, monitor.width_in_pixels, monitor.height_in_pixels
        )
    return result


def move_resize(
    window_id: int,
    rect: tuple[int, int, int, int],
    title: str = "",
    demaximize: bool = False,
) -> None:
    """用 xdotool 挪动并改尺寸一个窗口。

    demaximize=True 时先摘掉这个窗口的 _NET_WM_STATE_MAXIMIZED_* 状态位：
    仍带着这两个状态位的窗口会无视普通的 xdotool windowmove/windowsize
    请求（openbox 认为它就该待在"已最大化"的位置，不管请求的坐标是什么）
    ——生产里典型命中 wm_class="wechat" 的图片/视频查看器，会被 openbox
    的 <application class="wechat"><maximized>yes</maximized></application>
    规则自动最大化，但它其实该被当成 MOVABLE 平铺进副屏。摘完状态位之后
    只挪动/改尺寸，不像 restore_maximized_to() 那样再把状态位加回去——
    被平铺的窗口就该老老实实停在平铺给出的具体像素几何上，不应该在新
    位置上继续"表现得像最大化"。sleep 0.3 秒与 restore_maximized_to() 的
    第 1 步完全一致，同样是实测必须的，不是保险起见，具体原因见那个函数
    文档字符串里的说明。

    单个窗口的 xdotool/wmctrl 调用超时或失败不应该拖垮整个 reconcile
    循环——记一条警告日志就跳过，下一轮 reconcile 会自然重试（幂等）。
    title 只进本地日志，不会出现在任何 HTTP 响应里。
    """
    wid = str(window_id)
    if demaximize:
        try:
            subprocess.run(
                [
                    "wmctrl", "-i", "-r", hex(window_id),
                    "-b", "remove,maximized_vert,maximized_horz",
                ],
                capture_output=True, timeout=XDOTOOL_TIMEOUT_S, check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            LOG.warning("wmctrl demaximize failed for window %s (%r): %s", wid, title, exc)
            return
        time.sleep(0.3)

    x, y, w, h = rect
    for args in (
        ["xdotool", "windowmove", wid, str(x), str(y)],
        ["xdotool", "windowsize", wid, str(w), str(h)],
    ):
        try:
            subprocess.run(
                args, capture_output=True, timeout=XDOTOOL_TIMEOUT_S, check=False
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            LOG.warning("xdotool command failed for window %s (%r): %s", wid, title, exc)
            return


def restore_maximized_to(window_id: int, rect: tuple[int, int, int, int]) -> None:
    """把一个仍带着 _NET_WM_STATE_MAXIMIZED_* 状态位的窗口挪回 rect 并重新
    最大化——配方与 wechat-window-watchdog.sh 的 maximize() 完全一致，已经
    在生产环境验证过对微信主窗口有效：

      1. wmctrl 摘掉两个 maximized 状态位——不摘的话 wmctrl -b add 是空
         操作，openbox 认为"已经是最大化状态"就不会重新套用几何；
      2. xdotool 把窗口挪到目标位置、改成目标尺寸；
      3. wmctrl 重新加回两个状态位，让窗口在新位置上正确地"表现得像
         已最大化"（而不是停在第 2 步给的具体像素尺寸上）。

    步骤之间的 sleep 是必要的，不是保险起见——实测过省略它们时 openbox
    对"wechat" class 窗口的 <maximized>yes</maximized> 规则会在下一个步骤
    发出前就抢先重新生效（第 1 步摘掉状态位后几十毫秒内，第 2 步的
    move/resize 还没来得及被采纳，窗口就已经被 openbox 自己重新最大化回
    摘除前的几何），必须给 openbox 留出反应时间，数值与
    wechat-window-watchdog.sh 的 maximize() 完全一致（那份实现已经在生产
    验证过这两个具体数值足够）。

    wmctrl 用十六进制窗口 id（wmctrl -l 自己列出来的也是这个格式），
    xdotool 沿用 move_resize() 已经在用的十进制。单个子进程超时/失败只
    记警告并放弃这一轮的其余步骤，下一轮 reconcile 会自然重试——与
    move_resize() 同一容错风格。
    """
    hex_id = hex(window_id)
    wid = str(window_id)
    x, y, w, h = rect
    steps = (
        (["wmctrl", "-i", "-r", hex_id, "-b", "remove,maximized_vert,maximized_horz"], 0.3),
        (["xdotool", "windowmove", wid, str(x), str(y)], 0.0),
        (["xdotool", "windowsize", wid, str(w), str(h)], 0.0),
        (["wmctrl", "-i", "-r", hex_id, "-b", "add,maximized_vert,maximized_horz"], 0.7),
    )
    for args, settle_s in steps:
        try:
            subprocess.run(
                args, capture_output=True, timeout=XDOTOOL_TIMEOUT_S, check=False
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            LOG.warning("restore_maximized_to failed for window %s: %s", wid, exc)
            return
        if settle_s:
            time.sleep(settle_s)


def watch(wake: Callable[[], None]) -> None:
    """独立开一条新连接，阻塞等待窗口拓扑或显示器拓扑变化，每次事件到达都
    调用 wake()。必须在专门的线程里调用（自身是一个不返回的死循环）。

    不区分具体事件类型：SubstructureNotify 的一批事件类型（窗口创建/销毁/
    reparent/…）和 RandR 的 ScreenChangeNotify 对我们来说触发的动作完全
    一样——都是"拓扑可能变了，去跑一次 reconcile()"，而 reconcile() 本身是
    幂等的，分辨具体是哪种事件不会带来任何额外正确性，只会带来没必要的
    复杂度。真正的正确性保障来自 daemon.py 里雷打不动的 3 秒兜底轮询：这里
    的事件监听只是让响应更快，不是正确性的唯一来源。
    """
    disp = open_display()
    root = disp.screen().root
    root.change_attributes(event_mask=X.SubstructureNotifyMask)
    randr.select_input(root, randr.RRScreenChangeNotifyMask)
    disp.flush()
    while True:
        disp.next_event()  # 阻塞直到下一个事件；内容不重要，到达即唤醒
        wake()
