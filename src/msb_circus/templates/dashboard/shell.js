/* msb-circus browser shell: xterm.js front end for /api/shell.
   Plain ES2020, no modules; Terminal and FitAddon come from the UMD
   globals. All DOM changes go through createElement/textContent. */
/* global window, document, localStorage, WebSocket, Terminal, FitAddon */

"use strict";

(function () {
  var STORAGE_PREFIX = "msb-circus.";

  function el(id) {
    return document.getElementById(id);
  }

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function store(key, value) {
    try {
      localStorage.setItem(STORAGE_PREFIX + key, value);
    } catch (e) {
      /* ignore */
    }
  }

  function showMessage(nodes) {
    var box = el("shell-message");
    box.replaceChildren(...nodes);
    box.hidden = false;
  }

  function hideMessage() {
    el("shell-message").hidden = true;
  }

  var TOKEN_HELP =
    "This browser has no valid shell token. Open the dashboard with ";

  function showTokenHelp() {
    var wrap = make("span");
    wrap.append(
      make("span", null, TOKEN_HELP),
      make("code", null, "msb-circus dashboard"),
      make(
        "span",
        null,
        " (it adds the token to the address), then open the shell again."
      )
    );
    showMessage([wrap]);
  }

  var sandbox = new URLSearchParams(window.location.search).get("sandbox");
  if (!sandbox) {
    showMessage([make("span", null, "No sandbox given. Open the shell from the dashboard.")]);
    return;
  }
  el("shell-sandbox").textContent = sandbox;
  document.title = sandbox + " · shell";

  var token = null;
  try {
    token = localStorage.getItem(STORAGE_PREFIX + "shell-token");
  } catch (e) {
    /* localStorage unavailable */
  }
  if (!token) {
    showTokenHelp();
    return;
  }

  // ---- terminal -----------------------------------------------------------

  var styles = window.getComputedStyle(document.documentElement);
  function cssVar(name) {
    return styles.getPropertyValue(name).trim();
  }
  var dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  var term = new Terminal({
    cursorBlink: true,
    fontFamily: cssVar("--mono"),
    fontSize: parseInt(localStorage.getItem(STORAGE_PREFIX + "shell-font-size") || "14", 10),
    scrollback: 5000,
    convertEol: false,
    theme: {
      background: cssVar("--surface-1"),
      foreground: cssVar("--text-primary"),
      cursor: cssVar("--accent"),
      cursorAccent: cssVar("--surface-1"),
      selectionBackground: dark ? "rgba(57, 135, 229, 0.3)" : "rgba(42, 120, 214, 0.3)",
    },
  });
  var fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el("terminal"));
  fit.fit();

  var encoder = new TextEncoder();
  var ws = null;
  var exited = false;
  var lastError = null;
  var resizeTimer = null;

  function setStatus(statusClass, icon, label) {
    var node = el("shell-status");
    node.className = "st " + statusClass;
    node.replaceChildren(make("span", "ic", icon), make("span", null, " " + label));
  }

  function sendResize(size) {
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type: "resize", cols: size.cols, rows: size.rows }));
    }
  }

  // Registered once; they act on the current connection.
  term.onData(function (data) {
    if (ws && ws.readyState === 1) {
      ws.send(encoder.encode(data));
    }
  });
  term.onResize(function (size) {
    sendResize(size);
  });
  window.addEventListener("resize", function () {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(function () {
      fit.fit();
    }, 100);
  });

  var CLOSE_TEXTS = {
    1000: null, // handled via the exit message
    1002: "Connection error.",
    1008: "Connection error.",
    4404: "Sandbox is not running. Start it with: cd <workspace> && msb-circus up",
    4429: "Too many open shells (8). Close one and try again.",
    4503: "The shell is off. Restart the proxy with: msb-circus proxy --shell",
  };

  function onClosed(code) {
    setStatus("st-neutral", "○", "closed");
    term.write("\r\n\x1b[2m[connection closed]\x1b[0m\r\n");
    var text;
    if (code === 4401) {
      showTokenHelp();
      el("shell-reconnect").hidden = true;
      return;
    }
    if (code === 4503) {
      var wrap = make("span");
      wrap.append(
        make("span", null, "The shell is off. Restart the proxy with: "),
        make("code", null, "msb-circus proxy --shell")
      );
      showMessage([wrap]);
      el("shell-reconnect").hidden = true;
      return;
    }
    if (code === 4422 && lastError) {
      showMessage([make("span", null, lastError)]);
      el("shell-reconnect").hidden = false;
      return;
    }
    if (code === 1000 && lastExitCode !== null) {
      text = "Shell ended (exit " + lastExitCode + "). Reconnect?";
    } else {
      text = CLOSE_TEXTS[code] || "Connection error.";
    }
    showMessage([make("span", null, text)]);
    el("shell-reconnect").hidden = false;
  }

  var lastExitCode = null;

  function connect() {
    exited = false;
    lastError = null;
    lastExitCode = null;
    hideMessage();
    setStatus("st-neutral", "◐", "connecting…");
    var proto = window.location.protocol === "https:" ? "wss://" : "ws://";
    ws = new WebSocket(
      proto +
        window.location.host +
        "/api/shell?sandbox=" +
        encodeURIComponent(sandbox)
    );
    ws.binaryType = "arraybuffer";
    ws.onopen = function () {
      ws.send(JSON.stringify({ type: "hello", token: token, cols: term.cols, rows: term.rows }));
    };
    ws.onmessage = function (event) {
      if (event.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(event.data));
        return;
      }
      var data;
      try {
        data = JSON.parse(event.data);
      } catch (e) {
        return;
      }
      if (data.type === "ready") {
        setStatus("st-good", "✓", "connected");
        term.focus();
      } else if (data.type === "exit") {
        exited = true;
        lastExitCode = data.code;
        setStatus("st-neutral", "○", "exited (code " + data.code + ")");
      } else if (data.type === "error") {
        lastError = data.message;
      }
    };
    ws.onclose = function (event) {
      ws = null;
      onClosed(event.code);
    };
  }

  el("shell-reconnect").addEventListener("click", function () {
    term.reset();
    connect();
  });

  el("shell-font-down").addEventListener("click", function () {
    changeFontSize(-1);
  });
  el("shell-font-up").addEventListener("click", function () {
    changeFontSize(1);
  });

  function changeFontSize(delta) {
    var size = term.options.fontSize + delta;
    if (size < 10 || size > 24) return;
    term.options.fontSize = size;
    store("shell-font-size", String(size));
    fit.fit();
  }

  // Copy and paste on Linux and Windows (macOS uses Cmd+C/Cmd+V).
  term.attachCustomKeyEventHandler(function (event) {
    if (event.type !== "keydown") return true;
    if (event.ctrlKey && event.shiftKey && (event.key === "C" || event.key === "c")) {
      navigator.clipboard.writeText(term.getSelection());
      return false;
    }
    if (event.ctrlKey && event.shiftKey && (event.key === "V" || event.key === "v")) {
      navigator.clipboard.readText().then(function (text) {
        term.paste(text);
      });
      return false;
    }
    return true;
  });

  window.addEventListener("beforeunload", function (event) {
    if (ws && ws.readyState === 1 && !exited) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  connect();
})();
