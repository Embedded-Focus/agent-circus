"""HTTP frontend of the msb-circus dashboard, served by the proxy.

The proxy answers connections whose Host is ``localhost``,
``127.0.0.1`` or ``[::1]`` (each optionally with a port) with the
dashboard: three static files, a JSON state API and a health check.
Any other Host keeps the existing proxy routing (or the 421 rejection).
The host check doubles as the DNS rebinding guard: a foreign domain
that resolves to 127.0.0.1 still sends its own name in Host and never
reaches the dashboard.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from importlib import resources
from typing import TYPE_CHECKING

from .proxy_stats import parse_request_line

if TYPE_CHECKING:
    from .dashboard import DashboardCollector
    from .proxy_stats import ConnectionTracker

#: Files served from msb_circus/templates/dashboard/ (path -> (file, content type)).
STATIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
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
) -> bytes:
    """Render a complete HTTP/1.1 response with Connection: close.

    The security headers are always included.

    :param status: HTTP status code.
    :param reason: Reason phrase.
    :param body: Response body bytes.
    :param content_type: Content-Type header value.
    :param extra_headers: Additional headers appended last.
    :returns: The full response bytes (head plus body).
    """
    head = [f"HTTP/1.1 {status} {reason}", f"Content-Type: {content_type}"]
    head.extend(f"{name}: {value}" for name, value in SECURITY_HEADERS)
    head.extend(f"{name}: {value}" for name, value in extra_headers)
    head.extend((f"Content-Length: {len(body)}", "Connection: close"))
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


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
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Store the collaborators and the proxy start time.

        :param collector: Sandbox view collector.
        :param tracker: Connection tracker for the traffic data.
        :param proxy_port: Host port the proxy listens on.
        :param clock: Epoch-seconds callable (injected for tests).
        """
        self.collector = collector
        self.tracker = tracker
        self.proxy_port = proxy_port
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
            return await self._api_state()
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

        :param path: Request path (a key of STATIC_FILES).
        :returns: The full response bytes.
        """
        if path not in self._static:
            file_name, content_type = STATIC_FILES[path]
            content = (
                resources.files("msb_circus")
                .joinpath(f"templates/dashboard/{file_name}")
                .read_bytes()
            )
            self._static[path] = http_response(200, "OK", content, content_type)
        return self._static[path]

    async def _api_state(self) -> bytes:
        """Render the ``/api/state`` JSON document.

        :returns: The full response bytes with application/json.
        """
        from msb_circus import __version__

        views, generated_at, refresh_error = await self.collector.sandboxes()
        payload = {
            "generated_at": generated_at,
            "refresh_error": refresh_error,
            "proxy": {
                "port": self.proxy_port,
                "started_at": self.started_at,
                "version": __version__,
                "url_pattern": (
                    f"http://<daemon>.<sandbox>.localhost:{self.proxy_port}/"
                ),
            },
            "sandboxes": views,
            "traffic": self.tracker.snapshot(),
        }
        body = json.dumps(payload).encode()
        return http_response(200, "OK", body, "application/json")
