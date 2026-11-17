"""Tests for the central host proxy (msb-circus proxy)."""

import asyncio
import importlib.util
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar, cast

if TYPE_CHECKING:
    from msb_circus.dashboard_http import DashboardApp

import pytest
from typer.testing import CliRunner

import msb_circus.cli as cli_module
import msb_circus.proxy as proxy_module
from msb_circus.cli import app
from msb_circus.proxy import (
    RouterLookup,
    RouterResolver,
    handle,
    parse_proxy_host,
)

runner = CliRunner()

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "msb_circus"
    / "templates"
    / "circus_router.py"
)


def _load_router_module():
    spec = importlib.util.spec_from_file_location("circus_router_proxy", TEMPLATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("opencode-web.agents-foo.localhost", ("opencode-web", "agents-foo")),
        ("opencode-web.agents-foo.localhost:7700", ("opencode-web", "agents-foo")),
        ("OpenCode-Web.Agents-Foo.localhost", ("opencode-web", "agents-foo")),
    ],
)
def test_parse_proxy_host_accepts_daemon_sandbox_localhost(host, expected):
    assert parse_proxy_host(host) == expected


@pytest.mark.parametrize(
    "host",
    [
        "evil.com",
        "a.b.localhost.evil.com",
        "localhost",
        "a.localhost",
        "a.b.c.localhost",
        "localhost.",
        "",
    ],
)
def test_parse_proxy_host_rejects_foreign_domains(host):
    assert parse_proxy_host(host) is None


class FakeInvoker:
    """Counts inspect calls; returns a scripted ports payload."""

    behavior: ClassVar[dict] = {}

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append(args)
        if args[0] == "inspect":
            return SimpleNamespace(
                returncode=self.behavior.get("inspect_rc", 0),
                stdout=self.behavior.get("inspect_json", "{}"),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")


def test_resolver_caches_within_ttl(monkeypatch):
    FakeInvoker.behavior = {
        "inspect_json": json.dumps(
            {
                "config": {
                    "network": {
                        "ports": [
                            {
                                "guest_port": 7780,
                                "host_bind": "127.0.0.1",
                                "host_port": 51234,
                                "protocol": "tcp",
                            }
                        ]
                    }
                }
            }
        )
    }
    invoker = FakeInvoker(Path("/fake"))
    resolver = RouterResolver(invoker)
    clock = {"now": 0.0}
    monkeypatch.setattr(
        proxy_module, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    )

    async def scenario():
        first = await resolver.resolve("agents-foo")
        clock["now"] += 1.0
        second = await resolver.resolve("agents-foo")
        clock["now"] += 10.0
        third = await resolver.resolve("agents-foo")
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert first == second == RouterLookup("ok", ("127.0.0.1", 51234))
    assert third == RouterLookup("ok", ("127.0.0.1", 51234))
    # Two inspects: one initial, one after the TTL elapsed.
    assert len(FakeInvoker.behavior["calls"]) == 2


def test_resolver_unknown_sandbox():
    FakeInvoker.behavior = {"inspect_rc": 1}
    resolver = RouterResolver(FakeInvoker(Path("/fake")))
    assert asyncio.run(resolver.resolve("agents-nope")) == RouterLookup("unknown")


def test_resolver_no_router_forward():
    FakeInvoker.behavior = {
        "inspect_json": json.dumps(
            {
                "config": {
                    "network": {
                        "ports": [
                            {
                                "guest_port": 8000,
                                "host_bind": "127.0.0.1",
                                "host_port": 8000,
                                "protocol": "tcp",
                            }
                        ]
                    }
                }
            }
        )
    }
    resolver = RouterResolver(FakeInvoker(Path("/fake")))
    assert asyncio.run(resolver.resolve("agents-foo")) == RouterLookup("no-router")


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


def _proxy_scenario(lookup: RouterLookup, host: str, tmp_path: Path):
    """Run a full proxy + guest-router + backend chain; return response bytes."""

    async def scenario():
        router = _load_router_module()

        async def backend(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            body = b"backend-ok"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()

        backend_server = await asyncio.start_server(backend, "127.0.0.1", 0)
        backend_port = backend_server.sockets[0].getsockname()[1]
        routes_file = tmp_path / "routes.json"
        await asyncio.to_thread(
            routes_file.write_text, json.dumps({"web": backend_port})
        )
        router_server = await asyncio.start_server(
            lambda r, w: router.handle(r, w, router.Routes(str(routes_file))),
            "127.0.0.1",
            0,
        )
        router_port = router_server.sockets[0].getsockname()[1]

        # The proxy uses a stub resolver that returns the scripted lookup,
        # pointing at the real guest-router server above.
        if lookup.status == "ok" and lookup.address == ("127.0.0.1", 0):
            effective = RouterLookup("ok", ("127.0.0.1", router_port))
        else:
            effective = lookup

        class StubResolver:
            async def resolve(self, sandbox: str) -> RouterLookup:
                return effective

        proxy_server = await asyncio.start_server(
            lambda r, w: handle(r, w, StubResolver()),  # type: ignore[arg-type]
            "127.0.0.1",
            0,
        )
        proxy_port = proxy_server.sockets[0].getsockname()[1]
        try:
            cr, cw = await asyncio.open_connection("127.0.0.1", proxy_port)
            cw.write(f"GET / HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
            await cw.drain()
            data = await _read_all(cr)
            cw.close()
            return data
        finally:
            for server in (backend_server, router_server, proxy_server):
                server.close()
                await server.wait_closed()

    return asyncio.run(scenario())


def test_proxy_routes_through_router_to_daemon(tmp_path):
    # RouterLookup("ok", ("127.0.0.1", 0)) means: point the stub at the
    # real router server started inside the scenario.
    data = _proxy_scenario(
        RouterLookup("ok", ("127.0.0.1", 0)), "web.agents-foo.localhost:7700", tmp_path
    )
    assert data.startswith(b"HTTP/1.1 200 OK")
    assert b"backend-ok" in data


def test_proxy_rejects_foreign_host_with_421(tmp_path):
    data = _proxy_scenario(RouterLookup("ok", ("127.0.0.1", 1)), "evil.com", tmp_path)
    assert data.startswith(b"HTTP/1.1 421 Misdirected Request")


def test_proxy_reports_missing_router_with_502(tmp_path):
    data = _proxy_scenario(
        RouterLookup("no-router"), "web.agents-foo.localhost", tmp_path
    )
    assert data.startswith(b"HTTP/1.1 502 Bad Gateway")
    assert b"Re-create it" in data


def test_proxy_command_reports_port_in_use(tmp_path, monkeypatch):
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b"")
    monkeypatch.setattr(cli_module, "_resolve_invoker", lambda w: (None, None))
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        port = blocker.getsockname()[1]
        result = runner.invoke(app, ["proxy", "-p", str(port), "-w", str(tmp_path)])
        assert result.exit_code == 1
        assert "is in use" in result.output
        assert "MSB_CIRCUS_PROXY_PORT" in result.output
    finally:
        blocker.close()


def test_serve_closes_shells_on_shutdown():
    """serve() terminates every open shell when it stops."""

    class FakeShells:
        def __init__(self):
            self.closed = False

        async def close_all(self):
            self.closed = True

        @property
        def enabled(self):
            return True

        def sessions(self):
            return []

    async def scenario():
        import msb_circus.proxy as pm

        class FakeDashboard:
            pass

        # serve() only touches dashboard.shells in its finally branch; a
        # cancelled gather makes it reach that branch.
        task = asyncio.create_task(
            pm.serve(0, RouterResolver(_FakeInvoker(Path("/fake"))), dashboard=None)
        )
        task.cancel()

    # serve() with a dashboard is covered through the CLI wiring test;
    # here we verify the finally branch calls close_all by cancelling a
    # serve that owns a dashboard-like object.

    async def scenario2():
        import msb_circus.proxy as pm

        calls = {"close_all": 0}

        class Shells:
            async def close_all(self):
                calls["close_all"] += 1

        class Dashboard:
            shells = Shells()

        task = asyncio.create_task(
            pm.serve(
                0,
                RouterResolver(_FakeInvoker(Path("/fake"))),
                dashboard=cast("DashboardApp", Dashboard()),
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return calls["close_all"]

    assert asyncio.run(scenario2()) == 1


class _FakeInvoker:
    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(returncode=0, stdout="[]", stderr="")
