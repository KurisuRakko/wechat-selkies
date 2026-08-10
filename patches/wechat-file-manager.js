/*
 * 把侧边栏「文件」面板从「iframe 套 nginx 目录页」重构成自绘的、
 * Windows 资源管理器风格文件浏览器：面包屑 + 上级 + 刷新 + 筛选、
 * 可排序的四列表格、双击进目录、双击下载、拖出到宿主机下载。
 *
 * 旧面板整块空白的根因：nginx 的 location /files 块没有 index 指令，
 * 内建默认 index index.html 生效；下载目录（默认 ~/Desktop）里只要有一个
 * 用户存的 index.html，/files/ 的清单请求就会返回那个 HTML 而不是目录清单，
 * 再叠上同块里的 Content-Disposition: attachment——浏览器把它当附件下载，
 * iframe 里什么都渲染不出来。files-json-index.py 把清单改成 nginx autoindex
 * 的 JSON 格式（index 指向一个不可能存在的名字 .selkies-no-index），这里
 * 直接 fetch JSON 自绘，不再依赖 fancyindex 的 HTML 页面。
 *
 * 刻意不做：上传按钮（上传仍走侧边栏原有入口与拖入，目的地也不同）、
 * 重命名/删除/新建目录（需要 WebDAV，超出范围且危险）、预览与缩略图。
 *
 * 作为普通（非 module）脚本与 bundle 并列加载，不修改 minified bundle。
 * React 每次开关 modal 都会重建 .files-modal 节点，所以这里不绑定一次性
 * 监听，而是用 MutationObserver + 定时扫描接管每个新出现的节点；modal
 * 的状态挂在 mount 的闭包里，节点被移除后闭包自然可回收，不设 per-modal
 * 计时器，没有泄漏。
 */
(function () {
  "use strict";

  var TAG = "[wechat-file-manager]";
  var BASE = String(window.WECHAT_FILES_BASE || "./files/");
  var STYLE_ID = "wechat-file-manager-style";
  var COLUMNS = ["name", "mtime", "type", "size"];

  // 图标映射（目录单独处理；图片/视频/音频/压缩包/pdf/文档，其余用 📄）。
  var ICONS = {
    png: "🖼", jpg: "🖼", jpeg: "🖼", gif: "🖼", webp: "🖼", bmp: "🖼", svg: "🖼",
    mp4: "🎬", mkv: "🎬", mov: "🎬", avi: "🎬", webm: "🎬",
    mp3: "🎵", wav: "🎵", flac: "🎵", m4a: "🎵", ogg: "🎵",
    zip: "🗜", "7z": "🗜", rar: "🗜", gz: "🗜", tar: "🗜",
    pdf: "📕", doc: "📄", docx: "📄", xls: "📄", xlsx: "📄", ppt: "📄", pptx: "📄", txt: "📄", md: "📄"
  };
  // 拖出到宿主机用的 Chromium DownloadURL 格式需要 MIME，覆盖面对齐
  // wechat-dragdrop.js 里的 mimeFor（两个脚本之间没有模块系统，不共享代码）。
  var EXT_MIME = {
    png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp",
    bmp: "image/bmp", svg: "image/svg+xml", ico: "image/x-icon", pdf: "application/pdf",
    txt: "text/plain", md: "text/markdown", zip: "application/zip", "7z": "application/x-7z-compressed",
    rar: "application/x-rar-compressed", gz: "application/gzip", tar: "application/x-tar",
    mp4: "video/mp4", mkv: "video/x-matroska", mov: "video/quicktime", avi: "video/x-msvideo",
    webm: "video/webm", mp3: "audio/mpeg", wav: "audio/wav", flac: "audio/flac",
    m4a: "audio/mp4", ogg: "audio/ogg", doc: "application/msword", xls: "application/vnd.ms-excel",
    docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ppt: "application/vnd.ms-powerpoint",
    pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation"
  };

  var STYLE = [
    ".wfm{flex:1;min-height:0;display:flex;flex-direction:column;gap:8px;color:var(--sidebar-text);font-size:14px}",
    ".wfm-toolbar{display:flex;align-items:center;gap:8px;padding-right:32px;flex:none}",
    ".wfm-btn{background:var(--button-bg);color:var(--button-text);border:none;border-radius:10px;padding:4px 8px;cursor:pointer;font-size:13px;flex:none}",
    ".wfm-btn:hover:not(:disabled){background:var(--button-hover-bg)}.wfm-btn:disabled{opacity:.45;cursor:default}",
    ".wfm-crumbs{display:flex;align-items:center;gap:2px;flex:1;min-width:0;overflow:hidden;white-space:nowrap}",
    ".wfm-crumb{background:none;border:none;color:var(--sidebar-text);cursor:pointer;padding:2px 4px;border-radius:6px;font-size:13px}",
    ".wfm-crumb:hover{color:var(--sidebar-header-color);background:var(--section-bg)}.wfm-crumb-sep{color:var(--sidebar-border)}",
    ".wfm-filter{background:var(--input-bg);color:var(--input-text);border:1px solid var(--input-border);border-radius:10px;padding:4px 8px;font-size:13px;width:140px;flex:none}",
    ".wfm-table{display:flex;flex-direction:column;flex:1;min-height:0;border:1px solid var(--item-border);border-radius:10px;overflow:hidden}",
    ".wfm-thead{display:grid;grid-template-columns:minmax(0,1fr) 180px 140px 110px;background:var(--section-bg);border-bottom:1px solid var(--item-border)}",
    ".wfm-th{text-align:left;font-size:12px;color:var(--sidebar-header-color);padding:6px 12px;cursor:pointer;user-select:none;border:none;background:none}",
    ".wfm-th:hover{background:var(--section-bg)}.wfm-list{flex:1;overflow:auto;outline:none}",
    ".wfm-row{display:grid;grid-template-columns:minmax(0,1fr) 180px 140px 110px;align-items:center;height:32px;padding:0 12px;cursor:pointer;border-bottom:1px solid transparent}",
    ".wfm-row:hover{background:var(--section-bg)}.wfm-row.wfm-selected{background:var(--input-bg);border-bottom:1px solid var(--sidebar-header-color)}",
    ".wfm-cell{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}",
    ".wfm-name{display:flex;align-items:center;gap:8px}.wfm-icon{flex:none}",
    ".wfm-empty,.wfm-error{padding:16px;text-align:center;font-size:13px;color:var(--sidebar-text)}",
    ".wfm-error{color:var(--notification-error-color,#e74c3c)}",
    ".wfm-status{flex:none;font-size:12px;color:var(--sidebar-header-color);padding:0 4px}",
    "@media (max-width:780px){.wfm-thead,.wfm-row{grid-template-columns:minmax(0,1fr) 110px}.wfm-th-mtime,.wfm-th-type,.wfm-cell-mtime,.wfm-cell-type{display:none}}"
  ].join("");

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = STYLE;
    document.head.appendChild(style);
  }
  function extOf(name) {
    var m = /\.([^.]+)$/.exec(String(name).toLowerCase());
    return m ? m[1] : "";
  }
  function iconFor(entry) {
    if (entry.type === "directory") return "📁";
    return ICONS[extOf(entry.name)] || "📄";
  }
  function typeLabel(entry) {
    if (entry.type === "directory") return "文件夹";
    var ext = extOf(entry.name);
    return ext ? ext.toUpperCase() + " 文件" : "文件";
  }
  function mimeFor(name) {
    return EXT_MIME[extOf(name)] || "application/octet-stream";
  }
  // B 显示整数，KB 及以上固定 1 位小数；目录没有 size，显示 —。
  function formatSize(size) {
    var n = Number(size);
    if (!isFinite(n) || n < 0) return "—";
    var units = ["B", "KB", "MB", "GB"];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return (i === 0 ? String(Math.round(n)) : n.toFixed(1)) + " " + units[i];
  }
  // nginx autoindex JSON 的 mtime 恒为 RFC1123 GMT（autoindex_localtime
  // 只影响 HTML 清单），new Date 能直接解析；解析失败显示 —。
  function formatMtime(mtime) {
    var d = new Date(mtime);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleString();
  }
  // 目录 URL 带尾斜杠（nginx alias 需要），文件 URL 不带。
  function dirUrl(segs) {
    return BASE + segs.map(function (s) { return encodeURIComponent(s); }).join("/") +
      (segs.length ? "/" : "");
  }
  function fileUrl(segs, name) {
    return dirUrl(segs) + encodeURIComponent(name);
  }
  // 每个 .files-modal 节点由 mount 接管一次；React 每次开关 modal 都会重建
  // 节点，重建后的节点不带标记，observer 会再次接管。状态全部挂在本次
  // mount 的闭包里，旧节点被移除后闭包随旧 DOM 一起回收，不设 per-modal
  // 计时器，没有泄漏。
  function mount(modal) {
    if (modal.dataset.wechatFileManager) return;
    modal.dataset.wechatFileManager = "1";
    ensureStyle();

    var iframe = modal.querySelector("iframe");
    if (iframe) iframe.remove();

    var state = {
      segs: [],        // 当前目录的路径段（相对下载目录）
      entries: [],     // 当前目录条目（已滤隐藏项）
      displayed: [],   // 过滤 + 排序后的展示序列，索引与 rows 一一对应
      rows: [],        // 已渲染的行节点
      sortCol: "name",
      sortDir: 1,      // 1 升序 / -1 降序
      selected: -1,
      seq: 0,          // 请求自增序号：只渲染最后一次请求的结果
      filter: ""
    };

    var root = document.createElement("div");
    root.className = "wfm";

    /* ---------------------------------------------------------- toolbar */

    var toolbar = document.createElement("div");
    toolbar.className = "wfm-toolbar";
    var upBtn = document.createElement("button");
    upBtn.className = "wfm-btn";
    upBtn.textContent = "↑ 上级";
    upBtn.disabled = true;
    var refreshBtn = document.createElement("button");
    refreshBtn.className = "wfm-btn";
    refreshBtn.textContent = "⟳ 刷新";
    var crumbs = document.createElement("div");
    crumbs.className = "wfm-crumbs";
    var filterInput = document.createElement("input");
    filterInput.className = "wfm-filter";
    filterInput.setAttribute("type", "text");
    filterInput.setAttribute("placeholder", "筛选");
    toolbar.appendChild(upBtn);
    toolbar.appendChild(refreshBtn);
    toolbar.appendChild(crumbs);
    toolbar.appendChild(filterInput);

    /* -------------------------------------------------------------- table */

    var headers = [
      { key: "name", label: "名称" },
      { key: "mtime", label: "修改日期" },
      { key: "type", label: "类型" },
      { key: "size", label: "大小" }
    ];
    var table = document.createElement("div");
    table.className = "wfm-table";
    var thead = document.createElement("div");
    thead.className = "wfm-thead";
    var thList = [];
    for (var h = 0; h < headers.length; h++) {
      var th = document.createElement("button");
      th.className = "wfm-th wfm-th-" + headers[h].key;
      th.textContent = headers[h].label;
      th.addEventListener("click", sortClick(headers[h].key));
      thead.appendChild(th);
      thList.push(th);
    }
    // 列表容器 tabindex=0，键盘事件只绑在这里，绝不绑到 document——
    // 否则会抢走串流页面的方向键和回车。
    var list = document.createElement("div");
    list.className = "wfm-list";
    list.setAttribute("tabindex", "0");
    list.addEventListener("keydown", onKeydown);
    table.appendChild(thead);
    table.appendChild(list);

    var status = document.createElement("div");
    status.className = "wfm-status";

    root.appendChild(toolbar);
    root.appendChild(table);
    root.appendChild(status);
    modal.appendChild(root);

    upBtn.addEventListener("click", goUp);
    refreshBtn.addEventListener("click", refresh);
    filterInput.addEventListener("input", onFilterInput);

    /* ------------------------------------------------------------- data */

    function refresh() {
      var url = dirUrl(state.segs);
      state.seq += 1;
      var token = state.seq;
      fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" })
        .then(function (res) {
          if (token !== state.seq) return;   // 已有更新的请求，过期响应作废
          if (!res.ok) {
            showError("读取失败（HTTP " + res.status + "）");
            return;
          }
          return res.json().catch(function () {
            if (token === state.seq) {
              showError("目录清单不是 JSON（下载功能可能被 SELKIES_FILE_TRANSFERS 关闭）");
            }
          }).then(function (json) {
            if (token !== state.seq || !json) return;
            var entries = [];
            for (var i = 0; i < json.length; i++) {
              var item = json[i];
              if (!item || typeof item.name !== "string") continue;
              if (item.name.charAt(0) === ".") continue;   // 与资源管理器默认隐藏隐藏项一致
              entries.push(item);
            }
            state.entries = entries;
            state.selected = -1;
            renderList();
          });
        })
        .catch(function () {
          if (token === state.seq) showError("无法连接服务器");
        });
    }
    function goUp() {
      if (!state.segs.length) return;
      state.segs = state.segs.slice(0, -1);
      state.selected = -1;
      refresh();
    }
    /* ------------------------------------------------------------ render */

    function sortClick(col) {
      return function () {
        if (state.sortCol === col) {
          state.sortDir = -state.sortDir;
        } else {
          state.sortCol = col;
          state.sortDir = 1;
        }
        state.selected = -1;
        renderList();
      };
    }
    function filteredEntries() {
      var f = state.filter.toLowerCase();
      var out = [];
      for (var i = 0; i < state.entries.length; i++) {
        var e = state.entries[i];
        if (!f || String(e.name).toLowerCase().indexOf(f) !== -1) out.push(e);
      }
      return out;
    }
    // 目录恒在文件之前（资源管理器行为），同级再按当前列排序。
    function compare(a, b) {
      var ad = a.type === "directory" ? 0 : 1;
      var bd = b.type === "directory" ? 0 : 1;
      if (ad !== bd) return ad - bd;
      var r = 0;
      if (state.sortCol === "name") {
        r = String(a.name).localeCompare(String(b.name), undefined,
          { numeric: true, sensitivity: "base" });
      } else if (state.sortCol === "mtime") {
        r = (Date.parse(a.mtime) || 0) - (Date.parse(b.mtime) || 0);
      } else if (state.sortCol === "size") {
        r = (Number(a.size) || 0) - (Number(b.size) || 0);
      } else if (state.sortCol === "type") {
        r = typeLabel(a).localeCompare(typeLabel(b));
      }
      return r * state.sortDir;
    }
    function renderList() {
      var items = filteredEntries();
      items.sort(compare);
      state.displayed = items;
      list.textContent = "";
      if (!items.length) {
        var empty = document.createElement("div");
        empty.className = "wfm-empty";
        empty.textContent = "此文件夹为空";
        list.appendChild(empty);
      }
      state.rows = [];
      for (var i = 0; i < items.length; i++) {
        state.rows.push(buildRow(items[i], i));
      }
      renderCrumbs();
      renderHeaders();
      updateStatus();
    }
    function renderCrumbs() {
      crumbs.textContent = "";
      // 第一段固定叫「桌面」；每段都可点跳转。
      for (var i = -1; i < state.segs.length; i++) {
        if (i >= 0) {
          var sep = document.createElement("span");
          sep.className = "wfm-crumb-sep";
          sep.textContent = " / ";
          crumbs.appendChild(sep);
        }
        var crumb = document.createElement("button");
        crumb.className = "wfm-crumb";
        crumb.textContent = i === -1 ? "桌面" : state.segs[i];
        crumb.addEventListener("click", (function (level) {
          return function () {
            state.segs = state.segs.slice(0, level);
            state.selected = -1;
            refresh();
          };
        })(i + 1));
        crumbs.appendChild(crumb);
      }
      upBtn.disabled = state.segs.length === 0;
    }
    function renderHeaders() {
      for (var i = 0; i < thList.length; i++) {
        var base = headers[i].label;
        thList[i].textContent = headers[i].key === state.sortCol ?
          base + (state.sortDir > 0 ? " ▲" : " ▼") : base;
      }
    }
    function buildRow(entry, index) {
      var row = document.createElement("div");
      row.className = "wfm-row";
      // 目录行不可拖出；文件行拖到宿主机 = 下载（Chromium DownloadURL）。
      if (entry.type !== "directory") {
        row.setAttribute("draggable", "true");
        row.addEventListener("dragstart", function (ev) {
          var abs = new URL(fileUrl(state.segs, entry.name), location.href).href;
          try {
            ev.dataTransfer.setData("DownloadURL",
              mimeFor(entry.name) + ":" + entry.name + ":" + abs);
            ev.dataTransfer.setData("text/uri-list", abs);
            ev.dataTransfer.setData("text/plain", abs);
            ev.dataTransfer.effectAllowed = "copy";
          } catch (e) {
            console.warn(TAG, "dragstart failed for", entry.name, e);
          }
        });
      }
      row.addEventListener("click", function () { selectRow(row, index); });
      row.addEventListener("dblclick", function () { activate(entry); });

      var cells = [
        { cls: "wfm-name", text: iconFor(entry) + " " + entry.name },
        { cls: "wfm-cell-mtime", text: formatMtime(entry.mtime) },
        { cls: "wfm-cell-type", text: typeLabel(entry) },
        { cls: "wfm-cell-size", text: formatSize(entry.size) }
      ];
      for (var c = 0; c < cells.length; c++) {
        var cell = document.createElement("div");
        cell.className = "wfm-cell " + cells[c].cls;
        cell.textContent = cells[c].text;
        row.appendChild(cell);
      }
      list.appendChild(row);
      return row;
    }
    function showError(message) {
      state.entries = [];
      state.displayed = [];
      state.rows = [];
      state.selected = -1;
      list.textContent = "";
      var line = document.createElement("div");
      line.className = "wfm-error";
      line.textContent = message;
      list.appendChild(line);
      var retry = document.createElement("button");
      retry.className = "wfm-btn";
      retry.textContent = "重试";
      retry.addEventListener("click", refresh);
      list.appendChild(retry);
      renderCrumbs();
      updateStatus();
    }
    /* -------------------------------------------------------- interaction */

    function selectRow(row, index) {
      if (state.selected >= 0 && state.rows[state.selected]) {
        state.rows[state.selected].classList.remove("wfm-selected");
      }
      state.selected = index;
      row.classList.add("wfm-selected");
      updateStatus();
    }
    function activate(entry) {
      if (entry.type === "directory") {
        state.segs = state.segs.concat([entry.name]);
        state.selected = -1;
        refresh();
      } else {
        downloadFile(entry);
      }
    }
    function downloadFile(entry) {
      // 服务端已强制 Content-Disposition: attachment，download 属性只是保险。
      var a = document.createElement("a");
      a.href = fileUrl(state.segs, entry.name);
      a.download = entry.name;
      document.body.appendChild(a);
      a.click();
      a.remove();
    }
    function moveSelection(delta) {
      var count = state.rows.length;
      if (!count) return;
      var next = Math.max(0, Math.min(count - 1, state.selected + delta));
      if (state.selected >= 0 && state.rows[state.selected]) {
        state.rows[state.selected].classList.remove("wfm-selected");
      }
      state.selected = next;
      state.rows[next].classList.add("wfm-selected");
      if (typeof state.rows[next].scrollIntoView === "function") {
        state.rows[next].scrollIntoView({ block: "nearest" });
      }
      updateStatus();
    }
    function onKeydown(ev) {
      // Backspace 与行数无关：空目录里也要能返回上级。
      if (ev.key === "Backspace") {
        ev.preventDefault();
        goUp();
        return;
      }
      if (!state.rows.length) return;
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        moveSelection(1);
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        moveSelection(-1);
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        if (state.selected >= 0) activate(state.displayed[state.selected]);
      }
    }
    function onFilterInput() {
      state.filter = filterInput.value;
      state.selected = -1;
      renderList();
    }
    function updateStatus() {
      var text = state.rows.length + " 个项目";
      var e = state.displayed[state.selected];
      if (e) text += "｜已选中 " + e.name + "（" + formatSize(e.size) + "）";
      status.textContent = text;
    }
    refresh();
  }
  /* ---------------------------------------------------------------- scan */

  function scan() {
    var modals = document.querySelectorAll(".files-modal");
    for (var i = 0; i < modals.length; i++) mount(modals[i]);
  }
  function boot() {
    scan();
    // React 每次开关 modal 都会重建节点，observer 负责接管新节点；
    // 定时扫描只是兜底（同目录脚本的既有模式）。
    new MutationObserver(scan).observe(document.documentElement, {
      childList: true, subtree: true
    });
    setInterval(scan, 2000);
    console.log(TAG, "installed (base " + BASE + ")");
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
