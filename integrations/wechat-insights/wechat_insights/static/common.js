/* 两个页面共用的工具集，全部挂在全局 window 上（不使用模块打包）。 */

(function (global) {
  "use strict";

  /* ------------------------------------------------------------------ *
   * 常量
   * ------------------------------------------------------------------ */

  // 七维的固定顺序与中文名，雷达图、表格、排序都以此为准。
  var DIMENSIONS = [
    ["responsiveness", "响应"],
    ["initiative", "主动"],
    ["investment", "投入"],
    ["rhythm", "节奏"],
    ["depth", "深度"],
    ["constancy", "恒常"],
    ["reciprocity", "对等"]
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

  // ?token= 首次访问时由服务端校验并写 cookie，然后 302 到不带参数的地址；
  // 之后的请求全部靠 cookie 带凭证，前端不需要再碰 token。

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
      res = await fetch(path, Object.assign({ credentials: "same-origin" }, init));
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
   * body 存在时以 JSON 发送。
   */
  async function apiPost(path, body) {
    var init = { method: "POST" };
    if (body !== undefined) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    return request(path, init);
  }

  /** 页面链接原样返回：鉴权靠 cookie，链接里不需要再拼任何参数。 */
  function linkTo(path) {
    return path;
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

  // 关系类型取值 → 中文名，与后端 classify 模块的 KIND_VALUES 一致。
  var KIND_RELATION_LABELS = {
    friend: "朋友",
    family: "家人",
    transactional: "事务往来"
  };

  /**
   * 关系类型 badge：家人用次色描边 chip、事务往来用禁用灰；friend 不显示。
   * kind 缺失或未知时按默认 friend 处理，旧数据无需任何迁移。
   */
  function kindBadgeHtml(kind) {
    if (kind === "family") {
      return '<span class="chip chip--small chip--family">家人</span>';
    }
    if (kind === "transactional") {
      return '<span class="chip chip--small chip--transactional">事务往来</span>';
    }
    return "";
  }

  /**
   * 好感度校准角标：校准生效时显示带符号的综合分偏移（+1.2 / -0.8），
   * title 悬浮显示 LLM 给的校准理由；没有校准时返回空串。展示用
   * overall_delta，正好说明「比客观分抬了多少/压了多少」。
   */
  function calibrationChipHtml(payload) {
    var calibration = payload && payload.calibration;
    if (!calibration || !isNumber(calibration.overall_delta)) {
      return "";
    }
    var delta = calibration.overall_delta;
    var text = (delta > 0 ? "+" : "") + delta.toFixed(1);
    var tip = "好感度校准 " + text;
    if (calibration.note) {
      tip += "：" + String(calibration.note);
    }
    return (
      '<span class="chip chip--small chip--calibration" title="' +
      escapeHtml(tip) +
      '">校准 ' +
      text +
      "</span>"
    );
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
   * 右键菜单（好感度校准标记）
   * ------------------------------------------------------------------ */

  var contextMenuEl = null;
  // 当前菜单的动作项：click 监听只在首次创建时注册一次，闭包不能捕获
  // 当次的 items，否则之后在别的卡片上打开菜单会错按第一张卡片的动作。
  var contextMenuItems = null;

  /** 收起右键菜单并清空内容；菜单外点击/滚动/Esc/失焦都会走到这里。 */
  function closeContextMenu() {
    if (!contextMenuEl) {
      return;
    }
    contextMenuEl.classList.remove("context-menu--open");
    contextMenuEl.innerHTML = "";
  }

  /**
   * 在鼠标位置弹出一个迷你菜单（右键标记「偏高/偏低/清除」用）。
   * items：[{ label, hint, onClick }]；hint 是项右边的次要说明文字。
   * 全局单例：开新菜单前自动关掉旧的。菜单项点击后先收起再执行动作，
   * 因此动作里可以放心触发 snackbar 等界面反馈。
   */
  function openContextMenu(event, items) {
    if (!items || !items.length) {
      return;
    }
    contextMenuItems = items;
    if (!contextMenuEl) {
      contextMenuEl = document.createElement("div");
      contextMenuEl.className = "context-menu";
      contextMenuEl.setAttribute("role", "menu");
      document.body.appendChild(contextMenuEl);
      // 菜单项的 click 会先于 document 的收起监听到达，动作照常执行。
      contextMenuEl.addEventListener("click", function (menuEvent) {
        var button = menuEvent.target.closest(".context-menu__item");
        if (!button) {
          return;
        }
        var index = Number(button.dataset.index);
        closeContextMenu();
        if (contextMenuItems && contextMenuItems[index]) {
          contextMenuItems[index].onClick();
        }
      });
      global.addEventListener("click", closeContextMenu);
      global.addEventListener("scroll", closeContextMenu, true);
      global.addEventListener("blur", closeContextMenu);
      global.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
          closeContextMenu();
        }
      });
    }
    contextMenuEl.innerHTML = items
      .map(function (item, index) {
        return (
          '<button type="button" class="context-menu__item" role="menuitem" ' +
          'data-index="' +
          index +
          '">' +
          '<span class="context-menu__label">' +
          escapeHtml(item.label) +
          "</span>" +
          (item.hint
            ? '<span class="context-menu__hint">' +
              escapeHtml(item.hint) +
              "</span>"
            : "") +
          "</button>"
        );
      })
      .join("");
    // 菜单在右下边界处翻转，避免弹出视口外；尺寸按 240px 宽、每项 44px 估。
    var width = Math.min(240, global.document.documentElement.clientWidth - 16);
    var x = Math.min(event.clientX, global.innerWidth - width - 8);
    var y = Math.min(event.clientY, global.innerHeight - items.length * 44 - 16);
    contextMenuEl.style.left = Math.max(8, x) + "px";
    contextMenuEl.style.top = Math.max(8, y) + "px";
    contextMenuEl.style.minWidth = width + "px";
    contextMenuEl.classList.add("context-menu--open");
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
   * 分数说明弹窗
   * ------------------------------------------------------------------ */

  // 文案与 scoring.py 的维度权重一一对应，改权重时记得同步这里。
  var SCORING_HELP = {
    title: "这些分数是怎么算的",
    intro:
      "所有分数都是「相对分」：每项指标先在你的全部联系人里取百分位（0–100），" +
      "再按权重合成维度分；综合分是七个维度的平均。也就是说 80 分的意思是" +
      "「比 80% 的联系人强」，不是绝对好坏。统计范围是近两年的聊天，并按时间" +
      "衰减加权——昨天权重最高，三个月前约剩一半，一年前约 6%；两年内完全没有" +
      "往来就归零。",
    dimensions: [
      {
        name: "响应",
        desc:
          "TA 回复你的速度。回复延迟的中位数（占 60%，越短越好）＋ 秒回率" +
          "（60 秒内回复的比例，占 40%）。"
      },
      {
        name: "主动",
        desc:
          "谁在主动维系这段关系。TA 主动发起对话的占比（40%）＋ TA 连发多条" +
          "「追加」的比例（30%）＋ 对话由 TA 说最后一句的占比（30%）。"
      },
      {
        name: "投入",
        desc:
          "TA 每天花在你身上的「成本」。语音通话是强信号（通话×20、语音×5、" +
          "视频×3、图片/文件×1.5），文字每 20 字算 1 个单位，折成日均成本" +
          "（35%）；再加通话次数日均（20%）、日均消息条数（15%）、日均字数" +
          "（15%）、表情包占比（15%）。"
      },
      {
        name: "节奏",
        desc:
          "你们聊天的形态。深夜聊天占比（23:00–02:00，30%）＋ 周末聊天占比" +
          "（20%）＋ 平均每段对话的轮次（30%）＋ 长对话占比（超过 20 轮，20%）。"
      },
      {
        name: "深度",
        desc:
          "聊天内容的分量。TA 消息的平均长度（40%）＋ 疑问句占比（30%）＋" +
          "长消息占比（超过 50 字，30%）。目前用文本特征做代理，不读取聊天" +
          "内容本身含义。"
      },
      {
        name: "恒常",
        desc:
          "联系是否细水长流。有往来的天数占比（40%）＋ 当前已经沉默了多少天" +
          "（35%，越久越差）＋ 两年内最长的一次断联（25%，越长越差）。"
      },
      {
        name: "对等",
        desc:
          "关系是不是双向的。双方消息量的均衡度（40%）＋ 字数均衡度（30%）＋" +
          "谁发起对话的均衡度（30%）；完全对等记满值，一边倒则趋近于零。"
      }
    ],
    footnote:
      "往来消息不足 50 条的联系人不打分；打分每天自动更新一次。" +
      "右键联系人卡片可以标记「感觉偏高/偏低」，下一轮分析会自动校准。"
  };

  var helpDialogEl = null;
  var helpTrigger = null;

  /** 关闭说明弹窗并把焦点还给打开它的按钮。 */
  function closeScoringHelp() {
    if (!helpDialogEl || !helpDialogEl.classList.contains("dialog-scrim--open")) {
      return;
    }
    helpDialogEl.classList.remove("dialog-scrim--open");
    if (helpTrigger) {
      helpTrigger.focus();
    }
  }

  /** 打开「分数怎么算」弹窗；内容只在第一次打开时构建。 */
  function openScoringHelp() {
    if (!helpDialogEl) {
      helpDialogEl = document.createElement("div");
      helpDialogEl.className = "dialog-scrim";
      helpDialogEl.innerHTML =
        '<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="scoring-help-title">' +
        '<div class="dialog__head">' +
        '<h2 class="dialog__title" id="scoring-help-title">' +
        escapeHtml(SCORING_HELP.title) +
        "</h2>" +
        '<button class="icon-btn dialog__close" type="button" aria-label="关闭">×</button>' +
        "</div>" +
        '<div class="dialog__body">' +
        '<p class="dialog__intro">' +
        escapeHtml(SCORING_HELP.intro) +
        "</p>" +
        '<div class="dialog__dims">' +
        SCORING_HELP.dimensions
          .map(function (dim) {
            return (
              '<div class="dialog-dim">' +
              '<div class="dialog-dim__name">' +
              escapeHtml(dim.name) +
              "</div>" +
              '<div class="dialog-dim__desc">' +
              escapeHtml(dim.desc) +
              "</div>" +
              "</div>"
            );
          })
          .join("") +
        "</div>" +
        '<p class="dialog__footnote">' +
        escapeHtml(SCORING_HELP.footnote) +
        "</p>" +
        "</div>" +
        "</div>";
      document.body.appendChild(helpDialogEl);
      // 点遮罩（scrim）本身也关闭；内容区点击不冒泡到关闭逻辑。
      helpDialogEl.addEventListener("click", function (event) {
        if (event.target === helpDialogEl) {
          closeScoringHelp();
        }
      });
      helpDialogEl
        .querySelector(".dialog__close")
        .addEventListener("click", closeScoringHelp);
    }
    helpDialogEl.classList.add("dialog-scrim--open");
    helpDialogEl.querySelector(".dialog__close").focus();
  }

  global.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && helpDialogEl && helpDialogEl.classList.contains("dialog-scrim--open")) {
      closeScoringHelp();
    }
  });

  // 两个页面都在页头放一个 id="help-btn" 的「?」图标按钮，这里统一接线。
  helpTrigger = document.getElementById("help-btn");
  if (helpTrigger) {
    helpTrigger.addEventListener("click", openScoringHelp);
  }

  /* ------------------------------------------------------------------ *
   * 导出
   * ------------------------------------------------------------------ */

  global.Insights = {
    DIMENSIONS: DIMENSIONS,
    DASH: DASH,
    KIND_RELATION_LABELS: KIND_RELATION_LABELS,
    api: api,
    apiPost: apiPost,
    kindBadgeHtml: kindBadgeHtml,
    calibrationChipHtml: calibrationChipHtml,
    openContextMenu: openContextMenu,
    linkTo: linkTo,
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
