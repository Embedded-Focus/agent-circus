"""Static guards for the dashboard assets (no JS test runner here)."""

import shutil
import subprocess
from pathlib import Path

import pytest

DASHBOARD_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "msb_circus"
    / "templates"
    / "dashboard"
)

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
    "detail-daemons",
    "detail-kpis",
    "detail-conn-tabs",
    "detail-connections",
    "detail-ports",
    "layout-switch",
]


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
    for needle in ("hashchange", "#/sandbox/", "encodeURIComponent", "decodeURIComponent"):
        assert needle in js, f"dashboard.js does not handle routing: {needle}"


def test_dashboard_js_has_list_and_cards_layouts():
    js = _js()
    for needle in ('data-layout', '"list"', '"cards"', "miniSparkline", "by_sandbox"):
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_dashboard_js_parses():
    result = subprocess.run(
        ["node", "--check", str(DASHBOARD_DIR / "dashboard.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
