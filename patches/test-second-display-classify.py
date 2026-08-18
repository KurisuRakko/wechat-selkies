"""root/scripts/second_display 里 classify.py 与 layout.py 的纯函数测试。

两个模块都不依赖 Xlib，可以在没有 X 服务器的宿主机上直接跑：

    python3 -m pytest patches/test-second-display-classify.py -v

覆盖的核心不变量：真正的微信主窗口在任何 main_window_present/assigned 组合
下都不会被误判成 MOVABLE（这是最不能出错的一条——错判会把用户正在用的主
窗口拖去副屏）；托盘图标、无主窗口时的登录/登出窗受到保护；tile_rects/
cascade_rects 产出的矩形互不重叠、且落在目标区域内。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_SCRIPTS = Path(__file__).resolve().parent.parent / "root" / "scripts"
if str(ROOT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ROOT_SCRIPTS))

from second_display import classify  # noqa: E402
from second_display import layout  # noqa: E402

Category = classify.Category


def make_window(**overrides) -> classify.WindowInfo:
    """构造一个"平平无奇、满足 MOVABLE 尺寸下限"的窗口，按需覆盖字段。"""
    fields = {
        "window_id": 1,
        "wm_class": "wechat",
        "title": "",
        "width": 400,
        "height": 300,
        "x": 0,
        "y": 0,
        "mapped": True,
        "override_redirect": False,
        "is_modal": False,
        "transient_for": None,
    }
    fields.update(overrides)
    return classify.WindowInfo(**fields)


# --------------------------------------------------------------- classify()


def test_main_window_never_classified_as_movable():
    """不变量：满足主窗口判据的窗口，在任何 main_window_present/assigned
    组合下都必须分类成 MAIN_WINDOW，绝不会是 MOVABLE——这是唯一一条错了就
    会把用户正在用的主窗口拖去副屏的规则。
    """
    for width, height in ((600, 600), (600, 601), (1920, 1080), (3232, 2048)):
        window = make_window(width=width, height=height)
        for main_present in (True, False):
            for assigned in ({}, {window.window_id: "display2"}):
                result = classify.classify(window, main_present, assigned)
                assert result is Category.MAIN_WINDOW, (width, height, main_present, assigned, result)
                assert result is not Category.MOVABLE


def test_tray_icon_is_ignored():
    """24x24 的托盘图标——不管 override_redirect 是否显式为真，光凭尺寸下限
    就必须被过滤。"""
    tray_by_size = make_window(width=24, height=24, override_redirect=False)
    assert classify.classify(tray_by_size, True, {}) is Category.IGNORE

    tray_by_flag = make_window(width=24, height=24, override_redirect=True)
    assert classify.classify(tray_by_flag, True, {}) is Category.IGNORE

    # override_redirect 单独也足以判定 IGNORE，即便尺寸不小。
    override_redirect_large = make_window(width=800, height=600, override_redirect=True)
    assert classify.classify(override_redirect_large, True, {}) is Category.IGNORE


def test_login_window_without_main_window_is_protected():
    """无主窗口时，560x760 的登录/二维码窗（wechat-window-watchdog.sh 记录
    的真实尺寸）必须受保护，不能被当成 MOVABLE 搬走。"""
    login_window = make_window(width=560, height=760)
    assert classify.classify(login_window, False, {}) is Category.PROTECTED_LOGIN_OR_LOGOUT


def test_wechat_subwindow_with_main_window_present_is_movable():
    """主窗口在场时，一个不够格当主窗口、但尺寸高于下限的 wechat 类窗口
    （比如聊天详情/小窗口）应该是 MOVABLE。"""
    subwindow = make_window(width=500, height=700)
    assert classify.classify(subwindow, True, {}) is Category.MOVABLE


def test_wechat_app_ex_is_movable():
    """小程序运行时窗口 WeChatAppEx（WM_CLASS 含 wechat 但不是精确的
    "wechat"）在主窗口在场时应该是 MOVABLE。"""
    mini_program = make_window(wm_class="WeChatAppEx", width=400, height=600)
    assert classify.classify(mini_program, True, {}) is Category.MOVABLE


def test_modal_follows_assigned_parent():
    """模态窗口的 transient_for 指向一个已经分配到副屏的窗口时，跟随父窗口
    而不是被保护在原地。"""
    modal = make_window(window_id=2, is_modal=True, transient_for=1, width=300, height=200)
    assigned = {1: "display2"}
    assert classify.classify(modal, True, assigned) is Category.FOLLOWS_PARENT


def test_modal_without_assigned_parent_is_protected():
    """模态窗口没有 transient_for，或 transient_for 指向的窗口不在 assigned
    里（父窗口还没搬、或根本没有父窗口）时，必须保持受保护，不能跟着搬。"""
    modal_no_parent = make_window(window_id=2, is_modal=True, transient_for=None)
    assert classify.classify(modal_no_parent, True, {}) is Category.PROTECTED_MODAL

    modal_unassigned_parent = make_window(window_id=2, is_modal=True, transient_for=1)
    assert classify.classify(modal_unassigned_parent, True, {}) is Category.PROTECTED_MODAL


def test_tiny_non_wechat_window_is_ignored():
    """低于 REAL_MIN 下限的窗口（比如某些应用的辅助小窗）即使主窗口在场也
    应该被忽略，不当成 MOVABLE。"""
    tiny = make_window(wm_class="SomeHelper", width=100, height=100)
    assert classify.classify(tiny, True, {}) is Category.IGNORE


# ----------------------------------------------------------------- layout


def _rects_overlap(a: layout.Rect, b: layout.Rect) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _rect_within(rect: layout.Rect, bounds: layout.Rect) -> bool:
    x, y, w, h = rect
    bx, by, bw, bh = bounds
    return x >= bx and y >= by and x + w <= bx + bw and y + h <= by + bh


def test_tile_rects_counts_bounds_and_no_overlap():
    monitor = (0, 0, 1920, 1080)
    for n in range(1, 6):
        rects = layout.tile_rects(n, monitor)
        assert len(rects) == n, n
        for rect in rects:
            assert _rect_within(rect, monitor), (n, rect)
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                assert not _rects_overlap(rects[i], rects[j]), (n, rects[i], rects[j])


def test_tile_rects_zero_is_empty():
    assert layout.tile_rects(0, (0, 0, 1920, 1080)) == []


def test_cascade_rects_stay_within_primary():
    primary = (0, 0, 1920, 1080)
    # 混入一个比 primary 还大的窗口，专门验证钳制路径。
    sizes = [(400, 300), (400, 300), (400, 300), (2200, 1500)]
    rects = layout.cascade_rects(sizes, primary)
    assert len(rects) == len(sizes)
    for rect in rects:
        assert _rect_within(rect, primary), rect

    # 正常尺寸的窗口只改位置不改尺寸。
    assert rects[0][2:] == (400, 300)
    # 超尺寸的窗口被钳制到 primary 能放下的最大值。
    assert rects[3][2:] == (1920, 1080)


# ------------------------------------------------------- find_owning_display


def test_find_owning_display_picks_largest_overlap():
    monitors = {
        "primary": (0, 0, 1000, 1000),
        "display2": (1000, 0, 1000, 1000),
    }
    # 矩形横跨两块显示器，但绝大部分面积落在 display2 里。
    straddling = (900, 0, 300, 500)
    assert classify.find_owning_display(straddling, monitors) == "display2"

    # 完全落在 primary 内部。
    inside_primary = (100, 100, 200, 200)
    assert classify.find_owning_display(inside_primary, monitors) == "primary"


def test_find_owning_display_none_when_outside_all_monitors():
    monitors = {"primary": (0, 0, 1000, 1000)}
    outside = (5000, 5000, 100, 100)
    assert classify.find_owning_display(outside, monitors) is None
