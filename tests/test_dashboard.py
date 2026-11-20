"""Tests for the dashboard data collector (agent_circus.dashboard)."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from agent_circus.dashboard import (
    DashboardCollector,
    InspectFacts,
    RouterStatus,
    build_daemon_views,
    build_sandbox_view,
    fetch_router_status,
    inspect_facts,
    parse_router_status_response,
)
from agent_circus.exceptions import ConfigurationError
from agent_circus.sandbox import SandboxInfo

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agent_circus"
    / "templates"
    / "circus_router.py"
)


def _load_router_module():
    spec = importlib.util.spec_from_file_location("circus_router_dash", TEMPLATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _status_response(payload: dict) -> bytes:
    body = json.dumps(payload).encode()
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )


def test_parse_router_status_response_ok():
    raw = _status_response(
        {
            "api": 1,
            "started_at": 123.5,
            "routes": {"web": 4096},
            "reachable": {"web": True},
            "daemon_status": "web 0 true true 42 10\n",
        }
    )
    status = parse_router_status_response(raw)
    assert status is not None
    assert status.started_at == pytest.approx(123.5)
    assert status.routes == {"web": 4096}
    assert status.reachable == {"web": True}
    assert status.daemon_status == "web 0 true true 42 10\n"


def test_parse_router_status_response_non_200_is_none():
    router = _load_router_module()
    raw = router.error_response(400, "Bad Request", "Missing or invalid Host header")
    assert parse_router_status_response(raw) is None


def test_parse_router_status_response_wrong_api_is_none():
    raw = _status_response({"api": 2, "routes": {}, "reachable": {}})
    assert parse_router_status_response(raw) is None


def test_parse_router_status_response_ignores_bad_entries():
    raw = _status_response(
        {
            "api": 1,
            "started_at": "not-a-number",
            "routes": {"web": 4096, "bad": "x"},
            "reachable": {"web": True, "bad": "yes"},
            "daemon_status": 42,
        }
    )
    status = parse_router_status_response(raw)
    assert status is not None
    assert status.routes == {"web": 4096}
    assert status.reachable == {"web": True}
    assert status.started_at is None
    assert status.daemon_status is None


def test_fetch_router_status_unreachable():
    sock = __import__("socket").socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()
    result = asyncio.run(fetch_router_status(("127.0.0.1", dead_port), timeout=0.5))
    assert result == "unreachable"


def test_fetch_router_status_against_real_router(tmp_path):
    async def scenario():
        router = _load_router_module()
        routes_file = tmp_path / "routes.json"
        routes_file.write_text(json.dumps({"web": 4096}))
        status_script = tmp_path / "status.sh"
        status_script.write_text('echo "web 0 true true 42 10"\n')
        server = await asyncio.start_server(
            lambda r, w: router.handle(
                r, w, router.Routes(str(routes_file)), str(status_script)
            ),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        try:
            return await fetch_router_status(("127.0.0.1", port))
        finally:
            server.close()
            await server.wait_closed()

    status = asyncio.run(scenario())
    assert isinstance(status, object) and not isinstance(status, str)
    assert status.routes == {"web": 4096}
    assert "web 0 true" in status.daemon_status


INSPECT_JSON = json.dumps(
    {
        "config": {
            "network": {
                "ports": [
                    {
                        "guest_port": 7780,
                        "host_bind": "127.0.0.1",
                        "host_port": 56263,
                        "protocol": "tcp",
                    },
                    {
                        "guest_port": 8000,
                        "host_bind": "127.0.0.1",
                        "host_port": 8000,
                        "protocol": "tcp",
                    },
                ]
            },
            "volumes": [
                {"type": "Bind", "host": "/home/u/src/foo", "guest": "/workspace"},
                {"type": "Bind", "host": "/home/u/.claude", "guest": "/root/.claude"},
            ],
        }
    }
)


def test_inspect_facts_finds_workspace_mount():
    facts = inspect_facts(INSPECT_JSON)
    assert facts.workspace == "/home/u/src/foo"
    assert len(facts.ports) == 2
    assert facts.ports[0] == ("127.0.0.1", 56263, 7780, "tcp")


def test_inspect_facts_raises_on_bad_output():
    with pytest.raises(ConfigurationError):
        inspect_facts("not json")


def _router_status(**overrides) -> RouterStatus:
    defaults = {
        "started_at": 1000.0,
        "routes": {"web": 4096},
        "reachable": {"web": True},
        "daemon_status": "web 0 true true 5400 60\nworker 1 false false 10 5\n",
    }
    defaults.update(overrides)
    return RouterStatus(**defaults)


def test_build_daemon_views_merges_routes_and_status():
    status = _router_status(
        routes={"web": 4096},
        reachable={"web": True},
        daemon_status="web 0 true true 5400 60\nworker 1 false false 10 5\n",
    )
    views = build_daemon_views(status, "agents-foo", 7700)
    assert [v["name"] for v in views] == ["web", "worker"]
    web, worker = views
    assert web["port"] == 4096 and web["exposed"] is True
    assert web["url"] == "http://web.agents-foo.localhost:7700/"
    assert web["state"] == "running" and web["pid"] is not None
    assert web["uptime_s"] == 60
    assert web["reachable"] is True
    # A daemon known only from the status script: not exposed.
    assert worker["port"] is None and worker["exposed"] is False
    assert worker["url"] is None
    assert worker["state"] == "failed"
    assert worker["reachable"] is None
    # A route without status output is still listed.
    status = _router_status(
        routes={"web": 4096, "api": 5000},
        reachable={"web": True, "api": False},
        daemon_status=None,
    )
    views = build_daemon_views(status, "agents-foo", 7700)
    api = next(v for v in views if v["name"] == "api")
    assert api["exposed"] is True
    assert api["state"] == "unknown"
    assert api["pid"] is None and api["uptime_s"] is None
    assert api["reachable"] is False


def _sandbox_view(router, *, running=True, managed=True):
    info = SandboxInfo(
        name="agents-foo" if managed else "other-thing",
        status="Running" if running else "Stopped",
        created_at=1763300000.0,
    )
    return build_sandbox_view(info, inspect_facts(INSPECT_JSON), router, 7700)


def test_build_sandbox_view_router_states():
    assert _sandbox_view(None, running=False)["router"]["state"] == "stopped"
    facts = InspectFacts(
        ports=(("127.0.0.1", 8000, 8000, "tcp"),), workspace="/home/u/src/foo"
    )
    info = SandboxInfo(name="agents-foo", status="Running", created_at=1.0)
    view = build_sandbox_view(info, facts, None, 7700)
    assert view["router"]["state"] == "no-router"
    assert _sandbox_view("unreachable")["router"]["state"] == "unreachable"
    assert _sandbox_view("outdated")["router"]["state"] == "outdated"
    assert _sandbox_view(_router_status())["router"]["state"] == "ok"


def test_build_sandbox_view_hides_router_forward_from_ports():
    view = _sandbox_view(_router_status())
    ports = [(p["guest_port"], p["protocol"]) for p in view["ports"]]
    assert (7780, "tcp") not in ports
    assert (8000, "tcp") in ports
    assert view["router"]["address"] == "127.0.0.1:56263"


def test_build_sandbox_view_daemons_only_for_ok():
    view = _sandbox_view(_router_status())
    assert len(view["daemons"]) == 2
    view = _sandbox_view("outdated")
    assert view["daemons"] == []


def test_build_hints_stopped():
    view = _sandbox_view(None, running=False)
    assert view["router"]["state"] == "stopped"
    hint = view["hints"][0]
    assert hint["level"] == "info"
    assert hint["command"] == "cd /home/u/src/foo && msb-circus up"


def test_build_hints_no_router():
    view = _sandbox_view(None, running=True)
    assert view["router"]["state"] == "no-router"
    hint = view["hints"][0]
    assert hint["level"] == "warning"
    assert "rm -f && msb-circus up" in hint["command"]


def test_build_hints_unreachable():
    view = _sandbox_view("unreachable")
    hint = view["hints"][0]
    assert hint["level"] == "error"
    assert hint["command"] == "cd /home/u/src/foo && msb-circus up"


def test_build_hints_outdated():
    view = _sandbox_view("outdated")
    hint = view["hints"][0]
    assert hint["level"] == "info"
    assert "older than this msb-circus" in hint["text"]


def test_build_hints_failed_daemon():
    view = _sandbox_view(_router_status())
    failed = [h for h in view["hints"] if h["level"] == "error"]
    assert len(failed) == 1
    assert "Daemon worker failed" in failed[0]["text"]
    assert failed[0]["command"].endswith("msb-circus daemon restart worker")


def test_build_hints_not_listening():
    status = _router_status(
        routes={"web": 4096},
        reachable={"web": False},
        daemon_status="web 0 true true 60 60\n",
    )
    view = _sandbox_view(status)
    hint = view["hints"][0]
    assert hint["level"] == "warning"
    assert "does not listen on port 4096" in hint["text"]
    assert hint["command"].endswith("msb-circus daemon logs web")


def test_build_hints_stopped_daemon():
    status = _router_status(
        routes={"web": 4096},
        reachable={"web": True},
        daemon_status="web 0 false false 30 30\n",
    )
    view = _sandbox_view(status)
    hint = view["hints"][0]
    assert hint["level"] == "info"
    assert hint["command"].endswith("msb-circus daemon start web")


def test_build_hints_quotes_workspace_with_spaces():
    facts = InspectFacts(ports=(), workspace="/home/u/My Stuff")
    info = SandboxInfo(name="agents-foo", status="Stopped", created_at=1.0)
    view = build_sandbox_view(info, facts, None, 7700)
    assert view["hints"][0]["command"].startswith("cd '/home/u/My Stuff' && ")


def test_build_hints_none_for_unmanaged_sandbox():
    info = SandboxInfo(name="other-thing", status="Stopped", created_at=1.0)
    view = build_sandbox_view(info, None, None, 7700)
    assert view["managed"] is False
    assert view["hints"] == []


class FakeInvoker:
    """Counts list/inspect calls; scripted outputs for the collector."""

    behavior: ClassVar[dict] = {}

    def __init__(self, binary: Path = Path("/fake")) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append(args)
        if args[0] == "list":
            return SimpleNamespace(
                returncode=self.behavior.get("list_rc", 0),
                stdout=self.behavior.get("list_output", "[]"),
                stderr=self.behavior.get("list_stderr", ""),
            )
        if args[0] == "inspect":
            return SimpleNamespace(
                returncode=0,
                stdout=self.behavior.get("inspect_json", INSPECT_JSON),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _collector(fake_clock=None, invoker=None, router_fetch=None):
    clock = fake_clock if fake_clock is not None else {"now": 1000.0}
    invoker = invoker if invoker is not None else FakeInvoker()
    if router_fetch is None:

        async def router_fetch(address, timeout=2.0):
            return _router_status()

    collector = DashboardCollector(
        invoker, 7700, clock=lambda: clock["now"], router_fetch=router_fetch
    )
    return collector, clock, invoker


def test_collector_caches_within_interval():
    FakeInvoker.behavior = {
        "list_output": json.dumps(
            [{"name": "agents-foo", "status": "Running", "created_at": 1.0}]
        )
    }
    collector, clock, _invoker = _collector()

    async def scenario():
        first = await collector.sandboxes()
        clock["now"] += 1.0
        second = await collector.sandboxes()
        clock["now"] += 5.0
        third = await collector.sandboxes()
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert first[1] == second[1] == pytest.approx(1000.0)
    assert third[1] == pytest.approx(1006.0)
    list_calls = [c for c in FakeInvoker.behavior["calls"] if c[0] == "list"]
    assert len(list_calls) == 2  # one initial, one after the interval
    assert len(first[0]) == 1
    assert first[0][0]["name"] == "agents-foo"
    assert first[2] is None


def test_collector_reuses_inspect_cache_for_same_created_at():
    FakeInvoker.behavior = {
        "list_output": json.dumps(
            [{"name": "agents-foo", "status": "Running", "created_at": 42.0}]
        )
    }
    collector, clock, _invoker = _collector()
    FakeInvoker.behavior["list_output"] = json.dumps(
        [{"name": "agents-foo", "status": "Running", "created_at": 42.0}]
    )

    async def scenario():
        await collector.sandboxes()
        clock["now"] += 10.0
        await collector.sandboxes()
        clock["now"] += 10.0
        await collector.sandboxes()

    asyncio.run(scenario())
    inspect_calls = [c for c in FakeInvoker.behavior["calls"] if c[0] == "inspect"]
    assert len(inspect_calls) == 1  # cached across refreshes


def test_collector_keeps_old_views_and_reports_list_error():
    FakeInvoker.behavior = {
        "list_output": json.dumps(
            [{"name": "agents-foo", "status": "Running", "created_at": 1.0}]
        )
    }
    collector, clock, _invoker = _collector()

    async def scenario():
        first = await collector.sandboxes()
        FakeInvoker.behavior["list_rc"] = 1
        FakeInvoker.behavior["list_stderr"] = "boom\nmore"
        clock["now"] += 10.0
        second = await collector.sandboxes()
        return first, second

    first, second = asyncio.run(scenario())
    assert first[0] and second[0] == first[0]  # views kept
    assert second[2] == "msb list failed: boom"


def test_collector_sorts_managed_running_first():
    FakeInvoker.behavior = {
        "list_output": json.dumps(
            [
                {"name": "other-x", "status": "Running", "created_at": 1.0},
                {"name": "agents-b", "status": "Stopped", "created_at": 2.0},
                {"name": "agents-a", "status": "Running", "created_at": 3.0},
            ]
        ),
        "inspect_json": json.dumps({"config": {}}),
    }
    collector, _, _ = _collector()
    views, _, _ = asyncio.run(collector.sandboxes())
    assert [v["name"] for v in views] == ["agents-a", "agents-b", "other-x"]


class LogInvoker:
    """Records list/inspect/exec calls; returns canned outputs."""

    behavior: ClassVar[dict] = {}

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append(args)
        if args[0] == "list":
            return SimpleNamespace(
                returncode=0, stdout=self.behavior["list_output"], stderr=""
            )
        if args[0] == "inspect":
            return SimpleNamespace(
                returncode=0,
                stdout=self.behavior.get("inspect_json", INSPECT_JSON),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="log line\n", stderr="")


def _log_collector(list_output=None):

    LogInvoker.behavior = {
        "list_output": list_output
        or json.dumps(
            [
                {"name": "agents-foo", "status": "Running", "created_at": 1.0},
            ]
        ),
        "inspect_json": INSPECT_JSON,
    }
    invoker = LogInvoker()
    clock = {"now": 0.0}

    async def router_fetch(address, timeout=2.0):
        return _router_status()

    collector = DashboardCollector(
        invoker, 7700, clock=lambda: clock["now"], router_fetch=router_fetch
    )

    def prepare():
        async def run():
            await collector.sandboxes()

        asyncio.run(run())
        # Freeze the refresh cache so daemon_log does not re-run msb list.
        collector._generated_at = clock["now"] + 10_000.0

    return collector, invoker, clock, prepare


def test_collector_daemon_log_builds_exec_argv():
    collector, _invoker, _, prepare = _log_collector()
    prepare()
    text = asyncio.run(collector.daemon_log("agents-foo", "web", 50))
    assert text == "log line\n"
    args = next(c for c in LogInvoker.behavior["calls"] if c[0] == "exec")
    assert args[1] != "--tty" and args[1] != "--no-tty"  # tty=None
    joined = " ".join(args)
    assert "agents-foo" in joined
    assert "tail" in joined
    assert "-n" in joined and "50" in joined
    assert "current" in joined


def test_collector_router_log_tails_router_log_dir():
    collector, _invoker, _, prepare = _log_collector()
    prepare()
    asyncio.run(collector.daemon_log("agents-foo", "_router", 100))
    args = next(c for c in LogInvoker.behavior["calls"] if c[0] == "exec")
    joined = " ".join(args)
    assert "/var/log/circus-router/current" in joined
    assert "100" in joined


def test_collector_daemon_log_unknown_sandbox():
    collector, _, _, prepare = _log_collector()
    prepare()
    with pytest.raises(KeyError):
        asyncio.run(collector.daemon_log("agents-nope", "web", 10))


def test_collector_daemon_log_unknown_daemon():
    collector, _, _, prepare = _log_collector()
    prepare()
    with pytest.raises(KeyError):
        asyncio.run(collector.daemon_log("agents-foo", "nope", 10))


def test_collector_daemon_log_rejects_bad_daemon_name():
    collector, _, _, prepare = _log_collector()
    prepare()
    with pytest.raises(KeyError):
        asyncio.run(collector.daemon_log("agents-foo", "../etc", 10))


def test_running_managed_view_returns_view_with_workspace():
    FakeInvoker.behavior = {
        "list_output": json.dumps(
            [{"name": "agents-foo", "status": "Running", "created_at": 1.0}]
        ),
        "inspect_json": INSPECT_JSON,
    }
    collector, _clock, _invoker = _collector()

    async def scenario():
        view = await collector.running_managed_view("agents-foo")
        return view

    view = asyncio.run(scenario())
    assert view is not None
    assert view["managed"] is True and view["running"] is True
    assert view["workspace"] == "/home/u/src/foo"


def test_running_managed_view_none_for_stopped_or_unmanaged():
    FakeInvoker.behavior = {
        "list_output": json.dumps(
            [
                {"name": "agents-stop", "status": "Stopped", "created_at": 1.0},
                {"name": "other-x", "status": "Running", "created_at": 2.0},
            ]
        ),
        "inspect_json": json.dumps({"config": {}}),
    }
    collector, _clock, _invoker = _collector()

    async def scenario():
        stopped = await collector.running_managed_view("agents-stop")
        unmanaged = await collector.running_managed_view("other-x")
        missing = await collector.running_managed_view("agents-nope")
        return stopped, unmanaged, missing

    stopped, unmanaged, missing = asyncio.run(scenario())
    assert stopped is None
    assert unmanaged is None
    assert missing is None
