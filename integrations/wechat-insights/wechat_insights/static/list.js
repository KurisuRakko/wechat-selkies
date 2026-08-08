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
    bannerSlot: document.getElementById("banner-slot"),
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
    unauthorized: false
  };

  var PROGRESS_LABELS = {
    sync: "同步聊天记录",
    llm: "AI 分析",
    score: "重算打分"
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
      renderList();
    } catch (err) {
      if (err.status === 401) {
        showUnauthorized();
        return;
      }
      els.content.innerHTML = I.errorStateHtml(err);
      els.summaryLine.textContent = "";
    }
  }

  /* ------------------------------------------------------------------ *
   * 排序：有分的 > 数据不足 > 归零（未打分的都沉底，归零沉到最深）
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

  var RING_SIZE = 56;
  var RING_RADIUS = 24;
  var RING_STROKE = 4;

  /** 综合分环形：inline SVG，用 stroke-dasharray 表示 0–100 的占比。 */
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
      filled.toFixed(2) +
      " " +
      circumference.toFixed(2) +
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
      '<span class="contact-card__name">' +
      I.escapeHtml(item.display_name) +
      "</span>" +
      '<span class="contact-card__meta">' +
      recent +
      "</span>" +
      "</span>" +
      // 归零的联系人同样显示 0 分圆环：灰色弱化，不占主色。
      (item.scored || item.zeroed ? ringSvg(item.overall) : "") +
      "</div>";

    if (item.zeroed) {
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
    grid.className = "grid";
    var radarJobs = [];
    items.forEach(function (item) {
      var card = buildCard(item);
      grid.appendChild(card);
      if (item.scored) {
        radarJobs.push([card.querySelector(".contact-card__radar"), item.dimensions]);
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
