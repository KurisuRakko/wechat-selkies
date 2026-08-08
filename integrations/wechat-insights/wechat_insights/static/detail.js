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

  function render(payload) {
    var contact = payload.contact || {};
    var monthly = payload.monthly || [];
    var types = payload.types || [];

    els.name.textContent = contact.display_name || "联系人详情";
    document.title = (contact.display_name || "联系人详情") + " · 关系洞察";
    els.meta.textContent = contact.scored
      ? "综合分 " +
        I.formatNumber(contact.overall, 1) +
        " · 近 30 天 " +
        (contact.recent_messages || 0) +
        " 条"
      : contact.sample_note || "数据不足";

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

    els.content.innerHTML =
      '<div class="stack">' +
      card("七维画像", radarBody, contact.scored ? "与全联系人中位数对比" : "") +
      card("回复延迟中位数", monthlyBody, "按月，越低越快") +
      card("月度消息量", volumeBody) +
      card("消息类型构成", typesBody) +
      card("里程碑", milestonesHtml(payload.milestones || {})) +
      card("近期异动", anomaliesHtml(payload.anomalies || [])) +
      "</div>";

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

  function anomaliesHtml(list) {
    if (!list.length) {
      return emptyCardBody("近期没有明显异动");
    }
    return list
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
      .join("");
  }

  load();
})(window);
