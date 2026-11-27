"""Static guards for the dashboard assets (no JS test runner here)."""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from agent_circus.browser_protocol import (
    BROWSER_PROTOCOL_HEADER,
    BROWSER_PROTOCOL_SUBPROTOCOL,
    BROWSER_PROTOCOL_VERSION,
)

DASHBOARD_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agent_circus"
    / "templates"
    / "dashboard"
)

METRICS_JS = DASHBOARD_DIR / "dashboard-metrics.js"

REQUIRED_IDS = [
    "live-status",
    "updated-at",
    "theme-toggle",
    "pause-toggle",
    "kpi-sandboxes",
    "kpi-daemons",
    "kpi-active",
    "kpi-traffic",
    "filter",
    "show-stopped",
    "show-other",
    "sandboxes",
    "conn-tabs",
    "connections",
    "footer",
    "banner",
    "view-overview",
    "view-detail",
    "detail-back",
    "detail-name",
    "detail-missing",
    "detail-body",
    "detail-title",
    "detail-pill",
    "detail-meta",
    "detail-hints",
    "detail-resources",
    "detail-daemons",
    "detail-kpis",
    "detail-conn-tabs",
    "detail-connections",
    "detail-ports",
    "layout-switch",
]

#: Skips Node-based tests when Node.js is not installed.
needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


def _run_history_test(script: str) -> str:
    """Run a Node script against the real dashboard-metrics.js module.

    The script receives the module path as argv[2] and must require it
    itself, so the tests always execute the shipped implementation.

    :param script: JavaScript source of the test.
    :returns: The stdout of the script.
    :raises AssertionError: If Node exits non-zero.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(script)
        path = fh.name
    try:
        result = subprocess.run(
            ["node", path, str(METRICS_JS)],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


#: Common prologue of a history test: require the module and provide
#: payload builders matching the /api/state contract.
HISTORY_PROLOGUE = """
const assert = require("assert");
// The module is an IIFE that publishes its namespace on globalThis;
// requiring it runs the real implementation.
require(require("path").resolve(process.argv[2]));
const H = globalThis.AgentCircusMetricHistory;
assert(H && typeof H.createHistory === "function");

function state(sandboxes, startedAt) {
  return { proxy: { started_at: startedAt === undefined ? 1000 : startedAt }, sandboxes };
}

function view(name, sampledAt, overrides) {
  const resources = Object.assign(
    {
      sample_state: "running",
      sampled_at: sampledAt,
      cpu_percent: 80,
      cpu_count: 2,
      memory_used_bytes: 512,
      memory_available_bytes: 512,
      memory_limit_bytes: 1024,
      disks: [
        { id: "root", label: "Root disk", mount: "/",
          used_bytes: 100, available_bytes: 300, total_bytes: 400 },
        { id: "docker", label: "Docker disk", mount: "/var/lib/docker",
          used_bytes: 10, available_bytes: 90, total_bytes: 100 },
      ],
    },
    overrides || {}
  );
  return { name, resources };
}
"""


def _js() -> str:
    return (DASHBOARD_DIR / "dashboard.js").read_text()


def _index() -> str:
    return (DASHBOARD_DIR / "index.html").read_text()


def test_dashboard_js_never_uses_html_injection():
    js = _js()
    for needle in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert needle not in js, f"dashboard.js must not use {needle}"


def test_index_has_no_inline_script_or_style():
    index = _index()
    assert '<script src="dashboard.js"' in index
    # No inline <script> without a src attribute.
    assert "<script>" not in index
    # No style attributes and no inline event handlers.
    assert "style=" not in index
    import re

    assert not re.search(r"\son[a-z]+=", index)


def test_index_contains_required_ids():
    index = _index()
    for required in REQUIRED_IDS:
        assert f'id="{required}"' in index, f"missing #{required}"


def test_dashboard_js_routes_by_hash():
    js = _js()
    for needle in (
        "hashchange",
        "#/sandbox/",
        "encodeURIComponent",
        "decodeURIComponent",
    ):
        assert needle in js, f"dashboard.js does not handle routing: {needle}"


def test_dashboard_js_has_list_and_cards_layouts():
    js = _js()
    for needle in ("data-layout", '"list"', '"cards"', "miniSparkline", "by_sandbox"):
        assert needle in js, f"dashboard.js lacks list/cards support: {needle}"


def test_dashboard_js_reads_contract_keys():
    js = _js()
    for key in (
        "sandboxes",
        "traffic",
        "series",
        "buckets",
        "hints",
        "refresh_error",
        "scope",
    ):
        assert key in js, f"dashboard.js does not read {key}"


def test_dashboard_js_protocol_version_matches_python():
    js = _js()
    match = re.search(r"var BROWSER_PROTOCOL_VERSION = (\d+);", js)
    assert match, "dashboard.js has no BROWSER_PROTOCOL_VERSION"
    assert int(match.group(1)) == BROWSER_PROTOCOL_VERSION


def test_dashboard_js_sends_protocol_header_for_api_requests():
    js = _js()
    assert f'"{BROWSER_PROTOCOL_HEADER}"' in js
    # Both API call sites (state poll and log fetch) go through the
    # fetchApi helper, which adds the header and no-store caching.
    # The count includes the helper definition itself.
    assert js.count("fetchApi(url)") == 3, "an API request bypasses the fetchApi helper"


def test_dashboard_js_has_fatal_protocol_mismatch_path():
    js = _js()
    assert "setProtocolError" in js
    assert "isCompatibleState" in js
    assert "This page is incompatible with the running proxy. Reload the page." in js


def test_dashboard_js_both_api_paths_use_fatal_rejection_helper():
    """A 409 from state polling and from the log dialog must both set
    the fatal state; a log path that only throws would keep polling."""
    js = _js()
    assert "function handleProtocolRejection(response)" in js
    assert "handleProtocolRejection(response)" in js
    # Both call sites go through the helper (plus its definition).
    assert js.count("handleProtocolRejection(response)") == 3
    # The helper itself sets the fatal state and renders the banner.
    helper_at = js.index("function handleProtocolRejection")
    helper_body = js[helper_at : js.index("}", helper_at)]
    assert "setProtocolError()" in helper_body
    assert "renderBanner(state.data)" in helper_body
    # No call site keeps the old direct 409 comparison.
    assert js.count("response.status === 409") == 0


@needs_node
def test_dashboard_js_parses():
    result = subprocess.run(
        ["node", "--check", str(DASHBOARD_DIR / "dashboard.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@needs_node
def test_dashboard_metrics_js_parses():
    result = subprocess.run(
        ["node", "--check", str(METRICS_JS)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_index_loads_metrics_module_before_dashboard_js():
    index = _index()
    metrics_at = index.index('src="dashboard-metrics.js"')
    dashboard_at = index.index('src="dashboard.js"')
    assert metrics_at < dashboard_at, (
        "dashboard-metrics.js must load before dashboard.js"
    )


def test_dashboard_js_records_history_before_rendering():
    js = _js()
    assert "recordHistory(data)" in js
    record_at = js.index("recordHistory(data);")
    render_at = js.index("renderAll();", record_at)
    assert render_at > record_at
    assert "AgentCircusMetricHistory" in js


def test_dashboard_js_renders_resource_charts():
    js = _js()
    for needle in (
        "metricRow",
        "renderCardResources",
        "renderDetailResources",
        "metricListText",
        "diskListRow",
        "percentChart",
        "metrics_error",
    ):
        assert needle in js, f"dashboard.js lacks resource rendering: {needle}"


# ---- pure helpers (executed in Node.js) -------------------------------------


@needs_node
def test_cpu_usage_helper_splits_capacity_and_core_use():
    out = _run_history_test(
        HISTORY_PROLOGUE
        + """
// 80 across 2 cores: 0.8 busy cores, 40 percent of the capacity.
assert.deepEqual(H.cpuUsage(80, 2), { used_cores: 0.8, capacity_percent: 40 });
assert.deepEqual(H.cpuUsage(100, 1), { used_cores: 1, capacity_percent: 100 });
// Unusable counts give no capacity result.
assert.equal(H.cpuUsage(80, undefined), null);
assert.equal(H.cpuUsage(80, null), null);
assert.equal(H.cpuUsage(80, 0), null);
assert.equal(H.cpuUsage(80, true), null);
assert.equal(H.cpuUsage(80, NaN), null);
assert.equal(H.cpuUsage(80, Infinity), null);
// An unusable percent does not produce NaN either.
assert.equal(H.cpuUsage(undefined, 2), null);
assert.equal(H.cpuUsage(true, 2), null);
assert.equal(H.cpuUsage(NaN, 2), null);
console.log("ok");
"""
    )
    assert "ok" in out


@needs_node
def test_chart_time_bounds_spans_fixed_retention_window():
    out = _run_history_test(
        HISTORY_PROLOGUE
        + """
// The window is the retention length, not the sample span.
const bounds = H.chartTimeBounds([{ t: 100 }, { t: 103 }]);
assert.deepEqual(bounds, { start: -497, end: 103 });
assert.equal(bounds.end - bounds.start, H.RETENTION_S);
console.log("ok");
"""
    )
    assert "ok" in out


@needs_node
def test_chart_time_bounds_keeps_sparse_points_near_right_edge():
    out = _run_history_test(
        HISTORY_PROLOGUE
        + """
// Mirrors percentChart's x mapping on a 560 px detail chart.
const W = 560;
const points = [{ t: 100 }, { t: 103 }];
const bounds = H.chartTimeBounds(points);
function x(t) { return ((t - bounds.start) / (bounds.end - bounds.start)) * W; }
// The newest point is at the right edge; the older one is close by.
assert.equal(x(103), W);
assert.ok(x(100) > W * 0.99, "older point must stay near the right edge");
console.log("ok");
"""
    )
    assert "ok" in out


@needs_node
def test_chart_time_bounds_empty_series_is_finite_and_nonzero():
    out = _run_history_test(
        HISTORY_PROLOGUE
        + """
const bounds = H.chartTimeBounds([]);
assert.equal(Number.isFinite(bounds.start), true);
assert.equal(Number.isFinite(bounds.end), true);
assert.notEqual(bounds.start, bounds.end);
console.log("ok");
"""
    )
    assert "ok" in out


# ---- browser history module (executed in Node.js) ----------------------------


@needs_node
def test_history_deduplicates_cached_responses():
    out = _run_history_test(
        HISTORY_PROLOGUE
        + """
const h = H.createHistory();
h.record(state([view("a", 100)]));
h.record(state([view("a", 100)]));  // same sampled_at: cached duplicate
assert.equal(h.series("a", "cpu").length, 1);
h.record(state([view("a", 103)]));
assert.equal(h.series("a", "cpu").length, 2);
console.log("ok");
"""
    )
    assert "ok" in out


@needs_node
def test_history_returns_all_series_with_correct_percentages():
    out = _run_history_test(
        HISTORY_PROLOGUE
        + """
const h = H.createHistory();
h.record(state([view("a", 100)]));
const cpu = h.series("a", "cpu");
assert.equal(cpu.length, 1);
// CPU percent is the share of the allocated cores: 80 across 2 cores.
assert.equal(cpu[0].percent, 40);
assert.equal(cpu[0].used_bytes, null);
assert.equal(cpu[0].available_bytes, null);
assert.equal(cpu[0].total_bytes, null);
assert.equal(cpu[0].gap_before, false);
const mem = h.series("a", "memory");
assert.equal(mem[0].percent, 50);  // 100 * 512 / 1024
assert.equal(mem[0].used_bytes, 512);
assert.equal(mem[0].available_bytes, 512);
assert.equal(mem[0].total_bytes, 1024);
const root = h.series("a", "disk:root");
assert.equal(root[0].percent, 25);  // 100 * 100 / (100 + 300)
assert.equal(root[0].total_bytes, 400);
const docker = h.series("a", "disk:docker");
assert.equal(docker[0].percent, 10);
console.log("ok");
"""
    )
    assert "ok" in out


@needs_node
def test_history_percent_null_without_valid_denominator():
    out = _run_history_test(
        HISTORY_PROLOGUE
        + """
const h = H.createHistory();
h.record(state([
  view("a", 100, { cpu_count: null }),
  view("b", 100, { memory_used_bytes: 512, memory_limit_bytes: 0, memory_available_bytes: null }),
  view("c", 100, { disks: [{ id: "root", used_bytes: 0, available_bytes: 0, total_bytes: 0 }] }),
]));
assert.equal(h.series("a", "cpu")[0].percent, null);
assert.equal(h.series("b", "memory")[0].percent, null);
assert.equal(h.series("c", "disk:root")[0].percent, null);
console.log("ok");
"""
    )
    assert "ok" in out


@needs_node
def test_history_missing_data_gives_no_series():
    out = _run_history_test(
        HISTORY_PROLOGUE
        + """
const h = H.createHistory();
h.record(state([
  { name: "bare" },  // no resources object
  view("partial", 100, { disks: [] }),
]));
assert.deepEqual(h.series("bare", "cpu"), []);
assert.deepEqual(h.series("unknown", "cpu"), []);
assert.deepEqual(h.series("partial", "disk:root"), []);
assert.equal(h.series("partial", "cpu").length, 1);
console.log("ok");
"""
    )
    assert "ok" in out


@needs_node
def test_history_skips_stalled_samples_and_marks_gaps():
    out = _run_history_test(
        HISTORY_PROLOGUE
        + """
const h = H.createHistory();
h.record(state([view("a", 100)]));
h.record(state([view("a", 110, { sample_state: "stalled" })]));
assert.equal(h.series("a", "cpu").length, 1);  // stalled values are not live samples
h.record(state([view("a", 160)]));  // 60 s after the last live sample
const series = h.series("a", "cpu");
assert.equal(series.length, 2);
assert.equal(series[1].gap_before, true);
assert.equal(series[1].t, 160);
// A dense sample is not a gap.
h.record(state([view("a", 163)]));
assert.equal(h.series("a", "cpu")[2].gap_before, false);
console.log("ok");
"""
    )
    assert "ok" in out


@needs_node
def test_history_retention_and_point_cap():
    out = _run_history_test(
        HISTORY_PROLOGUE
        + """
const h = H.createHistory();
// 71 points, 10 s apart, ending at t=700: only t >= 100 survive.
for (let t = 0; t <= 700; t += 10) h.record(state([view("a", t)]));
const series = h.series("a", "cpu");
assert.equal(series.length, 61);
assert.equal(series[0].t, 100);
assert.equal(series[series.length - 1].t, 700);
// The hard cap applies before the retention window is full.
const h2 = H.createHistory();
for (let t = 0; t < 320; t += 1) h2.record(state([view("b", t)]));
assert.equal(h2.series("b", "cpu").length, 300);
assert.equal(h2.series("b", "cpu")[0].t, 20);
console.log("ok");
"""
    )
    assert "ok" in out


@needs_node
def test_history_limits_overview_series():
    out = _run_history_test(
        HISTORY_PROLOGUE
        + """
const h = H.createHistory();
for (let t = 0; t < 100; t += 1) h.record(state([view("a", t)]));
assert.equal(h.series("a", "cpu").length, 100);  // no limit: all retained
assert.equal(h.series("a", "cpu", 60).length, 60);
assert.equal(h.series("a", "cpu", 60)[0].t, 40);  // last 60 of 100 points
console.log("ok");
"""
    )
    assert "ok" in out


@needs_node
def test_history_removes_deleted_sandboxes():
    out = _run_history_test(
        HISTORY_PROLOGUE
        + """
const h = H.createHistory();
h.record(state([view("a", 100), view("b", 100)]));
assert.equal(h.series("b", "cpu").length, 1);
h.record(state([view("a", 103)]));  // b disappeared from the state
assert.deepEqual(h.series("b", "cpu"), []);
assert.equal(h.series("a", "cpu").length, 2);
console.log("ok");
"""
    )
    assert "ok" in out


@needs_node
def test_history_resets_after_proxy_restart():
    out = _run_history_test(
        HISTORY_PROLOGUE
        + """
const h = H.createHistory();
h.record(state([view("a", 100)], 1000));
assert.equal(h.series("a", "cpu").length, 1);
h.record(state([view("a", 103)], 2000));  // proxy restarted
// The restart clears the old lifetime; the revealing sample is fresh
// data of the new proxy, so it stays.
assert.equal(h.series("a", "cpu").length, 1);
assert.equal(h.series("a", "cpu")[0].t, 103);
h.record(state([view("a", 106)], 2000));
assert.equal(h.series("a", "cpu").length, 2);
console.log("ok");
"""
    )
    assert "ok" in out


@needs_node
def test_history_stores_are_independent():
    out = _run_history_test(
        HISTORY_PROLOGUE
        + """
const h1 = H.createHistory();
const h2 = H.createHistory();
h1.record(state([view("a", 100)]));
assert.equal(h2.series("a", "cpu").length, 0);
console.log("ok");
"""
    )
    assert "ok" in out


SHELL_DIR = DASHBOARD_DIR


def _shell_js() -> str:
    return (SHELL_DIR / "shell.js").read_text()


def _shell_html() -> str:
    return (SHELL_DIR / "shell.html").read_text()


def _favicon_bytes() -> bytes:
    return (DASHBOARD_DIR / "favicon.png").read_bytes()


def test_shell_js_never_uses_html_injection():
    js = _shell_js()
    for needle in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert needle not in js, f"shell.js must not use {needle}"


def test_shell_html_has_no_inline_script_or_style():
    import re

    html = _shell_html()
    assert "<script>" not in html
    assert "style=" not in html
    assert not re.search(r"\son[a-z]+=", html)
    for required in (
        "shell-back",
        "shell-sandbox",
        "shell-status",
        "shell-theme",
        "shell-font-down",
        "shell-font-up",
        "shell-reconnect",
        "shell-message",
        "terminal",
    ):
        assert f'id="{required}"' in html, f"missing #{required}"


def test_shell_js_sends_hello_with_token_and_size():
    js = _shell_js()
    for needle in (
        '"hello"',
        "token",
        "cols",
        "rows",
        "binaryType",
        "protocol_version",
    ):
        assert needle in js, f"shell.js does not send {needle}"


def test_shell_js_protocol_version_matches_python():
    js = _shell_js()
    match = re.search(r"var BROWSER_PROTOCOL_VERSION = (\d+);", js)
    assert match, "shell.js has no BROWSER_PROTOCOL_VERSION"
    assert int(match.group(1)) == BROWSER_PROTOCOL_VERSION


def test_shell_js_subprotocol_matches_python():
    js = _shell_js()
    match = re.search(r'var BROWSER_PROTOCOL_SUBPROTOCOL = "([^"]+)";', js)
    assert match, "shell.js has no BROWSER_PROTOCOL_SUBPROTOCOL"
    assert match.group(1) == BROWSER_PROTOCOL_SUBPROTOCOL


def test_shell_js_compares_subprotocol_before_hello():
    js = _shell_js()
    # The page requests the subprotocol in the WebSocket constructor.
    constructor_at = js.index("new WebSocket(")
    assert (
        "BROWSER_PROTOCOL_SUBPROTOCOL"
        in js[constructor_at : js.index(";", constructor_at)]
    )
    # ws.protocol is compared in onopen, and the mismatch path closes
    # with a return before the first ws.send call.
    onopen_at = js.index("ws.onopen")
    send_at = js.index("ws.send(", onopen_at)
    onopen_before_send = js[onopen_at:send_at]
    assert "ws.protocol !== BROWSER_PROTOCOL_SUBPROTOCOL" in onopen_before_send
    assert "ws.close(" in onopen_before_send
    assert "return;" in onopen_before_send


def test_shell_js_has_fatal_protocol_mismatch_path():
    js = _shell_js()
    assert "INCOMPATIBLE_PROTOCOL_CLOSE_CODE" in js
    assert "This page is incompatible with the running proxy. Reload the page." in js
    # The reload message must not offer a reconnect button.
    mismatch_at = js.index("INCOMPATIBLE_PROTOCOL_CLOSE_CODE)")
    block = js[mismatch_at : js.index("}", mismatch_at)]
    assert 'el("shell-reconnect").hidden = true' in block


def test_dashboard_js_strips_token_from_hash():
    js = _js()
    assert "replaceState" in js
    # The localStorage key (built from the prefix constant).
    assert 'STORAGE_PREFIX + "shell-token"' in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_shell_js_parses():
    result = subprocess.run(
        ["node", "--check", str(SHELL_DIR / "shell.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_pages_link_the_favicon():
    for html in (_index(), _shell_html()):
        assert 'rel="icon"' in html, "page does not link a favicon"
        assert 'href="favicon.png"' in html, "page does not link favicon.png"


def test_favicon_is_a_valid_32x32_png():
    import struct

    data = _favicon_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG file"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (32, 32), f"favicon is {width}x{height}, not 32x32"


def test_shell_html_loads_themes_before_shell_js():
    html = _shell_html()
    themes_at = html.index('src="shell-themes.js"')
    shell_at = html.index('src="shell.js"')
    assert themes_at < shell_at, "shell-themes.js must load before shell.js"
    tag_start = html.rindex("<script", 0, themes_at)
    tag_end = html.index(">", themes_at)
    assert "defer" in html[tag_start:tag_end], "shell-themes.js must have defer"


def test_shell_js_applies_themes():
    js = _shell_js()
    for needle in (
        "MSB_SHELL_THEMES",
        '"shell-theme"',
        "term.options.theme",
        "prefers-color-scheme",
        '"storage"',
        'read("theme")',
        "optgroup",
        "--term-bg",
    ):
        assert needle in js, f"shell.js lacks theme handling: {needle}"


def test_shell_js_has_no_hard_coded_selection_rgba():
    js = _shell_js()
    assert "rgba(" not in js, "shell.js must not hard-code selection colors"
