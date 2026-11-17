"""Tests for the dashboard HTTP app (msb_circus.dashboard_http)."""

import asyncio
import json
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from msb_circus.dashboard_http import STATIC_FILES, DashboardApp, is_dashboard_host
from msb_circus.proxy import RouterLookup
from msb_circus.proxy_stats import ConnectionTracker

if TYPE_CHECKING:
    from msb_circus.dashboard import DashboardCollector

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "msb_circus"
    / "templates"
    / "circus_router.py"
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "dashboard_state.json"
)

CONTRACT_KEYS = {"generated_at", "refresh_error", "proxy", "sandboxes", "traffic"}


@pytest.mark.parametrize(
    "host",
    ["localhost", "LOCALHOST:7700", "127.0.0.1:7700", "[::1]:7700", "[::1]"],
)
def test_is_dashboard_host_accepts_loopback_names(host):
    assert is_dashboard_host(host)


@pytest.mark.parametrize(
    "host",
    ["evil.com", "localhost.evil.com", "a.b.localhost", "127.0.0.2", None, ""],
)
def test_is_dashboard_host_rejects_others(host):
    assert not is_dashboard_host(host)


class FakeCollector:
    """Returns one canned sandbox view, counting calls."""

    def __init__(self, view: dict):
        self.view = view
        self.calls = 0

    async def sandboxes(self):
        self.calls += 1
        return [self.view], 1763380000.0, None


def _app(collector=None):
    tracker = ConnectionTracker(clock=lambda: 1763380000.0)
    if collector is None:
        collector = FakeCollector(
            {
                "name": "agents-foo",
                "status": "Running",
                "running": True,
                "managed": True,
                "created_at": 1763300000.0,
                "workspace": "/home/u/src/foo",
                "router": {
                    "state": "ok",
                    "address": "127.0.0.1:56263",
                    "started_at": 1763370000.0,
                },
                "ports": [],
                "daemons": [],
                "hints": [],
            }
        )
    return DashboardApp(
        cast("DashboardCollector", collector),
        tracker,
        7700,
        clock=lambda: 1763379000.0,
    )


def _respond(head: bytes, app: DashboardApp | None = None) -> bytes:
    return asyncio.run((app or _app()).respond(head))


def test_respond_serves_index_with_security_headers():
    response = _respond(b"GET / HTTP/1.1\r\nHost: localhost:7700\r\n\r\n")
    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"Content-Security-Policy: default-src 'self'" in response
    assert b"X-Content-Type-Options: nosniff" in response
    assert b"Cache-Control: no-store" in response
    assert b"<title>msb-circus dashboard</title>" in response


def test_respond_api_state_matches_contract():
    app = _app()
    response = _respond(b"GET /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n", app)
    body = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert CONTRACT_KEYS <= set(body)
    assert body["proxy"]["port"] == 7700
    assert body["proxy"]["started_at"] == pytest.approx(1763379000.0)
    assert body["proxy"]["version"]
    assert body["proxy"]["url_pattern"] == ("http://<daemon>.<sandbox>.localhost:7700/")
    assert len(body["traffic"]["series"]["buckets"]) == 60
    assert body["traffic"]["series"]["bucket_s"] == 10


def test_respond_rejects_post_with_405():
    response = _respond(b"POST /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert response.startswith(b"HTTP/1.1 405 Method Not Allowed")
    assert b"Allow: GET" in response


def test_respond_unknown_path_404():
    response = _respond(b"GET /nope HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert response.startswith(b"HTTP/1.1 404 Not Found")


def test_respond_healthz():
    response = _respond(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert response.startswith(b"HTTP/1.1 200 OK")
    assert response.endswith(b"ok\n")


def test_dashboard_assets_are_packaged():
    for path, (file_name, _content_type) in STATIC_FILES.items():
        resource = resources.files("msb_circus").joinpath(
            f"templates/dashboard/{file_name}"
        )
        assert resource.is_file(), f"missing packaged asset for {path}"


def test_fixture_state_has_contract_keys():
    state = json.loads(FIXTURE_PATH.read_text())
    assert CONTRACT_KEYS <= set(state)
    assert len(state["traffic"]["series"]["buckets"]) == 60
    assert len(state["sandboxes"]) == 3
    assert state["sandboxes"][0]["daemons"]


def test_proxy_serves_dashboard_for_localhost_host(tmp_path):
    """End to end: a request with Host localhost reaches the dashboard."""
    import msb_circus.proxy as proxy_module
    from msb_circus.dashboard_http import DashboardApp as App

    class StubCollector:
        async def sandboxes(self):
            return [], 0.0, None

    app = App(cast("DashboardCollector", StubCollector()), ConnectionTracker(), 7700)

    class StubResolver:
        async def resolve(self, sandbox: str):
            return RouterLookup("unknown")

    async def scenario():
        server = await asyncio.start_server(
            lambda r, w: proxy_module.handle(r, w, StubResolver(), None, app),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        try:
            cr, cw = await asyncio.open_connection("127.0.0.1", port)
            cw.write(
                f"GET /healthz HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n".encode()
            )
            await cw.drain()
            data = await _read_all(cr)
            cw.close()
            return data
        finally:
            server.close()
            await server.wait_closed()

    data = asyncio.run(scenario())
    assert data.startswith(b"HTTP/1.1 200 OK")
    assert data.endswith(b"ok\n")


def test_proxy_does_not_track_dashboard_requests(tmp_path):
    """End to end: dashboard requests stay out of the connection tracker."""
    import msb_circus.proxy as proxy_module
    from msb_circus.dashboard_http import DashboardApp as App
    from msb_circus.proxy_stats import ConnectionTracker

    class StubCollector:
        async def sandboxes(self):
            return [], 0.0, None

    tracker = ConnectionTracker()
    app = App(cast("DashboardCollector", StubCollector()), tracker, 7700)

    class StubResolver:
        async def resolve(self, sandbox: str):
            return RouterLookup("unknown")

    async def scenario():
        server = await asyncio.start_server(
            lambda r, w: proxy_module.handle(r, w, StubResolver(), tracker, app),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        try:
            cr, cw = await asyncio.open_connection("127.0.0.1", port)
            cw.write(
                f"GET /healthz HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n".encode()
            )
            await cw.drain()
            await _read_all(cr)
            cw.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
    snapshot = tracker.snapshot()
    assert snapshot["active"] == []
    assert snapshot["recent"] == []
    assert snapshot["routes"] == []


def test_proxy_rejects_foreign_host_with_421(tmp_path):
    data = _proxy_scenario(RouterLookup("ok", ("127.0.0.1", 1)), "evil.com", tmp_path)
    assert data.startswith(b"HTTP/1.1 421 Misdirected Request")
    assert b"for the dashboard" in data


async def _read_all(reader: asyncio.StreamReader) -> bytes:
    chunks = []
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), 2.0)
        except TimeoutError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _proxy_scenario(lookup, host: str, tmp_path: Path):
    from msb_circus.proxy import handle as proxy_handle

    async def scenario():
        class StubResolver:
            async def resolve(self, sandbox: str):
                return lookup

        server = await asyncio.start_server(
            lambda r, w: proxy_handle(r, w, StubResolver()),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        try:
            cr, cw = await asyncio.open_connection("127.0.0.1", port)
            cw.write(f"GET / HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
            await cw.drain()
            data = await _read_all(cr)
            cw.close()
            return data
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(scenario())


class LogCollector:
    """Fake collector with a canned view and a recording daemon_log."""

    def __init__(self):
        self.view = {
            "name": "agents-foo",
            "status": "Running",
            "running": True,
            "managed": True,
            "daemons": [{"name": "web"}],
        }
        self.calls = []

    async def sandboxes(self):
        return [self.view], 0.0, None

    async def daemon_log(self, sandbox, daemon, lines):
        self.calls.append((sandbox, daemon, lines))
        if sandbox != self.view["name"]:
            raise KeyError(f"no running managed sandbox named {sandbox!r}")
        if daemon == "boom":
            raise KeyError(f"no daemon named 'boom' in sandbox {sandbox!r}")
        return "log line\n"


def _logs_app():
    collector = LogCollector()
    app = DashboardApp(cast("DashboardCollector", collector), ConnectionTracker(), 7700)
    return app, collector


def _get(path: str, app: DashboardApp) -> bytes:
    head = f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
    return asyncio.run(app.respond(head))


def test_api_logs_returns_log_text():
    app, collector = _logs_app()
    response = _get("/api/logs?sandbox=agents-foo&daemon=web&lines=50", app)
    assert response.startswith(b"HTTP/1.1 200 OK")
    assert response.endswith(b"log line\n")
    assert collector.calls == [("agents-foo", "web", 50)]


def test_api_logs_lines_clamped():
    app, collector = _logs_app()
    _get("/api/logs?sandbox=agents-foo&daemon=web&lines=99999", app)
    assert collector.calls[0][2] == 1000
    _get("/api/logs?sandbox=agents-foo&daemon=web&lines=-5", app)
    assert collector.calls[1][2] == 1
    _get("/api/logs?sandbox=agents-foo&daemon=web", app)
    assert collector.calls[2][2] == 200


def test_api_logs_unknown_daemon_is_404_json():
    app, _ = _logs_app()
    response = _get("/api/logs?sandbox=agents-foo&daemon=boom", app)
    assert response.startswith(b"HTTP/1.1 404 Not Found")
    body = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert "error" in body


def test_api_logs_bad_daemon_name_is_404():
    app, collector = _logs_app()
    response = _get("/api/logs?sandbox=agents-foo&daemon=../etc", app)
    assert response.startswith(b"HTTP/1.1 404 Not Found")
    assert collector.calls == []


def test_api_logs_unknown_sandbox_is_404():
    app, _ = _logs_app()
    response = _get("/api/logs?sandbox=nope&daemon=x", app)
    assert response.startswith(b"HTTP/1.1 404 Not Found")


def test_api_logs_router_daemon_allowed():
    app, collector = _logs_app()
    response = _get("/api/logs?sandbox=agents-foo&daemon=_router", app)
    assert response.startswith(b"HTTP/1.1 200 OK")
    assert collector.calls == [("agents-foo", "_router", 200)]
