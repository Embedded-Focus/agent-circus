"""HTTP frontend of the agent-circus dashboard, served by the proxy.

The proxy answers connections whose Host is ``localhost``,
``127.0.0.1`` or ``[::1]`` (each optionally with a port) with the
dashboard: three static files, a JSON state API and a health check.
The query string is read by ``/api/state`` (an optional sandbox scope)
and ``/api/logs``; it is never logged or stored. Any other Host keeps
the existing proxy routing (or the 421 rejection). The host check
doubles as the DNS rebinding guard: a foreign domain that resolves to
127.0.0.1 still sends its own name in Host and never reaches the
dashboard.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Sequence
from importlib import resources
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from .browser_protocol import (
    BROWSER_PROTOCOL_HEADER,
    BROWSER_PROTOCOL_SUBPROTOCOL,
    BROWSER_PROTOCOL_VERSION,
    INCOMPATIBLE_PROTOCOL_MESSAGE,
    parse_header_version,
)
from .exceptions import MsbCommandError
from .proxy_stats import parse_request_line
from .webshell import ShellManager, origin_allowed
from .websocket import (
    HandshakeError,
    WebSocket,
    check_upgrade,
    handshake_response,
    parse_headers,
)

if TYPE_CHECKING:
    from .dashboard import DashboardCollector
    from .proxy_stats import ConnectionTracker

#: Files served from agent_circus/templates/dashboard/ (path -> (file, content type)).
STATIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    "/dashboard-metrics.js": (
        "dashboard-metrics.js",
        "text/javascript; charset=utf-8",
    ),
    "/shell": ("shell.html", "text/html; charset=utf-8"),
    "/shell.css": ("shell.css", "text/css; charset=utf-8"),
    "/shell.js": ("shell.js", "text/javascript; charset=utf-8"),
    "/shell-themes.js": ("shell-themes.js", "text/javascript; charset=utf-8"),
    "/vendor/xterm/xterm.js": (
        "vendor/xterm/xterm.js",
        "text/javascript; charset=utf-8",
    ),
    "/vendor/xterm/xterm.css": ("vendor/xterm/xterm.css", "text/css; charset=utf-8"),
    "/vendor/xterm/addon-fit.js": (
        "vendor/xterm/addon-fit.js",
        "text/javascript; charset=utf-8",
    ),
}

#: Security headers on every dashboard response. The strict CSP allows
#: only same-origin resources and blocks inline script/style.
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    (
        "Content-Security-Policy",
        (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'"
        ),
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
)

_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "[::1]", "::1"}

#: Valid daemon names for the log endpoint (config.DAEMON_NAME_PATTERN).
_DAEMON_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

#: Valid sandbox names for the ?sandbox= scope (agent-circus names are
#: lower-case DNS-like labels; other msb sandboxes are rare and only
#: get the overview).
_SANDBOX_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


def query_params(head: bytes) -> dict[str, list[str]]:
    """Return the parsed query string of a request head.

    The query string is read only by the dashboard API (/api/state and
    /api/logs) and is never logged or stored.

    :param head: Raw request head bytes.
    :returns: ``urllib.parse.parse_qs`` result (empty when no query).
    """
    request_line = head.split(b"\r\n", 1)[0]
    target = request_line.decode("latin-1", errors="replace")
    target = target.partition(" ")[2].rpartition(" ")[0]
    return parse_qs(target.partition("?")[2], keep_blank_values=True)


#: Default and bounds of the lines parameter of /api/logs.
LOG_LINES_DEFAULT = 200
LOG_LINES_MAX = 1000


def _host_without_port(host: str) -> str:
    """Return the host part of a Host header value.

    Handles bracketed IPv6 (``[::1]:7700``), which a plain split at the
    first colon would cut wrongly.

    :param host: Host header value.
    :returns: The host part, lowercased.
    """
    host = host.lower()
    if host.startswith("["):
        return host.partition("]")[0] + "]"
    return host.split(":", 1)[0]


def is_dashboard_host(host: str | None) -> bool:
    """Return whether *host* names the dashboard.

    True for ``localhost``, ``127.0.0.1`` and ``[::1]``, each with an
    optional ``:port`` (case-insensitive). This is the DNS rebinding
    guard for the dashboard: any other name is rejected.

    :param host: Host header value, or ``None``.
    :returns: ``True`` when the request targets the dashboard.
    """
    if host is None:
        return False
    return _host_without_port(host) in _LOOPBACK_NAMES


def http_response(
    status: int,
    reason: str,
    body: bytes,
    content_type: str,
    extra_headers: Sequence[tuple[str, str]] = (),
    *,
    csp: str | None = None,
) -> bytes:
    """Render a complete HTTP/1.1 response with Connection: close.

    The security headers are always included; *csp* overrides the
    default Content-Security-Policy.

    :param status: HTTP status code.
    :param reason: Reason phrase.
    :param body: Response body bytes.
    :param content_type: Content-Type header value.
    :param extra_headers: Additional headers appended last.
    :param csp: CSP header value; ``None`` uses the default strict one.
    :returns: The full response bytes (head plus body).
    """
    default_csp = dict(SECURITY_HEADERS)["Content-Security-Policy"]
    headers = [
        (name, value)
        for name, value in SECURITY_HEADERS
        if name != "Content-Security-Policy"
    ]
    headers.insert(0, ("Content-Security-Policy", csp or default_csp))
    head = [f"HTTP/1.1 {status} {reason}", f"Content-Type: {content_type}"]
    head.extend(f"{name}: {value}" for name, value in headers)
    head.extend(f"{name}: {value}" for name, value in extra_headers)
    head.extend((f"Content-Length: {len(body)}", "Connection: close"))
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


def _protocol_rejection(received: int | None) -> bytes:
    """Render the 409 answer for a request without a supported version.

    :param received: Version from the request header, or ``None`` when
        it was missing or malformed.
    :returns: The full response bytes with application/json.
    """
    body = json.dumps(
        {
            "error": "incompatible_protocol",
            "expected": BROWSER_PROTOCOL_VERSION,
            "received": received,
            "message": INCOMPATIBLE_PROTOCOL_MESSAGE,
        }
    ).encode()
    return http_response(409, "Conflict", body, "application/json")


def _negotiate_subprotocol(head: bytes) -> tuple[str | None, bool]:
    """Select the shell subprotocol from the offer in a request head.

    The caller keeps the policy; this helper only parses the offer.
    A request without a ``Sec-WebSocket-Protocol`` header selects
    nothing, so an old page still completes the upgrade and fails in
    the versioned hello message.

    :param head: Raw request head bytes.
    :returns: ``(selected, offered)``. *selected* is
        BROWSER_PROTOCOL_SUBPROTOCOL when the offer includes it, else
        ``None``. *offered* is ``True`` when the request offered any
        subprotocol.
    """
    header = parse_headers(head).get("sec-websocket-protocol")
    if header is None:
        return None, False
    offered_values = {value.strip() for value in header.split(",")}
    if BROWSER_PROTOCOL_SUBPROTOCOL in offered_values:
        return BROWSER_PROTOCOL_SUBPROTOCOL, True
    return None, True


class DashboardApp:
    """Answer dashboard requests on behalf of the proxy.

    Holds the collector (sandbox views), the tracker (traffic) and the
    proxy identity, and renders the static files and the JSON API.
    """

    def __init__(
        self,
        collector: DashboardCollector,
        tracker: ConnectionTracker,
        proxy_port: int,
        *,
        shells: ShellManager,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Store the collaborators and the proxy start time.

        :param collector: Sandbox view collector.
        :param tracker: Connection tracker for the traffic data.
        :param proxy_port: Host port the proxy listens on.
        :param shells: The shell session manager (a manager without a
            token answers every shell request with close 4503).
        :param clock: Epoch-seconds callable (injected for tests).
        """
        self.collector = collector
        self.tracker = tracker
        self.proxy_port = proxy_port
        self.shells = shells
        self.clock = clock
        self.started_at = clock()
        self._static: dict[str, bytes] = {}

    async def respond(self, head: bytes) -> bytes:
        """Render the response for one dashboard request head.

        :param head: Raw request head bytes.
        :returns: The full response bytes to write back.
        """
        method, path = parse_request_line(head)
        if method is not None and method != "GET":
            return http_response(
                405,
                "Method Not Allowed",
                b"Only GET is supported\n",
                "text/plain; charset=utf-8",
                extra_headers=[("Allow", "GET")],
            )
        if path in STATIC_FILES:
            return self._static_response(path)
        if path == "/api/state":
            return await self._api_state(head)
        if path == "/api/logs":
            return await self._api_logs(head)
        if path == "/healthz":
            return http_response(200, "OK", b"ok\n", "text/plain; charset=utf-8")
        return http_response(
            404,
            "Not Found",
            f"Not found. Open http://localhost:{self.proxy_port}/\n".encode(),
            "text/plain; charset=utf-8",
        )

    def _static_response(self, path: str) -> bytes:
        """Return one static file, reading and caching it on first use.

        The shell page gets a relaxed CSP: xterm.js creates ``<style>``
        elements at runtime (blocked by a strict ``style-src``), and
        some browsers do not match ``ws://`` with ``'self'`` in
        ``connect-src``. Scripts stay restricted to the same origin.

        :param path: Request path (a key of STATIC_FILES).
        :returns: The full response bytes.
        """
        if path not in self._static:
            file_name, content_type = STATIC_FILES[path]
            content = (
                resources.files("agent_circus")
                .joinpath(f"templates/dashboard/{file_name}")
                .read_bytes()
            )
            csp = None
            if file_name == "shell.html":
                csp = (
                    "default-src 'self'; script-src 'self'; "
                    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                    f"connect-src 'self' ws://localhost:{self.proxy_port} "
                    f"ws://127.0.0.1:{self.proxy_port} ws://[::1]:{self.proxy_port}; "
                    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
                )
            self._static[path] = http_response(
                200, "OK", content, content_type, csp=csp
            )
        return self._static[path]

    async def handle_shell(
        self,
        head: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle one ``/api/shell`` WebSocket upgrade and session.

        The Origin check, the handshake checks, the sandbox check and
        the subprotocol negotiation happen before the upgrade and answer
        with plain HTTP. After the upgrade the shell manager owns the
        connection.

        :param head: Raw request head bytes.
        :param reader: Client-side stream reader.
        :param writer: Client-side stream writer.
        """
        origin = parse_headers(head).get("origin")
        if not origin_allowed(origin, self.proxy_port):
            writer.write(
                http_response(
                    403,
                    "Forbidden",
                    b"Origin not allowed\n",
                    "text/plain; charset=utf-8",
                )
            )
            await writer.drain()
            writer.close()
            return
        try:
            key = check_upgrade(head)
        except HandshakeError as e:
            writer.write(
                http_response(
                    400, "Bad Request", f"{e}\n".encode(), "text/plain; charset=utf-8"
                )
            )
            await writer.drain()
            writer.close()
            return
        sandbox = (query_params(head).get("sandbox") or [None])[0]
        if sandbox is None or not _SANDBOX_NAME.fullmatch(sandbox):
            writer.write(
                http_response(
                    400,
                    "Bad Request",
                    b"invalid sandbox name\n",
                    "text/plain; charset=utf-8",
                )
            )
            await writer.drain()
            writer.close()
            return
        peername = writer.get_extra_info("peername")
        if isinstance(peername, tuple) and len(peername) >= 2:
            client = f"{peername[0]}:{peername[1]}"
        else:
            client = "-"
        selected, offered = _negotiate_subprotocol(head)
        if offered and selected is None:
            # An offered but unsupported subprotocol cannot reach the
            # shell manager: the page expects a protocol we do not speak.
            writer.write(
                http_response(
                    400,
                    "Bad Request",
                    b"Unsupported browser protocol\n",
                    "text/plain; charset=utf-8",
                )
            )
            await writer.drain()
            writer.close()
            return
        writer.write(handshake_response(key, selected))
        await writer.drain()
        ws = WebSocket(reader, writer)
        try:
            await self.shells.run(
                ws, self.collector.running_managed_view, sandbox, client
            )
        finally:
            writer.close()

    async def _api_state(self, head: bytes) -> bytes:
        """Render the ``/api/state`` JSON document.

        Requests must carry the browser protocol header; anything else
        gets a 409 before the collector runs. An optional
        ``?sandbox=<name>`` query scopes the ``traffic`` object to that
        sandbox; ``sandboxes`` always holds all of them.

        :param head: Raw request head bytes.
        :returns: The full response bytes with application/json.
        """
        from agent_circus import __version__

        rejection = self._protocol_guard(head)
        if rejection is not None:
            return rejection
        scope = (query_params(head).get("sandbox") or [None])[0]
        if scope is not None and not _SANDBOX_NAME.fullmatch(scope):
            body = json.dumps({"error": "invalid sandbox name"}).encode()
            return http_response(400, "Bad Request", body, "application/json")
        (
            views,
            generated_at,
            refresh_error,
            metrics_error,
        ) = await self.collector.sandboxes()
        payload = {
            "protocol_version": BROWSER_PROTOCOL_VERSION,
            "generated_at": generated_at,
            "refresh_error": refresh_error,
            # Nonfatal: the page shows it as a warning and keeps the
            # rest of the state; null after a successful refresh.
            "metrics_error": metrics_error,
            "scope": scope,
            "proxy": {
                "port": self.proxy_port,
                "started_at": self.started_at,
                "version": __version__,
                "url_pattern": (
                    f"http://<daemon>.<sandbox>.localhost:{self.proxy_port}/"
                ),
                "shell": {
                    "enabled": self.shells.enabled,
                    "sessions": self.shells.sessions(),
                },
            },
            "sandboxes": views,
            "traffic": self.tracker.snapshot(scope),
        }
        body = json.dumps(payload).encode()
        return http_response(200, "OK", body, "application/json")

    async def _api_logs(self, head: bytes) -> bytes:
        """Render the ``/api/logs`` answer for one request head.

        Requests must carry the browser protocol header; anything else
        gets a 409 before the log reader runs. The query string is read
        here and never logged or stored. The sandbox must be a running
        managed sandbox of the last views and the daemon one of its
        daemons (or ``_router`` for the router log).

        :param head: Raw request head bytes.
        :returns: The full response bytes (text/plain log or JSON error).
        """
        rejection = self._protocol_guard(head)
        if rejection is not None:
            return rejection
        query = query_params(head)
        sandbox = (query.get("sandbox") or [None])[0]
        daemon = (query.get("daemon") or [None])[0]
        try:
            lines = int((query.get("lines") or [LOG_LINES_DEFAULT])[0])
        except ValueError:
            lines = LOG_LINES_DEFAULT
        lines = max(1, min(lines, LOG_LINES_MAX))
        if not sandbox or not daemon:
            return self._logs_error("sandbox and daemon parameters are required")
        if daemon != "_router" and not _DAEMON_NAME.fullmatch(daemon):
            return self._logs_error(f"invalid daemon name {daemon!r}")
        try:
            text = await self.collector.daemon_log(sandbox, daemon, lines)
        except KeyError as e:
            return self._logs_error(str(e.args[0] if e.args else e))
        except (MsbCommandError, OSError, ValueError) as e:
            return self._logs_error(f"log fetch failed: {e}")
        return http_response(
            200, "OK", text.encode("utf-8", "replace"), "text/plain; charset=utf-8"
        )

    def _protocol_guard(self, head: bytes) -> bytes | None:
        """Return a 409 response when the request version is unsupported.

        :param head: Raw request head bytes.
        :returns: The rejection response, or ``None`` when the request
            may proceed.
        """
        value = parse_headers(head).get(BROWSER_PROTOCOL_HEADER.lower())
        received = parse_header_version(value)
        if received == BROWSER_PROTOCOL_VERSION:
            return None
        return _protocol_rejection(received)

    @staticmethod
    def _logs_error(message: str) -> bytes:
        """Render a JSON 404 for a rejected log request.

        :param message: Human-readable error text.
        :returns: The full response bytes.
        """
        body = json.dumps({"error": message}).encode()
        return http_response(404, "Not Found", body, "application/json")
