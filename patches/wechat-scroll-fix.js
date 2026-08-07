/*
 * 修复浏览器端鼠标滚轮时快时慢的问题。
 *
 * 上游 Selkies 的 input.js 用最近 4 个 |deltaY| 猜设备类型，macOS 滚轮经过
 * 系统加速后 deltaY 不恒定，真实滚轮经常被误判成触控板。触控板路径每 100ms
 * 只处理 1 个事件，限流窗口内的事件直接丢弃；_mouseWheel 又用会话级最小值
 * _smallestDeltaY 做除数，滚轮一格 deltaY≈120 会被放大成 magnitude=10。
 *
 * 这个补丁不修改 minified bundle：在 window 的 capture 阶段（passive:false）
 * 接管 webrtcInput.element 内的 wheel 事件，按 deltaMode 换算成像素后固定
 * 阈值累积。这样不依赖设备分类，不丢限流窗口内的事件，也不会被会话级最小
 * delta 放大。attach_context() 重连时会重新注册上游监听，所以这里每次事件
 * 都动态读取 window.webrtcInput，不缓存实例。
 *
 * 上游 trigger 方法置位 buttonMask 后要等 10ms 才发 release；两次事件间隔
 * 小于 10ms 时再调用 trigger，服务端会因 mask 无变化整批跳过。因此发送前
 * 检查对应滚轮位是否仍被按住，若仍按住就把这批 tick 退回累加器，等掩码空闲
 * 的下一个事件一起冲刷。超过 MAX_MAGNITUDE 的整 tick 直接丢弃，不延后。
 */
(function () {
  "use strict";

  var TAG = "[wechat-scroll-fix]";
  var LINE_TO_PIXELS = 40;
  var PIXELS_PER_TICK = Math.max(1, Number(window.WECHAT_SCROLL_PIXELS_PER_TICK) || 40);
  var MAX_MAGNITUDE = Math.max(1, Number(window.WECHAT_SCROLL_MAX_MAGNITUDE) || 10);
  var WHEEL_BUTTON_BITS = {
    up: 1 << 4,
    down: 1 << 3,
    left: 1 << 6,
    right: 1 << 7
  };

  if (String(location.hash).indexOf("shared") !== -1 ||
      String(location.hash).indexOf("player") !== -1) {
    return;
  }

  if (window.__wechatScrollFixInstalled) return;
  window.__wechatScrollFixInstalled = true;

  var accX = 0;
  var accY = 0;
  var warnedMissingApi = false;

  function numericDelta(value) {
    var n = Number(value);
    return isFinite(n) ? n : 0;
  }

  // deltaMode 0 是像素，1 是行，2 是页。页高优先用流元素自身高度。
  function elementHeight(element) {
    var height = 0;
    if (element) {
      if (element.clientHeight) {
        height = element.clientHeight;
      } else if (typeof element.getBoundingClientRect === "function") {
        try {
          var rect = element.getBoundingClientRect();
          if (rect && rect.height) height = rect.height;
        } catch (error) {
          height = 0;
        }
      }
    }
    if (height <= 0 && window.innerHeight) height = window.innerHeight;
    // 极端情况下仍给一个可换算的兜底值，避免整页滚动事件被吞掉。
    return height || 40;
  }

  function toPixels(delta, mode, pageHeight) {
    if (mode === 1) return delta * LINE_TO_PIXELS;
    if (mode === 2) return delta * pageHeight;
    return delta;
  }

  function targetInside(element, target) {
    if (!element || !target || typeof element.contains !== "function") return false;
    try {
      return element.contains(target);
    } catch (error) {
      return false;
    }
  }

  // 每跨过 PIXELS_PER_TICK 产生一个 tick；余数只保留 sub-tick 部分。超过
  // MAX_MAGNITUDE 的整 tick 直接丢弃，避免留在大数里让下一个事件瞬间爆冲。
  // 方向反转时先清零，避免反向滚动先“吃掉”旧方向留下的余数造成迟滞。
  function consume(axis, delta, mode, pageHeight) {
    var pixels = toPixels(delta, mode, pageHeight);
    if (pixels === 0) return null;

    var acc = axis === "x" ? accX : accY;
    if (acc !== 0 && (pixels < 0) !== (acc < 0)) acc = 0;
    acc += pixels;

    var ticks = Math.floor(Math.abs(acc) / PIXELS_PER_TICK);
    var magnitude = Math.min(ticks, MAX_MAGNITUDE);
    var sign = acc < 0 ? -1 : 1;
    var remainder = acc - sign * ticks * PIXELS_PER_TICK;

    if (axis === "x") accX = remainder;
    else accY = remainder;

    if (magnitude === 0) return null;

    var direction;
    if (axis === "x") direction = sign < 0 ? "left" : "right";
    else direction = sign < 0 ? "up" : "down";
    return { direction: direction, magnitude: magnitude, sign: sign, ticks: ticks };
  }

  // 上游 trigger 后 10ms 才 release；掩码仍占用时调用会让服务端因 mask 无
  // 变化整批跳过。把这次已经消费的整 tick 退回累加器，等空闲事件再冲刷。
  function restoreTicks(axis, result) {
    var pixels = result.sign * result.ticks * PIXELS_PER_TICK;
    if (axis === "x") accX += pixels;
    else accY += pixels;
  }

  function wheelButtonHeld(handler, direction) {
    var mask = Number(handler.buttonMask) || 0;
    return (mask & WHEEL_BUTTON_BITS[direction]) !== 0;
  }

  function onWheel(event) {
    var handler = window.webrtcInput;
    if (!handler || !targetInside(handler.element, event.target)) return;
    if (event.cancelable === false) return;

    var deltaY = numericDelta(event.deltaY);
    var deltaX = numericDelta(event.deltaX);
    if (deltaY === 0 && deltaX === 0) return;

    // 基镜像升级后如果这些私有 API 不存在，放行原生事件而不是吞掉滚动。
    if (typeof handler._triggerMouseWheel !== "function" ||
        typeof handler._triggerHorizontalMouseWheel !== "function") {
      if (!warnedMissingApi) {
        warnedMissingApi = true;
        console.warn(TAG, "Selkies wheel API unavailable; leaving native wheel handling enabled");
      }
      return;
    }
    warnedMissingApi = false;

    // capture 阶段拦截，让上游注册在 element bubble 阶段的 _mouseWheelWrapper
    // 不再收到这个事件。passive:false 保证 preventDefault 一定有效。
    event.preventDefault();
    event.stopPropagation();

    var deltaMode = numericDelta(event.deltaMode);
    var mode = (deltaMode === 1 || deltaMode === 2) ? deltaMode : 0;
    var pageHeight = elementHeight(handler.element);
    var vertical = deltaY !== 0 ? consume("y", deltaY, mode, pageHeight) : null;
    var horizontal = deltaX !== 0 ? consume("x", deltaX, mode, pageHeight) : null;

    // 一次事件的多个 tick 合并成一次 magnitude=N 发送，服务端会按 magnitude
    // 循环按下滚轮按钮，因此不会丢事件也不会逐 tick 刷屏。
    if (vertical) {
      if (wheelButtonHeld(handler, vertical.direction)) {
        restoreTicks("y", vertical);
      } else {
        handler._triggerMouseWheel(vertical.direction, vertical.magnitude);
      }
    }
    if (horizontal) {
      if (wheelButtonHeld(handler, horizontal.direction)) {
        restoreTicks("x", horizontal);
      } else {
        handler._triggerHorizontalMouseWheel(horizontal.direction, horizontal.magnitude);
      }
    }
  }

  window.addEventListener("wheel", onWheel, { capture: true, passive: false });
  console.log(TAG, "installed (pixels per tick " + PIXELS_PER_TICK +
    ", max magnitude " + MAX_MAGNITUDE + ")");
})();
