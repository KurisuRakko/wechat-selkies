/* 两个页面共用的工具集，全部挂在全局 window 上（不使用模块打包）。 */

(function (global) {
  "use strict";

  /* ------------------------------------------------------------------ *
   * 常量
   * ------------------------------------------------------------------ */

  // 五维的固定顺序与中文名，雷达图、表格、排序都以此为准。
  var DIMENSIONS = [
    ["responsiveness", "响应"],
    ["initiative", "主动"],
    ["investment", "投入"],
    ["rhythm", "节奏"],
    ["depth", "深度"]
  ];

  // Material Design 2 的 500 色板，用于生成首字母头像底色。
  var AVATAR_COLORS = [
    "#F44336", // red
    "#E91E63", // pink
    "#9C27B0", // purple
    "#3F51B5", // indigo
    "#2196F3", // blue
    "#009688", // teal
    "#4CAF50", // green
    "#FF9800", // orange
    "#795548", // brown
    "#607D8B" // blue grey
  ];

  /* ------------------------------------------------------------------ *
   * 网络请求
   * ------------------------------------------------------------------ */

  // 首次访问可能带 ?token=xxx；服务端校验通过后会写 cookie，但在同一次会话里
  // 继续透传更稳妥（用户可能在 cookie 被拒的环境下使用）。
  var TOKEN = new URLSearchParams(global.location.search).get("token");

  /** 给请求路径或页面链接补上 token 查询参数（若当前页面带了 token）。 */
  function withToken(path) {
    if (!TOKEN) {
      return path;
    }
    var sep = path.indexOf("?") === -1 ? "?" : "&";
    return path + sep + "token=" + encodeURIComponent(TOKEN);
  }

  /** 构造带 status / code 的错误对象，页面据此区分展示哪种空状态。 */
  function makeError(message, status, code) {
    var err = new Error(message);
    err.status = status;
    if (code) {
      err.code = code;
    }
    return err;
  }

  /** 发请求并解析 JSON；401 直接抛错（页面不应重试）。 */
  async function request(path, init) {
    var res;
    try {
      res = await fetch(withToken(path), Object.assign({ credentials: "same-origin" }, init));
    } catch (e) {
      throw makeError("无法连接到服务", 0, "NETWORK_ERROR");
    }
    if (res.status === 401) {
      throw makeError("需要访问令牌", 401, "UNAUTHORIZED");
    }
    var data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = null;
    }
    if (data === null) {
      throw makeError("服务返回了无法解析的内容", res.status, "BAD_RESPONSE");
    }
    return { status: res.status, data: data };
  }

  /**
   * GET 请求：成功返回解析后的 JSON；HTTP 失败或 ok:false 时抛出带
   * status / code / message 的错误。
   */
  async function api(path) {
    var result = await request(path, { method: "GET" });
    var data = result.data;
    if (!data.ok) {
      var info = data.error || {};
      throw makeError(info.message || "请求失败", result.status, info.code);
    }
    return data;
  }

  /**
   * POST 请求：返回 { status, data }。
   * 刷新接口需要区分 202（已启动）与 409（正在运行），所以这里不吞掉状态码。
   */
  async function apiPost(path) {
    return request(path, { method: "POST" });
  }

  /* ------------------------------------------------------------------ *
   * 格式化
   * ------------------------------------------------------------------ */

  var DASH = "—";

  function isNumber(value) {
    return typeof value === "number" && isFinite(value);
  }

  /** 秒 → 「35 秒」「12 分钟」「3.2 小时」「4.1 天」。 */
  function formatDuration(seconds) {
    if (!isNumber(seconds)) {
      return DASH;
    }
    var s = Math.abs(seconds);
    if (s < 60) {
      return Math.round(s) + " 秒";
    }
    if (s < 3600) {
      return Math.round(s / 60) + " 分钟";
    }
    if (s < 86400) {
      return (s / 3600).toFixed(1) + " 小时";
    }
    return (s / 86400).toFixed(1) + " 天";
  }

  /** 把 Unix 秒或 ISO 字符串转成本地时区的 Date；无法解析时返回 null。 */
  function toDate(value) {
    if (isNumber(value)) {
      return new Date(value * 1000);
    }
    if (typeof value === "string" && value) {
      var d = new Date(value);
      return isNaN(d.getTime()) ? null : d;
    }
    return null;
  }

  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  /** → 本地时区的 YYYY-MM-DD HH:mm。 */
  function formatDateTime(value) {
    var d = toDate(value);
    if (!d) {
      return DASH;
    }
    return (
      formatDate(value) + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes())
    );
  }

  /** → 本地时区的 YYYY-MM-DD。 */
  function formatDate(value) {
    var d = toDate(value);
    if (!d) {
      return DASH;
    }
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }

  /** 保留固定小数位；缺值返回破折号。 */
  function formatNumber(value, digits) {
    return isNumber(value) ? value.toFixed(digits || 0) : DASH;
  }

  /** 千分位整数。 */
  function formatCount(value) {
    return isNumber(value) ? Math.round(value).toLocaleString("zh-CN") : DASH;
  }

  /** 名字 → 稳定的头像底色。 */
  function avatarColor(name) {
    var text = String(name || "");
    var hash = 0;
    for (var i = 0; i < text.length; i += 1) {
      // 经典 djb2 变体，只要稳定即可，不需要抗碰撞。
      hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
    }
    return AVATAR_COLORS[hash % AVATAR_COLORS.length];
  }

  /** 取显示名的首字符作为头像文字（兼容 emoji 等代理对）。 */
  function initial(name) {
    var text = String(name || "").trim();
    if (!text) {
      return "?";
    }
    return Array.from(text)[0];
  }

  /** 插入 HTML 前做转义，联系人名字来自用户数据。 */
  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (ch) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[ch];
    });
  }

  /* ------------------------------------------------------------------ *
   * 状态占位（加载 / 空 / 错误）
   * ------------------------------------------------------------------ */

  /** 生成一段状态占位 HTML：loading 显示转圈，其余显示标题与说明。 */
  function stateHtml(kind, title, desc) {
    var head =
      kind === "loading" ? '<div class="spinner" role="progressbar"></div>' : "";
    var body = '<div class="state__title">' + escapeHtml(title) + "</div>";
    if (desc) {
      body += '<div class="state__desc">' + escapeHtml(desc) + "</div>";
    }
    return '<div class="state">' + head + body + "</div>";
  }

  /** 把错误对象翻译成用户能看懂的状态占位。 */
  function errorStateHtml(err) {
    if (err && err.status === 401) {
      return stateHtml(
        "empty",
        "需要访问令牌",
        "请在地址栏加上 ?token=你的令牌 后重新打开本页。"
      );
    }
    return stateHtml("error", "加载失败", (err && err.message) || "未知错误");
  }

  /* ------------------------------------------------------------------ *
   * Snackbar
   * ------------------------------------------------------------------ */

  var snackbarEl = null;
  var snackbarTimer = 0;

  /** 底部提示条，3 秒后自动消失；重复调用会重置计时。 */
  function snackbar(text) {
    if (!snackbarEl) {
      snackbarEl = document.createElement("div");
      snackbarEl.className = "snackbar";
      snackbarEl.setAttribute("role", "status");
      document.body.appendChild(snackbarEl);
    }
    snackbarEl.textContent = text;
    // 强制一次重排，保证连续调用时过渡动画能重新播放。
    void snackbarEl.offsetWidth;
    snackbarEl.classList.add("snackbar--open");
    global.clearTimeout(snackbarTimer);
    snackbarTimer = global.setTimeout(function () {
      snackbarEl.classList.remove("snackbar--open");
    }, 3000);
  }

  /* ------------------------------------------------------------------ *
   * 主题与图表
   * ------------------------------------------------------------------ */

  var darkQuery = global.matchMedia("(prefers-color-scheme: dark)");

  /** 读取 :root 上的 CSS 自定义属性，让 JS 里的图表配色跟随主题。 */
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /**
   * 返回一份可以直接展开进 ECharts option 的主题片段。
   * 额外的 axisColor / splitColor / subTextColor 字段是给调用方取色用的，
   * ECharts 会忽略这些未知的顶层键。
   */
  function chartTheme() {
    var dark = darkQuery.matches;
    return {
      dark: dark,
      backgroundColor: "transparent",
      textStyle: {
        color: cssVar("--md-on-surface"),
        fontFamily: cssVar("--md-font")
      },
      color: [
        cssVar("--md-primary"),
        cssVar("--md-secondary"),
        "#FF9800",
        "#9C27B0",
        "#00BCD4",
        "#795548",
        "#607D8B"
      ],
      primary: cssVar("--md-primary"),
      secondary: cssVar("--md-secondary"),
      axisColor: cssVar("--md-chart-axis"),
      splitColor: cssVar("--md-chart-split"),
      referenceColor: cssVar("--md-chart-reference"),
      subTextColor: cssVar("--md-on-surface-medium"),
      tooltipBg: dark ? "#2E2E2E" : "#FFFFFF"
    };
  }

  /** 常用的 tooltip 外观，跟随主题。 */
  function tooltipStyle(theme) {
    return {
      backgroundColor: theme.tooltipBg,
      borderColor: theme.splitColor,
      textStyle: { color: theme.textStyle.color, fontSize: 12 },
      extraCssText: "box-shadow:0 2px 6px rgba(0,0,0,.2);"
    };
  }

  // 已挂载的图表：{ instance, rebuild }
  var charts = [];

  /** 登记图表实例与它的重建函数，供主题切换与窗口缩放统一调度。 */
  function registerChart(instance, rebuildFn) {
    charts.push({ instance: instance, rebuild: rebuildFn });
  }

  /**
   * 在容器里挂一个图表。buildOption(theme) 返回完整的 ECharts option。
   * 重复调用会先销毁旧实例，因此可以直接用作主题切换时的重建函数。
   */
  function mountChart(el, buildOption) {
    var existing = global.echarts.getInstanceByDom(el);
    if (existing) {
      existing.dispose();
    }
    var instance = global.echarts.init(el);
    instance.setOption(buildOption(chartTheme()));
    registerChart(instance, function () {
      mountChart(el, buildOption);
    });
    return instance;
  }

  /** 销毁并清空所有已登记的图表，页面重绘前调用，避免实例随 DOM 一起泄漏。 */
  function disposeCharts() {
    charts.splice(0, charts.length).forEach(function (entry) {
      if (!entry.instance.isDisposed()) {
        entry.instance.dispose();
      }
    });
  }

  // 主题切换：销毁全部实例后按登记的重建函数重画（重建时会重新登记）。
  function handleThemeChange() {
    var pending = charts.splice(0, charts.length);
    pending.forEach(function (entry) {
      if (!entry.instance.isDisposed()) {
        entry.instance.dispose();
      }
      entry.rebuild();
    });
  }

  if (typeof darkQuery.addEventListener === "function") {
    darkQuery.addEventListener("change", handleThemeChange);
  } else if (typeof darkQuery.addListener === "function") {
    // Safari 13 及更早只有旧式接口。
    darkQuery.addListener(handleThemeChange);
  }

  // 窗口缩放：合并到下一帧统一 resize，避免拖动时反复重排。
  var resizeScheduled = false;
  global.addEventListener("resize", function () {
    if (resizeScheduled) {
      return;
    }
    resizeScheduled = true;
    global.requestAnimationFrame(function () {
      resizeScheduled = false;
      charts.forEach(function (entry) {
        if (!entry.instance.isDisposed()) {
          entry.instance.resize();
        }
      });
    });
  });

  /* ------------------------------------------------------------------ *
   * 导出
   * ------------------------------------------------------------------ */

  global.Insights = {
    DIMENSIONS: DIMENSIONS,
    DASH: DASH,
    api: api,
    apiPost: apiPost,
    linkTo: withToken,
    formatDuration: formatDuration,
    formatDateTime: formatDateTime,
    formatDate: formatDate,
    formatNumber: formatNumber,
    formatCount: formatCount,
    isNumber: isNumber,
    avatarColor: avatarColor,
    initial: initial,
    escapeHtml: escapeHtml,
    stateHtml: stateHtml,
    errorStateHtml: errorStateHtml,
    snackbar: snackbar,
    cssVar: cssVar,
    chartTheme: chartTheme,
    tooltipStyle: tooltipStyle,
    registerChart: registerChart,
    mountChart: mountChart,
    disposeCharts: disposeCharts
  };
})(window);
