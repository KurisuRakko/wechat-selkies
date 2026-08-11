/* 详情页：单个联系人的雷达、趋势、构成、里程碑与异动。 */

(function (global) {
  "use strict";

  var I = global.Insights;

  // 后端 kind 取值 → 中文名，与 constants.MESSAGE_KINDS 一一对应。
  var KIND_LABELS = {
    text: "文字",
    image: "图片",
    voice: "语音",
    video: "视频",
    sticker: "表情",
    location: "位置",
    link: "链接",
    call: "通话",
    file: "文件",
    contact_card: "名片",
    system: "系统",
    recalled: "撤回",
    unknown: "其他"
  };

  var els = {
    name: document.getElementById("contact-name"),
    meta: document.getElementById("contact-meta"),
    back: document.getElementById("back-link"),
    content: document.getElementById("content")
  };

  els.back.href = I.linkTo("/");

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
    els.meta.innerHTML =
      '<span class="contact-meta">' +
      I.kindBadgeHtml(contact.relation_kind) +
      "<span>" +
      I.escapeHtml(metaText) +
      "</span>" +
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
    // 少于两个采样点的曲线没有形状，部署首日整卡不渲染
    // （粒度控件也随之不渲染——还没有曲线可细化）。
    var tempBody = history.length >= 2
      ? '<div class="chart" id="temp-chart"></div>' +
        tempPickerHtml(payload.history_sampling || {})
      : "";
    // 副标题优先显示相识日（first_message_at）；没有相识日的联系人退回曲线
    // 首点的日期。tempBody 为空时整卡不渲染，不必算副标题——history 也可能
    // 是空数组。
    var tempSubtitle = "";
    if (tempBody) {
      var firstMessageAt = (payload.milestones || {}).first_message_at;
      tempSubtitle = I.isNumber(firstMessageAt)
        ? "每周/每日采样 · 自相识 " + I.formatDate(firstMessageAt) + " 起"
        : "每周/每日采样 · 自 " + history[0].day + " 起";
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
      I.mountChart(document.getElementById("temp-chart"), tempOption(history));
    }
    if (contact.scored) {
      I.mountChart(
        document.getElementById("radar-chart"),
        radarOption(contact, payload.medians || {})
      );
    }
    if (monthly.length) {
      I.mountChart(document.getElementById("reply-chart"), replyOption(monthly));
      I.mountChart(document.getElementById("volume-chart"), volumeOption(monthly));
    }
    if (types.length) {
      I.mountChart(document.getElementById("types-chart"), typesOption(types));
    }
  }

  /* ------------------------------------------------------------------ *
   * 0. 关系温度：历史综合分折线（相识起每周回放 + 部署日起每日采样）
   * ------------------------------------------------------------------ */

  /** 日键（YYYY-MM-DD）→ 本地时区当天 0 点的毫秒时间戳。
   * 不直接把字符串丢给 ECharts：它按 UTC 解析 "YYYY-MM-DD"，
   * 负时区（如美东）会把点画到前一天。 */
  function dayKeyMs(day) {
    var parts = day.split("-");
    return new Date(+parts[0], +parts[1] - 1, +parts[2]).getTime();
  }

  /** time 轴横坐标（毫秒时间戳，个别版本传字符串）→ 完整日期。 */
  function formatAxisFull(value) {
    return I.isNumber(value) ? I.formatDate(value / 1000) : I.formatDate(value);
  }

  /** 同上的 MM-DD 版本，给轴标签用。 */
  function formatAxisDay(value) {
    var full = formatAxisFull(value);
    return full === I.DASH ? "" : full.slice(5);
  }

  /**
   * 关系温度：历史折线。x 轴用 time 而不是 category：全史回放后是
   * 「每周一点 + 部署日起每日一点」的混合间距，category 轴会平均铺开、
   * 稀疏的周点失真；time 轴按真实时间落点。
   */
  function tempOption(history) {
    var points = history.map(function (row) {
      return [dayKeyMs(row.day), row.overall];
    });

    return function (theme) {
      return {
        backgroundColor: "transparent",
        // bottom 从 40 加大：要给 x 轴标签和滑块各留一段，两者不重叠。
        grid: { left: 48, right: 24, top: 24, bottom: 56 },
        // 只用 slider，故意不加 inside：inside 型 dataZoom 的 RoamController
        // 无论配置如何都会先吞掉滚轮与触屏单指事件（先 preventDefault+
        // stopPropagation 再按配置决定缩放），320px 高的图会把详情页这一段
        // 的滚动吃掉，比开着更糟；slider 拖拽没有这个问题。
        dataZoom: [
          {
            type: "slider",
            start: 0,
            end: 100,
            // Material 的克制尺寸，避免整条图表被滑块占掉太多高度。
            height: 20,
            bottom: 8,
            // 悬浮不弹大号数值气泡，太吵。
            showDetail: false,
            borderColor: theme.splitColor,
            // 主题变量是 6 位 hex，拼 8 位 alpha 后缀做主色 25% 透明填充。
            fillerColor: theme.primary + "40",
            handleStyle: { color: theme.primary },
            textStyle: { color: theme.subTextColor, fontSize: 12 }
          }
        ],
        tooltip: Object.assign(
          {
            trigger: "axis",
            formatter: function (params) {
              var lines = [I.escapeHtml(formatAxisFull(params[0].axisValue))];
              params.forEach(function (p) {
                // time 轴的数据是 [时刻, 分数] 对，取值取下标 1。
                var v = Array.isArray(p.value) ? p.value[1] : p.value;
                var value = I.isNumber(v)
                  ? I.formatNumber(v, 1) + " 分"
                  : "无数据";
                lines.push(p.marker + I.escapeHtml(p.seriesName) + "：" + value);
              });
              return lines.join("<br/>");
            }
          },
          I.tooltipStyle(theme)
        ),
        xAxis: {
          type: "time",
          boundaryGap: false,
          axisLine: { lineStyle: { color: theme.axisColor } },
          axisTick: { show: false },
          // 只露 MM-DD；刻度间距由 time 轴按真实时间取，跨年自动隔开。
          axisLabel: {
            color: theme.subTextColor,
            fontSize: 12,
            formatter: function (value) {
              return formatAxisDay(value);
            }
          }
        },
        yAxis: {
          type: "value",
          min: 0,
          max: 100,
          axisLine: { show: false },
          splitLine: { lineStyle: { color: theme.splitColor } },
          axisLabel: { color: theme.subTextColor, fontSize: 12 }
        },
        series: [
          {
            name: "综合分",
            type: "line",
            data: points,
            smooth: true,
            // 两年历史也不过百来点，仍偏密：不画符号，靠 hover 看值。
            showSymbol: false,
            lineStyle: { width: 2, color: theme.primary },
            itemStyle: { color: theme.primary }
          }
        ]
      };
    };
  }

  /* ------------------------------------------------------------------ *
   * 1. 七维大雷达（联系人 + 全联系人中位数参考层）
   * ------------------------------------------------------------------ */

  function dimensionValues(source) {
    return I.DIMENSIONS.map(function (dim) {
      var v = source ? source[dim[0]] : null;
      return I.isNumber(v) ? v : 0;
    });
  }

  function radarOption(contact, medians) {
    var selfName = contact.display_name || "本人视角";
    var refName = "全联系人中位数";
    return function (theme) {
      return {
        backgroundColor: "transparent",
        tooltip: Object.assign({ trigger: "item" }, I.tooltipStyle(theme)),
        legend: {
          bottom: 0,
          itemGap: 24,
          textStyle: { color: theme.subTextColor, fontSize: 12 },
          data: [selfName, refName]
        },
        radar: {
          center: ["50%", "46%"],
          radius: "64%",
          splitNumber: 4,
          axisName: { color: theme.subTextColor, fontSize: 12 },
          axisLine: { lineStyle: { color: theme.splitColor } },
          splitLine: { lineStyle: { color: theme.splitColor } },
          splitArea: { show: false },
          indicator: I.DIMENSIONS.map(function (dim) {
            return { name: dim[1], min: 0, max: 100 };
          })
        },
        series: [
          {
            type: "radar",
            symbolSize: 4,
            data: [
              {
                name: selfName,
                value: dimensionValues(contact.dimensions),
                lineStyle: { color: theme.primary, width: 2 },
                itemStyle: { color: theme.primary },
                areaStyle: { color: theme.primary, opacity: 0.25 }
              },
              {
                name: refName,
                value: dimensionValues(medians),
                lineStyle: { color: theme.referenceColor, width: 1, type: "dashed" },
                itemStyle: { color: theme.referenceColor },
                areaStyle: { color: theme.referenceColor, opacity: 0.12 }
              }
            ]
          }
        ]
      };
    };
  }

  /* ------------------------------------------------------------------ *
   * 2. 回复延迟中位数按月趋势（单位：秒）
   * ------------------------------------------------------------------ */

  function replyOption(monthly) {
    var months = monthly.map(function (row) {
      return row.month;
    });
    // null 保持原样，配合 connectNulls:false 让折线在缺样本的月份断开。
    var them = monthly.map(function (row) {
      return I.isNumber(row.reply_median_them) ? row.reply_median_them : null;
    });
    var me = monthly.map(function (row) {
      return I.isNumber(row.reply_median_me) ? row.reply_median_me : null;
    });

    return function (theme) {
      return {
        backgroundColor: "transparent",
        grid: { left: 64, right: 24, top: 40, bottom: 40 },
        tooltip: Object.assign(
          {
            trigger: "axis",
            formatter: function (params) {
              var lines = [I.escapeHtml(params[0].axisValue)];
              params.forEach(function (p) {
                var value = I.isNumber(p.value) ? I.formatDuration(p.value) : "无数据";
                lines.push(p.marker + I.escapeHtml(p.seriesName) + "：" + value);
              });
              return lines.join("<br/>");
            }
          },
          I.tooltipStyle(theme)
        ),
        legend: {
          top: 0,
          textStyle: { color: theme.subTextColor, fontSize: 12 }
        },
        xAxis: {
          type: "category",
          data: months,
          boundaryGap: false,
          axisLine: { lineStyle: { color: theme.axisColor } },
          axisTick: { show: false },
          axisLabel: { color: theme.subTextColor, fontSize: 12 }
        },
        yAxis: {
          type: "value",
          axisLine: { show: false },
          splitLine: { lineStyle: { color: theme.splitColor } },
          axisLabel: {
            color: theme.subTextColor,
            fontSize: 12,
            formatter: function (value) {
              return I.formatDuration(value);
            }
          }
        },
        series: [
          {
            name: "TA 回复我",
            type: "line",
            data: them,
            connectNulls: false,
            smooth: false,
            symbolSize: 6,
            lineStyle: { width: 2, color: theme.secondary },
            itemStyle: { color: theme.secondary }
          },
          {
            name: "我回复 TA",
            type: "line",
            data: me,
            connectNulls: false,
            smooth: false,
            symbolSize: 6,
            lineStyle: { width: 2, color: theme.primary },
            itemStyle: { color: theme.primary }
          }
        ]
      };
    };
  }

  /* ------------------------------------------------------------------ *
   * 3. 月度消息量堆叠柱状图
   * ------------------------------------------------------------------ */

  function volumeOption(monthly) {
    var months = monthly.map(function (row) {
      return row.month;
    });
    var incoming = monthly.map(function (row) {
      return I.isNumber(row["in"]) ? row["in"] : 0;
    });
    var outgoing = monthly.map(function (row) {
      return I.isNumber(row.out) ? row.out : 0;
    });

    return function (theme) {
      return {
        backgroundColor: "transparent",
        grid: { left: 56, right: 24, top: 40, bottom: 40 },
        tooltip: Object.assign(
          { trigger: "axis", axisPointer: { type: "shadow" } },
          I.tooltipStyle(theme)
        ),
        legend: { top: 0, textStyle: { color: theme.subTextColor, fontSize: 12 } },
        xAxis: {
          type: "category",
          data: months,
          axisLine: { lineStyle: { color: theme.axisColor } },
          axisTick: { show: false },
          axisLabel: { color: theme.subTextColor, fontSize: 12 }
        },
        yAxis: {
          type: "value",
          axisLine: { show: false },
          splitLine: { lineStyle: { color: theme.splitColor } },
          axisLabel: { color: theme.subTextColor, fontSize: 12 }
        },
        series: [
          {
            name: "TA 发的",
            type: "bar",
            stack: "total",
            barMaxWidth: 32,
            data: incoming,
            itemStyle: { color: theme.secondary }
          },
          {
            name: "我发的",
            type: "bar",
            stack: "total",
            barMaxWidth: 32,
            data: outgoing,
            itemStyle: { color: theme.primary }
          }
        ]
      };
    };
  }

  /* ------------------------------------------------------------------ *
   * 4. 消息类型构成环形图
   * ------------------------------------------------------------------ */

  function typesOption(types) {
    var data = types
      .filter(function (row) {
        return I.isNumber(row.count) && row.count > 0;
      })
      .map(function (row) {
        return {
          name: KIND_LABELS[row.kind] || KIND_LABELS.unknown,
          value: row.count
        };
      });

    return function (theme) {
      return {
        backgroundColor: "transparent",
        color: theme.color,
        tooltip: Object.assign(
          { trigger: "item", formatter: "{b}：{c} 条（{d}%）" },
          I.tooltipStyle(theme)
        ),
        legend: {
          bottom: 0,
          textStyle: { color: theme.subTextColor, fontSize: 12 }
        },
        series: [
          {
            type: "pie",
            radius: ["50%", "75%"],
            center: ["50%", "44%"],
            avoidLabelOverlap: true,
            itemStyle: { borderWidth: 2, borderColor: I.cssVar("--md-surface-1") },
            label: {
              color: theme.subTextColor,
              fontSize: 12,
              formatter: "{b} {d}%"
            },
            labelLine: { lineStyle: { color: theme.splitColor } },
            data: data
          }
        ]
      };
    };
  }

  /* ------------------------------------------------------------------ *
   * 5. 里程碑
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
   * 6. 近期异动
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
