"""Tests for the dashboard HTTP app (agent_circus.dashboard_http)."""

import asyncio
import json
import sys
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from agent_circus.browser_protocol import (
    BROWSER_PROTOCOL_HEADER,
    BROWSER_PROTOCOL_VERSION,
)
from agent_circus.dashboard_http import STATIC_FILES, DashboardApp, is_dashboard_host
from agent_circus.proxy import RouterLookup
from agent_circus.proxy_stats import ConnectionTracker

if TYPE_CHECKING:
    from websockets.typing import Origin

    from agent_circus.dashboard import DashboardCollector

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agent_circus"
    / "templates"
    / "circus_router.py"
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "dashboard_state.json"
)

CONTRACT_KEYS = {
    "protocol_version",
    "generated_at",
    "refresh_error",
    "proxy",
    "sandboxes",
    "traffic",
}


def _api_get(path: str) -> bytes:
    """Return a GET head for an API path with the protocol header."""
    return (
        f"GET {path} HTTP/1.1\r\nHost: localhost\r\n"
        f"{BROWSER_PROTOCOL_HEADER}: {BROWSER_PROTOCOL_VERSION}\r\n\r\n"
    ).encode()


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


def _manager():
    from agent_circus.webshell import ShellManager

    return ShellManager(
        token=None, argv_factory=lambda s, w: [], notify=lambda line: None
    )


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
        shells=_manager(),
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
    assert b"<title>agent-circus dashboard</title>" in response


def test_respond_api_state_matches_contract():
    app = _app()
    response = _respond(_api_get("/api/state"), app)
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


# ---- protocol version guard -------------------------------------------------


def test_api_state_reports_supported_protocol_version():
    app = _app()
    response = _respond(_api_get("/api/state"), app)
    body = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert body["protocol_version"] == BROWSER_PROTOCOL_VERSION


def test_api_state_accepts_supported_header():
    app = _app()
    response = _respond(_api_get("/api/state"), app)
    assert response.startswith(b"HTTP/1.1 200 OK")


def test_api_logs_accepts_supported_header():
    app, collector = _logs_app()
    response = _get("/api/logs?sandbox=agents-foo&daemon=web", app)
    assert response.startswith(b"HTTP/1.1 200 OK")
    assert collector.calls == [("agents-foo", "web", 200)]


def test_api_state_missing_header_is_409_incompatible():
    app = _app()
    response = _respond(b"GET /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n", app)
    assert response.startswith(b"HTTP/1.1 409 Conflict")
    assert b"Content-Type: application/json" in response
    body = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert body == {
        "error": "incompatible_protocol",
        "expected": BROWSER_PROTOCOL_VERSION,
        "received": None,
        "message": (
            "This page is incompatible with the running proxy. Reload the page."
        ),
    }


@pytest.mark.parametrize("value", ["", "1.0", "v1", "one", " "])
def test_api_state_malformed_header_is_409_with_null_received(value):
    app = _app()
    head = (
        f"GET /api/state HTTP/1.1\r\nHost: localhost\r\n"
        f"{BROWSER_PROTOCOL_HEADER}: {value}\r\n\r\n"
    ).encode()
    response = _respond(head, app)
    assert response.startswith(b"HTTP/1.1 409 Conflict")
    body = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert body["error"] == "incompatible_protocol"
    assert body["received"] is None


def test_api_state_unsupported_version_is_409_with_received_value():
    app = _app()
    head = (
        f"GET /api/state HTTP/1.1\r\nHost: localhost\r\n"
        f"{BROWSER_PROTOCOL_HEADER}: 2\r\n\r\n"
    ).encode()
    response = _respond(head, app)
    body = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert body["expected"] == BROWSER_PROTOCOL_VERSION
    assert body["received"] == 2


def test_rejected_state_request_skips_collector():
    collector = FakeCollector({"name": "agents-foo"})
    app = _app(collector)
    _respond(b"GET /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n", app)
    assert collector.calls == 0


def test_rejected_log_request_skips_log_reader():
    app, collector = _logs_app()
    _get("/api/logs?sandbox=agents-foo&daemon=web", app)  # sanity: one call
    head = (
        b"GET /api/logs?sandbox=agents-foo&daemon=web HTTP/1.1\r\n"
        b"Host: localhost\r\n\r\n"
    )
    _respond(head, app)
    assert collector.calls == [("agents-foo", "web", 200)]


def test_api_logs_missing_header_is_409():
    app, _ = _logs_app()
    response = _respond(
        b"GET /api/logs?sandbox=agents-foo&daemon=web HTTP/1.1\r\n"
        b"Host: localhost\r\n\r\n",
        app,
    )
    assert response.startswith(b"HTTP/1.1 409 Conflict")


def test_fixtures_carry_supported_protocol_version():
    for path in (FIXTURE_PATH, SCOPED_FIXTURE_PATH):
        state = json.loads(path.read_text())
        assert state["protocol_version"] == BROWSER_PROTOCOL_VERSION


def test_dashboard_assets_are_packaged():
    for path, (file_name, _content_type) in STATIC_FILES.items():
        resource = resources.files("agent_circus").joinpath(
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
    import agent_circus.proxy as proxy_module
    from agent_circus.dashboard_http import DashboardApp as App

    class StubCollector:
        async def sandboxes(self):
            return [], 0.0, None

    app = App(
        cast("DashboardCollector", StubCollector()),
        ConnectionTracker(),
        7700,
        shells=_manager(),
    )

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
    import agent_circus.proxy as proxy_module
    from agent_circus.dashboard_http import DashboardApp as App
    from agent_circus.proxy_stats import ConnectionTracker

    class StubCollector:
        async def sandboxes(self):
            return [], 0.0, None

    tracker = ConnectionTracker()
    app = App(
        cast("DashboardCollector", StubCollector()), tracker, 7700, shells=_manager()
    )

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
    from agent_circus.proxy import handle as proxy_handle

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
    app = DashboardApp(
        cast("DashboardCollector", collector),
        ConnectionTracker(),
        7700,
        shells=_manager(),
    )
    return app, collector


def _get(path: str, app: DashboardApp) -> bytes:
    return asyncio.run(app.respond(_api_get(path)))


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


SCOPED_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "dashboard_state_scoped.json"
)


def _tracker_app():
    tracker = ConnectionTracker(clock=lambda: 1763380000.0)
    record = tracker.open(
        client="c",
        head=b"GET / HTTP/1.1\r\nHost: web.agents-foo.localhost:7700\r\n\r\n",
        host="web.agents-foo.localhost:7700",
        sandbox="agents-foo",
        daemon="web",
    )
    tracker.close(record)
    app = DashboardApp(
        cast("DashboardCollector", FakeCollector({"name": "agents-foo"})),
        tracker,
        7700,
        shells=_manager(),
    )
    return app


def test_api_state_without_scope_has_by_sandbox():
    app = _tracker_app()
    response = _respond(_api_get("/api/state"), app)
    body = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert body["scope"] is None
    by_sandbox = body["traffic"]["by_sandbox"]
    assert "agents-foo" in by_sandbox
    entry = by_sandbox["agents-foo"]
    assert entry["active"] == 0
    assert entry["connections"] == 1
    assert len(entry["spark"]) == 60
    assert sum(entry["spark"]) == entry["connections"]


def test_api_state_with_scope_filters_traffic():
    tracker = ConnectionTracker(clock=lambda: 1763380000.0)
    foo = tracker.open(
        client="c", head=None, host=None, sandbox="agents-foo", daemon="web"
    )
    tracker.close(foo)
    bar = tracker.open(
        client="c", head=None, host=None, sandbox="agents-bar", daemon="web"
    )
    app = DashboardApp(
        cast("DashboardCollector", FakeCollector({"name": "agents-foo"})),
        tracker,
        7700,
        shells=_manager(),
    )
    response = _respond(_api_get("/api/state?sandbox=agents-foo"), app)
    body = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert body["scope"] == "agents-foo"
    assert [r["sandbox"] for r in body["traffic"]["active"]] == []
    assert [r["sandbox"] for r in body["traffic"]["recent"]] == ["agents-foo"]
    assert [r["sandbox"] for r in body["traffic"]["routes"]] == ["agents-foo"]
    assert "by_sandbox" not in body["traffic"]
    assert len(body["traffic"]["series"]["buckets"]) == 60
    # bar is still open and must not leak into the scoped answer
    assert all(r["id"] != bar.id for r in body["traffic"]["active"])
    assert len(body["sandboxes"]) == 1  # fake collector returns one view


def test_api_state_rejects_invalid_scope_with_400():
    app = _tracker_app()
    for scope in ("../x", "A%20B", "", "a" * 200):
        query = f"sandbox={scope}" if scope else "sandbox="
        response = _respond(
            _api_get(f"/api/state?{query}"),
            app,
        )
        assert response.startswith(b"HTTP/1.1 400 Bad Request"), scope
        body = json.loads(response.split(b"\r\n\r\n", 1)[1])
        assert body["error"] == "invalid sandbox name"


def test_api_state_unknown_scope_is_empty_not_error():
    app = _tracker_app()
    response = _respond(_api_get("/api/state?sandbox=agents-ghost"), app)
    assert response.startswith(b"HTTP/1.1 200 OK")
    body = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert body["scope"] == "agents-ghost"
    assert body["traffic"]["active"] == []
    assert body["traffic"]["recent"] == []
    assert body["traffic"]["routes"] == []
    buckets = body["traffic"]["series"]["buckets"]
    assert len(buckets) == 60
    assert all(b["connections"] == 0 for b in buckets)


def test_fixture_state_has_scope_and_by_sandbox():
    state = json.loads(FIXTURE_PATH.read_text())
    assert state["scope"] is None
    by_sandbox = state["traffic"]["by_sandbox"]
    running = {s["name"] for s in state["sandboxes"] if s["running"]}
    assert set(by_sandbox) == running
    for entry in by_sandbox.values():
        assert len(entry["spark"]) == 60
        assert sum(entry["spark"]) == entry["connections"]
    scoped = json.loads(SCOPED_FIXTURE_PATH.read_text())
    assert scoped["scope"] == state["sandboxes"][0]["name"]
    assert "by_sandbox" not in scoped["traffic"]
    assert len(scoped["traffic"]["series"]["buckets"]) == 60


# ---- /api/shell ------------------------------------------------------------

import websockets.asyncio.client as ws_client
import websockets.exceptions

from agent_circus.proxy import handle as proxy_handle
from agent_circus.webshell import ShellManager


def _upgrade_head(port: int, origin: str | None = "http://localhost:7700") -> bytes:
    lines = [
        "GET /api/shell?sandbox=agents-foo HTTP/1.1",
        f"Host: localhost:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
    ]
    if origin is not None:
        lines.append(f"Origin: {origin}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def _shell_app(manager: ShellManager) -> DashboardApp:
    return DashboardApp(
        cast("DashboardCollector", FakeShellCollector()),
        ConnectionTracker(),
        7700,
        shells=manager,
    )


class FakeShellCollector(FakeCollector):
    """FakeCollector with the running_managed_view the shell needs."""

    def __init__(self):
        super().__init__({"name": "agents-foo"})

    async def running_managed_view(self, name):
        if name == "agents-foo":
            return {
                "name": name,
                "managed": True,
                "running": True,
                "workspace": "/w",
                "daemons": [],
            }
        return None


def _raw_exchange(head: bytes, app: DashboardApp) -> bytes:
    """Run handle() against a raw socket pair and return the response bytes."""

    async def scenario():
        server = await asyncio.start_server(
            lambda r, w: proxy_handle(r, w, _StubResolver(), None, app),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(head)
            await writer.drain()
            writer.write_eof()
            data = b""
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), 2)
                if not chunk:
                    break
                data += chunk
            writer.close()
            return data
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(scenario())


class _StubResolver:
    async def resolve(self, sandbox: str):
        return RouterLookup("unknown")


def test_shell_rejects_foreign_origin_with_403():
    app = _shell_app(
        ShellManager(token="t", argv_factory=lambda s, w: [], notify=lambda l: None)
    )
    data = _raw_exchange(_upgrade_head(7700, "http://evil.com"), app)
    assert data.startswith(b"HTTP/1.1 403 Forbidden")


def test_shell_rejects_missing_origin_with_403():
    app = _shell_app(
        ShellManager(token="t", argv_factory=lambda s, w: [], notify=lambda l: None)
    )
    data = _raw_exchange(_upgrade_head(7700, None), app)
    assert data.startswith(b"HTTP/1.1 403 Forbidden")


def test_shell_rejects_bad_upgrade_with_400():
    app = _shell_app(
        ShellManager(token="t", argv_factory=lambda s, w: [], notify=lambda l: None)
    )
    head = _upgrade_head(7700).replace(
        b"Sec-WebSocket-Version: 13", b"Sec-WebSocket-Version: 8"
    )
    data = _raw_exchange(head, app)
    assert data.startswith(b"HTTP/1.1 400 Bad Request")


def test_shell_rejects_invalid_sandbox_name_with_400():
    app = _shell_app(
        ShellManager(token="t", argv_factory=lambda s, w: [], notify=lambda l: None)
    )
    head = _upgrade_head(7700).replace(b"sandbox=agents-foo", b"sandbox=../x")
    data = _raw_exchange(head, app)
    assert data.startswith(b"HTTP/1.1 400 Bad Request")


def test_shell_disabled_by_default_closes_with_4503():
    app = _shell_app(
        ShellManager(token=None, argv_factory=lambda s, w: [], notify=lambda l: None)
    )

    async def scenario():
        server = await asyncio.start_server(
            lambda r, w: proxy_handle(r, w, _StubResolver(), None, app),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        try:
            async with await ws_client.connect(
                f"ws://127.0.0.1:{port}/api/shell?sandbox=agents-foo",
                origin=cast("Origin", "http://localhost:7700"),
                open_timeout=5,
            ) as ws:
                try:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "hello",
                                "protocol_version": BROWSER_PROTOCOL_VERSION,
                                "token": "x",
                                "cols": 80,
                                "rows": 24,
                            }
                        )
                    )
                    while True:
                        await asyncio.wait_for(ws.recv(), 5)
                except websockets.exceptions.ConnectionClosed as e:
                    return e.rcvd.code if e.rcvd else None
                except websockets.exceptions.ConnectionClosedError as e:
                    return e.rcvd.code if e.rcvd else None
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario()) == 4503


def test_shell_end_to_end_through_proxy_handle(tmp_path):
    """Full flow: upgrade, hello, ready, PTY I/O, exit through handle()."""
    child = tmp_path / "child.py"
    child.write_text("print('SHELL-UP', flush=True)\n")

    async def scenario():
        calls = []

        class Collector:
            async def sandboxes(self):
                return [], 0.0, None

            async def running_managed_view(self, name):
                return {
                    "name": name,
                    "managed": True,
                    "running": True,
                    "workspace": "/w",
                    "daemons": [],
                }

        manager = ShellManager(
            token="secret",
            argv_factory=lambda s, w: (
                calls.append((s, w)) or [sys.executable, str(child)]
            ),
            notify=lambda line: None,
        )
        app = DashboardApp(
            cast("DashboardCollector", Collector()),
            ConnectionTracker(),
            7700,
            shells=manager,
        )
        server = await asyncio.start_server(
            lambda r, w: proxy_handle(r, w, _StubResolver(), None, app),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        try:
            async with await ws_client.connect(
                f"ws://127.0.0.1:{port}/api/shell?sandbox=agents-foo",
                origin=cast("Origin", "http://localhost:7700"),
                open_timeout=5,
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "protocol_version": BROWSER_PROTOCOL_VERSION,
                            "token": "secret",
                            "cols": 80,
                            "rows": 24,
                        }
                    )
                )
                while True:
                    message = await asyncio.wait_for(ws.recv(), 5)
                    if isinstance(message, bytes) and b"SHELL-UP" in message:
                        break
                exit_message = None
                try:
                    while True:
                        message = await asyncio.wait_for(ws.recv(), 5)
                        if isinstance(message, str):
                            data = json.loads(message)
                            if data.get("type") == "exit":
                                exit_message = data
                                break
                except websockets.exceptions.ConnectionClosed as e:
                    assert e.rcvd is None or e.rcvd.code == 1000
                return calls, exit_message
        finally:
            await manager.close_all()
            server.close()
            await server.wait_closed()

    calls, exit_message = asyncio.run(scenario())
    assert calls == [("agents-foo", "/w")]
    assert exit_message is not None and exit_message["code"] == 0


def test_state_reports_shell_enabled_and_sessions():
    class SessionManager(ShellManager):
        def sessions(self):
            return [
                {"id": 1, "sandbox": "agents-foo", "client": "c", "started_at": 0.0}
            ]

    app = DashboardApp(
        cast("DashboardCollector", FakeCollector({"name": "agents-foo"})),
        ConnectionTracker(),
        7700,
        shells=SessionManager(
            token="t", argv_factory=lambda s, w: [], notify=lambda l: None
        ),
    )
    response = _respond(_api_get("/api/state"), app)
    body = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert body["proxy"]["shell"]["enabled"] is True
    assert body["proxy"]["shell"]["sessions"][0]["sandbox"] == "agents-foo"


def test_shell_page_has_relaxed_style_csp_and_ws_connect_src():
    app = _shell_app(
        ShellManager(token="t", argv_factory=lambda s, w: [], notify=lambda l: None)
    )
    response = _respond(b"GET /shell HTTP/1.1\r\nHost: localhost\r\n\r\n", app)
    head = response.split(b"\r\n\r\n", 1)[0]
    assert b"style-src 'self' 'unsafe-inline'" in head
    assert b"connect-src 'self' ws://localhost:7700" in head


def test_dashboard_page_keeps_strict_csp():
    app = _shell_app(
        ShellManager(token="t", argv_factory=lambda s, w: [], notify=lambda l: None)
    )
    response = _respond(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n", app)
    head = response.split(b"\r\n\r\n", 1)[0]
    assert b"style-src 'self';" in head
    assert b"unsafe-inline" not in head


def test_favicon_is_served_as_png():
    response = _respond(b"GET /favicon.png HTTP/1.1\r\nHost: localhost\r\n\r\n")
    head, body = response.split(b"\r\n\r\n", 1)
    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"Content-Type: image/png" in head
    assert body[:8] == b"\x89PNG\r\n\x1a\n"


def test_shell_assets_are_packaged():
    for path in ("/shell", "/shell.css", "/shell.js", "/shell-themes.js"):
        file_name, _ = STATIC_FILES[path]
        resource = resources.files("agent_circus").joinpath(
            f"templates/dashboard/{file_name}"
        )
        assert resource.is_file(), f"missing packaged asset for {path}"
