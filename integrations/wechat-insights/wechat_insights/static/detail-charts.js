/* 详情页图表配置：按职责与页面骨架（detail.js）分开的独立脚本。

   detail.js 负责渲染骨架、卡片与交互，本文件只产出 ECharts 配置：
   关系温度折线、七维大雷达、回复延迟趋势、月度消息量、消息类型构成。
   构建器统一返回 function (theme)，由 I.mountChart 在挂载与主题切换时
   调用；先于 detail.js 加载，产物挂在 global.InsightsDetailCharts。
   与 detail.js 共享 global.Insights（I），不引入额外依赖。
*/

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
   * cutoff 是后端下发的绝交日标记 {day, kind, certainty}：确认绝交
   * 的联系人曲线只画到绝交日（含）——数据已在服务端截断，这里在
   * 当天位置补一条虚线，标出曲线为什么到这里就结束了。
   */
  function tempOption(history, cutoff) {
    var points = history.map(function (row) {
      return [dayKeyMs(row.day), row.overall];
    });

    return function (theme) {
      var series = [
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
      ];
      if (cutoff) {
        // 绝交标记线用错误红（与绝交角标同一变量，深浅主题自动跟随）；
        // silent 让标记线不参与 tooltip 交互。
        var errorColor = I.cssVar("--md-error");
        series[0].markLine = {
          symbol: "none",
          silent: true,
          lineStyle: { type: "dashed", color: errorColor },
          label: {
            formatter: "绝交",
            position: "end",
            fontSize: 12,
            color: errorColor
          },
          data: [{ xAxis: dayKeyMs(cutoff.day) }]
        };
      }
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
        series: series
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



  global.InsightsDetailCharts = {
    tempOption: tempOption,
    radarOption: radarOption,
    replyOption: replyOption,
    volumeOption: volumeOption,
    typesOption: typesOption
  };
})(window);
