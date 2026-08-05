/**
 * Keenetic admin live updates via wsServer (socket event "Keenetic").
 * Compatible with DataTables: rows on other pages are not in the DOM.
 */
(function () {
  if (typeof socket === "undefined") return;

  var i18n = window.KeeneticLiveI18n || {
    online: "Online",
    offline: "Offline",
  };

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

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function dash(text) {
    var v = String(text == null ? "" : text).trim();
    return v ? v : "—";
  }

  /** Find row even if DataTables paged it out of the document. */
  function findEntityRow(attrName, id) {
    var want = String(id);
    var inDom = document.querySelector("tr[" + attrName + '="' + want + '"]');
    if (inDom) return inDom;
    if (typeof jQuery === "undefined" || !jQuery.fn || !jQuery.fn.dataTable) return null;
    var found = null;
    jQuery("table").each(function () {
      if (found || !jQuery.fn.dataTable.isDataTable(this)) return;
      var nodes = jQuery(this).DataTable().rows().nodes();
      for (var i = 0; i < nodes.length; i++) {
        var node = nodes[i];
        if (node && node.getAttribute(attrName) === want) {
          found = node;
          break;
        }
      }
    });
    return found;
  }

  /** Tell DataTables to re-read cell text after DOM live updates. */
  function invalidateRow(row) {
    if (!row || typeof jQuery === "undefined" || !jQuery.fn.dataTable) return;
    var table = row.closest ? row.closest("table") : null;
    if (!table) {
      var p = row.parentElement;
      while (p && p.tagName !== "TABLE") p = p.parentElement;
      table = p;
    }
    if (!table || !jQuery.fn.dataTable.isDataTable(table)) return;
    try {
      jQuery(table).DataTable().row(row).invalidate("dom");
    } catch (e) {}
  }

  function setText(row, field, text) {
    var el = row.querySelector('[data-field="' + field + '"]');
    if (el) el.textContent = text == null ? "" : String(text);
  }

  function setOnline(row, online) {
    var el = row.querySelector('[data-field="online"]');
    if (!el) return;
    var on = online === true || online === 1 || online === "1";
    el.className = "badge " + (on ? "text-bg-success" : "text-bg-danger");
    el.textContent = on ? i18n.online : i18n.offline;
  }

  function isAccessDenied(access) {
    var v = String(access == null ? "" : access).trim().toLowerCase();
    return v === "deny" || v === "denied" || v === "false" || v === "0";
  }

  function setDeviceAccess(row, access) {
    var denied = isAccessDenied(access);
    row.setAttribute("data-access", String(access == null ? "" : access).trim().toLowerCase());
    row.querySelectorAll("td").forEach(function (cell) {
      cell.classList.toggle("bg-warning-subtle", denied);
    });
    var badge = row.querySelector('[data-field="access-denied"]');
    if (badge) badge.style.display = denied ? "" : "none";
    var permitBtn = row.querySelector('[data-access-action="permit"]');
    var denyBtn = row.querySelector('[data-access-action="deny"]');
    if (permitBtn) permitBtn.style.display = denied ? "" : "none";
    if (denyBtn) denyBtn.style.display = denied ? "none" : "";
  }

  function applyDevice(data) {
    if (!data || data.id == null) return;
    var row = findEntityRow("data-keenetic-device", data.id);
    if (!row) return;
    if ("online" in data) setOnline(row, data.online);
    if ("ip" in data) setText(row, "ip", data.ip || "");
    if ("rssi" in data) setText(row, "rssi", data.rssi == null || data.rssi === "" ? "" : data.rssi);
    if ("uptime" in data) setText(row, "uptime", fmtUptime(data.uptime));
    if ("rxbytes" in data || "txbytes" in data) {
      var trafficEl = row.querySelector('[data-field="traffic"]');
      if (trafficEl) {
        if ("rxbytes" in data) trafficEl.setAttribute("data-rx", String(data.rxbytes == null ? "" : data.rxbytes));
        if ("txbytes" in data) trafficEl.setAttribute("data-tx", String(data.txbytes == null ? "" : data.txbytes));
        trafficEl.textContent =
          "↓ " + fmtBytes(trafficEl.getAttribute("data-rx")) +
          " ↑ " + fmtBytes(trafficEl.getAttribute("data-tx"));
      }
    }
    if ("access" in data) setDeviceAccess(row, data.access);
    if ("updated" in data) setText(row, "updated", data.updated || "");
    invalidateRow(row);
  }

  function renderSessions(row, sessions, vpnId) {
    var body = row.querySelector('[data-field="sessions-body"]');
    if (!body) return;
    var list = Array.isArray(sessions) ? sessions : [];
    if (!list.length) {
      body.innerHTML =
        '<tr class="keenetic-sessions-empty"><td colspan="6" class="small text-muted">' +
        esc(i18n.no_clients || "No active clients") +
        "</td></tr>";
      return;
    }
    body.innerHTML = list
      .map(function (s) {
        if (!s) return "";
        var sid = s.session_id;
        var kick =
          sid == null || sid === ""
            ? ""
            : '<a href="?op=vpn_kick&vpn=' +
              encodeURIComponent(vpnId) +
              "&session=" +
              encodeURIComponent(sid) +
              '" class="btn btn-sm btn-outline-danger" title="' +
              esc(i18n.disconnect_client || "Disconnect client") +
              "\" onClick=\"return confirm('" +
              esc(i18n.disconnect_client_confirm || "Disconnect this VPN client?").replace(/'/g, "\\'") +
              "')\"><i class=\"fas fa-user-slash\"></i></a>";
        return (
          '<tr data-session-id="' +
          esc(sid == null ? "" : sid) +
          '">' +
          "<td>" +
          esc(dash(s.name)) +
          "</td>" +
          "<td>" +
          esc(dash(s.address)) +
          "</td>" +
          "<td>" +
          esc(dash(s.remote)) +
          "</td>" +
          '<td class="text-nowrap small" data-rx="' +
          esc(s.rxbytes || 0) +
          '" data-tx="' +
          esc(s.txbytes || 0) +
          '" data-field="session-traffic">↓ ' +
          fmtBytes(s.rxbytes) +
          " ↑ " +
          fmtBytes(s.txbytes) +
          "</td>" +
          '<td class="text-nowrap" data-field="session-uptime">' +
          esc(fmtUptime(s.uptime) || dash(s.uptime)) +
          "</td>" +
          '<td width="1%" nowrap>' +
          kick +
          "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  function applyVpn(data) {
    if (!data || data.id == null) return;
    var row = findEntityRow("data-keenetic-vpn", data.id);
    if (!row) return;
    if ("online" in data) setOnline(row, data.online);
    if ("ip" in data) setText(row, "ip", data.ip || "");
    if ("address" in data && !("ip" in data)) setText(row, "ip", data.address || "");
    if (row.getAttribute("data-role") === "server") {
      if ("sessions" in data) {
        renderSessions(row, data.sessions, data.id);
      }
    }
    if ("updated" in data) setText(row, "updated", data.updated || "");
    invalidateRow(row);
  }

  function applyRouter(data) {
    if (!data || data.id == null) return;
    var row = findEntityRow("data-keenetic-router", data.id);
    if (!row) return;
    if ("online" in data) setOnline(row, data.online);
    if ("cpu" in data) setText(row, "cpu", data.cpu == null || data.cpu === "" ? "" : data.cpu + "%");
    if ("ram" in data) setText(row, "ram", data.ram == null || data.ram === "" ? "" : data.ram + "%");
    if ("uptime" in data) setText(row, "uptime", fmtUptime(data.uptime));
    if ("firmware_version" in data) setText(row, "firmware_version", data.firmware_version || "");
    if ("updated" in data) setText(row, "updated", data.updated || "");
    if ("update_available" in data || "update_version" in data) {
      var badge = row.querySelector('[data-field="update_available"]');
      var applyBtn = row.querySelector('[data-field="apply_update"]');
      var avail = data.update_available;
      if (avail === undefined && badge) {
        avail = badge.getAttribute("data-available") === "1";
      }
      var on = avail === true || avail === 1 || avail === "1";
      if (badge) {
        badge.style.display = on ? "" : "none";
        if ("update_available" in data) badge.setAttribute("data-available", on ? "1" : "0");
        if ("update_version" in data) {
          if (data.update_version) {
            badge.textContent = (i18n.update_available || "Update available") + ": " + data.update_version;
          } else if (!on) {
            badge.textContent = i18n.update_available || "Update available";
          }
        }
      }
      if (applyBtn) applyBtn.style.display = on ? "" : "none";
    }
    invalidateRow(row);
  }

  function levelBadgeHtml(level) {
    var raw = String(level == null ? "" : level).trim();
    if (!raw) return "";
    var lvl = raw.toLowerCase();
    var cls = "text-bg-secondary";
    if (lvl === "error" || lvl === "critical" || lvl === "fatal" || lvl === "alert" || lvl === "emerg" || lvl === "emergency") {
      cls = "text-bg-danger";
    } else if (lvl === "warning" || lvl === "warn") {
      cls = "text-bg-warning";
    } else if (lvl === "notice") {
      cls = "text-bg-info";
    } else if (lvl === "debug" || lvl === "trace") {
      cls = "text-bg-secondary";
    }
    return '<span class="badge fw-medium text-lowercase ' + cls + '">' + esc(raw) + "</span>";
  }

  function appendLog(data) {
    if (!data || data.id == null) return;
    var pane = document.querySelector('[data-keenetic-log-router="' + data.id + '"]');
    if (!pane) return;
    var tableEl = pane.querySelector("table#keenetic-log-table") || pane.querySelector("table");
    var body = pane.querySelector('[data-field="log-body"]');
    if (!body && !tableEl) return;
    var limit = parseInt(pane.getAttribute("data-log-limit") || "200", 10) || 200;
    var entries = Array.isArray(data.entries) ? data.entries : [];
    var dt =
      tableEl &&
      typeof jQuery !== "undefined" &&
      jQuery.fn.dataTable &&
      jQuery.fn.dataTable.isDataTable(tableEl)
        ? jQuery(tableEl).DataTable()
        : null;

    function buildRow(e) {
      var tr = document.createElement("tr");
      var eid = e.id == null ? "" : String(e.id);
      if (eid) tr.setAttribute("data-log-id", eid);
      tr.innerHTML =
        '<td class="text-nowrap">' +
        esc(e.time || "") +
        "</td><td>" +
        levelBadgeHtml(e.level) +
        '</td><td class="small text-muted">' +
        esc(e.facility || "") +
        "</td><td>" +
        esc(e.message || "") +
        "</td>";
      return tr;
    }

    if (dt) {
      if (data.replace) {
        dt.clear();
      }
      if (!entries.length && data.replace) {
        dt.draw(false);
        updateLogCount(0);
        return;
      }
      var existing = {};
      dt.rows().every(function () {
        var node = this.node();
        if (!node) return;
        var id = node.getAttribute("data-log-id");
        if (id) existing[id] = true;
      });
      var added = [];
      for (var i = 0; i < entries.length; i++) {
        var e = entries[i];
        if (!e) continue;
        var eid = e.id == null ? "" : String(e.id);
        if (eid && existing[eid]) continue;
        added.push(buildRow(e));
        if (eid) existing[eid] = true;
      }
      for (var a = 0; a < added.length; a++) {
        dt.row.add(added[a]);
      }
      dt.order([[0, "desc"]]).draw(false);
      while (dt.rows().count() > limit) {
        var idxs = dt.rows({ order: "applied" }).indexes();
        var last = idxs[idxs.length - 1];
        dt.row(last).remove();
      }
      dt.draw(false);
      updateLogCount(dt.rows().count());
      return;
    }

    if (!body) return;
    if (data.replace) {
      body.innerHTML = "";
    }
    if (!entries.length && data.replace) {
      body.innerHTML =
        '<tr class="keenetic-log-empty"><td colspan="4" class="text-muted">' +
        esc(i18n.no_log_entries || "No log entries") +
        "</td></tr>";
      updateLogCount(0);
      return;
    }
    var empty = body.querySelector(".keenetic-log-empty");
    if (empty) empty.remove();
    for (var i = entries.length - 1; i >= 0; i--) {
      var e = entries[i];
      if (!e) continue;
      var eid = e.id == null ? "" : String(e.id);
      if (eid) {
        var found = false;
        for (var j = 0; j < body.children.length; j++) {
          if (body.children[j].getAttribute("data-log-id") === eid) {
            found = true;
            break;
          }
        }
        if (found) continue;
      }
      body.insertBefore(buildRow(e), body.firstChild);
    }
    while (body.children.length > limit) {
      body.removeChild(body.lastChild);
    }
    updateLogCount(body.querySelectorAll("tr[data-log-id]").length);
  }

  function updateLogCount(n) {
    var badge = document.querySelector('[data-field="log-count"]');
    if (!badge) return;
    badge.textContent = String(n || 0);
    badge.style.display = n > 0 ? "" : "none";
  }

  function onKeenetic(msg) {
    if (!msg || !msg.operation) return;
    var data = msg.data || {};
    if (msg.operation === "updateDevice") applyDevice(data);
    else if (msg.operation === "updateVpn") applyVpn(data);
    else if (msg.operation === "updateRouter") applyRouter(data);
    else if (msg.operation === "appendLog") appendLog(data);
  }

  socket.emit("subscribeData", ["Keenetic"]);
  socket.on("Keenetic", onKeenetic);

  window.addEventListener("beforeunload", function () {
    try {
      socket.emit("unsubscribeData", ["Keenetic"]);
      if (socket.off) socket.off("Keenetic", onKeenetic);
    } catch (e) {}
  });
})();
