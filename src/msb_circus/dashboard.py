"""Dashboard data collection: sandbox state, router status and hints.

The dashboard shows every msb sandbox and the daemons inside the
running ones. All data comes from ``msb list`` / ``msb inspect`` (cached,
refreshed at most every :data:`REFRESH_INTERVAL_S`) and from the running
guest routers' status endpoints (see ``templates/circus_router.py``).
The module keeps the pure view-building functions separate from the
I/O, so most tests need no sockets.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .config import (
    DEFAULT_SANDBOX_NAME_PREFIX,
    DEFAULT_WORKDIR,
    ROUTER_GUEST_PORT,
    ROUTER_STATUS_API,
    ROUTER_STATUS_HOST,
    ROUTER_STATUS_PATH,
    proxy_url,
)
from .daemons import parse_status_output
from .exceptions import ConfigurationError, MsbCommandError
from .sandbox import (
    SandboxInfo,
    build_inspect_args,
    build_list_args,
    parse_inspect_bind_mounts,
    parse_inspect_ports_ordered,
    parse_sandbox_list_output,
    router_forward,
)

if TYPE_CHECKING:
    from .proxy import _MsbRunner

#: Minimum seconds between two msb list refreshes.
REFRESH_INTERVAL_S = 3.0

#: Router state as shown per sandbox.
RouterState = Literal["ok", "stopped", "no-router", "unreachable", "outdated"]

_DAEMON_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


@dataclass(frozen=True)
class RouterStatus:
    """Parsed answer of a guest router's status endpoint.

    :ivar started_at: Router process start time (epoch seconds).
    :ivar routes: Mapping of daemon name to guest TCP port.
    :ivar reachable: Per-daemon TCP reachability of the routed port.
    :ivar daemon_status: Raw output of the guest status script, or
        ``None`` when there is no script.
    """

    started_at: float | None
    routes: dict[str, int]
    reachable: dict[str, bool]
    daemon_status: str | None


def parse_router_status_response(raw: bytes) -> RouterStatus | None:
    """Parse a raw HTTP response of the router status endpoint.

    :param raw: The full response bytes.
    :returns: The status, or ``None`` when the response is not a 200
        with a JSON object of api version ROUTER_STATUS_API (an outdated
        router answers 400).
    """
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        return None
    status_line = head.split(b"\r\n", 1)[0]
    parts = status_line.split(b" ")
    if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) != 200:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("api") != ROUTER_STATUS_API:
        return None
    started_at = payload.get("started_at")
    if isinstance(started_at, bool) or not isinstance(started_at, int | float):
        started_at = None
    routes: dict[str, int] = {}
    raw_routes = payload.get("routes")
    if isinstance(raw_routes, dict):
        routes = {
            name: port
            for name, port in raw_routes.items()
            if isinstance(name, str)
            and isinstance(port, int)
            and not isinstance(port, bool)
        }
    reachable: dict[str, bool] = {}
    raw_reachable = payload.get("reachable")
    if isinstance(raw_reachable, dict):
        reachable = {
            name: flag
            for name, flag in raw_reachable.items()
            if isinstance(name, str) and isinstance(flag, bool)
        }
    daemon_status = payload.get("daemon_status")
    if not isinstance(daemon_status, str):
        daemon_status = None
    return RouterStatus(
        started_at=float(started_at) if started_at is not None else None,
        routes=routes,
        reachable=reachable,
        daemon_status=daemon_status,
    )


async def fetch_router_status(
    address: tuple[str, int], timeout: float = 2.0
) -> RouterStatus | Literal["unreachable", "outdated"]:
    """Ask a sandbox router for its status.

    :param address: ``(host, host_port)`` of the sandbox's router
        forward.
    :param timeout: Seconds for the whole exchange.
    :returns: The parsed status; ``"unreachable"`` when the router does
        not answer in time, ``"outdated"`` when the answer is not a
        current status payload.
    """

    async def _exchange() -> bytes:
        reader, writer = await asyncio.open_connection(*address)
        request = (
            f"GET {ROUTER_STATUS_PATH} HTTP/1.1\r\n"
            f"Host: {ROUTER_STATUS_HOST}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        writer.write(request)
        await writer.drain()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await reader.read(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 1024 * 1024:
                break
        writer.close()
        return b"".join(chunks)

    try:
        raw = await asyncio.wait_for(_exchange(), timeout)
    except (TimeoutError, OSError):
        return "unreachable"
    status = parse_router_status_response(raw)
    if status is None:
        return "outdated"
    return status


@dataclass(frozen=True)
class InspectFacts:
    """Static facts of a sandbox from ``msb inspect``.

    :ivar ports: Published ports as ``(host, host_port, guest_port,
        protocol)`` tuples in msb's order.
    :ivar workspace: Host path of the workspace bind mount, or ``None``.
    """

    ports: tuple[tuple[str, int, int, str], ...]
    workspace: str | None


def inspect_facts(output: str) -> InspectFacts:
    """Extract ports and the workspace host path from msb inspect JSON.

    The workspace is the host path of the bind mount whose guest path is
    DEFAULT_WORKDIR (``/workspace``).

    :param output: Raw stdout of ``msb inspect --format json``.
    :returns: The extracted facts.
    :raises ConfigurationError: If the output cannot be parsed.
    """
    ports = parse_inspect_ports_ordered(output)
    workspace: str | None = None
    for host_path, guest_path in parse_inspect_bind_mounts(output):
        if guest_path == DEFAULT_WORKDIR:
            workspace = host_path
            break
    return InspectFacts(ports=tuple(ports), workspace=workspace)


def build_daemon_views(
    status: RouterStatus, sandbox: str, proxy_port: int
) -> list[dict]:
    """Build the daemon view dicts of one running sandbox.

    :param status: The router's status answer.
    :param sandbox: Sandbox name (for the proxy URLs).
    :param proxy_port: Host port of the central proxy.
    :returns: One dict per daemon, sorted by name.
    """
    parsed = parse_status_output(status.daemon_status) if status.daemon_status else {}
    names = sorted(set(parsed) | set(status.routes))
    views: list[dict] = []
    for name in names:
        port = status.routes.get(name)
        exposed = port is not None
        state = parsed[name].state if name in parsed else "unknown"
        pid = parsed[name].pid if name in parsed else None
        uptime_s = parsed[name].seconds if name in parsed else None
        views.append(
            {
                "name": name,
                "port": port,
                "exposed": exposed,
                "url": proxy_url(name, sandbox, proxy_port) if exposed else None,
                "state": state,
                "pid": pid,
                "uptime_s": uptime_s,
                "reachable": status.reachable.get(name) if exposed else None,
            }
        )
    return views


def build_hints(sandbox: dict) -> list[dict]:
    """Build the "what to do now" hints of one sandbox view.

    Every hint carries a copyable command; commands are prefixed with
    ``cd <workspace> && `` when the workspace is known. Sandboxes that
    are not managed by msb-circus get no hints.

    :param sandbox: A sandbox view dict (as built by
        :func:`build_sandbox_view`, with ``hints`` not yet set).
    :returns: A list of ``{"level", "text", "command"}`` dicts.
    """
    if not sandbox.get("managed"):
        return []
    workspace = sandbox.get("workspace")

    def command(cmd: str) -> str:
        return f"cd {shlex.quote(workspace)} && {cmd}" if workspace else cmd

    hints: list[dict] = []
    router_state = sandbox.get("router", {}).get("state")
    if router_state == "stopped":
        hints.append(
            {
                "level": "info",
                "text": "Sandbox is stopped.",
                "command": command("msb-circus up"),
            }
        )
    elif router_state == "no-router":
        hints.append(
            {
                "level": "warning",
                "text": "Created without the router port. The proxy cannot "
                "reach its daemons.",
                "command": command("msb-circus rm -f && msb-circus up"),
            }
        )
    elif router_state == "unreachable":
        hints.append(
            {
                "level": "error",
                "text": "The router in this sandbox does not answer.",
                "command": command("msb-circus up"),
            }
        )
    elif router_state == "outdated":
        hints.append(
            {
                "level": "info",
                "text": "The router is older than this msb-circus. Daemon "
                "details are not available.",
                "command": command("msb-circus up"),
            }
        )
    for daemon in sandbox.get("daemons", []):
        name = daemon["name"]
        if daemon["state"] == "failed":
            hints.append(
                {
                    "level": "error",
                    "text": f"Daemon {name} failed (crash limit reached).",
                    "command": command(f"msb-circus daemon restart {name}"),
                }
            )
        elif (
            daemon["state"] == "running"
            and daemon["exposed"]
            and daemon["reachable"] is False
        ):
            hints.append(
                {
                    "level": "warning",
                    "text": f"Daemon {name} runs but does not listen on "
                    f"port {daemon['port']}.",
                    "command": command(f"msb-circus daemon logs {name}"),
                }
            )
        elif daemon["state"] == "stopped":
            hints.append(
                {
                    "level": "info",
                    "text": f"Daemon {name} is stopped.",
                    "command": command(f"msb-circus daemon start {name}"),
                }
            )
    return hints


def build_sandbox_view(
    info: SandboxInfo,
    facts: InspectFacts | None,
    router: RouterStatus | str | None,
    proxy_port: int,
) -> dict:
    """Build the dashboard view dict of one sandbox.

    :param info: The ``msb list`` entry.
    :param facts: Inspected ports and workspace, or ``None`` when the
        inspect failed.
    :param router: The router status answer, one of the failure strings
        (``"unreachable"``/``"outdated"``), or ``None``.
    :param proxy_port: Host port of the central proxy.
    :returns: The sandbox view dict with a ``hints`` key.
    """
    running = (info.status or "").lower() == "running"
    managed = info.name.startswith(f"{DEFAULT_SANDBOX_NAME_PREFIX}-")
    ports = list(facts.ports) if facts is not None else []
    if running:
        forward = router_forward(ports) if ports else None
    else:
        forward = None
    if not running:
        router_state: RouterState = "stopped"
    elif forward is None:
        router_state = "no-router"
    elif router == "unreachable":
        router_state = "unreachable"
    elif router == "outdated":
        router_state = "outdated"
    elif isinstance(router, RouterStatus):
        router_state = "ok"
    else:
        router_state = "no-router"
    daemons = (
        build_daemon_views(router, info.name, proxy_port)
        if router_state == "ok" and isinstance(router, RouterStatus)
        else []
    )
    view = {
        "name": info.name,
        "status": info.status,
        "running": running,
        "managed": managed,
        "created_at": info.created_at,
        "workspace": facts.workspace if facts is not None else None,
        "router": {
            "state": router_state,
            "address": f"{forward[0]}:{forward[1]}" if forward else None,
            "started_at": router.started_at
            if isinstance(router, RouterStatus)
            else None,
        },
        "ports": [
            {"host": host, "host_port": hp, "guest_port": gp, "protocol": proto}
            for host, hp, gp, proto in ports
            if not (proto == "tcp" and gp == ROUTER_GUEST_PORT)
        ],
        "daemons": daemons,
    }
    view["hints"] = build_hints(view)
    return view


class DashboardCollector:
    """Collect dashboard state on demand, with short-lived caching.

    ``sandboxes()`` refreshes the sandbox views when the cached data is
    older than :data:`REFRESH_INTERVAL_S`; concurrent callers wait for
    the same refresh instead of starting a second one. Inspect facts are
    cached per sandbox while the creation time stays the same (ports and
    mounts are fixed at creation).
    """

    def __init__(
        self,
        invoker: _MsbRunner,
        proxy_port: int,
        *,
        clock: Callable[[], float] = time.time,
        router_fetch=fetch_router_status,
    ) -> None:
        """Store the invoker and settings.

        :param invoker: Invoker bound to the resolved msb binary.
        :param proxy_port: Host port of the central proxy.
        :param clock: Epoch-seconds callable (injected for tests).
        :param router_fetch: Async router status fetcher (injected for
            tests).
        """
        self._invoker = invoker
        self._proxy_port = proxy_port
        self._clock = clock
        self._router_fetch = router_fetch
        self._lock = asyncio.Lock()
        self._views: list[dict] = []
        self._generated_at: float | None = None
        self._refresh_error: str | None = None
        self._facts_cache: dict[str, tuple[float | None, InspectFacts | None]] = {}

    async def _refresh(self) -> None:
        """Run one ``msb list``, inspect and router-status round."""
        try:
            result = await asyncio.to_thread(
                self._invoker.run, build_list_args(running_only=False), check=False
            )
            if result.returncode != 0:
                raise MsbCommandError(
                    "msb list failed",
                    returncode=result.returncode,
                    stderr=result.stderr or "",
                )
            infos = parse_sandbox_list_output(result.stdout)
        except (MsbCommandError, ConfigurationError) as e:
            stderr_line = str(getattr(e, "stderr", "") or "").strip().splitlines()
            detail = stderr_line[0] if stderr_line else str(e)
            self._refresh_error = f"msb list failed: {detail}"
            return
        self._refresh_error = None
        self._generated_at = self._clock()

        async def facts_for(info: SandboxInfo) -> tuple[str, InspectFacts | None]:
            """Inspect one sandbox, reusing the cache for the same creation."""
            cached = self._facts_cache.get(info.name)
            if (
                cached is not None
                and cached[0] is not None
                and cached[0] == info.created_at
            ):
                return info.name, cached[1]
            try:
                result = await asyncio.to_thread(
                    self._invoker.run, build_inspect_args(info.name), check=False
                )
                if result.returncode != 0:
                    facts = None
                else:
                    facts = inspect_facts(result.stdout)
            except (MsbCommandError, ConfigurationError):
                facts = None
            if info.created_at is not None and facts is not None:
                self._facts_cache[info.name] = (info.created_at, facts)
            return info.name, facts

        fact_pairs = await asyncio.gather(*(facts_for(info) for info in infos))
        facts_by_name = dict(fact_pairs)
        gone = set(self._facts_cache) - {info.name for info in infos}
        for name in gone:
            del self._facts_cache[name]

        async def router_for(info: SandboxInfo) -> RouterStatus | str | None:
            """Fetch the router status of one running sandbox."""
            if (info.status or "").lower() != "running":
                return None
            facts = facts_by_name.get(info.name)
            if facts is None or not facts.ports:
                return None
            forward = router_forward(facts.ports)
            if forward is None:
                return None
            return await self._router_fetch(forward)

        routers = await asyncio.gather(*(router_for(info) for info in infos))
        views = [
            build_sandbox_view(
                info, facts_by_name.get(info.name), router, self._proxy_port
            )
            for info, router in zip(infos, routers)
        ]
        views.sort(
            key=lambda v: (
                not v["managed"],
                not v["running"],
                v["name"],
            )
        )
        self._views = views

    async def sandboxes(self) -> tuple[list[dict], float | None, str | None]:
        """Return the sandbox views, refreshing stale data first.

        :returns: ``(sandbox views, generated_at, refresh_error)``.
            On a failed refresh the previous views are kept and
            ``refresh_error`` describes the failure.
        """
        now = self._clock()
        if self._generated_at is None or now - self._generated_at >= REFRESH_INTERVAL_S:
            async with self._lock:
                if (
                    self._generated_at is None
                    or self._clock() - self._generated_at >= REFRESH_INTERVAL_S
                ):
                    await self._refresh()
        return self._views, self._generated_at, self._refresh_error

    async def running_managed_view(self, name: str) -> dict[str, Any] | None:
        """Return the view of a running managed sandbox, refreshing first.

        :param name: Sandbox name.
        :returns: The sandbox view (with ``workspace``), or ``None``
            when the sandbox is unknown, not managed or not running.
        """
        await self.sandboxes()  # make sure the views are fresh
        return next(
            (
                v
                for v in self._views
                if v["name"] == name and v["managed"] and v["running"]
            ),
            None,
        )

    async def daemon_log(self, sandbox: str, daemon: str, lines: int) -> str:
        """Return the last lines of a daemon log or the router log.

        :param sandbox: Name of a running managed sandbox.
        :param daemon: Daemon name, or ``"_router"`` for the router log.
        :param lines: Number of trailing lines (1..1000).
        :returns: The log text (stdout, plus stderr after a separator
            when the command failed).
        :raises KeyError: If the sandbox is not a running managed
            sandbox in the last views or the daemon is unknown.
        """
        from .daemons import build_logs_command
        from .exec_ import build_exec_args

        view = await self.running_managed_view(sandbox)
        if view is None:
            raise KeyError(f"no running managed sandbox named {sandbox!r}")
        if daemon == "_router":
            from .router import ROUTER_LOG_DIR

            command = ["tail", "-n", str(lines), f"{ROUTER_LOG_DIR}/current"]
        else:
            if not _DAEMON_NAME.fullmatch(daemon) or not any(
                d["name"] == daemon for d in view["daemons"]
            ):
                raise KeyError(f"no daemon named {daemon!r} in sandbox {sandbox!r}")
            command = build_logs_command(daemon, lines, follow=False)
        args = build_exec_args(sandbox, command, tty=None)
        result = await asyncio.to_thread(
            self._invoker.run, args, check=False, timeout=10
        )
        text = result.stdout or ""
        if result.returncode != 0 and result.stderr:
            text += "\n--- stderr ---\n" + result.stderr
        return text
