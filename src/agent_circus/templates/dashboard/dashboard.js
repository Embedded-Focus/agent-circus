/* agent-circus dashboard: polling and rendering. Plain ES2020, no
   framework. All DOM changes go through createElement/textContent (or
   replaceChildren); sandbox names and paths from msb are never parsed
   as HTML. */
/* global navigator, document, window, localStorage */

"use strict";

(function () {
  // ---- browser protocol version -------------------------------------------

  // Copy of the version in agent_circus.browser_protocol (the
  // authoritative source). Increase it there and here only for a
  // breaking protocol change.
  var BROWSER_PROTOCOL_VERSION = 1;
  var INCOMPATIBLE_PROTOCOL_MESSAGE =
    "This page is incompatible with the running proxy. Reload the page.";

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

  var STORAGE_PREFIX = "agent-circus.";

  var state = {
    data: null, // last parsed /api/state payload
    lastFetchAt: null, // epoch ms of the last successful fetch
    paused: false,
    failed: false, // last fetch errored
    protocolError: null, // fatal protocol mismatch message, stops polling
    backoff: 0, // current retry delay in ms
    tab: "active", // active | recent | routes
    theme: "auto", // auto | light | dark
    openCards: new Set(), // sandbox names with an open details section
    route: { view: "overview" }, // or { view: "detail", sandbox: name }
    layout: "list", // list | cards (overview only)
  };

  // ---- API fetch and protocol check ---------------------------------------

  /** Fetch an API URL with the protocol header and no caching. */
  function fetchApi(url) {
    return fetch(url, {
      cache: "no-store",
      headers: {
        "X-Agent-Circus-Protocol-Version": String(BROWSER_PROTOCOL_VERSION),
      },
    });
  }

  /** True when a state answer speaks this page's protocol version. */
  function isCompatibleState(data) {
    return !!data && data.protocol_version === BROWSER_PROTOCOL_VERSION;
  }

  /** Enter the fatal mismatch state. Only a page reload recovers. */
  function setProtocolError(received) {
    state.protocolError = INCOMPATIBLE_PROTOCOL_MESSAGE;
  }

  // ---- shell token and buttons ----------------------------------------------

  /** Store a token from the address hash and strip it from the URL. */
  function takeTokenFromHash() {
    var match = /(?:^#|[&?])token=([A-Za-z0-9_-]{32,})/.exec(window.location.hash || "");
    if (!match) return;
    try {
      localStorage.setItem(STORAGE_PREFIX + "shell-token", match[1]);
    } catch (e) {
      return;
    }
    var hash = window.location.hash;
    var start = match.index;
    var end = start + match[0].length;
    // Drop a separator (& or ?) directly before the token, if any.
    if (start > 0 && (hash[start - 1] === "&" || hash[start - 1] === "?")) {
      start -= 1;
    }
    var newHash = hash.slice(0, start) + hash.slice(end);
    window.history.replaceState(null, "", window.location.pathname +
      window.location.search + newHash);
  }

  function hasShellToken() {
    try {
      return !!localStorage.getItem(STORAGE_PREFIX + "shell-token");
    } catch (e) {
      return false;
    }
  }

  function shellEnabled(data) {
    return !!(data && data.proxy && data.proxy.shell && data.proxy.shell.enabled);
  }

  /** Shell link for a running managed sandbox (or a disabled button). */
  function shellButton(sandboxName, data) {
    if (!shellEnabled(data)) return null;
    if (!hasShellToken()) {
      var disabled = make("button", "copy-btn", "Shell");
      disabled.type = "button";
      disabled.disabled = true;
      disabled.title = "Open the dashboard with agent-circus dashboard to enable the shell";
      return disabled;
    }
    var link = make("a", "copy-btn", "Shell");
    link.href = "/shell?sandbox=" + encodeURIComponent(sandboxName);
    link.target = "_blank";
    link.rel = "noopener";
    return link;
  }

  // ---- routing --------------------------------------------------------------

  /** Parse the location hash into a route object. */
  function parseRoute(hash) {
    var match = /^#\/sandbox\/(.+)$/.exec(hash || "");
    if (!match) return { view: "overview" };
    try {
      return { view: "detail", sandbox: decodeURIComponent(match[1]) };
    } catch (e) {
      return { view: "overview" };
    }
  }

  /** URL of a sandbox's detail view. */
  function detailHref(name) {
    return "#/sandbox/" + encodeURIComponent(name);
  }

  /** Apply state.route to the DOM and restart polling for its scope. */
  function applyRoute() {
    takeTokenFromHash();
    var route = parseRoute(window.location.hash);
    var scopeChanged =
      route.view !== state.route.view ||
      (route.view === "detail" && route.sandbox !== state.route.sandbox);
    state.route = route;
    el("view-overview").hidden = route.view !== "overview";
    el("view-detail").hidden = route.view !== "detail";
    document.title =
      route.view === "detail"
        ? route.sandbox + " · agent-circus dashboard"
        : "agent-circus dashboard";
    window.scrollTo(0, 0);
    if (scopeChanged) state.data = null;
    scheduleFetch(0);
  }

  window.addEventListener("hashchange", applyRoute);

  function loadStored() {
    try {
      state.tab = localStorage.getItem(STORAGE_PREFIX + "tab") || state.tab;
      state.theme = localStorage.getItem(STORAGE_PREFIX + "theme") || state.theme;
      var stopped = localStorage.getItem(STORAGE_PREFIX + "show-stopped");
      var other = localStorage.getItem(STORAGE_PREFIX + "show-other");
      var filter = localStorage.getItem(STORAGE_PREFIX + "filter");
      var paused = localStorage.getItem(STORAGE_PREFIX + "paused");
      var layout = localStorage.getItem(STORAGE_PREFIX + "layout");
      if (stopped !== null) el("show-stopped").checked = stopped === "1";
      if (other !== null) el("show-other").checked = other === "1";
      if (filter !== null) el("filter").value = filter;
      if (paused !== null) state.paused = paused === "1";
      if (layout === "cards" || layout === "list") state.layout = layout;
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

  /** Hint rows of a sandbox (same look in cards and detail view). */
  function hintRows(sandbox) {
    var rows = make("div", "hint-rows");
    (sandbox.hints || []).forEach(function (hint) {
      var row = make("div", "hint-row " + hint.level);
      row.append(make("span", "text", hint.text), hintCopy(hint.command));
      rows.append(row);
    });
    return rows;
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
    var healthy = daemons.filter(isHealthy).length;
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

  /** Healthy daemon: running and (not exposed or port answers). */
  function isHealthy(d) {
    return d.state === "running" && (!d.exposed || d.reachable !== false);
  }

  function daemonPill(d) {
    if (isHealthy(d)) {
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
    var nameLink = make("a", "card-name", sandbox.name, "Open the detail view");
    nameLink.href = detailHref(sandbox.name);
    head.append(
      nameLink,
      sandboxPill(sandbox)
    );
    var shell = shellButton(sandbox.name, state.data);
    if (shell) head.append(shell);
    head.append(
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
      if (sandbox.running) {
        row.append(logsButton(sandbox.name, d.name));
      }
      card.append(row);
    });

    (sandbox.hints || []).forEach(function (hint) {
      var row = make("div", "hint-row " + hint.level);
      row.append(make("span", "text", hint.text), hintCopy(hint.command));
      card.append(row);
    });
    // (hint rows are built inline here to keep the card flat; see hintRows)

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

    if (sandbox.running) {
      var logRow = make("div", "daemon-row");
      logRow.append(logsButton(sandbox.name, "_router", "Router log"));
      card.append(logRow);
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

  // ---- list view ------------------------------------------------------------

  /** Small 80x20 sparkline of connections per bucket. */
  function miniSparkline(counts, label) {
    var W = 80;
    var H = 20;
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("width", "80");
    svg.setAttribute("height", "20");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", label);
    var max = 1;
    counts.forEach(function (c) {
      if (c > max) max = c;
    });
    var allZero = counts.every(function (c) {
      return c === 0;
    });
    var line = document.createElementNS(SVG_NS, "polyline");
    var points = "";
    counts.forEach(function (c, i) {
      var x = (i / (counts.length - 1)) * W;
      var y = allZero ? H - 1 : H - (c / max) * (H - 2) - 1;
      points += x.toFixed(2) + "," + y.toFixed(2) + " ";
    });
    line.setAttribute("points", points.trim());
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", allZero ? "var(--border)" : "var(--accent)");
    line.setAttribute("stroke-width", "1.5");
    line.setAttribute("stroke-linejoin", "round");
    svg.append(line);
    return svg;
  }

  /** Worst daemon state of a sandbox, as a list icon (or null). */
  function worstDaemonIcon(daemons) {
    var states = daemons.map(function (d) {
      return d.state === "running" && d.exposed && d.reachable === false
        ? "no-port"
        : d.state;
    });
    if (states.indexOf("failed") >= 0) return ["✕", "st-critical", "a daemon failed"];
    if (states.indexOf("no-port") >= 0) return ["!", "st-serious", "a daemon port is closed"];
    if (states.indexOf("starting") >= 0 || states.indexOf("stopping") >= 0) {
      return ["◐", "st-warning", "a daemon is starting or stopping"];
    }
    return null;
  }

  /** Worst hint level of a sandbox, as an icon + count (or null). */
  function worstHint(hints) {
    if (!hints || hints.length === 0) return null;
    var errors = hints.filter(function (h) {
      return h.level === "error";
    }).length;
    if (errors > 0) return ["✕", "st-critical", errors, hints[0].text];
    var warnings = hints.filter(function (h) {
      return h.level === "warning";
    }).length;
    if (warnings > 0) return ["!", "st-serious", warnings, hints[0].text];
    return ["i", "st-good", hints.length, hints[0].text];
  }

  function renderSandboxList(visible, data, now) {
    var bySandbox = (data.traffic || {}).by_sandbox || {};
    var wrap = make("div", "table-wrap");
    var table = document.createElement("table");
    table.className = "sandbox-list";
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    [
      "Status",
      "Name",
      "Workspace",
      "Daemons",
      "Problems",
      "Active",
      "Traffic",
      "Age",
      "",
    ].forEach(function (h, i) {
      var cls = null;
      if (i === 5) cls = "num";
      if (i === 2 || i === 6) cls = "col-wide";
      headRow.append(make("th", cls, h));
    });
    thead.append(headRow);
    var tbody = document.createElement("tbody");
    visible.forEach(function (s) {
      var tr = document.createElement("tr");
      tr.className = "clickable";
      tr.addEventListener("click", function (event) {
        if (event.target.closest("a, button")) return;
        window.location.hash = detailHref(s.name);
      });

      var statusCell = make("td");
      statusCell.append(sandboxPill(s));

      var nameCell = make("td");
      var nameLink = make("a", "mono", s.name, "Open the detail view");
      nameLink.href = detailHref(s.name);
      nameCell.append(nameLink);

      var wsCell = make("td", "mono col-wide", s.workspace || "-");
      if (s.workspace) wsCell.title = s.workspace;

      var daemonCell = make("td");
      if (!s.running) {
        daemonCell.append(make("span", null, "-"));
      } else {
        var daemons = s.daemons || [];
        var healthy = daemons.filter(isHealthy).length;
        daemonCell.append(
          make("span", null, healthy + " / " + daemons.length)
        );
        var worst = worstDaemonIcon(daemons);
        if (worst) {
          daemonCell.append(make("span", "ic " + worst[1], worst[0], worst[2]));
        }
      }

      var problems = make("td");
      var hint = worstHint(s.hints);
      if (hint) {
        problems.append(
          make("span", "ic " + hint[1], hint[0] + " " + hint[2], hint[3])
        );
      }

      var summary = bySandbox[s.name];
      var activeCell = make("td", "num", String(summary ? summary.active : 0));

      var trafficCell = make("td", "col-wide");
      var spark = summary ? summary.spark : null;
      var counts = spark && spark.length === 60 ? spark : new Array(60).fill(0);
      trafficCell.append(
        miniSparkline(
          counts,
          (summary ? summary.connections : 0) + " connections in 10 minutes"
        )
      );

      var ageCell = make(
        "td",
        null,
        s.created_at ? fmtAgo(now - s.created_at) : "-"
      );

      var moreCell = make("td");
      var moreLink = make("a", null, "Details →", "Open the detail view");
      moreLink.href = detailHref(s.name);
      moreCell.append(moreLink);
      var shell = shellButton(s.name, state.data);
      if (shell) moreCell.append(shell);

      tr.append(
        statusCell,
        nameCell,
        wsCell,
        daemonCell,
        problems,
        activeCell,
        trafficCell,
        ageCell,
        moreCell
      );
      tbody.append(tr);
    });
    table.append(thead, tbody);
    wrap.append(table);
    return wrap;
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
      wrap.className = "cards";
      wrap.replaceChildren(
        make("div", "empty", "No sandboxes match. Start one with: agent-circus up")
      );
      return;
    }
    if (state.layout === "cards") {
      wrap.className = "cards";
      wrap.replaceChildren(
        ...visible.map(function (s) {
          return renderSandboxCard(s, now, filter, activeByRoute);
        })
      );
    } else {
      wrap.className = "list";
      wrap.replaceChildren(renderSandboxList(visible, data, now));
    }
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

  function connRow(record, now, opts) {
    var tr = document.createElement("tr");
    if (record.outcome === "open") tr.className = "open-row";
    var time = make("td", null, fmtClock(record.started_at));
    var target = make("td", "mono");
    if (record.daemon && record.sandbox) {
      target.append(make("span", null, record.daemon + "."));
      if (opts.showSandbox) {
        var link = make("a", null, record.sandbox, "Open the detail view");
        link.href = detailHref(record.sandbox);
        target.append(link);
      } else {
        target.append(make("span", null, record.sandbox));
      }
    } else {
      target.append(make("span", null, record.host || "-"));
    }
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

  function connTable(records, now, opts) {
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
      tbody.append(connRow(r, now, opts));
    });
    table.append(thead, tbody);
    wrap.append(table);
    return wrap;
  }

  function routeTable(routes, now, opts) {
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
      var route = make("td", "mono");
      route.append(make("span", null, r.daemon + "."));
      if (opts.showSandbox) {
        var link = make("a", null, r.sandbox, "Open the detail view");
        link.href = detailHref(r.sandbox);
        route.append(link);
      } else {
        route.append(make("span", null, r.sandbox));
      }
      tr.append(
        route,
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

  /** Render the connection tabs and table into the given elements. */
  function renderConnections(traffic, tabsEl, targetEl, opts) {
    var tabDefs = [
      ["active", "Active (" + (traffic.active || []).length + ")"],
      ["recent", "Recent (" + (traffic.recent || []).length + ")"],
      ["routes", "By route"],
    ];
    tabsEl.replaceChildren(
      ...tabDefs.map(function (def) {
        var btn = make("button", "tab", def[1]);
        btn.type = "button";
        btn.setAttribute("role", "tab");
        btn.setAttribute("aria-selected", state.tab === def[0] ? "true" : "false");
        btn.addEventListener("click", function () {
          state.tab = def[0];
          store("tab", def[0]);
          renderConnections(traffic, tabsEl, targetEl, opts);
        });
        return btn;
      })
    );
    var now = Date.now() / 1000;
    if (state.tab === "routes") {
      var routes = traffic.routes || [];
      if (routes.length === 0) {
        targetEl.replaceChildren(
          make("div", "empty", opts.recentEmptyText)
        );
      } else {
        targetEl.replaceChildren(routeTable(routes, now, opts));
      }
      return;
    }
    var records = traffic[state.tab] || [];
    if (records.length === 0) {
      var emptyText =
        state.tab === "active" ? "No open connections." : opts.recentEmptyText;
      targetEl.replaceChildren(make("div", "empty", emptyText));
      return;
    }
    targetEl.replaceChildren(connTable(records, now, opts));
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
    if (state.protocolError) {
      banner.hidden = false;
      banner.className = "banner serious";
      banner.textContent = state.protocolError;
      return;
    }
    if (state.failed) {
      banner.hidden = false;
      banner.className = "banner serious";
      banner.textContent =
        "Lost contact with the proxy. Is agent-circus proxy still running?";
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
    var badge = el("shell-badge");
    if (state.data && shellEnabled(state.data)) {
      var sessions = ((state.data.proxy || {}).shell || {}).sessions || [];
      if (sessions.length > 0) {
        badge.hidden = false;
        badge.textContent = "▣ " + sessions.length + " shell" +
          (sessions.length === 1 ? "" : "s");
      } else {
        badge.hidden = true;
      }
    } else {
      badge.hidden = true;
    }
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
    renderFooter(data);
    if (state.route.view === "detail") {
      renderDetail(data, now);
      return;
    }
    renderKpis(data);
    renderSandboxes(data, now);
    renderConnections(
      data.traffic || {},
      el("conn-tabs"),
      el("connections"),
      { showSandbox: true, recentEmptyText:
        "No connections yet. Open a daemon URL from a sandbox's details." }
    );
  }

  // ---- detail view ----------------------------------------------------------

  var ROUTER_STATE_WORDS = {
    ok: "ok",
    stopped: "stopped",
    "no-router": "no router port",
    unreachable: "not answering",
    outdated: "outdated",
  };

  function renderDetail(data, now) {
    var name = state.route.sandbox;
    var sandbox = (data.sandboxes || []).find(function (s) {
      return s.name === name;
    });
    var missing = el("detail-missing");
    var body = el("detail-body");
    if (!sandbox) {
      body.hidden = true;
      missing.hidden = false;
      var back = make("a", null, "Back to all sandboxes");
      back.href = "#/";
      missing.replaceChildren(
        make(
          "div",
          null,
          "Sandbox " + name + " does not exist (any more). It may have " +
            "been removed."
        ),
        back
      );
      return;
    }
    missing.hidden = true;
    body.hidden = false;

    el("detail-name").textContent = name;
    el("detail-title").textContent = name;
    el("detail-pill").replaceChildren(sandboxPill(sandbox));
    var pillCell = el("detail-pill");
    var pillShell = shellButton(name, data);
    if (pillShell) pillCell.append(" ", pillShell);
    renderDetailMeta(sandbox, now, data);

    var hintsEl = el("detail-hints");
    hintsEl.replaceChildren(hintRows(sandbox));

    renderDetailDaemons(sandbox, data.traffic || {});
    renderDetailTraffic(data, sandbox, now);
    renderDetailPorts(sandbox);
  }

  function metaPair(label, ddNode) {
    var dt = make("dt", null, label);
    var dd = make("dd");
    dd.append(ddNode);
    return [dt, dd];
  }

  function renderDetailMeta(sandbox, now, data) {
    var dl = el("detail-meta");
    var pairs = [];
    pairs.push(...metaPair("Status", make("span", null, sandbox.status || "-")));
    if (sandbox.created_at) {
      var created = make(
        "span",
        null,
        fmtAgo(now - sandbox.created_at),
        new Date(sandbox.created_at * 1000).toLocaleString()
      );
      pairs.push(...metaPair("Created", created));
    }
    if (sandbox.workspace) {
      var ws = make("span");
      ws.append(make("span", "mono", sandbox.workspace), copyButton(sandbox.workspace));
      pairs.push(...metaPair("Workspace", ws));
    } else {
      pairs.push(...metaPair("Workspace", make("span", null, "unknown")));
    }
    var router = sandbox.router || {};
    var routerCell = make("span");
    routerCell.append(
      make("span", null, ROUTER_STATE_WORDS[router.state] || router.state || "-")
    );
    if (router.address) {
      routerCell.append(make("span", "mono", " " + router.address));
    }
    if (router.started_at) {
      routerCell.append(
        make("span", null, " · up " + fmtDuration(now - router.started_at))
      );
    }
    pairs.push(...metaPair("Router", routerCell));
    pairs.push(
      ...metaPair(
        "Kind",
        make("span", null, sandbox.managed ? "agent-circus sandbox" : "other msb sandbox")
      )
    );
    if (shellEnabled(data)) {
      var sessions = ((data.proxy || {}).shell || {}).sessions || [];
      var mine = sessions.filter(function (s) {
        return s.sandbox === sandbox.name;
      });
      var shellCell = make("span");
      shellCell.append(
        make("span", null, String(mine.length)),
        make(
          "span",
          null,
          mine.length
            ? " (" +
                mine
                  .map(function (s) {
                    return fmtClock(s.started_at);
                  })
                  .join(", ") +
                ")"
            : ""
        )
      );
      pairs.push(...metaPair("Shells", shellCell));
    } else if (sandbox.managed && sandbox.running) {
      var off = make("span");
      off.append(
        make("span", null, "Browser shell is off. Restart the proxy with: ")
      );
      off.append(make("code", null, "agent-circus proxy --shell"));
      off.append(copyButton("agent-circus proxy --shell"));
      pairs.push(...metaPair("Shells", off));
    }
    dl.replaceChildren(...pairs);
  }

  function renderDetailDaemons(sandbox, traffic) {
    var target = el("detail-daemons");
    if (!sandbox.running) {
      target.replaceChildren(
        make("div", "empty", "Daemon details need a running sandbox.")
      );
      return;
    }
    var daemons = sandbox.daemons || [];
    if (daemons.length === 0) {
      target.replaceChildren(
        make(
          "div",
          "empty",
          "No daemons registered. Add [[daemons]] to the workspace " +
            "config and run agent-circus up."
        )
      );
      return;
    }
    var activeByRoute = {};
    (traffic.routes || []).forEach(function (r) {
      activeByRoute[r.sandbox + "/" + r.daemon] = r.active;
    });
    var wrap = make("div", "table-wrap");
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    ["State", "Name", "Port", "URL", "PID", "Uptime", "Port answers", "Active"].forEach(
      function (h, i) {
        headRow.append(make("th", h === "Active" ? "num" : null, h));
      }
    );
    headRow.append(make("th"));
    thead.append(headRow);
    var tbody = document.createElement("tbody");
    daemons.forEach(function (d) {
      var tr = document.createElement("tr");
      var stateCell = make("td");
      stateCell.append(daemonPill(d));
      var port = make("td", "mono", d.exposed && d.port !== null ? ":" + d.port : "-");
      var urlCell = make("td");
      if (d.exposed && d.url) {
        var link = make("a", null, "open ↗", d.url);
        link.href = d.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        urlCell.append(link);
      } else {
        urlCell.append(make("span", null, "not exposed"));
      }
      var answers = make(
        "td",
        null,
        d.exposed ? (d.reachable === true ? "yes" : d.reachable === false ? "no" : "-") : "-"
      );
      var active = make(
        "td",
        "num",
        String(activeByRoute[sandbox.name + "/" + d.name] || 0)
      );
      var logsCell = make("td");
      logsCell.append(logsButton(sandbox.name, d.name));
      tr.append(
        stateCell,
        make("td", "mono", d.name),
        port,
        urlCell,
        make("td", "num", d.pid === null || d.pid === undefined ? "-" : String(d.pid)),
        make("td", null, d.uptime_s === null || d.uptime_s === undefined
          ? "-"
          : fmtDuration(d.uptime_s)),
        answers,
        active,
        logsCell
      );
      tbody.append(tr);
    });
    table.append(thead, tbody);
    wrap.append(table);
    var under = make("div", "daemon-under");
    under.append(logsButton(sandbox.name, "_router", "Router log"));
    target.replaceChildren(wrap, under);
  }

  function renderDetailTraffic(data, sandbox, now) {
    var traffic = data.traffic || { active: [], series: { buckets: [] } };
    var buckets = (traffic.series && traffic.series.buckets) || [];
    var inTenMin = buckets.reduce(function (sum, b) {
      return sum + b.connections;
    }, 0);
    var errors = buckets.reduce(function (sum, b) {
      return sum + b.errors;
    }, 0);
    var totalBytes = buckets.reduce(function (sum, b) {
      return sum + b.bytes;
    }, 0);
    el("detail-kpis").replaceChildren(
      kpiTile(
        "Active connections",
        String((traffic.active || []).length),
        null
      ),
      kpiTile("Connections", String(inTenMin), errors + " errors"),
      kpiTile("Traffic", sparkline(buckets), fmtBytes(totalBytes) + " in 10 min")
    );
    renderConnections(
      traffic,
      el("detail-conn-tabs"),
      el("detail-connections"),
      {
        showSandbox: false,
        recentEmptyText: "No connections yet. Open a daemon URL from the table above.",
      }
    );
  }

  /** One KPI tile: label, value node, optional sub-line text. */
  function kpiTile(label, value, subText) {
    var tile = make("div", "kpi");
    tile.append(make("div", "kpi-label", label));
    if (typeof value === "string") {
      tile.append(make("div", "kpi-value", value));
    } else {
      tile.append(value);
    }
    if (subText) tile.append(make("div", "kpi-sub", subText));
    return tile;
  }

  function renderDetailPorts(sandbox) {
    var target = el("detail-ports");
    var ports = sandbox.ports || [];
    if (ports.length === 0) {
      target.replaceChildren(make("div", "empty", "No published ports."));
      return;
    }
    var wrap = make("div", "table-wrap");
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    ["Host", "Host port", "Guest port", "Protocol"].forEach(function (h, i) {
      headRow.append(make("th", i >= 1 && i <= 2 ? "num" : null, h));
    });
    thead.append(headRow);
    var tbody = document.createElement("tbody");
    ports.forEach(function (p) {
      var tr = document.createElement("tr");
      tr.append(
        make("td", "mono", p.host),
        make("td", "num", String(p.host_port)),
        make("td", "num", String(p.guest_port)),
        make("td", null, p.protocol)
      );
      tbody.append(tr);
    });
    table.append(thead, tbody);
    wrap.append(table);
    target.replaceChildren(
      wrap,
      make(
        "div",
        "muted-note",
        "The router port is not listed; the proxy uses it internally."
      )
    );
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

  // ---- layout switch --------------------------------------------------------

  function renderLayoutSwitch() {
    el("layout-switch")
      .querySelectorAll("button[data-layout]")
      .forEach(function (btn) {
        btn.setAttribute(
          "aria-pressed",
          btn.dataset.layout === state.layout ? "true" : "false"
        );
      });
  }

  el("layout-switch")
    .querySelectorAll("button[data-layout]")
    .forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.layout = btn.dataset.layout;
        store("layout", state.layout);
        renderLayoutSwitch();
        if (state.data) renderSandboxes(state.data, Date.now() / 1000);
      });
    });

  // ---- log viewer ---------------------------------------------------------

  var logTarget = null; // { sandbox, daemon }

  function openLogDialog(sandbox, daemon) {
    logTarget = { sandbox: sandbox, daemon: daemon };
    el("log-title").textContent =
      daemon === "_router"
        ? "Router log — " + sandbox
        : "Daemon log — " + daemon + " (" + sandbox + ")";
    el("log-body").textContent = "Loading…";
    el("log-dialog").showModal();
    fetchLog();
  }

  function fetchLog() {
    if (!logTarget) return;
    var lines = el("log-lines").value;
    var url =
      "/api/logs?sandbox=" + encodeURIComponent(logTarget.sandbox) +
      "&daemon=" + encodeURIComponent(logTarget.daemon) +
      "&lines=" + encodeURIComponent(lines);
    el("log-body").textContent = "Loading…";
    fetchApi(url)
      .then(function (response) {
        if (response.status === 409) {
          throw new Error(INCOMPATIBLE_PROTOCOL_MESSAGE);
        }
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.text();
      })
      .then(function (text) {
        el("log-body").textContent = text || "(empty)";
      })
      .catch(function (e) {
        el("log-body").textContent = "Could not load the log: " + e.message;
      });
  }

  el("log-refresh").addEventListener("click", fetchLog);
  el("log-lines").addEventListener("change", fetchLog);
  el("log-close").addEventListener("click", function () {
    el("log-dialog").close();
  });

  /** Small button that opens the log dialog for a daemon. */
  function logsButton(sandbox, daemon, label) {
    var btn = make("button", "copy-btn", label || "Logs");
    btn.type = "button";
    btn.addEventListener("click", function () {
      openLogDialog(sandbox, daemon);
    });
    return btn;
  }

  // ---- polling ------------------------------------------------------------

  var POLL_MS = 2000;
  var MAX_BACKOFF_MS = 10000;
  var fetchTimer = null;

  function scheduleFetch(delay) {
    if (fetchTimer !== null) window.clearTimeout(fetchTimer);
    fetchTimer = window.setTimeout(poll, delay === undefined ? POLL_MS : delay);
  }

  function poll() {
    if (state.protocolError) return; // a mismatch is fatal, only a reload recovers
    if (state.paused || document.hidden) return;
    var scope =
      state.route.view === "detail"
        ? encodeURIComponent(state.route.sandbox)
        : null;
    var url = scope === null ? "/api/state" : "/api/state?sandbox=" + scope;
    fetchApi(url)
      .then(function (response) {
        if (response.status === 409) {
          // The proxy reports an incompatible protocol before any work.
          return response.json().then(function (body) {
            setProtocolError(body && body.received);
          });
        }
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        if (state.protocolError) {
          renderBanner(state.data);
          return; // do not schedule another poll
        }
        if (!isCompatibleState(data)) {
          // The answer is missing or speaks another protocol version;
          // its fields may not match this page's expectations.
          setProtocolError(data && data.protocol_version);
          renderBanner(state.data);
          return;
        }
        // Drop the answer when the user navigated meanwhile.
        var currentScope =
          state.route.view === "detail" ? state.route.sandbox : null;
        if ((data.scope || null) !== currentScope) {
          scheduleFetch();
          return;
        }
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
  renderLayoutSwitch();
  applyRoute();
})();
