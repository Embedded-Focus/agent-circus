/* msb-circus dashboard: polling and rendering. Plain ES2020, no
   framework. All DOM changes go through createElement/textContent (or
   replaceChildren); sandbox names and paths from msb are never parsed
   as HTML. */
/* global navigator, document, window, localStorage */

"use strict";

(function () {
  // ---- formatting helpers -------------------------------------------------

  /** Format bytes as B, kB, MB, GB (base 1000, one decimal). */
  function fmtBytes(n) {
    if (n === null || n === undefined) return "-";
    if (n < 1000) return n + " B";
    var units = ["kB", "MB", "GB", "TB"];
    var value = n;
    for (var i = 0; i < units.length; i++) {
      value = value / 1000;
      if (value < 1000 || i === units.length - 1) {
        return value.toFixed(1) + " " + units[i];
      }
    }
    return n + " B";
  }

  /** Format a duration in seconds compactly: 45 s, 3 m, 2 h, 4 d. */
  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined) return "-";
    if (seconds < 60) return Math.floor(seconds) + " s";
    if (seconds < 3600) return Math.floor(seconds / 60) + " m";
    if (seconds < 86400) return Math.floor(seconds / 3600) + " h";
    return Math.floor(seconds / 86400) + " d";
  }

  /** Format an age: "12 s ago". */
  function fmtAgo(seconds) {
    if (seconds === null || seconds === undefined) return "-";
    return fmtDuration(seconds) + " ago";
  }

  /** Format an epoch timestamp as local HH:MM:SS. */
  function fmtClock(epoch) {
    if (epoch === null || epoch === undefined) return "-";
    var d = new Date(epoch * 1000);
    function two(x) {
      return String(x).padStart(2, "0");
    }
    return two(d.getHours()) + ":" + two(d.getMinutes()) + ":" + two(d.getSeconds());
  }

  // ---- state --------------------------------------------------------------

  var STORAGE_PREFIX = "msb-circus.";

  var state = {
    data: null, // last parsed /api/state payload
    lastFetchAt: null, // epoch ms of the last successful fetch
    paused: false,
    failed: false, // last fetch errored
    backoff: 0, // current retry delay in ms
    tab: "active", // active | recent | routes
    theme: "auto", // auto | light | dark
    openCards: new Set(), // sandbox names with an open details section
  };

  function loadStored() {
    try {
      state.tab = localStorage.getItem(STORAGE_PREFIX + "tab") || state.tab;
      state.theme = localStorage.getItem(STORAGE_PREFIX + "theme") || state.theme;
      var stopped = localStorage.getItem(STORAGE_PREFIX + "show-stopped");
      var other = localStorage.getItem(STORAGE_PREFIX + "show-other");
      var filter = localStorage.getItem(STORAGE_PREFIX + "filter");
      var paused = localStorage.getItem(STORAGE_PREFIX + "paused");
      if (stopped !== null) el("show-stopped").checked = stopped === "1";
      if (other !== null) el("show-other").checked = other === "1";
      if (filter !== null) el("filter").value = filter;
      if (paused !== null) state.paused = paused === "1";
    } catch (e) {
      /* localStorage may be unavailable; defaults are fine. */
    }
  }

  function store(key, value) {
    try {
      localStorage.setItem(STORAGE_PREFIX + key, value);
    } catch (e) {
      /* ignore */
    }
  }

  // ---- DOM helpers --------------------------------------------------------

  function el(id) {
    return document.getElementById(id);
  }

  /** Create an element with optional class, text and title. */
  function make(tag, className, text, title) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    if (title) node.title = title;
    return node;
  }

  /** Status pill: an icon in the status colour plus a plain-text label. */
  function pill(statusClass, icon, label) {
    var span = make("span", "st " + statusClass);
    span.append(make("span", "ic", icon), make("span", "lbl", label));
    return span;
  }

  /** Copy button that flashes "Copied" for 1.5 s. */
  function copyButton(text) {
    var btn = make("button", "copy-btn", "copy");
    btn.type = "button";
    btn.addEventListener("click", function () {
      navigator.clipboard.writeText(text).then(
        function () {
          btn.textContent = "Copied";
          btn.classList.add("copied");
          setTimeout(function () {
            btn.textContent = "copy";
            btn.classList.remove("copied");
          }, 1500);
        },
        function () {
          /* clipboard unavailable; nothing to do */
        }
      );
    });
    return btn;
  }

  /** Copy button for a hint command, prefixed "cd … && " in a code box. */
  function hintCopy(command) {
    var wrap = make("span");
    var code = make("code", null, command);
    wrap.append(code, copyButton(command));
    return wrap;
  }

  // ---- rendering ----------------------------------------------------------

  var SVG_NS = "http://www.w3.org/2000/svg";

  /** Traffic sparkline with hover tooltip and error ticks. */
  function sparkline(buckets) {
    var W = 240;
    var H = 48;
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");
    var maxConn = 1;
    var totalConn = 0;
    var totalBytes = 0;
    var totalErrors = 0;
    buckets.forEach(function (b) {
      if (b.connections > maxConn) maxConn = b.connections;
      totalConn += b.connections;
      totalBytes += b.bytes;
      totalErrors += b.errors;
    });
    svg.setAttribute(
      "aria-label",
      "Traffic: " + totalConn + " connections, " + totalErrors +
        " errors, " + fmtBytes(totalBytes) + " in 10 minutes"
    );

    function x(i) {
      return (i / (buckets.length - 1)) * W;
    }
    function y(v) {
      return H - (v / maxConn) * (H - 4) - 2;
    }

    // Area fill at 15 % opacity.
    var area = document.createElementNS(SVG_NS, "polygon");
    var points = "0," + H;
    buckets.forEach(function (b, i) {
      points += " " + x(i).toFixed(2) + "," + y(b.connections).toFixed(2);
    });
    points += " " + W + "," + H;
    area.setAttribute("points", points);
    area.setAttribute("fill", "var(--accent)");
    area.setAttribute("fill-opacity", "0.15");
    svg.append(area);

    // 2 px line.
    var line = document.createElementNS(SVG_NS, "polyline");
    var lpoints = "";
    buckets.forEach(function (b, i) {
      lpoints +=
        x(i).toFixed(2) + "," + y(b.connections).toFixed(2) + " ";
    });
    line.setAttribute("points", lpoints.trim());
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", "var(--accent)");
    line.setAttribute("stroke-width", "2");
    line.setAttribute("stroke-linejoin", "round");
    svg.append(line);

    // Error ticks on the baseline.
    buckets.forEach(function (b, i) {
      if (b.errors > 0) {
        var tick = document.createElementNS(SVG_NS, "rect");
        tick.setAttribute("x", (x(i) - 1).toFixed(2));
        tick.setAttribute("y", H - 2);
        tick.setAttribute("width", "2");
        tick.setAttribute("height", "2");
        tick.setAttribute("fill", "var(--critical)");
        svg.append(tick);
      }
    });

    // Hover layer: transparent column per bucket plus tooltip/crosshair.
    var tooltip = make(
      "div",
      "spark-tooltip",
      null
    );
    tooltip.style.display = "none";
    tooltip.style.position = "absolute";
    tooltip.style.pointerEvents = "none";
    var crosshair = document.createElementNS(SVG_NS, "line");
    crosshair.setAttribute("y1", "0");
    crosshair.setAttribute("y2", String(H));
    crosshair.setAttribute("stroke", "var(--text-muted)");
    crosshair.setAttribute("stroke-width", "1");
    crosshair.style.display = "none";

    var holder = make("div", "spark-holder");
    holder.style.position = "relative";
    holder.append(svg, tooltip);

    buckets.forEach(function (b, i) {
      var rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("x", (x(i) - W / (buckets.length - 1) / 2).toFixed(2));
      rect.setAttribute("y", "0");
      rect.setAttribute("width", (W / (buckets.length - 1)).toFixed(2));
      rect.setAttribute("height", String(H));
      rect.setAttribute("fill", "transparent");
      rect.addEventListener("mouseenter", function () {
        tooltip.textContent =
          fmtClock(b.t) + " · " + b.connections + " connections · " +
          b.errors + " errors · " + fmtBytes(b.bytes);
        tooltip.style.display = "block";
        tooltip.style.left = Math.min((i / buckets.length) * 100, 70) + "%";
        tooltip.style.top = "0";
        crosshair.setAttribute("x1", x(i).toFixed(2));
        crosshair.setAttribute("x2", x(i).toFixed(2));
        crosshair.style.display = "";
      });
      rect.addEventListener("mouseleave", function () {
        tooltip.style.display = "none";
        crosshair.style.display = "none";
      });
      svg.append(rect);
    });
    svg.append(crosshair);
    return holder;
  }

  function renderKpis(data) {
    var sandboxes = data.sandboxes || [];
    var managed = sandboxes.filter(function (s) {
      return s.managed;
    });
    var running = managed.filter(function (s) {
      return s.running;
    });
    var stopped = managed.length - running.length;

    var ks = el("kpi-sandboxes");
    ks.replaceChildren(
      make("div", "kpi-label", "Sandboxes"),
      make("div", "kpi-value", running.length + " / " + managed.length),
      make(
        "div",
        "kpi-sub",
        stopped > 0 ? stopped + " stopped" : "all running"
      )
    );

    var daemons = [];
    running.forEach(function (s) {
      (s.daemons || []).forEach(function (d) {
        daemons.push(d);
      });
    });
    var healthy = daemons.filter(function (d) {
      return d.state === "running" && (!d.exposed || d.reachable !== false);
    }).length;
    var failed = daemons.filter(function (d) {
      return d.state === "failed";
    }).length;
    var kd = el("kpi-daemons");
    var kdSub = make("div", "kpi-sub", daemons.length + " daemons");
    if (failed > 0) {
      kdSub = make("div", "kpi-sub", "");
      kdSub.append(make("span", "failed", failed + " failed"));
    }
    kd.replaceChildren(
      make("div", "kpi-label", "Daemons"),
      make("div", "kpi-value", healthy + " / " + daemons.length),
      kdSub
    );

    var traffic = data.traffic || { active: [], series: { buckets: [] } };
    var buckets = (traffic.series && traffic.series.buckets) || [];
    var inTenMin = buckets.reduce(function (sum, b) {
      return sum + b.connections;
    }, 0);
    var ka = el("kpi-active");
    ka.replaceChildren(
      make("div", "kpi-label", "Active connections"),
      make("div", "kpi-value", String((traffic.active || []).length)),
      make("div", "kpi-sub", inTenMin + " in the last 10 min")
    );

    var kt = el("kpi-traffic");
    var totalBytes = buckets.reduce(function (sum, b) {
      return sum + b.bytes;
    }, 0);
    kt.replaceChildren(
      make("div", "kpi-label", "Traffic"),
      sparkline(buckets),
      make("div", "kpi-sub", fmtBytes(totalBytes) + " in 10 min")
    );
  }

  /** Sandbox headline pill from its router state and hints. */
  function sandboxPill(sandbox) {
    if (!sandbox.running) {
      return pill("st-neutral", "○", "Stopped");
    }
    var levels = (sandbox.hints || []).map(function (h) {
      return h.level;
    });
    if (levels.indexOf("error") >= 0) return pill("st-critical", "✕", "Problem");
    if (levels.indexOf("warning") >= 0) return pill("st-serious", "!", "Attention");
    return pill("st-good", "✓", "Running");
  }

  function daemonPill(d) {
    if (d.state === "running" && (!d.exposed || d.reachable !== false)) {
      return pill("st-good", "✓", d.exposed ? "running" : "running (hidden)");
    }
    if (d.state === "starting") return pill("st-warning", "◐", "starting");
    if (d.state === "stopping") return pill("st-warning", "◐", "stopping");
    if (d.state === "running" && d.reachable === false) {
      return pill("st-serious", "!", "no port");
    }
    if (d.state === "failed") return pill("st-critical", "✕", "failed");
    if (d.state === "stopped") return pill("st-neutral", "○", "stopped");
    return pill("st-neutral", "?", "unknown");
  }

  function renderSandboxCard(sandbox, now, filter, activeByRoute) {
    var card = make("article", "card");
    var head = make("div", "card-head");
    head.append(
      make("span", "card-name", sandbox.name),
      sandboxPill(sandbox),
      make("span", "spacer"),
      make(
        "span",
        "card-age",
        sandbox.created_at ? fmtAgo(now - sandbox.created_at) : "-"
      )
    );
    card.append(head);

    var ws = make("div", "card-workspace");
    if (sandbox.workspace) {
      ws.append(make("span", "path", sandbox.workspace), copyButton(sandbox.workspace));
    } else {
      ws.append(make("span", "path", "workspace unknown"));
    }
    card.append(ws);

    (sandbox.daemons || []).forEach(function (d) {
      var row = make("div", "daemon-row");
      row.append(daemonPill(d), make("span", "name", d.name));
      if (d.exposed && d.port !== null) {
        row.append(make("span", "port", ":" + d.port));
      }
      if (d.exposed && d.url) {
        var link = make("a", null, "open ↗", d.url);
        link.href = d.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        row.append(link);
      }
      if (d.uptime_s !== null && d.uptime_s !== undefined) {
        row.append(make("span", "port", "up " + fmtDuration(d.uptime_s)));
      }
      if (d.state === "running" && d.exposed) {
        var active = activeByRoute[sandbox.name + "/" + d.name];
        if (active !== undefined) {
          row.append(make("span", "port", active + " active conn"));
        }
      }
      card.append(row);
    });

    (sandbox.hints || []).forEach(function (hint) {
      var row = make("div", "hint-row " + hint.level);
      row.append(make("span", "text", hint.text), hintCopy(hint.command));
      card.append(row);
    });

    if (sandbox.ports && sandbox.ports.length > 0) {
      var fwd = make(
        "div",
        "forwards",
        "forwards: " +
          sandbox.ports
            .map(function (p) {
              return p.host + ":" + p.host_port + "->" + p.guest_port + "/" + p.protocol;
            })
            .join(", ")
      );
      card.append(fwd);
    }
    return card;
  }

  function matchesFilter(sandbox, filter) {
    if (!filter) return true;
    var needle = filter.toLowerCase();
    if (sandbox.name.toLowerCase().indexOf(needle) >= 0) return true;
    if (sandbox.workspace && sandbox.workspace.toLowerCase().indexOf(needle) >= 0) {
      return true;
    }
    return (sandbox.daemons || []).some(function (d) {
      return d.name.toLowerCase().indexOf(needle) >= 0;
    });
  }

  function renderSandboxes(data, now) {
    var filter = el("filter").value.trim();
    var showStopped = el("show-stopped").checked;
    var showOther = el("show-other").checked;
    var activeByRoute = {};
    ((data.traffic || {}).routes || []).forEach(function (r) {
      activeByRoute[r.sandbox + "/" + r.daemon] = r.active;
    });
    var visible = (data.sandboxes || []).filter(function (s) {
      if (!showOther && !s.managed) return false;
      if (!s.running && !showStopped) return false;
      return matchesFilter(s, filter);
    });
    var wrap = el("sandboxes");
    if (visible.length === 0) {
      wrap.replaceChildren(
        make("div", "empty", "No sandboxes match. Start one with: msb-circus up")
      );
      return;
    }
    wrap.replaceChildren(
      ...visible.map(function (s) {
        return renderSandboxCard(s, now, filter, activeByRoute);
      })
    );
  }

  /** HTTP status chip class from the state mapping. */
  function statusChip(record) {
    if (record.status === null || record.status === undefined) {
      return make("span", "chip", "-");
    }
    var cls = "";
    if (record.status === 101) {
      var ws = make("span", "chip", "101 WS");
      return ws;
    }
    if (record.status >= 500 || record.outcome === "rejected") cls = "st-critical";
    else if (record.status >= 400) cls = "st-warning";
    var chip = make("span", "chip " + cls, String(record.status));
    if (record.error) chip.title = record.error;
    return chip;
  }

  function connRow(record, now) {
    var tr = document.createElement("tr");
    if (record.outcome === "open") tr.className = "open-row";
    var time = make("td", null, fmtClock(record.started_at));
    var target = make(
      "td",
      "mono",
      record.daemon ? record.daemon + "." + record.sandbox : record.host || "-"
    );
    var request = make(
      "td",
      "request",
      (record.method || "-") + " " + (record.path || "-")
    );
    if (record.method && record.path) {
      request.title = record.method + " " + record.path;
    }
    var status = make("td");
    status.append(statusChip(record));
    var up = make("td", "num", fmtBytes(record.bytes_up));
    var down = make("td", "num", fmtBytes(record.bytes_down));
    var duration = make(
      "td",
      null,
      record.outcome === "open" ? "open " : fmtDuration(record.duration_s)
    );
    if (record.outcome === "open") {
      var pulse = make("span", "pulse");
      duration.prepend(pulse);
    }
    tr.append(time, target, request, status, up, down, duration);
    return tr;
  }

  var CONN_HEADERS = ["Time", "Target", "Request", "Status", "↑", "↓", "Duration"];

  function connTable(records, now) {
    var wrap = make("div", "table-wrap");
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    CONN_HEADERS.forEach(function (h, i) {
      headRow.append(make("th", i >= 4 && i <= 5 ? "num" : null, h));
    });
    thead.append(headRow);
    var tbody = document.createElement("tbody");
    records.forEach(function (r) {
      tbody.append(connRow(r, now));
    });
    table.append(thead, tbody);
    wrap.append(table);
    return wrap;
  }

  function routeTable(routes, now) {
    var wrap = make("div", "table-wrap");
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    ["Route", "Active", "Total", "Errors", "↑", "↓", "Last seen"].forEach(
      function (h, i) {
        headRow.append(make("th", i >= 1 && i <= 5 ? "num" : null, h));
      }
    );
    thead.append(headRow);
    var tbody = document.createElement("tbody");
    routes.forEach(function (r) {
      var tr = document.createElement("tr");
      tr.append(
        make("td", "mono", r.daemon + "." + r.sandbox),
        make("td", "num", String(r.active)),
        make("td", "num", String(r.total)),
        make("td", "num", String(r.errors)),
        make("td", "num", fmtBytes(r.bytes_up)),
        make("td", "num", fmtBytes(r.bytes_down)),
        make("td", null, r.last_at ? fmtAgo(now - r.last_at) : "-")
      );
      tbody.append(tr);
    });
    table.append(thead, tbody);
    wrap.append(table);
    return wrap;
  }

  function renderConnections(data, now) {
    var traffic = data.traffic || {};
    var tabs = el("conn-tabs");
    var tabDefs = [
      ["active", "Active (" + (traffic.active || []).length + ")"],
      ["recent", "Recent (" + (traffic.recent || []).length + ")"],
      ["routes", "By route"],
    ];
    tabs.replaceChildren(
      ...tabDefs.map(function (def) {
        var btn = make("button", "tab", def[1]);
        btn.type = "button";
        btn.setAttribute("role", "tab");
        btn.setAttribute("aria-selected", state.tab === def[0] ? "true" : "false");
        btn.addEventListener("click", function () {
          state.tab = def[0];
          store("tab", def[0]);
          renderConnections(state.data, Date.now() / 1000);
        });
        return btn;
      })
    );
    var target = el("connections");
    var now = Date.now() / 1000;
    if (state.tab === "routes") {
      var routes = traffic.routes || [];
      if (routes.length === 0) {
        target.replaceChildren(
          make("div", "empty", "No connections yet. Open a daemon URL from a sandbox card.")
        );
      } else {
        target.replaceChildren(routeTable(routes, now));
      }
      return;
    }
    var records = traffic[state.tab] || [];
    if (records.length === 0) {
      var emptyText =
        state.tab === "active"
          ? "No open connections."
          : "No connections yet. Open a daemon URL from a sandbox card.";
      target.replaceChildren(make("div", "empty", emptyText));
      return;
    }
    target.replaceChildren(connTable(records, now));
  }

  function renderFooter(data) {
    el("footer").replaceChildren(
      make("span", "mono", data.proxy.url_pattern),
      make("span", null, "v" + data.proxy.version),
      make("span", null, "local only")
    );
    el("proxy-label").textContent = "proxy :" + data.proxy.port;
  }

  function renderBanner(data) {
    var banner = el("banner");
    if (state.failed) {
      banner.hidden = false;
      banner.className = "banner serious";
      banner.textContent =
        "Lost contact with the proxy. Is msb-circus proxy still running?";
      return;
    }
    if (data && data.refresh_error) {
      banner.hidden = false;
      banner.className = "banner warning";
      banner.textContent = data.refresh_error;
      return;
    }
    banner.hidden = true;
  }

  function renderLive() {
    var live = el("live-status");
    if (state.failed) {
      live.className = "live st-serious";
      live.replaceChildren(make("span", "ic dot", "!"), make("span", null, " offline"));
    } else {
      live.className = "live";
      live.replaceChildren(make("span", "ic dot", "●"), make("span", null, " live"));
    }
  }

  function renderUpdatedAt() {
    var node = el("updated-at");
    if (state.lastFetchAt === null) {
      node.textContent = "";
      return;
    }
    var age = (Date.now() - state.lastFetchAt) / 1000;
    node.textContent = "updated " + Math.floor(age) + " s ago";
    node.className = "updated" + (age > 15 ? " st-warning" : "");
  }

  function renderAll() {
    var data = state.data;
    if (!data) return;
    var now = Date.now() / 1000;
    renderBanner(data);
    renderLive();
    renderKpis(data);
    renderSandboxes(data, now);
    renderConnections(data, now);
    renderFooter(data);
  }

  // ---- theme --------------------------------------------------------------

  function applyTheme() {
    if (state.theme === "auto") {
      delete document.documentElement.dataset.theme;
    } else {
      document.documentElement.dataset.theme = state.theme;
    }
  }

  el("theme-toggle").addEventListener("click", function () {
    var order = ["auto", "light", "dark"];
    state.theme = order[(order.indexOf(state.theme) + 1) % order.length];
    store("theme", state.theme);
    applyTheme();
  });

  // ---- pause --------------------------------------------------------------

  function renderPause() {
    el("pause-toggle").textContent = state.paused ? "▶" : "⏸";
    el("pause-toggle").title = state.paused ? "Resume polling" : "Pause polling";
  }

  el("pause-toggle").addEventListener("click", function () {
    state.paused = !state.paused;
    store("paused", state.paused ? "1" : "0");
    renderPause();
    if (!state.paused) scheduleFetch(0);
  });

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && !state.paused) scheduleFetch(0);
  });

  // ---- filter inputs ------------------------------------------------------

  el("filter").addEventListener("input", function () {
    store("filter", el("filter").value);
    if (state.data) renderSandboxes(state.data, Date.now() / 1000);
  });
  el("show-stopped").addEventListener("change", function () {
    store("show-stopped", el("show-stopped").checked ? "1" : "0");
    if (state.data) renderSandboxes(state.data, Date.now() / 1000);
  });
  el("show-other").addEventListener("change", function () {
    store("show-other", el("show-other").checked ? "1" : "0");
    if (state.data) renderSandboxes(state.data, Date.now() / 1000);
  });

  // ---- polling ------------------------------------------------------------

  var POLL_MS = 2000;
  var MAX_BACKOFF_MS = 10000;
  var fetchTimer = null;

  function scheduleFetch(delay) {
    if (fetchTimer !== null) window.clearTimeout(fetchTimer);
    fetchTimer = window.setTimeout(poll, delay === undefined ? POLL_MS : delay);
  }

  function poll() {
    if (state.paused || document.hidden) return;
    fetch("/api/state", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        state.data = data;
        state.lastFetchAt = Date.now();
        state.failed = false;
        state.backoff = 0;
        renderAll();
        renderUpdatedAt();
        scheduleFetch();
      })
      .catch(function () {
        state.failed = true;
        state.backoff = state.backoff === 0 ? 2000 : Math.min(state.backoff * 2, MAX_BACKOFF_MS);
        renderLive();
        renderBanner(state.data);
        scheduleFetch(state.backoff);
      });
  }

  // "updated N s ago" ticks once per second.
  setInterval(renderUpdatedAt, 1000);

  // ---- start ---------------------------------------------------------------

  loadStored();
  applyTheme();
  renderPause();
  poll();
})();
