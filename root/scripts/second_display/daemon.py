"""副屏窗口管家的核心状态机：读窗口/显示器现状 → 分类 → 布局 → 写回几何。

reconcile() 是唯一的状态转移函数，且是幂等的：不管从哪个触发源调用（X11
事件、3 秒兜底轮询、启动自愈），效果只取决于"此刻的窗口/显示器实况"，不
依赖调用历史。run_forever() 保证同一时刻只有一次 reconcile() 在执行（事件
唤醒和兜底轮询共用同一个循环，从不并发），所以 assigned/first_seen 这两个
内部状态不需要加锁；唯一跨线程的读——HTTP handler 读 status_snapshot()——
读到的是 reconcile() 每轮结束时整体替换出来的一个新字典引用，而不是被原地
修改的字典，CPython 下引用赋值/读取本身是原子的，这也不需要加锁。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from . import classify, layout, x11

LOG = logging.getLogger("wechat-second-display.daemon")

# 事件监听覆盖不到的拓扑变化（例如窗口被重新父化之后的状态变化，见
# x11.watch() 的说明）由这个周期性兜底轮询兜底，是正确性的最终保障。
SAFETY_POLL_INTERVAL_S = 3.0

Rect = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class DisplayStatus:
    display_id: str
    connected: bool
    geometry: Rect

    def payload(self) -> dict[str, object]:
        x, y, w, h = self.geometry
        return {
            "id": self.display_id,
            "connected": self.connected,
            "geometry": {"x": x, "y": y, "w": w, "h": h},
        }


def _empty_snapshot() -> dict[str, object]:
    return {"version": 1, "displays": [], "movable_count": 0, "unassigned_count": 0}


class Daemon:
    def __init__(self, disp, root):
        self._disp = disp
        self._root = root
        # window_id -> displayId：当前认定"已经归位"在哪块副屏上。
        self.assigned: dict[int, str] = {}
        # window_id -> 首次见到的单调时间戳，决定平铺顺序——先来的排前面，
        # 平铺结果不会因为每轮枚举顺序的抖动而跳来跳去。
        self.first_seen: dict[int, float] = {}
        # 当选的主窗口 id（恰好一个，或者还没选出来时是 None）——见
        # _elect_main_window()。
        self._elected_main_id: int | None = None
        self._snapshot: dict[str, object] = _empty_snapshot()

    # ---------------------------------------------------------- 对外查询

    def status_snapshot(self) -> dict[str, object]:
        """线程安全：见模块顶部关于"整体替换而非原地修改"的说明。"""
        return self._snapshot

    # ---------------------------------------------------------------- 主循环

    def run_forever(self) -> None:
        """阻塞式主循环，只应该从一个线程调用一次（__main__.py 通过
        run_in_executor 丢进线程池的那一次）。异常会向上抛出，让整个进程
        退出，交给 s6 supervisor 重启，而不是在这里悄悄吞掉未知错误。
        """
        wake = threading.Event()
        watcher = threading.Thread(target=x11.watch, args=(wake.set,), daemon=True)
        watcher.start()

        self.reconcile()  # 启动自愈：进程一起来先跑一次，不等第一个事件
        while True:
            wake.wait(timeout=SAFETY_POLL_INTERVAL_S)
            wake.clear()
            self.reconcile()

    # ------------------------------------------------------------- reconcile

    def reconcile(self) -> None:
        try:
            self._reconcile_once()
        except Exception:  # noqa: BLE001 - 见下方说明
            # 单轮 reconcile 失败（多半是窗口在读取过程中消失的竞态）不应该
            # 杀死整个循环：下一轮兜底轮询会在至多 3 秒后重新尝试，行为仍然
            # 是幂等的。只有 run_forever() 本身或 watch() 线程崩溃才交给
            # s6 重启整个进程。
            LOG.exception("reconcile() failed, will retry on next tick")

    def _reconcile_once(self) -> None:
        windows = x11.list_windows(self._disp, self._root)
        monitors = x11.list_monitors(self._disp, self._root)
        by_id = {window.window_id: window for window in windows}
        live_ids = set(by_id)

        now = time.monotonic()
        for window_id in live_ids:
            self.first_seen.setdefault(window_id, now)
        # 清理已经不存在的窗口的登记信息，避免 assigned/first_seen 无限增长，
        # 且必须在 classify() 之前完成——FOLLOWS_PARENT 判据依赖 assigned
        # 只包含"当前仍然存在"的窗口。
        for stale in [wid for wid in self.assigned if wid not in live_ids]:
            del self.assigned[stale]
        for stale in [wid for wid in self.first_seen if wid not in live_ids]:
            del self.first_seen[stale]

        main_window_id = self._elect_main_window(windows)
        categories = {
            window.window_id: classify.classify(window, main_window_id, self.assigned)
            for window in windows
        }

        secondary = _pick_secondary_display(monitors)
        if secondary is not None:
            display_id, monitor_rect = secondary
            self._assign_movable(by_id, categories, display_id, monitor_rect)
            self._assign_followers(by_id, categories, display_id, monitor_rect)
        else:
            self._recall_all(by_id, categories, monitors)

        self._snapshot = self._build_snapshot(by_id, monitors, categories)

    def _elect_main_window(self, windows: list[classify.WindowInfo]) -> int | None:
        """选出恰好一个主窗口 id，并把结果记进 self._elected_main_id 供下一轮
        连任判断——真正的决策规则是纯函数 classify.elect_main_window()（连带
        候选资格判据 classify.is_main_candidate() 一起单测覆盖），这里只做
        "从 windows 里筛出候选池、喂给决策函数、记住跨轮次状态"这层编排。

        生产环境观察到微信的图片/视频查看器窗口 WM_CLASS 同样是 "wechat"
        且经常 >=600x600，必须在多个候选里选出恰好一个当主窗口，其余的
        落回 MOVABLE，因此才需要"选举"而不是"只要满足几何判据就是主窗口"。
        """
        candidates = {w.window_id: w for w in windows if classify.is_main_candidate(w)}
        self._elected_main_id = classify.elect_main_window(
            candidates, self.first_seen, self._elected_main_id
        )
        return self._elected_main_id

    def _is_main_window(self, window: classify.WindowInfo) -> bool:
        return window.window_id == self._elected_main_id

    def _assign_movable(self, by_id, categories, display_id: str, monitor_rect: Rect) -> None:
        movable_ids = [
            wid
            for wid, category in categories.items()
            if category is classify.Category.MOVABLE and by_id[wid].mapped
        ]
        movable_ids.sort(key=lambda wid: self.first_seen.get(wid, 0.0))
        rects = layout.tile_rects(len(movable_ids), monitor_rect)
        for wid, rect in zip(movable_ids, rects):
            self.assigned[wid] = display_id
            self._move_if_needed(by_id[wid], rect)

    def _assign_followers(self, by_id, categories, display_id: str, monitor_rect: Rect) -> None:
        for wid, category in categories.items():
            if category is not classify.Category.FOLLOWS_PARENT:
                continue
            window = by_id[wid]
            parent = by_id.get(window.transient_for)
            if parent is None:
                # 不应该发生：classify() 判定 FOLLOWS_PARENT 的前提正是
                # transient_for 在 assigned 里，而 assigned 已经在本轮开头
                # 按 live_ids 清理过——防御性写法，不代表这是预期路径。
                continue
            rect = _center_clamped(window, parent, monitor_rect)
            self.assigned[wid] = display_id
            self._move_if_needed(window, rect)

    def _recall_all(self, by_id, categories, monitors) -> None:
        # 副屏不在了：把所有仍标记着 assigned 的窗口，连同"滞留在所有已知
        # 显示器范围之外"的 MOVABLE 窗口，一起用 cascade 摆回主屏，清空
        # assigned——它们在下一轮 reconcile 里会被当成普通的"未分配"窗口
        # 重新判断。只改位置，不恢复"搬去副屏之前"的尺寸——那个尺寸本来就
        # 没有被记录下来，和计划的"只改位置不改尺寸"是同一件事。
        #
        # 后一类滞留窗口是启动自愈需要的场景：assigned 是纯内存状态，
        # 守护进程在窗口还位于副屏区域时被杀掉/重启后，assigned 会是空的；
        # 如果此时上游已经把副屏的 xrandr 布局收回，这些窗口的几何就会
        # 永远停在"不属于任何当前显示器"的位置，直到用户凑巧再开一次副屏。
        # 这里不依赖 assigned 的记忆，而是每轮直接用窗口当前几何去判断
        # "它现在是不是不在任何显示器上"（find_owning_display 返回 None），
        # 天然覆盖重启，也天然幂等——一旦窗口被搬回 primary，下一轮就会
        # 命中 primary，不再判定为滞留。
        primary_rect = monitors.get("primary")
        cascade_target = primary_rect if primary_rect is not None else self._fallback_primary_rect(by_id)

        recall_ids = {wid for wid in self.assigned if wid in by_id}
        # 只有确认 selkies-primary 已经存在时才做这条自愈：容器极早期 RandR
        # 还没建立任何显示器时，"不属于任何显示器"对所有窗口都成立，贸然
        # 判定滞留会在启动瞬间把窗口错误地拉去兜底矩形。
        if primary_rect is not None:
            for wid, category in categories.items():
                if category is not classify.Category.MOVABLE or wid in recall_ids:
                    continue
                window = by_id[wid]
                if not window.mapped:
                    continue
                geometry = (window.x, window.y, window.width, window.height)
                if classify.find_owning_display(geometry, monitors) is None:
                    recall_ids.add(wid)

        recalled = sorted(recall_ids, key=lambda wid: self.first_seen.get(wid, 0.0))
        sizes = [(by_id[wid].width, by_id[wid].height) for wid in recalled]
        rects = layout.cascade_rects(sizes, cascade_target)
        for wid, rect in zip(recalled, rects):
            self._move_if_needed(by_id[wid], rect)
        self.assigned.clear()

    def _fallback_primary_rect(self, by_id) -> Rect:
        # RandR 还没建立 selkies-primary（极早期启动窗口）时的退化路径：用
        # 主窗口当前几何聊胜于无地给 cascade 一个落点。
        for window in by_id.values():
            if self._is_main_window(window):
                return (window.x, window.y, window.width, window.height)
        return (0, 0, 1920, 1080)

    def _move_if_needed(self, window: classify.WindowInfo, rect: Rect) -> None:
        if (window.x, window.y, window.width, window.height) == rect:
            return  # 防抖：目标几何和当前几何一致就不发 xdotool 命令
        x11.move_resize(window.window_id, rect, window.title)

    def _build_snapshot(self, by_id, monitors, categories) -> dict[str, object]:
        # 两个计数都只统计 mapped 的窗口，和 _assign_movable() 的过滤条件
        # 对齐——否则一个未 map（比如被最小化）的 MOVABLE 窗口会让
        # unassigned_count 恒大于 0，提示条会在副屏已经打开、没有任何真正
        # 未分配窗口的情况下继续常驻。
        movable_count = sum(
            1
            for wid, c in categories.items()
            if c is classify.Category.MOVABLE and by_id[wid].mapped
        )
        unassigned_count = sum(
            1
            for wid, c in categories.items()
            if c is classify.Category.MOVABLE and by_id[wid].mapped and wid not in self.assigned
        )
        # displays 只列出"当前实际已连接"的副屏（RandR 里存在对应
        # selkies-* 显示器），空数组即代表没有副屏——v1 单副屏下这已经是
        # 客户端判断"该不该提示打开副屏"所需的全部信息；数组形状本身对
        # N 副屏演进保持开放（多个非 primary 条目自然并存）。
        displays = [
            DisplayStatus(display_id, True, rect).payload()
            for display_id, rect in sorted(monitors.items())
            if display_id != "primary"
        ]
        return {
            "version": 1,
            "displays": displays,
            "movable_count": movable_count,
            "unassigned_count": unassigned_count,
        }


def _pick_secondary_display(
    monitors: dict[str, Rect],
) -> tuple[str, Rect] | None:
    # v1 只支持单副屏，上游本身也同一时刻只允许一个非 primary 的逻辑显示器
    # 存在（同 id 新连接会 KILL 旧连接），因此这里不需要处理多个候选的排序
    # 问题——真正的 N 副屏需要先改上游，属于独立后续项目（见 docs）。
    for display_id, rect in monitors.items():
        if display_id != "primary":
            return display_id, rect
    return None


def _center_clamped(window: classify.WindowInfo, parent: classify.WindowInfo, monitor_rect: Rect) -> Rect:
    """把 window 居中于 parent，钳制在 monitor_rect 内，只在超尺寸时改变宽高。"""
    mx, my, mw, mh = monitor_rect
    w = min(window.width, mw)
    h = min(window.height, mh)
    cx = parent.x + (parent.width - w) // 2
    cy = parent.y + (parent.height - h) // 2
    x = max(mx, min(cx, mx + mw - w))
    y = max(my, min(cy, my + mh - h))
    return (x, y, w, h)
