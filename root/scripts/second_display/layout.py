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
