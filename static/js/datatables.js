/**
 * Keenetic DataTables init: format cells, show spinner until initComplete.
 */
(function (window) {
  "use strict";

  function fmtBytes(v) {
    var n = Number(v);
    if (!isFinite(n) || n < 0) return "";
    if (n < 1024) return n + " B";
    var u = ["KB", "MB", "GB", "TB"];
    var i = -1;
    do {
      n /= 1024;
      i++;
    } while (n >= 1024 && i < u.length - 1);
    return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + " " + u[i];
  }

  function fmtUptime(v) {
    var n = Number(v);
    if (!isFinite(n) || n <= 0) return "";
    var s = Math.floor(n);
    var d = Math.floor(s / 86400);
    s %= 86400;
    var h = Math.floor(s / 3600);
    s %= 3600;
    var m = Math.floor(s / 60);
    s %= 60;
    if (d > 0) return d + "d " + h + "h";
    if (h > 0) return h + "h " + m + "m";
    if (m > 0) return m + "m " + s + "s";
    return s + "s";
  }

  function formatTableCells(root) {
    var scope = root || document;
    scope.querySelectorAll('td[data-field="traffic"], td[data-field="session-traffic"]').forEach(function (el) {
      el.textContent = "↓ " + fmtBytes(el.getAttribute("data-rx")) + " ↑ " + fmtBytes(el.getAttribute("data-tx"));
    });
    scope.querySelectorAll('td[data-field="uptime"], td[data-field="session-uptime"]').forEach(function (el) {
      var raw = (el.textContent || "").trim();
      if (/^\d+$/.test(raw)) el.textContent = fmtUptime(raw);
    });
  }

  function markDtReady(tableNode) {
    if (window.AsyncLoading) {
      AsyncLoading.revealClosest(tableNode);
      return;
    }
    var wrap = tableNode && tableNode.closest("[data-async-loading]");
    if (wrap) wrap.removeAttribute("data-async-loading");
  }

  function isDataTable(table) {
    return typeof jQuery !== "undefined" && jQuery.fn.dataTable && jQuery.fn.dataTable.isDataTable(table);
  }

  function init(selector, options) {
    var table = document.querySelector(selector);
    if (!table || isDataTable(table)) return null;

    var scope = table.closest("[data-async-loading]") || document;
    formatTableCells(scope);

    if (typeof jQuery === "undefined" || !jQuery.fn.dataTable) {
      markDtReady(table);
      return null;
    }

    options = options || {};
    options.columnDefs = (options.columnDefs || []).concat([
      { className: "text-start", targets: "_all" },
    ]);
    var userInitComplete = options.initComplete;
    options.initComplete = function (settings) {
      markDtReady(settings.nTable);
      if (typeof userInitComplete === "function") {
        userInitComplete.call(this, settings);
      }
    };
    return jQuery(selector).DataTable(options);
  }

  /** Init when table tab is visible; defer hidden Bootstrap tabs until shown. */
  function initWhenVisible(selector, options) {
    var table = document.querySelector(selector);
    if (!table || isDataTable(table)) return null;

    var pane = table.closest(".tab-pane");
    if (!pane || pane.classList.contains("active")) {
      return init(selector, options);
    }

    var tabId = pane.id;
    document.querySelectorAll('[aria-controls="' + tabId + '"]').forEach(function (btn) {
      btn.addEventListener(
        "shown.bs.tab",
        function () {
          init(selector, options);
        },
        { once: true }
      );
    });

    return null;
  }

  window.KeeneticDt = {
    init: init,
    initWhenVisible: initWhenVisible,
    formatCells: formatTableCells,
    fmtBytes: fmtBytes,
    fmtUptime: fmtUptime,
  };
})(window);
