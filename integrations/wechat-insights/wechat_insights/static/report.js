/* 友谊年报页：一年统计的卡片流 + 年份切换 + 可选的大模型叙事。 */

(function (global) {
  "use strict";

  var I = global.Insights;

  var MIN_YEAR = 2000;

  var els = {
    back: document.getElementById("back-link"),
    meta: document.getElementById("report-meta"),
    yearLabel: document.getElementById("year-label"),
    prevYear: document.getElementById("prev-year"),
    nextYear: document.getElementById("next-year"),
    content: document.getElementById("content")
  };

  els.back.href = I.linkTo("/");

  var state = {
    year: new Date().getFullYear(),
    // 首屏入场动画只播一次：切换年份后的重绘不重播。
    entered: false
  };

  /* ------------------------------------------------------------------ *
   * 入口与年份切换
   * ------------------------------------------------------------------ */

  function currentYear() {
    return new Date().getFullYear();
  }

  function updateNav() {
    els.yearLabel.textContent = state.year;
    els.prevYear.disabled = state.year <= MIN_YEAR;
    els.nextYear.disabled = state.year >= currentYear();
  }

  function switchYear(delta) {
    var target = state.year + delta;
    if (target < MIN_YEAR || target > currentYear()) {
      return;
    }
    state.year = target;
    load();
  }

  async function load() {
    els.content.innerHTML = I.stateHtml("loading", "正在生成年报…");
    els.meta.textContent = state.year + " 年";
    updateNav();
    var payload;
    try {
      payload = await I.api("/api/report?year=" + state.year);
    } catch (err) {
      els.content.innerHTML = I.errorStateHtml(err);
      return;
    }
    render(payload);
  }

  /* ------------------------------------------------------------------ *
   * 卡片骨架
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

  function milestone(label, value) {
    return (
      '<div class="milestone">' +
      '<div class="milestone__label">' +
      I.escapeHtml(label) +
      "</div>" +
      '<div class="milestone__value">' +
      I.escapeHtml(value) +
      "</div>" +
      "</div>"
    );
  }

  /** 榜单行：首字母头像 + 名字 + 说明，整行可点进详情页（复用淡出卡行样式）。 */
  function contactRowHtml(item, metaText) {
    return (
      '<button type="button" class="fading-row" data-hash="' +
      I.escapeHtml(item.hash) +
      '">' +
      '<span class="avatar" style="background:' +
      I.avatarColor(item.display_name) +
      '" aria-hidden="true">' +
      I.escapeHtml(I.initial(item.display_name)) +
      "</span>" +
      '<span class="fading-row__id">' +
      '<span class="fading-row__name">' +
      I.escapeHtml(item.display_name) +
      "</span>" +
      '<span class="fading-row__meta">' +
      metaText +
      "</span>" +
      "</span>" +
      "</button>"
    );
  }

  function rankCard(title, rows, subtitle) {
    if (!rows.length) {
      return "";
    }
    return (
      card(
        title,
        '<div class="fading-card__rows">' +
          rows
            .map(function (row) {
              return contactRowHtml(row, I.formatCount(row.messages) + " 条");
            })
            .join("") +
          "</div>",
        subtitle
      )
    );
  }

  /* ------------------------------------------------------------------ *
   * 各卡片渲染
   * ------------------------------------------------------------------ */

  function narrativeCard(text) {
    return (
      card(
        "年度总结",
        '<p class="report-narrative">' +
          I.escapeHtml(text) +
          "</p>" +
          '<p class="md-caption">由大模型基于匿名统计数据生成</p>',
        ""
      )
    );
  }

  function overviewCard(overview) {
    var total = (overview.incoming || 0) + (overview.outgoing || 0);
    var themPct = total ? Math.round(((overview.incoming || 0) / total) * 100) : 0;
    // 事务往来联系人不进任何榜单，overview 注脚说明排除个数（后端给的是
    // 数字，直接拼接即可）。
    var excluded = overview.excluded_transactional || 0;
    var footnote =
      excluded > 0
        ? '<p class="md-caption">已排除 ' + excluded + " 个事务往来联系人</p>"
        : "";
    return (
      card(
        "这一年",
        '<div class="milestones">' +
          milestone("总消息数", I.formatCount(overview.messages) + " 条") +
          milestone("联系人数", I.formatCount(overview.contacts) + " 位") +
          milestone(
            "双向比例",
            "TA " + themPct + "% / 我 " + (100 - themPct) + "%"
          ) +
          "</div>" +
          footnote
      )
    );
  }

  function monthlyCard(monthly) {
    return (
      card(
        "月度消息量",
        monthly && monthly.length
          ? '<div class="chart" id="monthly-chart"></div>'
          : '<p class="md-caption">暂无按月数据</p>'
      )
    );
  }

  function newFriendsCard(rows) {
    if (!rows.length) {
      return "";
    }
    return card(
      "新朋友",
      '<div class="fading-card__rows">' +
        rows
          .map(function (row) {
            return contactRowHtml(row, I.formatCount(row.messages) + " 条");
          })
          .join("") +
        "</div>",
      "这一年才认识的" + rows.length + " 位联系人"
    );
  }

  function fadedCard(rows) {
    if (!rows.length) {
      return "";
    }
    return card(
      "淡出的人",
      '<div class="fading-card__rows">' +
        rows
          .map(function (row) {
            return contactRowHtml(
              row,
              "上一年 " +
                I.formatCount(row.previous_messages) +
                " 条 → 今年 " +
                I.formatCount(row.messages) +
                " 条"
            );
          })
          .join("") +
        "</div>",
      "上一年往来过百、今年骤降的联系人"
    );
  }

  function hahaCard(king) {
    if (!king) {
      return "";
    }
    return card(
      "哈哈哈之王",
      '<div class="fading-card__rows">' +
        contactRowHtml(king, I.formatCount(king.max_laugh_run) + " 连") +
        "</div>",
      "全时段成就 · 不限今年"
    );
  }

  /* ------------------------------------------------------------------ *
   * 月度条形图（ECharts，抄详情页月度柱状图模式，单系列）
   * ------------------------------------------------------------------ */

  function monthlyOption(monthly) {
    var labels = monthly.map(function (row) {
      return row.month.slice(5) + "月";
    });
    var counts = monthly.map(function (row) {
      return row.count;
    });
    return function (theme) {
      return {
        backgroundColor: "transparent",
        grid: { left: 48, right: 24, top: 24, bottom: 40 },
        tooltip: Object.assign(
          {
            trigger: "axis",
            axisPointer: { type: "shadow" },
            formatter: function (params) {
              var p = params[0];
              return (
                I.escapeHtml(p.axisValue) +
                "<br/>" +
                p.marker +
                "消息 " +
                I.formatCount(p.value) +
                " 条"
              );
            }
          },
          I.tooltipStyle(theme)
        ),
        xAxis: {
          type: "category",
          data: labels,
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
            name: "消息数",
            type: "bar",
            barMaxWidth: 32,
            data: counts,
            itemStyle: { color: theme.primary }
          }
        ]
      };
    };
  }

  /* ------------------------------------------------------------------ *
   * 渲染
   * ------------------------------------------------------------------ */

  function render(payload) {
    I.disposeCharts();
    var stats = payload.stats || {};
    var overview = stats.overview || {};
    var monthly = stats.monthly || [];

    document.title = state.year + " 年友谊年报 · 关系洞察";

    var cards = [];
    if (payload.narrative) {
      cards.push(narrativeCard(payload.narrative));
    }
    cards.push(overviewCard(overview));
    cards.push(rankCard("聊得最多", stats.top || [], "这一年聊得最多的五位"));
    cards.push(
      rankCard(
        "深夜之王",
        stats.night || [],
        "深夜 23 点至凌晨 2 点还在聊的三位"
      )
    );
    cards.push(
      rankCard("周末搭子", stats.weekend || [], "周末聊得最勤的三位")
    );
    cards.push(monthlyCard(monthly));
    cards.push(newFriendsCard(stats.new_friends || []));
    cards.push(fadedCard(stats.faded || []));
    cards.push(hahaCard(stats.haha_king));

    els.content.innerHTML = '<div class="stack">' + cards.join("") + "</div>";

    // 首屏成功渲染时才加入场类；换年份的重绘已置位，不再重播。
    if (!state.entered) {
      state.entered = true;
      var stackEl = els.content.querySelector(".stack");
      if (stackEl) {
        stackEl.classList.add("enter-stagger");
      }
    }

    els.content.querySelectorAll(".fading-row[data-hash]").forEach(function (row) {
      row.addEventListener("click", function () {
        global.location.href = I.linkTo(
          "/contact/" + encodeURIComponent(row.dataset.hash)
        );
      });
    });

    if (monthly.length) {
      I.mountChart(document.getElementById("monthly-chart"), monthlyOption(monthly));
    }
  }

  /* ------------------------------------------------------------------ *
   * 交互
   * ------------------------------------------------------------------ */

  els.prevYear.addEventListener("click", function () {
    switchYear(-1);
  });
  els.nextYear.addEventListener("click", function () {
    switchYear(1);
  });

  load();
})(window);
