/* 详情页：单个联系人的雷达、趋势、构成、里程碑与异动。 */

(function (global) {
  "use strict";

  var I = global.Insights;
  // 图表配置构建器按职责拆在 detail-charts.js（先于本文件加载）。
  var charts = global.InsightsDetailCharts;

  var els = {
    name: document.getElementById("contact-name"),
    meta: document.getElementById("contact-meta"),
    back: document.getElementById("back-link"),
    bar: document.querySelector(".app-bar"),
    content: document.getElementById("content")
  };

  els.back.href = I.linkTo("/");

  /* ------------------------------------------------------------------ *
   * 好感度校准：页头右键弹出标记菜单
   * ------------------------------------------------------------------ */

  // 当前联系人的 payload：右键菜单、标记动作都从它取 hash 与校准状态。
  var current = null;

  function markFeedback(action) {
    I.apiPost(
      "/api/contact/" + encodeURIComponent(current.hash) + "/feedback",
      { action: action }
    )
      .then(function (result) {
        if (action === "clear") {
          // 清除是即时的：服务端已按 payload 里的 base 快照还原分数，
          // 重拉详情页让页头角标与七维立刻回到客观口径。
          load();
          I.snackbar("已清除校准，分数还原为客观口径");
          return;
        }
        var pending = result.data && result.data.pending;
        if (pending) {
          // 服务端已把 calibration_pending 写进 payload，重拉详情页让
          // 「校准排队中」提示与清除项立刻可见。
          load();
          I.snackbar(
            action === "up"
              ? "已标记「感觉偏低」：下一轮分析后分数上调"
              : "已标记「感觉偏高」：下一轮分析后分数下调"
          );
        }
      })
      .catch(function (err) {
        I.snackbar((err && err.message) || "标记失败");
      });
  }

  function postBreakup(action, date, certainty) {
    var body = { action: action, date: date, certainty: certainty };
    if (action !== "mark") {
      body = { action: action };
    }
    I.apiPost("/api/contact/" + encodeURIComponent(current.hash) + "/breakup", body)
      .then(function (result) {
        if (action === "clear") {
          // 清除是即时的：服务端已按 base 快照还原分数，重拉详情页。
          load();
          I.snackbar("已清除");
          return;
        }
        if (result.data && result.data.pending) {
          load();
          I.snackbar("已标记，下一轮分析核实");
        }
      })
      .catch(function (err) {
        I.snackbar((err && err.message) || "标记失败");
      });
  }

  function feedbackItems() {
    var items = [
      {
        label: "标记：感觉偏低",
        hint: "校准后分数上调",
        onClick: function () {
          markFeedback("up");
        }
      },
      {
        label: "标记：感觉偏高",
        hint: "校准后分数下调",
        onClick: function () {
          markFeedback("down");
        }
      }
    ];
    // 只有校准生效中或标记排队中才需要「清除」；干净的联系人没有可清的东西。
    if (current.calibration || current.calibration_pending) {
      items.push({
        label: "清除校准",
        hint: "还原客观分",
        onClick: function () {
          markFeedback("clear");
        }
      });
    }
    // 绝交标记：两种置信度都要先选日期，核实排队中或已有结论时提供清除。
    function pushBreakupItem(label, hint, certainty) {
      items.push({
        label: label,
        hint: hint,
        onClick: function () {
          I.openDateDialog("你们是什么时候绝交的？", function (date) {
            postBreakup("mark", date, certainty);
          });
        }
      });
    }
    pushBreakupItem("已经绝交…", "输入日期，下轮核实", "certain");
    pushBreakupItem("我认为的绝交…", "存疑标记，AI 复核", "suspected");
    if (current.breakup || current.breakup_pending) {
      items.push({ label: "清除绝交标记", onClick: function () { postBreakup("clear"); } });
    }
    return items;
  }

  // 页头右键 = 好感度标记菜单；数据还没加载出来时忽略。
  els.bar.addEventListener("contextmenu", function (event) {
    if (!current) {
      return;
    }
    event.preventDefault();
    I.openContextMenu(event, feedbackItems());
  });

  // 首屏入场动画只播一次：改判/换粒度后的整页重载会重置，但同一载入
  // 周期内的重绘不重播。
  var entered = false;

  /* ------------------------------------------------------------------ *
   * 入口
   * ------------------------------------------------------------------ */

  /** 路由是 /contact/<hash>，取路径最后一段。 */
  function readHash() {
    var parts = global.location.pathname.split("/").filter(Boolean);
    if (!parts.length) {
      return "";
    }
    try {
      return decodeURIComponent(parts[parts.length - 1]);
    } catch (e) {
      // 畸形的百分号转义会让 decodeURIComponent 抛 URIError，
      // 按「没有联系人标识」处理，走下面的错误占位而不是卡在加载态。
      return "";
    }
  }

  async function load() {
    var hash = readHash();
    if (!hash) {
      els.content.innerHTML = I.stateHtml("error", "缺少联系人标识", "请从列表页进入。");
      return;
    }
    els.content.innerHTML = I.stateHtml("loading", "正在加载…");
    var payload;
    try {
      payload = await I.api("/api/contact/" + encodeURIComponent(hash));
    } catch (err) {
      if (err.code === "CONTACT_NOT_FOUND") {
        els.content.innerHTML = I.stateHtml(
          "empty",
          "找不到这个联系人",
          "他可能已经在最近一轮分析中被移除。"
        );
        return;
      }
      els.content.innerHTML = I.errorStateHtml(err);
      return;
    }
    render(payload);
  }

  /* ------------------------------------------------------------------ *
   * 骨架渲染
   * ------------------------------------------------------------------ */

  function card(title, bodyHtml, subtitle) {
    return (
      '<section class="card">' +
      '<h2 class="card__title">' +
      I.escapeHtml(title) +
      "</h2>" +
      (subtitle ? '<p class="card__subtitle">' + I.escapeHtml(subtitle) + "</p>" : "") +
      bodyHtml +
      "</section>"
    );
  }

  function emptyCardBody(text) {
    return '<p class="md-caption">' + I.escapeHtml(text) + "</p>";
  }

  /** → 本地时区的 MM-DD；复用 formatDate 的解析与缺值处理。 */
  function formatMonthDay(value) {
    var full = I.formatDate(value);
    return full === I.DASH ? I.DASH : full.slice(5);
  }

  /** 话题标签 chips（大模型输出，逐个 escapeHtml）；无标签返回空串。 */
  function tagChipsHtml(tags) {
    if (!tags || !tags.length) {
      return "";
    }
    return (
      '<div class="tag-chips">' +
      tags
        .map(function (tag) {
          return '<span class="chip">' + I.escapeHtml(tag) + "</span>";
        })
        .join("") +
      "</div>"
    );
  }

  /** 「关系画像」卡：summary 下方挂话题标签；只有其一存在也照常渲染。 */
  function portraitCard(contact) {
    var tags = contact.llm_tags || [];
    if (!contact.llm_summary && !tags.length) {
      return "";
    }
    var body = "";
    if (contact.llm_summary) {
      body += '<p class="md-body2">' + I.escapeHtml(contact.llm_summary) + "</p>";
    }
    body += tagChipsHtml(tags);
    var subtitle = contact.llm_summary_at
      ? "大模型基于最近的脱敏对话生成 · " + formatMonthDay(contact.llm_summary_at)
      : "大模型基于最近的脱敏对话生成";
    return card("关系画像", body, subtitle);
  }

  /** 「关系类型」小卡：手动改判控件，改动立即 POST 并重载页面。 */
  function kindCard(contact) {
    var current = contact.relation_kind || "friend";
    var source = contact.kind_source || "default";
    var currentLabel = I.KIND_RELATION_LABELS[current] || "朋友";
    // 手动改判过的联系人不显示自动判定结果（前端看不到底下的 kind_auto）。
    var autoLabel =
      source === "manual" ? "自动判定" : "自动判定（" + currentLabel + "）";
    var options = [
      { value: "auto", label: autoLabel },
      { value: "friend", label: "朋友" },
      { value: "family", label: "家人" },
      { value: "transactional", label: "事务往来" }
    ];
    // 手动改判过的选中对应类型，其余选中「自动判定」。
    var selected = source === "manual" ? current : "auto";
    return (
      card(
        "关系类型",
        '<span class="select">' +
          '<select class="kind-picker__select" aria-label="关系类型">' +
          options
            .map(function (option) {
              return (
                '<option value="' +
                option.value +
                '"' +
                (option.value === selected ? " selected" : "") +
                ">" +
                I.escapeHtml(option.label) +
                "</option>"
              );
            })
            .join("") +
          "</select>" +
          "</span>" +
          '<p class="md-caption">手动改判立即生效，下一轮分析会按新类型全面重算：' +
          "事务往来不参与打分，家人不会被判淡出。</p>",
        "影响打分与年报"
      )
    );
  }

  /**
   * 「关系温度」卡内的采样粒度选择器：select + 说明文字，DOM 结构与
   * kindCard 的改判控件一致。说明文案按当前粒度与细化进度区分，
   * 动态内容（日期等）一律 escapeHtml。
   */
  function tempPickerHtml(sampling) {
    var current = sampling.granularity === "day" ? "day" : "week";
    var caption;
    if (current === "day") {
      // pending 只表示「还有历史没细化完」——网格停在昨天，跨过零点后到
      // 当晚分析之前必然为真，并不代表此刻在跑（细化只在分析轮里执行）。
      // 三种状态：待开始 / 已推进到某天 / 已到最近。
      if (sampling.pending) {
        caption = sampling.daily_until
          ? "已逐日细化到 " + I.escapeHtml(sampling.daily_until) +
            "，其余的下一轮分析继续补。"
          : "下一轮分析开始从相识那天起逐日细化，全史可能要一两个小时，完成前曲线保持现状。";
      } else {
        caption = "已逐日细化到最近。切回每周不会删掉已经算出来的细节。";
      }
    } else {
      caption =
        "切到每日会把这位联系人从相识那天起逐日重算，采样点上千、耗时可能" +
        "一两个小时，下一轮分析开始，完成前曲线保持现状。";
    }
    return (
      '<span class="select">' +
      '<select class="history-picker__select" aria-label="采样粒度">' +
      '<option value="week"' +
      (current === "week" ? " selected" : "") +
      ">每周采样</option>" +
      '<option value="day"' +
      (current === "day" ? " selected" : "") +
      ">每日采样</option>" +
      "</select>" +
      "</span>" +
      '<p class="md-caption">' +
      caption +
      "</p>"
    );
  }

  /** 手动改判关系类型：POST 后重载页面（服务端已改写该联系人的 payload）。 */
  async function changeKind(kind) {
    var result;
    try {
      result = await I.apiPost(
        "/api/contact/" + encodeURIComponent(readHash()) + "/kind",
        { kind: kind }
      );
    } catch (err) {
      I.snackbar(err.message || "改判失败");
      return;
    }
    var data = result.data || {};
    if (result.status !== 200 || !data.ok) {
      var info = data.error || {};
      I.snackbar(info.message || "改判失败");
      return;
    }
    I.snackbar("已更新关系类型");
    global.location.reload();
  }

  /** 切换采样粒度：POST 后重载页面（服务端按粒度重算历史）。 */
  async function changeGranularity(granularity) {
    var result;
    try {
      result = await I.apiPost(
        "/api/contact/" + encodeURIComponent(readHash()) + "/history",
        { granularity: granularity }
      );
    } catch (err) {
      I.snackbar(err.message || "切换失败");
      return;
    }
    var data = result.data || {};
    if (result.status !== 200 || !data.ok) {
      var info = data.error || {};
      I.snackbar(info.message || "切换失败");
      return;
    }
    I.snackbar(
      granularity === "day"
        ? "已切到每日采样，下一轮分析开始细化"
        : "已切回每周采样"
    );
    global.location.reload();
  }

  function render(payload) {
    var contact = payload.contact || {};
    current = contact;
    var monthly = payload.monthly || [];
    var types = payload.types || [];
    var history = payload.history || [];

    els.name.textContent = contact.display_name || "联系人详情";
    document.title = (contact.display_name || "联系人详情") + " · 关系洞察";
    // 页头副标题一行排：关系类型 badge + 状态文字（文本来自后端，逐个转义）。
    var metaText = contact.scored
      ? "综合分 " +
        I.formatNumber(contact.overall, 1) +
        " · 近 30 天 " +
        (contact.recent_messages || 0) +
        " 条"
      : contact.sample_note || "数据不足";
    var pendingText = "";
    if (contact.calibration_pending) {
      pendingText =
        contact.calibration_pending === "up"
          ? "校准排队中（偏低）"
          : "校准排队中（偏高）";
    }
    els.meta.innerHTML =
      '<span class="contact-meta">' +
      I.kindBadgeHtml(contact.relation_kind) +
      I.calibrationChipHtml(contact) +
      I.breakupChipHtml(contact) +
      "<span>" +
      I.escapeHtml(metaText) +
      "</span>" +
      (pendingText
        ? '<span class="md-caption">' + I.escapeHtml(pendingText) + "</span>"
        : "") +
      "</span>";

    var radarBody = contact.scored
      ? '<div class="chart chart--tall" id="radar-chart"></div>'
      : contact.zeroed
        ? '<div class="insufficient">' +
          '<div class="insufficient__title">已归零</div>' +
          '<div class="insufficient__desc">两年内没有往来，已归零</div>' +
          "</div>"
        : '<div class="insufficient">' +
          '<div class="insufficient__title">数据不足</div>' +
          '<div class="insufficient__desc">近两年往来消息不足，暂不打分</div>' +
          "</div>";

    var monthlyBody = monthly.length
      ? '<div class="chart" id="reply-chart"></div>'
      : emptyCardBody("暂无按月数据");
    var volumeBody = monthly.length
      ? '<div class="chart" id="volume-chart"></div>'
      : emptyCardBody("暂无按月数据");
    var typesBody = types.length
      ? '<div class="chart" id="types-chart"></div>'
      : emptyCardBody("暂无消息类型数据");
    // 少于两个采样点的曲线没有形状，部署首日整卡不渲染（粒度控件也随之
    // 不渲染——还没有曲线可细化）；确认绝交后截断导致不足两点时，卡保留、
    // 只显示一句说明，不静默消失。
    var cutoff = payload.history_cutoff || null;
    var tempBody = history.length >= 2
      ? '<div class="chart" id="temp-chart"></div>' +
        tempPickerHtml(payload.history_sampling || {})
      : cutoff
        ? emptyCardBody("绝交日在曲线起点之前，没有可显示的区间")
        : "";
    // 副标题优先显示相识日（first_message_at）；没有相识日的联系人退回曲线
    // 首点的日期。tempBody 为空时整卡不渲染，不必算副标题——history 也可能
    // 是空数组（截断后不足两点时没有首点可退）。
    var tempSubtitle = "";
    if (tempBody) {
      var firstMessageAt = (payload.milestones || {}).first_message_at;
      if (I.isNumber(firstMessageAt)) {
        tempSubtitle = "每周/每日采样 · 自相识 " + I.formatDate(firstMessageAt) + " 起";
      } else if (history.length) {
        tempSubtitle = "每周/每日采样 · 自 " + history[0].day + " 起";
      } else {
        tempSubtitle = "每周/每日采样";
      }
      if (cutoff) {
        // 绝交日之后的点照常计算但不下发，副标题注明曲线止于当天。
        tempSubtitle += " · 止于绝交 " + cutoff.day;
      }
    }

    // 卡片顺序：七维画像 → 关系温度 → 关系画像 → 回复延迟 → 月度消息量 →
    // 消息类型构成 → 里程碑 → 近期异动；「关系类型」是改判控件，放最后。
    els.content.innerHTML =
      '<div class="stack">' +
      card("七维画像", radarBody, contact.scored ? "与全联系人中位数对比" : "") +
      (tempBody
        ? card("关系温度", tempBody, tempSubtitle)
        : "") +
      // 画像与标签由大模型生成，文本不可信，必须整体 escapeHtml。
      portraitCard(contact) +
      card("回复延迟中位数", monthlyBody, "按月，越低越快") +
      card("月度消息量", volumeBody) +
      card("消息类型构成", typesBody) +
      card("里程碑", milestonesHtml(payload.milestones || {})) +
      card(
        "近期异动",
        anomaliesHtml(payload.anomalies || [], contact.anomaly_note)
      ) +
      kindCard(contact) +
      "</div>";

    // 首屏成功渲染时才加入场类；图表挂载前加好，让动画与图表首次绘制
    // 同步发生。
    if (!entered) {
      entered = true;
      var stackEl = els.content.querySelector(".stack");
      if (stackEl) {
        stackEl.classList.add("enter-stagger");
      }
    }

    var picker = els.content.querySelector(".kind-picker__select");
    if (picker) {
      picker.addEventListener("change", function () {
        changeKind(picker.value);
      });
    }

    var historyPicker = els.content.querySelector(".history-picker__select");
    if (historyPicker) {
      historyPicker.addEventListener("change", function () {
        changeGranularity(historyPicker.value);
      });
    }

    if (history.length >= 2) {
      I.mountChart(
        document.getElementById("temp-chart"),
        charts.tempOption(history, cutoff)
      );
    }
    if (contact.scored) {
      I.mountChart(
        document.getElementById("radar-chart"),
        charts.radarOption(contact, payload.medians || {})
      );
    }
    if (monthly.length) {
      I.mountChart(document.getElementById("reply-chart"), charts.replyOption(monthly));
      I.mountChart(document.getElementById("volume-chart"), charts.volumeOption(monthly));
    }
    if (types.length) {
      I.mountChart(document.getElementById("types-chart"), charts.typesOption(types));
    }
  }

  /* ------------------------------------------------------------------ *
   * 里程碑
   * ------------------------------------------------------------------ */

  function milestoneItem(label, value, note) {
    return (
      '<div class="milestone">' +
      '<div class="milestone__label">' +
      I.escapeHtml(label) +
      "</div>" +
      '<div class="milestone__value">' +
      I.escapeHtml(value) +
      "</div>" +
      (note ? '<div class="milestone__note">' + I.escapeHtml(note) + "</div>" : "") +
      "</div>"
    );
  }

  function milestonesHtml(m) {
    var items = [];

    items.push(milestoneItem("第一条消息", I.formatDate(m.first_message_at)));
    items.push(
      milestoneItem(
        "认识天数",
        I.isNumber(m.days_known) ? m.days_known + " 天" : I.DASH
      )
    );
    items.push(milestoneItem("累计消息数", I.formatCount(m.total_messages)));

    var silenceNote = I.isNumber(m.longest_silence_ended_at)
      ? "结束于 " + I.formatDate(m.longest_silence_ended_at)
      : "";
    items.push(
      milestoneItem(
        "最长连续沉默",
        I.formatDuration(m.longest_silence_seconds),
        silenceNote
      )
    );

    var nightNote = I.isNumber(m.latest_night_at)
      ? I.formatDate(m.latest_night_at)
      : "";
    items.push(
      milestoneItem("聊到最晚的一次", m.latest_night_clock || I.DASH, nightNote)
    );

    items.push(
      milestoneItem(
        "最长「哈」连击",
        I.isNumber(m.max_haha_run) && m.max_haha_run > 0
          ? m.max_haha_run + " 连"
          : I.DASH
      )
    );

    return '<div class="milestones">' + items.join("") + "</div>";
  }

  /* ------------------------------------------------------------------ *
   * 近期异动
   * ------------------------------------------------------------------ */

  function anomaliesHtml(list, note) {
    // 异动原因由大模型生成，文本不可信，必须 escapeHtml。
    var lead = note
      ? '<p class="md-caption">可能的原因：' + I.escapeHtml(note) + "</p>"
      : "";
    if (!list.length) {
      return lead + emptyCardBody("近期没有明显异动");
    }
    return (
      lead +
      list
        .map(function (item) {
        // 箭头本身表示「变成了」，好坏只靠颜色区分。
        var direction = item.direction === "better" ? "better" : "worse";
        return (
          '<div class="anomaly">' +
          '<span class="anomaly__label">' +
          I.escapeHtml(item.label) +
          "</span>" +
          '<span class="anomaly__change">' +
          '<span class="anomaly__before">' +
          I.escapeHtml(item.before) +
          "</span>" +
          '<span class="anomaly__arrow--' +
          direction +
          '" aria-hidden="true">→</span>' +
          "<span>" +
          I.escapeHtml(item.after) +
          "</span>" +
          "</span>" +
          "</div>"
        );
        })
        .join("")
    );
  }

  load();
})(window);
