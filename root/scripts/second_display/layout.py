"""纯函数窗口几何布局：把 N 个矩形铺进一块显示器区域，或按级联方式摆回主屏。

同样不依赖 Xlib，可脱离 X 服务器直接跑 pytest。
"""

from __future__ import annotations

import math

Rect = tuple[int, int, int, int]  # (x, y, w, h)，统一约定

TILE_GAP_PX = 8
CASCADE_STEP_PX = 40
# 级联错开的循环周期：超过这个数量就从头叠一层，避免级联跑出屏幕外。
CASCADE_WRAP = 8

# xdotool 请求的目标几何和 xdotool getwindowgeometry/Xlib 读回来的 client
# 几何之间，openbox 装饰（标题栏/边框）可能造成的最大偏移余量——生产实测过
# tile 目标 (4072,8) 落地成 client 几何 (4073,48)。用来判断"窗口是不是已经
# 停在了它该在的地方"，不能用精确相等（见 is_converged()）。
FRAME_TOLERANCE_PX = 64


def tile_rects(n: int, monitor: Rect, gap: int = TILE_GAP_PX) -> list[Rect]:
    """把 n 个矩形在 monitor 区域内均分成网格。

    行列数取 cols=ceil(sqrt(n))、rows=ceil(n/cols)：n=1 铺满、n=2 左右对半、
    n=3/4 2x2 宫格（3 用掉 4 格中的 3 格）、n>=5 自然延伸成更多行。末行数量
    不足一整行时在该行内居中，而不是拉伸单元格或把空位全部堆在左边。
    """
    if n <= 0:
        return []
    mx, my, mw, mh = monitor
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)

    # 整数除法保证 cols*cell_w+(cols+1)*gap <= mw（行同理），因此任何一行/
    # 列都不会超出 monitor 边界；余下的几像素舍入误差只会留白，不会溢出。
    cell_w = max((mw - gap * (cols + 1)) // cols, 1)
    cell_h = max((mh - gap * (rows + 1)) // rows, 1)

    rects: list[Rect] = []
    remaining = n
    for row in range(rows):
        count_in_row = min(cols, remaining)
        remaining -= count_in_row
        # 末行按实际数量居中：整行宽度用实际列数重算，起始 x 偏移让两侧留白
        # 对称，而不是让空位全部堆在一边。
        row_width = count_in_row * cell_w + (count_in_row - 1) * gap
        row_x = mx + (mw - row_width) // 2
        row_y = my + gap + row * (cell_h + gap)
        for col in range(count_in_row):
            x = row_x + col * (cell_w + gap)
            rects.append((x, row_y, cell_w, cell_h))
    return rects


def is_converged(
    last_commanded: Rect | None,
    target: Rect,
    current: Rect,
    tolerance: int = FRAME_TOLERANCE_PX,
) -> bool:
    """判断一个窗口是否已经收敛到 target，不需要再重发 xdotool 命令。

    收敛需要同时满足两个条件：

      1. 上一次已经commanded 的目标就是这次要发的 target——不是"这次目标
         恰好等于当前几何"，而是"上次已经吩咐它去这儿了"。目标变了（比如
         平铺窗口数变化、副屏改分辨率）必须重发，与 last_commanded 无关。
      2. 当前几何在 tolerance 像素内贴近 target——不能要求精确相等：
         openbox 的窗口装饰（标题栏/边框）会让 xdotool 实际命中的 client
         几何和请求的目标几何差着几十像素（生产实测 tile 目标 (4072,8)
         落地成 (4073,48)），精确相等永远不成立，daemon 会每轮都重发同一条
         命令；命令本身触发的 ConfigureNotify 又会唤醒事件驱动的
         reconcile，形成比 3 秒兜底轮询快得多的自激循环，持续用 xdotool
         骚扰 X 服务器。

    两个条件都满足才算收敛；只要 last_commanded 还没记录过这个 target，
    或者窗口漂移超出容差（用户手动拖走、被其它程序改了位置），都要重发。
    """
    if last_commanded != target:
        return False
    return all(abs(a - b) <= tolerance for a, b in zip(current, target))


def cascade_rects(
    sizes: list[tuple[int, int]], primary: Rect, step: int = CASCADE_STEP_PX
) -> list[Rect]:
    """把 sizes 里每个 (w, h) 按对角线错开的方式摆回 primary 区域左上角附近。

    只决定新位置，尺寸原样保留——除非超出 primary 边界，那种情况下把尺寸
    钳制到 primary 能放下的最大值，并让位置退回 primary 内部，保证整个
    窗口都落在主屏可见范围内，不会有一半探出屏幕外。
    """
    px, py, pw, ph = primary
    rects: list[Rect] = []
    for i, (w, h) in enumerate(sizes):
        offset = (i % CASCADE_WRAP) * step
        cw = min(w, pw)
        ch = min(h, ph)
        x = min(px + 24 + offset, px + pw - cw)
        y = min(py + 24 + offset, py + ph - ch)
        rects.append((x, y, cw, ch))
    return rects
