"""纯函数窗口分类：判断一个 X11 顶层窗口该不该被副屏窗口管家接管。

不导入 Xlib、不做任何 X 请求，可以脱离 X 服务器直接跑 pytest。窗口的原始
属性（WM_CLASS、_NET_WM_STATE 等）由 x11.py 读出后装进 WindowInfo 传进来。

阈值直接对齐 root/scripts/wechat/wechat-window-watchdog.sh 已经在生产验证过
的判据（该脚本注释里记录了每个数字背后的真实观察），避免副屏管家和主窗口
看门狗对"什么算主窗口/什么算托盘图标"产生分歧。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Category(enum.Enum):
    """窗口分类结果。"""

    IGNORE = "ignore"  # 系统托盘图标一类的 chrome，从不触碰
    MAIN_WINDOW = "main_window"  # 微信主窗口；watchdog 的地盘，这里只识别不搬动
    PROTECTED_MODAL = "protected_modal"  # 模态弹窗/登录二维码窗，禁止搬动
    FOLLOWS_PARENT = "follows_parent"  # 模态且父窗口已在副屏，跟随父窗口居中
    PROTECTED_LOGIN_OR_LOGOUT = "protected_login_or_logout"  # 无主窗口时的登录/登出窗
    MOVABLE = "movable"  # 可以搬去副屏平铺的窗口


# 与 wechat-window-watchdog.sh 的 MIN_W/MIN_H 完全一致：主窗口的最小宽高。
# 交叉引用：root/scripts/wechat/wechat-window-watchdog.sh 里的同名常量。
MAIN_MIN_W = 600
MAIN_MIN_H = 600

# 与 wechat-window-watchdog.sh 的 MIN_REAL_W/MIN_REAL_H 完全一致：区分"真实
# 窗口"（登录窗、弹窗）和纯 chrome（24x24 托盘图标）的下限。
# 交叉引用：root/scripts/wechat/wechat-window-watchdog.sh 里的同名常量。
REAL_MIN_W = 200
REAL_MIN_H = 200

# 托盘图标固定 24x24——wechat-window-watchdog.sh 注释里记录的实测尺寸。
TRAY_ICON_MAX_SIZE = 24

# 微信主窗口/子窗口共用的 WM_CLASS class 部分（get_wm_class() 的第二个元素）。
WECHAT_WM_CLASS = "wechat"


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """一个 X11 顶层窗口分类所需的全部信息：脱离 Xlib 类型的纯数据快照。"""

    window_id: int
    wm_class: str  # get_wm_class() 的第二个元素（class，不是 instance）；没有则空串
    title: str  # 仅用于本地调试日志，绝不通过 HTTP 状态端点外泄
    width: int
    height: int
    x: int  # 相对根窗口（屏幕）的绝对坐标
    y: int
    mapped: bool
    override_redirect: bool
    is_modal: bool  # _NET_WM_STATE 是否含 _NET_WM_STATE_MODAL
    transient_for: int | None  # WM_TRANSIENT_FOR 指向的窗口 id，没有则 None


def classify(
    window: WindowInfo,
    main_window_present: bool,
    assigned: dict[int, str],
) -> Category:
    """按固定规则表逐条判断，命中即返回；规则顺序本身就是语义的一部分。

    override-redirect/托盘图标最先被过滤掉，避免它们被后面任何一条规则误判
    成别的类别；MAIN_WINDOW 判据完全基于几何/类名/模态，不依赖
    main_window_present/assigned，因此调用方可以用探测性的参数
    （True、{}）复用这条规则去单独判断"这是不是主窗口"。
    """
    if window.override_redirect or (
        window.width <= TRAY_ICON_MAX_SIZE and window.height <= TRAY_ICON_MAX_SIZE
    ):
        return Category.IGNORE

    if (
        window.wm_class == WECHAT_WM_CLASS
        and window.width >= MAIN_MIN_W
        and window.height >= MAIN_MIN_H
        and not window.is_modal
    ):
        return Category.MAIN_WINDOW

    if window.is_modal:
        if window.transient_for is not None and window.transient_for in assigned:
            return Category.FOLLOWS_PARENT
        return Category.PROTECTED_MODAL

    if not main_window_present:
        # 主窗口不在场时，唯一会出现的"真实窗口"是登录/二维码窗，或强制登出
        # 后的提示弹窗——两者都由 wechat-window-watchdog.sh 负责自愈，这里
        # 一律不碰，避免和它的点击序列打架。
        return Category.PROTECTED_LOGIN_OR_LOGOUT

    if window.width < REAL_MIN_W or window.height < REAL_MIN_H:
        return Category.IGNORE

    return Category.MOVABLE


def find_owning_display(
    rect: tuple[int, int, int, int],
    monitors: dict[str, tuple[int, int, int, int]],
) -> str | None:
    """按重叠面积最大的显示器判定一个矩形"属于"哪块屏幕。

    rect 与 monitors 的矩形统一用 (x, y, w, h)。不与任何显示器重叠时返回
    None（例如窗口几何暂时性地跑到了所有已知显示器范围之外）。
    """
    best_id: str | None = None
    best_area = 0
    rx, ry, rw, rh = rect
    for display_id, (mx, my, mw, mh) in monitors.items():
        overlap_w = min(rx + rw, mx + mw) - max(rx, mx)
        overlap_h = min(ry + rh, my + mh) - max(ry, my)
        if overlap_w <= 0 or overlap_h <= 0:
            continue
        area = overlap_w * overlap_h
        if area > best_area:
            best_area = area
            best_id = display_id
    return best_id
