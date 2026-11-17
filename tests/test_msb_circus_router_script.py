"""Tests for the guest circus-router script (templates/circus_router.py)."""

import asyncio
import importlib.util
import json
import os
import socket
from pathlib import Path

import pytest

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "msb_circus"
    / "templates"
    / "circus_router.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("circus_router", TEMPLATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def router():
    return _load_module()


def test_extract_host_finds_case_insensitive_header(router):
    head = b"GET / HTTP/1.1\r\nhOsT: example.com:8080\r\n\r\n"
    assert router.extract_host(head) == "example.com:8080"


def test_extract_host_none_without_header(router):
    assert router.extract_host(b"GET / HTTP/1.1\r\nAccept: */*\r\n\r\n") is None


def test_daemon_label_strips_port_and_lowercases(router):
    assert router.daemon_label("OpenCode-Web.Agents-Foo.localhost:7700") == (
        "opencode-web"
    )


@pytest.mark.parametrize("label", ["-web", "web_", "a" * 64, "", "web!"])
def test_daemon_label_rejects_invalid_label(router, label):
    assert router.daemon_label(f"{label}.localhost:7700") is None


def test_routes_reload_after_file_change(tmp_path, router):
    routes_file = tmp_path / "routes.json"
    routes_file.write_text('{"web": 4096}')
    routes = router.Routes(str(routes_file))
    assert routes.get("web") == 4096
    assert routes.get("other") is None
    # Rewrite with a different mtime; the router must pick up the change.
    routes_file.write_text('{"web": 4097, "other": 5000}')
    stat = routes_file.stat()
    os.utime(routes_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert routes.get("web") == 4097
    assert routes.get("other") == 5000


def test_routes_invalid_file_gives_empty_mapping(tmp_path, router, caplog):
    routes_file = tmp_path / "routes.json"
    routes_file.write_text("not json")
    routes = router.Routes(str(routes_file))
    assert routes.get("web") is None


def _run(coro):
    return asyncio.run(coro)


async def _start_backend(handler):
    return await asyncio.start_server(handler, "127.0.0.1", 0)


def test_router_forwards_request_to_daemon(tmp_path, router):
    async def scenario():
        async def backend(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            body = b"host=" + dict(
                line.split(b": ", 1)
                for line in head.split(b"\r\n")[1:]
                if b": " in line
            ).get(b"Host", b"")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()

        server = await _start_backend(backend)
        port = server.sockets[0].getsockname()[1]
        routes_file = tmp_path / "routes.json"
        routes_file.write_text(f'{{"web": {port}}}')
        rserver = await asyncio.start_server(
            lambda r, w: router.handle(r, w, router.Routes(str(routes_file))),
            "127.0.0.1",
            0,
        )
        rport = rserver.sockets[0].getsockname()[1]
        try:
            cr, cw = await asyncio.open_connection("127.0.0.1", rport)
            cw.write(b"GET / HTTP/1.1\r\nHost: web.agents-foo.localhost:7700\r\n\r\n")
            await cw.drain()
            data = await reader_all(cr)
            cw.close()
            return data
        finally:
            server.close()
            await server.wait_closed()
            rserver.close()
            await rserver.wait_closed()

    data = _run(scenario())
    assert data.startswith(b"HTTP/1.1 200 OK")
    assert b"host=web.agents-foo.localhost:7700" in data


async def reader_all(reader: asyncio.StreamReader) -> bytes:
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


def test_router_unknown_daemon_returns_404(tmp_path, router):
    async def scenario():
        routes_file = tmp_path / "routes.json"
        routes_file.write_text('{"web": 4096}')
        server = await asyncio.start_server(
            lambda r, w: router.handle(r, w, router.Routes(str(routes_file))),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        try:
            cr, cw = await asyncio.open_connection("127.0.0.1", port)
            cw.write(b"GET / HTTP/1.1\r\nHost: nope.agents-foo.localhost\r\n\r\n")
            await cw.drain()
            data = await reader_all(cr)
            cw.close()
            return data
        finally:
            server.close()
            await server.wait_closed()

    data = _run(scenario())
    assert data.startswith(b"HTTP/1.1 404 Not Found")
    assert b"No exposed daemon named 'nope'" in data


def test_router_closed_port_returns_502(tmp_path, router):
    # Grab a port and close it: nothing listens there.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()

    async def scenario():
        routes_file = tmp_path / "routes.json"
        routes_file.write_text(f'{{"web": {dead_port}}}')
        server = await asyncio.start_server(
            lambda r, w: router.handle(r, w, router.Routes(str(routes_file))),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        try:
            cr, cw = await asyncio.open_connection("127.0.0.1", port)
            cw.write(b"GET / HTTP/1.1\r\nHost: web.agents-foo.localhost\r\n\r\n")
            await cw.drain()
            data = await reader_all(cr)
            cw.close()
            return data
        finally:
            server.close()
            await server.wait_closed()

    data = _run(scenario())
    assert data.startswith(b"HTTP/1.1 502 Bad Gateway")
    assert b"daemon logs web" in data


def test_router_streams_after_head(tmp_path, router):
    """The router copies bytes after the head: SSE-like streaming works."""

    async def scenario():
        chunks_sent = asyncio.Event()

        async def backend(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"chunk-one\n")
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.write(b"chunk-two\n")
            await writer.drain()
            writer.close()
            chunks_sent.set()

        server = await _start_backend(backend)
        port = server.sockets[0].getsockname()[1]
        routes_file = tmp_path / "routes.json"
        routes_file.write_text(f'{{"web": {port}}}')
        rserver = await asyncio.start_server(
            lambda r, w: router.handle(r, w, router.Routes(str(routes_file))),
            "127.0.0.1",
            0,
        )
        rport = rserver.sockets[0].getsockname()[1]
        try:
            cr, cw = await asyncio.open_connection("127.0.0.1", rport)
            cw.write(b"GET / HTTP/1.1\r\nHost: web.agents-foo.localhost\r\n\r\n")
            await cw.drain()
            first = await asyncio.wait_for(cr.readline(), 2.0)
            second = await asyncio.wait_for(cr.readline(), 2.0)
            cw.close()
            return first, second
        finally:
            server.close()
            await server.wait_closed()
            rserver.close()
            await rserver.wait_closed()

    first, second = _run(scenario())
    assert first == b"chunk-one\n"
    assert second == b"chunk-two\n"


def test_request_target_parses_method_and_path(router):
    head = b"GET /status?x=1 HTTP/1.1\r\nHost: _circus\r\n\r\n"
    assert router.request_target(head) == ("GET", "/status?x=1")


def test_request_target_none_for_garbage(router):
    assert router.request_target(b"NOT-A-REQUEST-LINE\r\n\r\n") is None
    assert router.request_target(b"GET / HTTP/1.1 extra\r\n\r\n") is None


def test_status_endpoint_returns_routes_and_reachability(tmp_path, router):
    async def scenario():
        async def backend(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await _start_backend(backend)
        live_port = server.sockets[0].getsockname()[1]
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
        sock.close()

        routes_file = tmp_path / "routes.json"
        routes_file.write_text(json.dumps({"web": live_port, "down": dead_port}))
        status_script = tmp_path / "status.sh"
        status_script.write_text('echo "web 0 true true 42 10"\n')
        rserver = await asyncio.start_server(
            lambda r, w: router.handle(
                r, w, router.Routes(str(routes_file)), str(status_script)
            ),
            "127.0.0.1",
            0,
        )
        rport = rserver.sockets[0].getsockname()[1]
        try:
            cr, cw = await asyncio.open_connection("127.0.0.1", rport)
            cw.write(b"GET /status HTTP/1.1\r\nHost: _circus\r\n\r\n")
            await cw.drain()
            data = await reader_all(cr)
            cw.close()
            return data
        finally:
            server.close()
            await server.wait_closed()
            rserver.close()
            await rserver.wait_closed()

    data = _run(scenario())
    assert data.startswith(b"HTTP/1.1 200 OK")
    payload = json.loads(data.split(b"\r\n\r\n", 1)[1])
    assert payload["api"] == 1
    assert set(payload["routes"]) == {"web", "down"}
    assert payload["reachable"]["web"] is True
    assert payload["reachable"]["down"] is False
    assert "web 0 true" in payload["daemon_status"]
    assert isinstance(payload["started_at"], float)


def test_status_endpoint_without_script_gives_null_daemon_status(tmp_path, router):
    async def scenario():
        routes_file = tmp_path / "routes.json"
        routes_file.write_text("{}")
        rserver = await asyncio.start_server(
            lambda r, w: router.handle(r, w, router.Routes(str(routes_file))),
            "127.0.0.1",
            0,
        )
        rport = rserver.sockets[0].getsockname()[1]
        try:
            cr, cw = await asyncio.open_connection("127.0.0.1", rport)
            cw.write(b"GET /status HTTP/1.1\r\nHost: _circus\r\n\r\n")
            await cw.drain()
            data = await reader_all(cr)
            cw.close()
            return data
        finally:
            rserver.close()
            await rserver.wait_closed()

    data = _run(scenario())
    payload = json.loads(data.split(b"\r\n\r\n", 1)[1])
    assert payload["daemon_status"] is None


def test_status_endpoint_unknown_path_returns_404(tmp_path, router):
    async def scenario():
        routes_file = tmp_path / "routes.json"
        routes_file.write_text("{}")
        rserver = await asyncio.start_server(
            lambda r, w: router.handle(r, w, router.Routes(str(routes_file))),
            "127.0.0.1",
            0,
        )
        rport = rserver.sockets[0].getsockname()[1]
        try:
            cr, cw = await asyncio.open_connection("127.0.0.1", rport)
            cw.write(b"GET /nope HTTP/1.1\r\nHost: _circus\r\n\r\n")
            await cw.drain()
            data = await reader_all(cr)
            cw.close()
            return data
        finally:
            rserver.close()
            await rserver.wait_closed()

    data = _run(scenario())
    assert data.startswith(b"HTTP/1.1 404 Not Found")


def test_router_constants_match_config(router):
    from msb_circus.config import (
        ROUTER_STATUS_API,
        ROUTER_STATUS_HOST,
        ROUTER_STATUS_PATH,
    )

    assert router.STATUS_HOST == ROUTER_STATUS_HOST
    assert router.STATUS_PATH == ROUTER_STATUS_PATH
    assert router.STATUS_API == ROUTER_STATUS_API
