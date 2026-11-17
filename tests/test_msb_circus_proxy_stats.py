"""Tests for the proxy connection tracker (msb_circus.proxy_stats)."""

import asyncio
import importlib.util
import json
from pathlib import Path

import msb_circus.proxy as proxy_module
from msb_circus.proxy import RouterLookup
from msb_circus.proxy_stats import (
    BUCKET_COUNT,
    RECENT_LIMIT,
    ConnectionTracker,
    parse_request_line,
    parse_response_status,
)

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "msb_circus"
    / "templates"
    / "circus_router.py"
)


def _load_router_module():
    spec = importlib.util.spec_from_file_location("circus_router_stats", TEMPLATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_parse_request_line_strips_query_and_fragment():
    head = b"GET /api/session?token=secret#frag HTTP/1.1\r\nHost: x\r\n\r\n"
    assert parse_request_line(head) == ("GET", "/api/session")


def test_parse_request_line_truncates_long_path():
    head = b"GET /" + b"a" * 500 + b" HTTP/1.1\r\n\r\n"
    method, path = parse_request_line(head)
    assert method == "GET"
    assert path is not None
    assert len(path) == 120


def test_parse_request_line_malformed_returns_none():
    assert parse_request_line(b"NOT-A-LINE\r\n\r\n") == (None, None)
    assert parse_request_line(b"GET / HTTP/1.1 extra\r\n\r\n") == (None, None)


def test_parse_response_status_reads_status_line():
    assert parse_response_status(b"HTTP/1.1 200 OK\r\n\r\n") == 200
    assert parse_response_status(b"HTTP/1.0 502 Bad Gateway\r\n") == 502


def test_parse_response_status_none_for_body_bytes():
    assert parse_response_status(b"backend-ok") is None
    assert parse_response_status(b"HTTP/2.0 200 OK\r\n") is None


def test_tracker_open_and_close_moves_record_to_recent():
    clock = {"now": 1000.0}
    tracker = ConnectionTracker(clock=lambda: clock["now"])
    record = tracker.open(
        client="127.0.0.1:53422",
        head=b"GET /api HTTP/1.1\r\nHost: web.agents-foo.localhost:7700\r\n\r\n",
        host="web.agents-foo.localhost:7700",
        sandbox="agents-foo",
        daemon="web",
    )
    snapshot = tracker.snapshot()
    assert len(snapshot["active"]) == 1
    assert snapshot["recent"] == []
    clock["now"] += 3.0
    tracker.close(record)
    snapshot = tracker.snapshot()
    assert snapshot["active"] == []
    recent = snapshot["recent"]
    assert len(recent) == 1
    assert recent[0]["outcome"] == "closed"
    assert abs(recent[0]["duration_s"] - 3.0) < 1e-9
    assert recent[0]["path"] == "/api"


def test_tracker_close_is_idempotent():
    tracker = ConnectionTracker(clock=lambda: 0.0)
    record = tracker.open(client="c", head=None, host=None, sandbox=None, daemon=None)
    tracker.close(record)
    tracker.close(record)
    assert len(tracker.snapshot()["recent"]) == 1


def test_tracker_reject_counts_error_once():
    tracker = ConnectionTracker(clock=lambda: 0.0)
    record = tracker.open(
        client="c",
        head=b"GET / HTTP/1.1\r\n\r\n",
        host="evil.com",
        sandbox=None,
        daemon=None,
    )
    tracker.reject(record, 421, "Misdirected Request")
    tracker.close(record)
    snapshot = tracker.snapshot()
    assert snapshot["recent"][0]["outcome"] == "rejected"
    assert snapshot["recent"][0]["status"] == 421
    assert snapshot["series"]["buckets"][-1]["errors"] == 1


def test_tracker_status_from_first_down_chunk_only():
    tracker = ConnectionTracker(clock=lambda: 0.0)
    record = tracker.open(client="c", head=None, host=None, sandbox=None, daemon=None)
    tracker.add_down(record, b"HTTP/1.1 200 OK\r\n")
    tracker.add_down(record, b"HTTP/1.1 500 Nope\r\n")
    assert record.status == 200


def test_tracker_5xx_counts_as_error_on_close():
    tracker = ConnectionTracker(clock=lambda: 0.0)
    record = tracker.open(
        client="c",
        head=b"GET / HTTP/1.1\r\nHost: web.agents-foo.localhost\r\n\r\n",
        host="web.agents-foo.localhost",
        sandbox="agents-foo",
        daemon="web",
    )
    tracker.add_down(record, b"HTTP/1.1 502 Bad Gateway\r\n")
    tracker.close(record)
    snapshot = tracker.snapshot()
    assert snapshot["routes"][0]["errors"] == 1
    assert snapshot["series"]["buckets"][-1]["errors"] == 1


def test_tracker_route_stats_track_active_total_and_bytes():
    clock = {"now": 0.0}
    tracker = ConnectionTracker(clock=lambda: clock["now"])
    record = tracker.open(
        client="c",
        head=b"GET / HTTP/1.1\r\nHost: web.agents-foo.localhost\r\n\r\n",
        host="web.agents-foo.localhost",
        sandbox="agents-foo",
        daemon="web",
    )
    tracker.add_up(record, b"x" * 10)
    tracker.add_down(record, b"y" * 20)
    snapshot = tracker.snapshot()
    route = snapshot["routes"][0]
    assert route["active"] == 1
    assert route["total"] == 1
    assert route["bytes_up"] == 10
    assert route["bytes_down"] == 20
    tracker.close(record)
    assert tracker.snapshot()["routes"][0]["active"] == 0


def test_tracker_recent_is_bounded():
    tracker = ConnectionTracker(clock=lambda: 0.0)
    for _ in range(RECENT_LIMIT + 5):
        record = tracker.open(
            client="c", head=None, host=None, sandbox=None, daemon=None
        )
        tracker.close(record)
    snapshot = tracker.snapshot()
    # The internal deque keeps RECENT_LIMIT records; the snapshot exposes
    # the newest 100 of them.
    assert len(tracker._recent) == RECENT_LIMIT
    assert len(snapshot["recent"]) == 100
    assert snapshot["active"] == []


def test_tracker_series_has_fixed_length_and_zero_gaps():
    clock = {"now": 1_000_000.0}
    tracker = ConnectionTracker(clock=lambda: clock["now"])
    first = tracker.open(client="c", head=None, host=None, sandbox=None, daemon=None)
    tracker.close(first)
    clock["now"] += 35.0
    second = tracker.open(client="c", head=None, host=None, sandbox=None, daemon=None)
    tracker.close(second)
    buckets = tracker.snapshot()["series"]["buckets"]
    assert len(buckets) == BUCKET_COUNT
    non_zero = [b for b in buckets if b["connections"] > 0]
    assert len(non_zero) == 2
    gap = (non_zero[1]["t"] - non_zero[0]["t"]) / 10
    assert gap == 3  # 35 s later, three 10 s buckets apart


def test_tracker_snapshot_active_is_oldest_first():
    clock = {"now": 0.0}
    tracker = ConnectionTracker(clock=lambda: clock["now"])
    first = tracker.open(client="c", head=None, host=None, sandbox=None, daemon=None)
    clock["now"] += 1.0
    second = tracker.open(client="c", head=None, host=None, sandbox=None, daemon=None)
    active = tracker.snapshot()["active"]
    assert [r["id"] for r in active] == [first.id, second.id]


def test_tracker_rejects_host_without_route_stats():
    """A rejected foreign Host has no sandbox/daemon and no route row."""
    tracker = ConnectionTracker(clock=lambda: 0.0)
    record = tracker.open(
        client="c",
        head=b"GET / HTTP/1.1\r\nHost: evil.com\r\n\r\n",
        host="evil.com",
        sandbox=None,
        daemon=None,
    )
    tracker.reject(record, 421, "nope")
    snapshot = tracker.snapshot()
    assert snapshot["routes"] == []
    assert snapshot["recent"][0]["sandbox"] is None
    assert snapshot["recent"][0]["daemon"] is None


def test_proxy_records_spliced_connection(tmp_path):
    """End to end: a proxied connection is recorded with bytes and status."""

    async def scenario():
        router = _load_router_module()

        async def backend(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            await reader.readexactly(10)
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
        routes_file = Path(tmp_path / "routes.json")
        await asyncio.to_thread(
            routes_file.write_text, json.dumps({"web": backend_port})
        )
        router_server = await asyncio.start_server(
            lambda r, w: router.handle(r, w, router.Routes(str(routes_file))),
            "127.0.0.1",
            0,
        )
        router_port = router_server.sockets[0].getsockname()[1]

        class StubResolver:
            async def resolve(self, sandbox: str) -> RouterLookup:
                return RouterLookup("ok", ("127.0.0.1", router_port))

        tracker = ConnectionTracker()
        proxy_server = await asyncio.start_server(
            lambda r, w: proxy_module.handle(r, w, StubResolver(), tracker),
            "127.0.0.1",
            0,
        )
        proxy_port = proxy_server.sockets[0].getsockname()[1]
        try:
            cr, cw = await asyncio.open_connection("127.0.0.1", proxy_port)
            cw.write(
                b"POST /api/session?token=x HTTP/1.1\r\n"
                b"Host: web.agents-foo.localhost:7700\r\n"
                b"Content-Length: 10\r\n\r\n"
                b"1234567890"
            )
            await cw.drain()
            await _read_all(cr)
            cw.close()
            # The proxy handler closes the record when the splice ends,
            # which may lag the client's EOF by a tick.
            for _ in range(20):
                snapshot = tracker.snapshot()
                if snapshot["recent"]:
                    return snapshot
                await asyncio.sleep(0.05)
            return tracker.snapshot()
        finally:
            for server in (backend_server, router_server, proxy_server):
                server.close()
                await server.wait_closed()

    snapshot = asyncio.run(scenario())
    assert len(snapshot["recent"]) == 1
    record = snapshot["recent"][0]
    assert record["status"] == 200
    assert record["outcome"] == "closed"
    assert record["path"] == "/api/session"
    assert record["bytes_down"] == len(
        b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nbackend-ok"
    )
    assert record["bytes_up"] == 10
    route = snapshot["routes"][0]
    assert (route["sandbox"], route["daemon"]) == ("agents-foo", "web")
    assert route["total"] == 1
    assert route["active"] == 0


def test_proxy_records_rejected_host_as_421(tmp_path):
    """End to end: a foreign Host is tracked as a rejected connection."""

    async def scenario():
        class StubResolver:
            async def resolve(self, sandbox: str) -> RouterLookup:
                return RouterLookup("ok", ("127.0.0.1", 1))

        tracker = ConnectionTracker()
        proxy_server = await asyncio.start_server(
            lambda r, w: proxy_module.handle(r, w, StubResolver(), tracker),
            "127.0.0.1",
            0,
        )
        proxy_port = proxy_server.sockets[0].getsockname()[1]
        try:
            cr, cw = await asyncio.open_connection("127.0.0.1", proxy_port)
            cw.write(b"GET / HTTP/1.1\r\nHost: evil.com\r\n\r\n")
            await cw.drain()
            await _read_all(cr)
            cw.close()
            return tracker.snapshot()
        finally:
            proxy_server.close()
            await proxy_server.wait_closed()

    snapshot = asyncio.run(scenario())
    assert len(snapshot["recent"]) == 1
    record = snapshot["recent"][0]
    assert record["status"] == 421
    assert record["outcome"] == "rejected"
    assert record["sandbox"] is None
    assert record["daemon"] is None
    assert snapshot["series"]["buckets"][-1]["errors"] == 1


def _open_close(tracker, sandbox, daemon="web", *, status=None, up=b"", down=b""):
    record = tracker.open(
        client="c",
        head=b"GET / HTTP/1.1\r\nHost: %s.%s.localhost:7700\r\n\r\n"
        % (daemon.encode(), sandbox.encode()),
        host=f"{daemon}.{sandbox}.localhost:7700",
        sandbox=sandbox,
        daemon=daemon,
    )
    if up:
        tracker.add_up(record, up)
    if down:
        tracker.add_down(record, down)
    tracker.close(record)
    return record


def test_snapshot_without_scope_has_by_sandbox_summary():
    tracker = ConnectionTracker(clock=lambda: 1000.0)
    _open_close(tracker, "agents-foo", down=b"HTTP/1.1 200 OK\r\n" + b"x" * 100)
    _open_close(tracker, "agents-bar")
    open_record = tracker.open(
        client="c", head=None, host=None, sandbox="agents-foo", daemon="web"
    )
    tracker.close(open_record)
    open_record = tracker.open(
        client="c", head=None, host=None, sandbox="agents-foo", daemon="web"
    )
    summary = tracker.snapshot()["by_sandbox"]
    assert set(summary) == {"agents-foo", "agents-bar"}
    foo = summary["agents-foo"]
    assert foo["active"] == 1
    assert foo["connections"] == 3
    assert foo["bytes"] == len(b"HTTP/1.1 200 OK\r\n") + 100
    assert len(foo["spark"]) == 60
    assert sum(foo["spark"]) == 3


def test_snapshot_scope_filters_active_recent_routes_and_series():
    tracker = ConnectionTracker(clock=lambda: 1000.0)
    _open_close(tracker, "agents-foo", down=b"HTTP/1.1 200 OK\r\n")
    open_foo = tracker.open(
        client="c", head=None, host=None, sandbox="agents-foo", daemon="web"
    )
    _open_close(tracker, "agents-bar", down=b"HTTP/1.1 200 OK\r\n")
    scoped = tracker.snapshot("agents-foo")
    assert len(scoped["active"]) == 1
    assert scoped["active"][0]["id"] == open_foo.id
    assert len(scoped["recent"]) == 1
    assert scoped["recent"][0]["sandbox"] == "agents-foo"
    assert [r["sandbox"] for r in scoped["routes"]] == ["agents-foo"]
    assert len(scoped["series"]["buckets"]) == 60
    assert sum(b["connections"] for b in scoped["series"]["buckets"]) == 2
    assert "by_sandbox" not in scoped


def test_snapshot_scope_keeps_quiet_sandbox_history():
    from msb_circus.proxy_stats import RECENT_LIMIT

    tracker = ConnectionTracker(clock=lambda: 1000.0)
    _open_close(tracker, "agents-quiet")
    for i in range(RECENT_LIMIT + 10):
        _open_close(tracker, "agents-busy")
    global_snapshot = tracker.snapshot()
    assert all(r["sandbox"] != "agents-quiet" for r in global_snapshot["recent"])
    scoped = tracker.snapshot("agents-quiet")
    assert len(scoped["recent"]) == 1
    assert scoped["recent"][0]["sandbox"] == "agents-quiet"


def test_snapshot_scope_unknown_sandbox_is_empty():
    tracker = ConnectionTracker(clock=lambda: 1000.0)
    scoped = tracker.snapshot("agents-ghost")
    assert scoped["active"] == []
    assert scoped["recent"] == []
    assert scoped["routes"] == []
    buckets = scoped["series"]["buckets"]
    assert len(buckets) == 60
    assert all(
        b["connections"] == 0 and b["errors"] == 0 and b["bytes"] == 0 for b in buckets
    )


def test_rejected_connection_counts_for_its_sandbox():
    tracker = ConnectionTracker(clock=lambda: 1000.0)
    record = tracker.open(
        client="c",
        head=b"GET / HTTP/1.1\r\nHost: web.agents-foo.localhost:7700\r\n\r\n",
        host="web.agents-foo.localhost:7700",
        sandbox="agents-foo",
        daemon="web",
    )
    tracker.reject(record, 404, "No sandbox named 'agents-foo'")
    summary = tracker.snapshot()["by_sandbox"]
    assert summary["agents-foo"]["errors"] == 1
    assert summary["agents-foo"]["connections"] == 1
    scoped = tracker.snapshot("agents-foo")
    assert len(scoped["recent"]) == 1
    assert scoped["recent"][0]["outcome"] == "rejected"


def test_sandbox_tracking_is_bounded_lru():
    from msb_circus.proxy_stats import SANDBOX_TRACK_LIMIT

    tracker = ConnectionTracker(clock=lambda: 1000.0)
    # Open for names 0..N, then touch name 0 again, then one more name.
    for i in range(SANDBOX_TRACK_LIMIT):
        _open_close(tracker, f"agents-s{i}")
    _open_close(tracker, "agents-s0")
    _open_close(tracker, f"agents-s{SANDBOX_TRACK_LIMIT}")
    by_sandbox = tracker.snapshot()["by_sandbox"]
    assert f"agents-s{SANDBOX_TRACK_LIMIT}" in by_sandbox
    assert "agents-s0" in by_sandbox  # recently used, kept
    assert "agents-s1" not in by_sandbox  # least recently used, evicted
    assert len(by_sandbox) == SANDBOX_TRACK_LIMIT


def test_snapshot_does_not_change_lru_order():
    from msb_circus.proxy_stats import SANDBOX_TRACK_LIMIT

    tracker = ConnectionTracker(clock=lambda: 1000.0)
    for i in range(SANDBOX_TRACK_LIMIT):
        _open_close(tracker, f"agents-s{i}")
    # Reading the snapshot repeatedly must not reorder the LRU dict.
    for _ in range(3):
        tracker.snapshot()
    _open_close(tracker, f"agents-s{SANDBOX_TRACK_LIMIT}")
    by_sandbox = tracker.snapshot()["by_sandbox"]
    assert "agents-s0" not in by_sandbox  # oldest, evicted
