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
    is_maximized: bool  # _NET_WM_STATE 是否含 MAXIMIZED_VERT 或 _HORZ 任一；
    # 只影响 x11.move_resize() 搬运窗口前要不要先摘状态位，不参与分类判据
    # ——一个窗口是不是主窗口/该不该搬走，和它当前是不是最大化无关。
    transient_for: int | None  # WM_TRANSIENT_FOR 指向的窗口 id，没有则 None


def is_main_candidate(window: WindowInfo) -> bool:
    """判断一个窗口是否"够格参选"主窗口——纯几何/类名/模态判据，不涉及
    "多个候选里最终选哪一个"。

    生产环境里发现：图片/视频查看器一类窗口的 WM_CLASS 也是 "wechat"，
    尺寸又常常 >=600x600（比如全屏查看大图），如果直接把"满足这条判据"
    当成"就是主窗口"，会出现不止一个窗口同时被判定成主窗口——查看器进不了
    搬运名单，提示条也不会为它出现。真正"谁是主窗口"的决定权交给
    daemon.py 的选举逻辑（_elect_main_window），这里只提供候选资格判断，
    选举逻辑和单测共用同一份判据，避免两处对"什么样的窗口配参选"产生分歧。
    """
    return (
        window.wm_class == WECHAT_WM_CLASS
        and window.width >= MAIN_MIN_W
        and window.height >= MAIN_MIN_H
        and not window.is_modal
    )


def elect_main_window(
    candidates: dict[int, WindowInfo],
    first_seen: dict[int, float],
    incumbent: int | None,
) -> int | None:
    """从候选窗口里选出恰好一个主窗口 id：纯决策函数，不持有任何状态——
    "记住上一轮选了谁"是调用方（daemon.py 的 Daemon._elected_main_id）的
    职责，这里只回答"给定这一轮的候选池 + 首见时间戳 + 上一轮的当选者，
    该选谁"这一次性判断，因此和 tile_rects()/find_owning_display() 一样
    可以脱离 X 服务器直接单测。

    规则：incumbent 只要仍在候选池里就连任（粘性，避免主窗口在多个同样
    满足 is_main_candidate() 的窗口之间反复横跳）；否则选 first_seen 最早
    的一个，first_seen 相同（同一轮首见）时选 window_id 最小的——微信真正
    的主窗口总是先于小程序/查看器一类的大尺寸子窗口创建，这条决胜规则已经
    在生产环境的真实窗口 id 上验证过成立。候选池为空时返回 None。
    """
    if not candidates:
        return None
    if incumbent in candidates:
        return incumbent
    return min(candidates, key=lambda wid: (first_seen.get(wid, 0.0), wid))


def classify(
    window: WindowInfo,
    main_window_id: int | None,
    assigned: dict[int, str],
) -> Category:
    """按固定规则表逐条判断，命中即返回；规则顺序本身就是语义的一部分。

    override-redirect/托盘图标最先被过滤掉，避免它们被后面任何一条规则误判
    成别的类别。main_window_id 是 daemon.py 选举出的"恰好一个"主窗口 id
    （或者还没选出任何主窗口时的 None）——MAIN_WINDOW 判据因此是一次身份
    比较，不是重新跑一遍几何判据；同样满足 is_main_candidate() 但没有当选
    的窗口（比如生产里观察到的大尺寸图片查看器）会继续往下走，落进后面的
    规则（通常是 MOVABLE，可以搬去副屏）。
    """
    if window.override_redirect or (
        window.width <= TRAY_ICON_MAX_SIZE and window.height <= TRAY_ICON_MAX_SIZE
    ):
        return Category.IGNORE

    if window.window_id == main_window_id:
        return Category.MAIN_WINDOW

    if window.is_modal:
        if window.transient_for is not None and window.transient_for in assigned:
            return Category.FOLLOWS_PARENT
        return Category.PROTECTED_MODAL

    if main_window_id is None:
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
