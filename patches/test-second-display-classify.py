"""root/scripts/second_display 里 classify.py 与 layout.py 的纯函数测试。

两个模块都不依赖 Xlib，可以在没有 X 服务器的宿主机上直接跑：

    python3 -m pytest patches/test-second-display-classify.py -v

覆盖的核心不变量：真正当选的主窗口在任何 assigned 组合下都不会被误判成
MOVABLE（这是最不能出错的一条——错判会把用户正在用的主窗口拖去副屏）；
同样满足主窗口几何判据、但没有当选的窗口（生产环境实测：图片/视频查看器
WM_CLASS 也是 wechat 且经常 >=600x600）必须落回 MOVABLE，而不是被误判成
"又一个主窗口"；托盘图标、无主窗口时的登录/登出窗受到保护；选举规则
（粘性连任、first_seen 最早胜出、id 决胜）；tile_rects/cascade_rects 产出
的矩形互不重叠、且落在目标区域内；is_converged() 的收敛判断（带装饰偏移
的几何应判收敛、漂移超容差或目标变更都应判需要重发命令）。
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

# 表示"主窗口存在，但不是当前正在测试的这个窗口"——window_id 用 1 起步的
# 小整数，999 绝不会和它们撞车。classify() 现在只按身份（window_id ==
# main_window_id）判定 MAIN_WINDOW，不再重新跑一遍几何判据，因此这类测试
# 只需要一个"存在但不是我"的哨兵 id，而不是像旧签名那样传布尔值。
OTHER_MAIN_ID = 999


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


def test_elected_main_window_never_classified_as_movable():
    """不变量：当选的主窗口在任何 assigned 组合下都必须分类成 MAIN_WINDOW，
    绝不会是 MOVABLE——这是唯一一条错了就会把用户正在用的主窗口拖去副屏
    的规则。"""
    for width, height in ((600, 600), (600, 601), (1920, 1080), (3232, 2048)):
        window = make_window(width=width, height=height)
        for assigned in ({}, {window.window_id: "display2"}):
            result = classify.classify(window, window.window_id, assigned)
            assert result is Category.MAIN_WINDOW, (width, height, assigned, result)
            assert result is not Category.MOVABLE


def test_main_candidate_that_lost_the_election_is_movable():
    """生产实测的核心场景：两个窗口都满足主窗口的几何判据（WM_CLASS
    "wechat"、>=600x600、非 modal——典型例子是图片/视频查看器全屏打开
    时），只有当选的那个是 MAIN_WINDOW，另一个必须落回 MOVABLE，而不是
    也被当成主窗口晾在原地搬不走。"""
    elected = make_window(window_id=1, width=700, height=700)
    loser = make_window(window_id=2, width=1200, height=900)
    assert classify.is_main_candidate(elected)
    assert classify.is_main_candidate(loser)  # 落选者本身仍然"够格参选"

    assert classify.classify(elected, elected.window_id, {}) is Category.MAIN_WINDOW
    assert classify.classify(loser, elected.window_id, {}) is Category.MOVABLE


def test_tray_icon_is_ignored():
    """24x24 的托盘图标——不管 override_redirect 是否显式为真，光凭尺寸下限
    就必须被过滤。"""
    tray_by_size = make_window(width=24, height=24, override_redirect=False)
    assert classify.classify(tray_by_size, OTHER_MAIN_ID, {}) is Category.IGNORE

    tray_by_flag = make_window(width=24, height=24, override_redirect=True)
    assert classify.classify(tray_by_flag, OTHER_MAIN_ID, {}) is Category.IGNORE

    # override_redirect 单独也足以判定 IGNORE，即便尺寸不小。
    override_redirect_large = make_window(width=800, height=600, override_redirect=True)
    assert classify.classify(override_redirect_large, OTHER_MAIN_ID, {}) is Category.IGNORE


def test_login_window_without_main_window_is_protected():
    """无主窗口（main_window_id 为 None）时，560x760 的登录/二维码窗
    （wechat-window-watchdog.sh 记录的真实尺寸）必须受保护，不能被当成
    MOVABLE 搬走。"""
    login_window = make_window(width=560, height=760)
    assert classify.classify(login_window, None, {}) is Category.PROTECTED_LOGIN_OR_LOGOUT


def test_wechat_subwindow_with_main_window_present_is_movable():
    """主窗口在场时，一个不够格当主窗口、但尺寸高于下限的 wechat 类窗口
    （比如聊天详情/小窗口）应该是 MOVABLE。"""
    subwindow = make_window(width=500, height=700)
    assert classify.classify(subwindow, OTHER_MAIN_ID, {}) is Category.MOVABLE


def test_wechat_app_ex_is_movable():
    """小程序运行时窗口 WeChatAppEx（WM_CLASS 含 wechat 但不是精确的
    "wechat"）在主窗口在场时应该是 MOVABLE。"""
    mini_program = make_window(wm_class="WeChatAppEx", width=400, height=600)
    assert classify.classify(mini_program, OTHER_MAIN_ID, {}) is Category.MOVABLE


def test_modal_follows_assigned_parent():
    """模态窗口的 transient_for 指向一个已经分配到副屏的窗口时，跟随父窗口
    而不是被保护在原地。"""
    modal = make_window(window_id=2, is_modal=True, transient_for=1, width=300, height=200)
    assigned = {1: "display2"}
    assert classify.classify(modal, OTHER_MAIN_ID, assigned) is Category.FOLLOWS_PARENT


def test_modal_without_assigned_parent_is_protected():
    """模态窗口没有 transient_for，或 transient_for 指向的窗口不在 assigned
    里（父窗口还没搬、或根本没有父窗口）时，必须保持受保护，不能跟着搬。"""
    modal_no_parent = make_window(window_id=2, is_modal=True, transient_for=None)
    assert classify.classify(modal_no_parent, OTHER_MAIN_ID, {}) is Category.PROTECTED_MODAL

    modal_unassigned_parent = make_window(window_id=2, is_modal=True, transient_for=1)
    assert classify.classify(modal_unassigned_parent, OTHER_MAIN_ID, {}) is Category.PROTECTED_MODAL


def test_tiny_non_wechat_window_is_ignored():
    """低于 REAL_MIN 下限的窗口（比如某些应用的辅助小窗）即使主窗口在场也
    应该被忽略，不当成 MOVABLE。"""
    tiny = make_window(wm_class="SomeHelper", width=100, height=100)
    assert classify.classify(tiny, OTHER_MAIN_ID, {}) is Category.IGNORE


# ------------------------------------------------------- is_main_candidate()


def test_is_main_candidate_geometry():
    """候选资格只看 WM_CLASS/尺寸/模态，和"最终选不选它"无关。"""
    assert classify.is_main_candidate(make_window(width=600, height=600))
    assert not classify.is_main_candidate(make_window(width=599, height=600)), "宽度差 1px 不够格"
    assert not classify.is_main_candidate(make_window(width=600, height=599)), "高度差 1px 不够格"
    assert not classify.is_main_candidate(
        make_window(wm_class="WeChatAppEx", width=1200, height=900)
    ), "WM_CLASS 不是精确的 wechat，再大也不参选"
    assert not classify.is_main_candidate(
        make_window(width=1200, height=900, is_modal=True)
    ), "模态窗口不参选，即便尺寸够格"


# ---------------------------------------------------------- elect_main_window()


def test_elect_main_window_no_candidates_returns_none():
    assert classify.elect_main_window({}, {}, incumbent=None) is None
    assert classify.elect_main_window({}, {}, incumbent=1) is None, "候选池清空后不能凭空连任"


def test_elect_main_window_earliest_first_seen_wins():
    candidates = {
        1: make_window(window_id=1, width=700, height=700),
        2: make_window(window_id=2, width=700, height=700),
    }
    first_seen = {1: 100.0, 2: 50.0}  # 2 先出现
    assert classify.elect_main_window(candidates, first_seen, incumbent=None) == 2


def test_elect_main_window_tie_break_by_window_id():
    """生产实测的决胜规则：同一轮首见（first_seen 相同）时选 window_id
    最小的——微信真正的主窗口 0x1800013 先于图片查看器 0x180001d 创建，
    id 更小。"""
    candidates = {
        0x1800013: make_window(window_id=0x1800013, width=700, height=700),
        0x180001D: make_window(window_id=0x180001D, width=700, height=700),
    }
    first_seen = {0x1800013: 10.0, 0x180001D: 10.0}
    assert classify.elect_main_window(candidates, first_seen, incumbent=None) == 0x1800013


def test_elect_main_window_sticky_incumbent():
    """上一轮已当选者只要仍在候选池里就连任，即便另一个候选的 first_seen
    更早/id 更小——避免主窗口在多个候选之间反复横跳。"""
    candidates = {
        1: make_window(window_id=1, width=700, height=700),
        2: make_window(window_id=2, width=700, height=700),
    }
    first_seen = {1: 999.0, 2: 1.0}  # 2 明显更早，但 1 是在任者
    assert classify.elect_main_window(candidates, first_seen, incumbent=1) == 1


def test_elect_main_window_incumbent_replaced_when_no_longer_candidate():
    """在任者一旦跌出候选池（消失、缩小、变 modal……），必须重新选举，
    而不是没有候选人也继续"连任"。"""
    candidates = {2: make_window(window_id=2, width=700, height=700)}
    first_seen = {2: 5.0}
    assert classify.elect_main_window(candidates, first_seen, incumbent=1) == 2


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


# ------------------------------------------------------------- is_converged


def test_is_converged_with_frame_offset():
    """命令过的目标，client 几何带着 openbox 装饰造成的偏移（生产实测
    (4072,8) 目标落地成 (4073,48)）——应该判定已收敛，不再重发命令。"""
    target = (4072, 8, 300, 300)
    current = (4073, 48, 300, 300)  # x 偏 1px、y 偏 40px，都在 64px 容差内
    assert layout.is_converged(last_commanded=target, target=target, current=current)


def test_is_converged_false_when_drifted_beyond_tolerance():
    """已经命令过这个目标，但当前几何偏出容差之外（用户手动拖走、或被
    其它程序改了位置）——必须判定未收敛，需要重发命令。"""
    target = (0, 0, 300, 300)
    drifted = (0, 100, 300, 300)  # y 偏 100px，超过 64px 容差
    assert not layout.is_converged(last_commanded=target, target=target, current=drifted)


def test_is_converged_false_when_target_changed():
    """即使当前几何恰好等于新目标，只要 last_commanded 不是这次的 target
    （比如从没命令过，或者上一次命令的是别的目标），也要重发一次——
    确保"目标变了"这件事总会被至少命令一次，不会因为凑巧位置已经对上就
    被跳过。"""
    old_target = (0, 0, 300, 300)
    new_target = (100, 0, 300, 300)
    assert not layout.is_converged(last_commanded=old_target, target=new_target, current=new_target)
    assert not layout.is_converged(last_commanded=None, target=new_target, current=new_target)


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
