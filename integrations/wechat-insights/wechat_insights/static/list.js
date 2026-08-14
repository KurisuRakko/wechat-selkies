/* 列表页：联系人卡片网格 + 刷新与轮询。 */

(function (global) {
  "use strict";

  var I = global.Insights;

  var POLL_INTERVAL = 5000;
  var PROGRESS_INTERVAL = 1000;
  var PROGRESS_FAIL_LIMIT = 10;

  var els = {
    statusLine: document.getElementById("status-line"),
    refreshBtn: document.getElementById("refresh-btn"),
    reportBtn: document.getElementById("report-btn"),
    bannerSlot: document.getElementById("banner-slot"),
    fadingSlot: document.getElementById("fading-slot"),
    sortSelect: document.getElementById("sort-select"),
    summaryLine: document.getElementById("summary-line"),
    content: document.getElementById("content"),
    progressSlot: document.getElementById("progress-slot"),
    progressBar: document.getElementById("progress-bar"),
    progressIndeterminate: document.getElementById("progress-indeterminate"),
    progressCaption: document.getElementById("progress-caption")
  };

  // items 只在拉取时更新，排序是纯前端行为。
  var state = {
    items: [],
    running: false,
    pollTimer: 0,
    progressTimer: 0,
    progressFails: 0,
    // 只有亲眼见过 running=true 才算「从 true 变 false」：POST /api/refresh
    // 返回 202 后分析要等下一个事件循环才置 running，不能把开跑前那一瞬的
    // running=false 当成「分析完成」。
    progressSawRunning: false,
    // 本轮完成收尾已由进度轮询做过（防止在途的旧状态轮询再重拉一次列表）。
    progressDone: false,
    hideTimer: 0,
    unauthorized: false,
    // 首屏入场动画只播一次：排序切换、刷新完成后的重拉都不再播。
    entered: false
  };

  var PROGRESS_LABELS = {
    sync: "同步聊天记录",
    llm: "AI 分析",
    classify: "关系分类",
    // 时段化评分：按自然月给历史时段补 LLM 分，首轮回填量大。
    period: "AI 时段评分",
    // 好感度校准：消化右键标记，把幅度分配到七维。
    calibrate: "好感度校准",
    // 绝交核实：消化右键绝交标记，核实后封顶压低分数。
    breakup: "绝交核实",
    score: "重算打分",
    history: "回放历史",
    // 逐日细化：把某位联系人的历史从每周重算成每天，全史很耗时。
    refine: "细化日采样"
  };

  /* ------------------------------------------------------------------ *
   * 进度条：轮询 /api/progress 渲染确定/不确定态
   * ------------------------------------------------------------------ */

  function setProgressIndeterminate(indeterminate) {
    els.progressBar.style.width = indeterminate ? "" : "0";
    els.progressIndeterminate.style.display = indeterminate ? "block" : "none";
  }

  function renderProgress(progress) {
    var phase = progress.phase || "";
    var label = PROGRESS_LABELS[phase] || "";
    if (phase === "score") {
      // 重算打分没有逐项计数，只显示进行中的文案。
      els.progressCaption.textContent = label ? label + "…" : "";
      setProgressIndeterminate(true);
      return;
    }
    var done = I.isNumber(progress.done) ? progress.done : 0;
    var total = I.isNumber(progress.total) ? progress.total : 0;
    if (total > 0) {
      var pct = Math.max(0, Math.min(100, (done / total) * 100));
      els.progressBar.style.width = pct.toFixed(2) + "%";
      els.progressIndeterminate.style.display = "none";
    } else {
      // 不知道总量（还没开始计数）就用不确定态。
      setProgressIndeterminate(true);
    }
    var text = label ? label + " " + done + "/" + total : "";
    var detail = progress.detail || "";
    if (detail) {
      text += " · " + I.escapeHtml(detail);
    }
    els.progressCaption.innerHTML = text;
  }

  function showProgress() {
    els.progressSlot.hidden = false;
  }

  function hideProgress() {
    els.progressSlot.hidden = true;
  }

  function startProgressPolling() {
    if (state.progressTimer) {
      return;
    }
    state.progressFails = 0;
    state.progressSawRunning = false;
    state.progressDone = false;
    showProgress();
    state.progressTimer = global.setInterval(progressOnce, PROGRESS_INTERVAL);
  }

  function stopProgressPolling() {
    global.clearInterval(state.progressTimer);
    state.progressTimer = 0;
  }

  function finishProgress() {
    // 分析结束：恢复按钮并停掉状态轮询，显示「分析完成」约 1.5 秒后收起
    // 进度条，然后重新拉列表数据。
    state.progressDone = true;
    stopProgressPolling();
    stopPolling();
    els.progressCaption.textContent = "分析完成";
    setProgressIndeterminate(true);
    global.clearTimeout(state.hideTimer);
    state.hideTimer = global.setTimeout(function () {
      hideProgress();
    }, 1500);
    loadContacts();
  }

  async function progressOnce() {
    var payload;
    try {
      payload = await I.api("/api/progress");
    } catch (err) {
      if (err.status === 401) {
        // 令牌失效，停止轮询并提示，避免无意义的重试。
        stopProgressPolling();
        hideProgress();
        showUnauthorized();
        return;
      }
      // 网络抖动不终止轮询，跳过该次；连续失败太多次才放弃。
      state.progressFails += 1;
      if (state.progressFails >= PROGRESS_FAIL_LIMIT) {
        stopProgressPolling();
        hideProgress();
        stopPolling();
      }
      return;
    }
    state.progressFails = 0;
    var progress = payload.progress || {};
    renderProgress(progress);
    if (progress.running) {
      state.progressSawRunning = true;
    } else if (state.progressSawRunning) {
      finishProgress();
    }
    // 开跑前的 running=false 只渲染、不当作完成，等下一轮再看。
  }

  /* ------------------------------------------------------------------ *
   * App Bar：状态、错误条与刷新按钮
   * ------------------------------------------------------------------ */

  function renderStatus(status) {
    if (status.last_analyzed_iso || I.isNumber(status.last_analyzed_at)) {
      var when = status.last_analyzed_iso || status.last_analyzed_at;
      els.statusLine.textContent = "上次分析：" + I.formatDateTime(when);
    } else {
      els.statusLine.textContent = "尚未分析";
    }

    // error 非 null 时在 App Bar 下方挂一条警告条。
    if (status.error && status.error.message) {
      els.bannerSlot.innerHTML =
        '<div class="banner" role="alert"><div class="banner__inner">' +
        '<span class="banner__icon" aria-hidden="true">⚠</span>' +
        "<span>" +
        I.escapeHtml(status.error.message) +
        "</span></div></div>";
    } else {
      els.bannerSlot.innerHTML = "";
    }
  }

  function setRefreshBusy(busy) {
    els.refreshBtn.disabled = busy;
    els.refreshBtn.textContent = busy ? "分析中…" : "刷新";
  }

  /* ------------------------------------------------------------------ *
   * 轮询：分析进行中时每 5 秒问一次 /api/status
   * ------------------------------------------------------------------ */

  function startPolling() {
    if (state.pollTimer) {
      return;
    }
    state.running = true;
    setRefreshBusy(true);
    state.pollTimer = global.setInterval(pollOnce, POLL_INTERVAL);
  }

  function stopPolling() {
    global.clearInterval(state.pollTimer);
    state.pollTimer = 0;
    state.running = false;
    setRefreshBusy(false);
  }

  async function pollOnce() {
    var status;
    try {
      status = await I.api("/api/status");
    } catch (err) {
      if (err.status === 401) {
        // 令牌失效，停止轮询并提示，避免无意义的重试。
        stopPolling();
        showUnauthorized();
      }
      return;
    }
    renderStatus(status);
    if (!status.running) {
      // 完成收尾（重拉列表、恢复按钮）由 1 秒的进度轮询负责；状态轮询只
      // 停掉自己，避免与进度轮询双重重拉列表。
      if (state.progressTimer || state.progressDone) {
        global.clearInterval(state.pollTimer);
        state.pollTimer = 0;
        return;
      }
      stopPolling();
      loadContacts();
    }
  }

  function showUnauthorized() {
    state.unauthorized = true;
    els.fadingSlot.innerHTML = "";
    els.content.innerHTML = I.errorStateHtml({ status: 401 });
    els.summaryLine.textContent = "";
    els.statusLine.textContent = "尚未分析";
  }

  /* ------------------------------------------------------------------ *
   * 数据加载
   * ------------------------------------------------------------------ */

  async function loadStatus() {
    try {
      var status = await I.api("/api/status");
      renderStatus(status);
      if (status.running) {
        // 分析在跑（每日定时或别的标签页触发的）：直接进入进度轮询。
        startPolling();
        startProgressPolling();
      }
    } catch (err) {
      if (err.status === 401) {
        showUnauthorized();
      } else {
        els.statusLine.textContent = "状态获取失败";
      }
    }
  }

  async function loadContacts() {
    if (state.unauthorized) {
      return;
    }
    els.content.innerHTML = I.stateHtml("loading", "正在加载联系人…");
    try {
      var payload = await I.api("/api/contacts");
      state.items = payload.items || [];
      renderFading(payload.fading || []);
      renderList();
      // fading 卡与网格是同一屏的两块，必须等两者都渲染完再置位，
      // 否则先置位的那一方会把另一块的入场动画吃掉。
      state.entered = true;
    } catch (err) {
      if (err.status === 401) {
        showUnauthorized();
        return;
      }
      // 渲染异常与请求异常共用这个「加载失败」出口：不打控制台就彻底
      // 没有堆栈，排查时无从下手。
      console.error("[insights] 联系人列表渲染失败", err);
      els.fadingSlot.innerHTML = "";
      els.content.innerHTML = I.errorStateHtml(err);
      els.summaryLine.textContent = "";
    }
  }

  /* ------------------------------------------------------------------ *
   * 排序：有分的 > 数据不足 > 归零 > 事务往来
   * （未打分的都沉底、归零沉到最深；事务往来不参与打分，沉到最底）
   * ------------------------------------------------------------------ */

  var SORT_KEYS = {
    overall: function (item) {
      return item.overall;
    },
    recent: function (item) {
      return item.last_message_at;
    },
    trend: function (item) {
      return item.trends ? item.trends.overall : null;
    }
  };

  function itemTier(item) {
    if (item.relation_kind === "transactional") {
      return -1;
    }
    if (item.scored) {
      return 2;
    }
    return item.zeroed ? 0 : 1;
  }

  function sortedItems() {
    var pick = SORT_KEYS[els.sortSelect.value] || SORT_KEYS.overall;
    return state.items.slice().sort(function (a, b) {
      var at = itemTier(a);
      var bt = itemTier(b);
      if (at !== bt) {
        return bt - at;
      }
      var av = pick(a);
      var bv = pick(b);
      var aOk = I.isNumber(av);
      var bOk = I.isNumber(bv);
      if (aOk && bOk && av !== bv) {
        return bv - av; // 一律降序
      }
      if (aOk !== bOk) {
        return aOk ? -1 : 1;
      }
      // 数值相同或都缺失时按名字排，保证顺序稳定。
      return String(a.display_name).localeCompare(String(b.display_name), "zh-CN");
    });
  }

  /* ------------------------------------------------------------------ *
   * 卡片渲染
   * ------------------------------------------------------------------ */

  /**
   * 「正在淡出」提醒卡：沉默已久但分还高的关系，一行一人、整行可点。
   * 默认收起只看头部（标题 + 人数 + 警示点 + chevron），点头部展开；
   * 展开状态不持久化，每轮重绘回到收起。无命中时清空插槽（后端每一轮
   * 都会写，空数组或未写都按无命中处理）。
   */
  function renderFading(fading) {
    if (!fading.length) {
      els.fadingSlot.innerHTML = "";
      // fadingSlot 是常驻元素，清空 innerHTML 不会带走类；首屏没播过也
      // 不能残留类名，否则会误导后续重绘。
      els.fadingSlot.classList.remove("enter-stagger");
      return;
    }
    var rows = fading
      .map(function (item) {
        return (
          '<button type="button" class="fading-row" data-hash="' +
          item.hash +
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
          '<span class="fading-row__meta">已沉默 ' +
          item.gap_days +
          " 天 · 综合 " +
          I.formatNumber(item.overall, 1) +
          " 分</span>" +
          "</span>" +
          "</button>"
        );
      })
      .join("");

    els.fadingSlot.innerHTML =
      '<section class="card fading-card">' +
      '<button type="button" class="fading-card__header" aria-expanded="false">' +
      '<span class="fading-card__title">' +
      "正在淡出" +
      '<span class="fading-card__count">(' +
      fading.length +
      ")</span>" +
      '<span class="fading-card__dot" aria-hidden="true"></span>' +
      "</span>" +
      '<span class="fading-card__chevron" aria-hidden="true"></span>' +
      "</button>" +
      '<div class="fading-card__rows" hidden>' +
      rows +
      "</div>" +
      "</section>";

    // 入场动画只给首屏。fading 卡与联系人网格是同一屏的两块，置位统一
    // 由 loadContacts 在两者都渲染完后做；这里只按是否已入场决定类名，
    // 已入场时必须清掉，否则重绘后类名残留会误导。
    if (state.entered) {
      els.fadingSlot.classList.remove("enter-stagger");
    } else {
      els.fadingSlot.classList.add("enter-stagger");
    }

    els.fadingSlot
      .querySelector(".fading-card__header")
      .addEventListener("click", function () {
        var expanded = this.getAttribute("aria-expanded") === "true";
        els.fadingSlot.querySelector(".fading-card__rows").hidden = expanded;
        this.setAttribute("aria-expanded", expanded ? "false" : "true");
      });

    els.fadingSlot.querySelectorAll(".fading-row").forEach(function (row) {
      row.addEventListener("click", function () {
        global.location.href = I.linkTo(
          "/contact/" + encodeURIComponent(row.dataset.hash)
        );
      });
    });
  }

  var RING_SIZE = 56;
  var RING_RADIUS = 24;
  var RING_STROKE = 4;

  /** 综合分环形：inline SVG。stroke-dasharray 固定为整周长，占比由
   *  stroke-dashoffset（--ring-offset）表示：首屏时 CSS 动画从 0 描边
   *  到目标分数，之后常驻静态偏移。 */
  function ringSvg(overall) {
    var circumference = 2 * Math.PI * RING_RADIUS;
    var pct = Math.max(0, Math.min(100, overall));
    var color;
    if (pct < 40) {
      color = I.cssVar("--md-on-surface-disabled");
    } else if (pct <= 70) {
      color = I.cssVar("--md-primary");
    } else {
      color = I.cssVar("--md-secondary");
    }
    var filled = (circumference * pct) / 100;
    var center = RING_SIZE / 2;
    return (
      '<svg class="ring" width="' +
      RING_SIZE +
      '" height="' +
      RING_SIZE +
      '" viewBox="0 0 ' +
      RING_SIZE +
      " " +
      RING_SIZE +
      '" role="img" aria-label="综合分 ' +
      Math.round(pct) +
      '">' +
      '<circle class="ring__track" cx="' +
      center +
      '" cy="' +
      center +
      '" r="' +
      RING_RADIUS +
      '" stroke-width="' +
      RING_STROKE +
      '"></circle>' +
      '<circle class="ring__value" cx="' +
      center +
      '" cy="' +
      center +
      '" r="' +
      RING_RADIUS +
      '" stroke-width="' +
      RING_STROKE +
      '" stroke="' +
      color +
      '" stroke-dasharray="' +
      circumference.toFixed(2) +
      '" style="--ring-circumference: ' +
      circumference.toFixed(2) +
      "; --ring-offset: " +
      (circumference - filled).toFixed(2) +
      '"></circle>' +
      '<text class="ring__label" x="' +
      center +
      '" y="' +
      center +
      '" text-anchor="middle" dominant-baseline="central">' +
      Math.round(pct) +
      "</text>" +
      "</svg>"
    );
  }

  /** 名字旁的异动角标：警示色小圆点 + 数量，悬停提示「N 项近期异动」。 */
  function anomalyBadgeHtml(item) {
    var count = item.anomalies && item.anomalies.length;
    if (!count) {
      return "";
    }
    return (
      '<span class="contact-card__badge" title="' +
      count +
      ' 项近期异动" aria-label="' +
      count +
      ' 项近期异动">' +
      '<span class="contact-card__badge__dot" aria-hidden="true"></span>' +
      count +
      "</span>"
    );
  }

  /** 名字行下方最多 2 个小号话题标签 chip（大模型输出，逐个 escapeHtml）。 */
  function cardTagsHtml(tags) {
    if (!tags || !tags.length) {
      return "";
    }
    return (
      '<span class="contact-card__tags">' +
      tags
        .slice(0, 2)
        .map(function (tag) {
          return '<span class="chip chip--small">' + I.escapeHtml(tag) + "</span>";
        })
        .join("") +
      "</span>"
    );
  }

  /** 综合分趋势箭头：±8 以内视为持平（阈值只在 JS 里维护）。 */
  function trendHtml(delta) {
    if (!I.isNumber(delta)) {
      // 基线窗口样本不足时后端不提供趋势：不画箭头、不写数值，沿用「数据
      // 不足」的说法——没有数据不能假装成持平。
      return '<span class="trend trend--flat">数据不足</span>';
    }
    var modifier = "flat";
    var arrow = "→";
    if (delta >= 8) {
      modifier = "up";
      arrow = "↑";
    } else if (delta <= -8) {
      modifier = "down";
      arrow = "↓";
    }
    return (
      '<span class="trend trend--' +
      modifier +
      '"><span class="trend__arrow">' +
      arrow +
      "</span>" +
      delta.toFixed(1) +
      "</span>"
    );
  }

  /** 卡片里的七维 mini 雷达，无标题无图例、关动画。 */
  function miniRadarOption(dimensions) {
    return function (theme) {
      return {
        backgroundColor: "transparent",
        animation: false,
        radar: {
          center: ["50%", "50%"],
          radius: "58%",
          splitNumber: 3,
          axisName: { color: theme.subTextColor, fontSize: 10 },
          nameGap: 4,
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
            symbol: "none",
            silent: true,
            lineStyle: { width: 1.5, color: theme.primary },
            areaStyle: { color: theme.primary, opacity: 0.22 },
            data: [
              {
                value: I.DIMENSIONS.map(function (dim) {
                  var v = dimensions ? dimensions[dim[0]] : null;
                  return I.isNumber(v) ? v : 0;
                })
              }
            ]
          }
        ]
      };
    };
  }

  /* ------------------------------------------------------------------ *
   * 好感度校准：右键卡片弹出标记菜单
   * ------------------------------------------------------------------ */

  function markFeedback(item, action) {
    I.apiPost(
      "/api/contact/" + encodeURIComponent(item.hash) + "/feedback",
      { action: action }
    )
      .then(function (result) {
        if (action === "clear") {
          // 清除是即时的：服务端已按 payload 里的 base 快照还原分数并
          // 清空校准，重新拉一遍列表让角标与分数立刻回到客观口径。
          loadContacts();
          I.snackbar("已清除校准，分数还原为客观口径");
          return;
        }
        var pending = result.data && result.data.pending;
        if (pending) {
          // 服务端已把 calibration_pending 写进 payload，重拉列表让
          // 「校准排队中」角标与清除项立刻可见。
          loadContacts();
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

  function postBreakup(item, action, date, certainty) {
    var body = { action: action };
    if (action === "mark") {
      body.date = date;
      body.certainty = certainty;
    }
    I.apiPost(
      "/api/contact/" + encodeURIComponent(item.hash) + "/breakup",
      body
    )
      .then(function (result) {
        if (action === "clear") {
          // 清除是即时的：服务端已按 payload 里的 base 快照还原分数并
          // 清空结论，重新拉一遍列表让角标与分数立刻回到绝交前的口径。
          loadContacts();
          I.snackbar("已清除");
          return;
        }
        var pending = result.data && result.data.pending;
        if (pending) {
          // 服务端已把 breakup_pending 写进 payload，重拉列表让
          // 「绝交核实中」角标与清除项立刻可见。
          loadContacts();
          I.snackbar("已标记，下一轮分析核实");
        }
      })
      .catch(function (err) {
        I.snackbar((err && err.message) || "标记失败");
      });
  }

  function feedbackItems(item) {
    var items = [
      {
        label: "标记：感觉偏低",
        hint: "校准后分数上调",
        onClick: function () {
          markFeedback(item, "up");
        }
      },
      {
        label: "标记：感觉偏高",
        hint: "校准后分数下调",
        onClick: function () {
          markFeedback(item, "down");
        }
      }
    ];
    // 只有校准生效中或标记排队中才需要「清除」；干净的联系人没有可清的东西。
    if (item.calibration || item.calibration_pending) {
      items.push({
        label: "清除校准",
        hint: "还原客观分",
        onClick: function () {
          markFeedback(item, "clear");
        }
      });
    }
    // 绝交标记：两种置信度都要先选日期，核实排队中或已有结论时提供清除。
    items.push({
      label: "已经绝交…",
      hint: "输入日期，下轮核实",
      onClick: function () {
        I.openDateDialog("你们是什么时候绝交的？", function (date) {
          postBreakup(item, "mark", date, "certain");
        });
      }
    });
    items.push({
      label: "我认为的绝交…",
      hint: "存疑标记，AI 复核",
      onClick: function () {
        I.openDateDialog("你们是什么时候绝交的？", function (date) {
          postBreakup(item, "mark", date, "suspected");
        });
      }
    });
    if (item.breakup || item.breakup_pending) {
      items.push({
        label: "清除绝交标记",
        onClick: function () {
          postBreakup(item, "clear");
        }
      });
    }
    return items;
  }

  function buildCard(item) {
    var card = document.createElement("button");
    card.type = "button";
    card.className = "card contact-card";

    var recent =
      "近 30 天 " + (I.isNumber(item.recent_messages) ? item.recent_messages : 0) + " 条";

    var head =
      '<div class="contact-card__head">' +
      '<span class="avatar" style="background:' +
      I.avatarColor(item.display_name) +
      '" aria-hidden="true">' +
      I.escapeHtml(I.initial(item.display_name)) +
      "</span>" +
      '<span class="contact-card__id">' +
      '<span class="contact-card__name-row">' +
      '<span class="contact-card__name">' +
      I.escapeHtml(item.display_name) +
      "</span>" +
      I.kindBadgeHtml(item.relation_kind) +
      I.calibrationChipHtml(item) +
      I.breakupChipHtml(item) +
      anomalyBadgeHtml(item) +
      "</span>" +
      cardTagsHtml(item.llm_tags) +
      '<span class="contact-card__meta">' +
      recent +
      "</span>" +
      "</span>" +
      // 归零的联系人同样显示 0 分圆环：灰色弱化，不占主色。
      (item.scored || item.zeroed ? ringSvg(item.overall) : "") +
      "</div>";

    if (item.relation_kind === "transactional") {
      // 事务往来不参与打分：不显示圆环与雷达，正文一行说明即可。
      card.innerHTML =
        head +
        '<div class="insufficient">' +
        '<div class="insufficient__title">事务往来，不参与打分</div>' +
        "</div>";
    } else if (item.zeroed) {
      card.innerHTML =
        head +
        '<div class="insufficient">' +
        '<div class="insufficient__title">已归零</div>' +
        '<div class="insufficient__desc">两年内没有往来，已归零</div>' +
        "</div>";
    } else if (!item.scored) {
      card.innerHTML =
        head +
        '<div class="insufficient">' +
        '<div class="insufficient__title">数据不足</div>' +
        '<div class="insufficient__desc">近两年往来消息不足</div>' +
        "</div>";
    } else {
      card.innerHTML =
        head +
        '<div class="contact-card__body">' +
        '<div class="contact-card__radar"></div>' +
        '<div class="contact-card__side">' +
        '<span class="md-caption">综合分趋势</span>' +
        trendHtml(item.trends ? item.trends.overall : null) +
        "</div>" +
        "</div>";
    }

    card.addEventListener("click", function () {
      global.location.href = I.linkTo("/contact/" + encodeURIComponent(item.hash));
    });
    // 右键 = 好感度标记菜单；左键跳详情不受影响。
    card.addEventListener("contextmenu", function (event) {
      event.preventDefault();
      I.openContextMenu(event, feedbackItems(item));
    });
    return card;
  }

  function renderList() {
    // 整块重绘，先把上一轮的图表实例销毁，否则会连着旧 DOM 一起留在登记表里。
    I.disposeCharts();
    var items = sortedItems();
    var scored = items.filter(function (item) {
      return item.scored;
    }).length;
    els.summaryLine.textContent =
      "共 " + items.length + " 位联系人 · " + scored + " 位已打分";

    if (!items.length) {
      els.content.innerHTML = I.stateHtml(
        "empty",
        "还没有联系人数据",
        "等待第一轮分析完成后再回来看看。"
      );
      return;
    }

    var grid = document.createElement("div");
    grid.className = state.entered ? "grid" : "grid enter-stagger";
    var radarJobs = [];
    items.forEach(function (item) {
      var card = buildCard(item);
      grid.appendChild(card);
      // 以 buildCard 实际渲染出的容器为准登记雷达图任务：事务往来 / 已归零 /
      // 数据不足等分支不渲染容器时自然不登记。条件只保留 buildCard 里这一份，
      // 不在此处再抄一遍，避免两处判断将来再次失配。
      var radarSlot = card.querySelector(".contact-card__radar");
      if (radarSlot) {
        radarJobs.push([radarSlot, item.dimensions]);
      }
    });

    els.content.innerHTML = "";
    els.content.appendChild(grid);

    // 图表必须在节点入文档后再初始化，否则拿不到容器尺寸。
    radarJobs.forEach(function (job) {
      I.mountChart(job[0], miniRadarOption(job[1]));
    });
  }

  /* ------------------------------------------------------------------ *
   * 交互
   * ------------------------------------------------------------------ */

  els.sortSelect.addEventListener("change", function () {
    if (state.items.length) {
      renderList();
    }
  });

  els.reportBtn.addEventListener("click", function () {
    global.location.href = I.linkTo("/report");
  });

  els.refreshBtn.addEventListener("click", async function () {
    setRefreshBusy(true);
    var result;
    try {
      result = await I.apiPost("/api/refresh");
    } catch (err) {
      setRefreshBusy(false);
      if (err.status === 401) {
        showUnauthorized();
      } else {
        I.snackbar(err.message || "刷新失败");
      }
      return;
    }
    if (result.status === 409) {
      // 已经有一轮在跑：提示后进入进度轮询；快照还没跟上（开跑前
      // 的那一瞬）就先把进度条露出来。
      I.snackbar("分析正在进行中");
      startPolling();
      showProgress();
      startProgressPolling();
      return;
    }
    if (result.status === 202) {
      startPolling();
      startProgressPolling();
      return;
    }
    setRefreshBusy(false);
    var info = result.data && result.data.error;
    I.snackbar((info && info.message) || "刷新失败");
  });

  loadStatus();
  loadContacts();
})(window);
