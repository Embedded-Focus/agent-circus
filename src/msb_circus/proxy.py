"""Central HTTP proxy on the host: routes ``<daemon>.<sandbox>.localhost``.

``msb-circus proxy`` listens on one local port and forwards every
connection to the router forward of the target sandbox (found via
``msb inspect``), which in turn routes to the daemon inside the guest.

Routing happens per connection, not per request: after the first
request head, bytes are copied both ways without interpretation. This
handles WebSockets, SSE and chunked bodies. The shortcut: a client that
reuses one keep-alive connection for a different Host stays on the
first backend; browsers do not do this over plain HTTP/1.1.

This module duplicates the small HTTP helpers of the guest router
(``templates/circus_router.py``): ``extract_host``, ``error_response``,
``read_head``, ``pipe`` and ``splice``. The duplication is intended:
the router must stay standalone (stdlib only, no ``msb_circus`` import).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from .config import ConfigurationError
from .exceptions import MsbCommandError
from .sandbox import build_inspect_args, parse_inspect_ports_ordered, router_forward

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from subprocess import CompletedProcess


class _MsbRunner(Protocol):
    """Structural type of the invoker the resolver needs (fakes included)."""

    def run(
        self,
        args: list[str],
        *,
        capture_output: bool = True,
        timeout: float | None = None,
        check: bool = True,
    ) -> CompletedProcess[str]: ...


class _RouterResolverLike(Protocol):
    """Structural type of the resolver the handler needs (fakes included)."""

    def resolve(self, sandbox: str) -> Awaitable[RouterLookup]: ...


logger = logging.getLogger(__name__)

#: TTL of a cached ``msb inspect`` answer, in seconds.
RESOLVER_TTL_S = 5.0

#: Largest accepted request head, in bytes (one HTTP/1.x request head
#: plus headers).
MAX_HEAD_BYTES = 64 * 1024
#: Seconds to wait for the request head before answering 400.
HEAD_TIMEOUT_S = 10.0
#: Copy buffer size for the byte splice.
CHUNK = 64 * 1024

#: Strict ``<daemon>.<sandbox>.localhost[:port]`` grammar. The check is
#: the DNS rebinding guard: a foreign domain that resolves to 127.0.0.1
#: still sends its own name in Host and is rejected here.
_PROXY_HOST = re.compile(
    r"^(?P<daemon>[a-z0-9][a-z0-9-]{0,39})\.(?P<sandbox>[a-z0-9][a-z0-9-]*)"
    r"\.localhost(?::\d{1,5})?$",
    re.IGNORECASE,
)


def parse_proxy_host(host: str) -> tuple[str, str] | None:
    """Split ``<daemon>.<sandbox>.localhost[:port]`` (case-insensitive).

    :param host: Host header value.
    :returns: ``(daemon, sandbox)`` (lowercased), or ``None`` for any
        other host. This strict check is the DNS rebinding guard: a
        foreign domain that resolves to 127.0.0.1 still sends its own
        name in Host.
    """
    match = _PROXY_HOST.fullmatch(host.strip())
    if match is None:
        return None
    return match.group("daemon").lower(), match.group("sandbox").lower()


def extract_host(head: bytes) -> str | None:
    """Return the Host header value of a request head, or None.

    :param head: Raw request head bytes (headers separated by CRLF).
    :returns: The Host header value (latin-1 decoded), or ``None`` when
        the head carries no Host header.
    """
    for line in head.split(b"\r\n")[1:]:
        name, sep, value = line.partition(b":")
        if sep and name.strip().lower() == b"host":
            return value.strip().decode("latin-1")
    return None


def error_response(status: int, reason: str, text: str) -> bytes:
    """Render a plain-text HTTP error response.

    :param status: HTTP status code.
    :param reason: HTTP reason phrase.
    :param text: Human-readable body text.
    :returns: The full response bytes (head plus body).
    """
    body = (text + "\n").encode()
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body


async def read_head(reader: asyncio.StreamReader) -> bytes | None:
    """Read up to and including the blank line that ends the request head.

    :param reader: Client-side stream reader.
    :returns: The head bytes, or ``None`` when the peer closed early, the
        head exceeded the stream limit, the read timed out, or the
        connection failed.
    """
    try:
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
    except (
        TimeoutError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
        ConnectionError,
    ):
        return None


async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    """Copy bytes until EOF, then half-close *dst*.

    :param src: Stream to read from.
    :param dst: Stream to write to; its write end is half-closed at EOF.
    """
    try:
        while data := await src.read(CHUNK):
            dst.write(data)
            await dst.drain()
        if dst.can_write_eof():
            dst.write_eof()
    except (ConnectionError, OSError, RuntimeError):
        pass


async def splice(cr, cw, ur, uw) -> None:
    """Copy both directions until both end, then close both writers.

    :param cr: Client-side stream reader.
    :param cw: Client-side stream writer.
    :param ur: Upstream stream reader.
    :param uw: Upstream stream writer.
    """
    try:
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
    finally:
        for w in (uw, cw):
            w.close()
            with contextlib.suppress(Exception):
                await w.wait_closed()


@dataclass(frozen=True)
class RouterLookup:
    """Result of a sandbox router lookup.

    :ivar status: ``"ok"`` with an *address*, ``"unknown"`` when msb does
        not know the sandbox, ``"no-router"`` when it was created without
        the router port.
    :ivar address: ``(host, host_port)`` of the router forward, if any.
    """

    status: Literal["ok", "unknown", "no-router"]
    address: tuple[str, int] | None = None


class RouterResolver:
    """Map a sandbox name to the host address of its router forward.

    Calls ``msb inspect`` (in a worker thread) and caches each answer,
    including "not found", for *ttl* seconds, so a burst of connections
    to one sandbox does not spawn an inspect per connection.
    """

    def __init__(self, invoker: _MsbRunner, ttl: float = RESOLVER_TTL_S) -> None:
        """Store the invoker and the cache TTL.

        :param invoker: Invoker bound to the resolved msb binary.
        :param ttl: Seconds an inspect answer stays cached.
        """
        self._invoker = invoker
        self._ttl = ttl
        self._cache: dict[str, tuple[float, RouterLookup]] = {}

    async def resolve(self, sandbox: str) -> RouterLookup:
        """Return the router lookup of a sandbox, from cache when fresh.

        :param sandbox: Sandbox name.
        :returns: The lookup result.
        """
        now = time.monotonic()
        cached = self._cache.get(sandbox)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        lookup = await self._inspect(sandbox)
        self._cache[sandbox] = (now, lookup)
        return lookup

    async def _inspect(self, sandbox: str) -> RouterLookup:
        """Run ``msb inspect`` in a worker thread and parse the ports.

        :param sandbox: Sandbox name.
        :returns: The lookup result; failures degrade to ``"unknown"``.
        """
        try:
            result = await asyncio.to_thread(
                self._invoker.run, build_inspect_args(sandbox), check=False
            )
        except MsbCommandError as e:
            logger.debug("Router inspect for %s failed: %s", sandbox, e)
            return RouterLookup(status="unknown")
        if result.returncode != 0:
            return RouterLookup(status="unknown")
        try:
            ports = parse_inspect_ports_ordered(result.stdout)
        except ConfigurationError as e:
            logger.debug("Router inspect parse for %s failed: %s", sandbox, e)
            return RouterLookup(status="unknown")
        forward = router_forward(ports)
        if forward is None:
            return RouterLookup(status="no-router")
        return RouterLookup(status="ok", address=forward)


async def handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    resolver: _RouterResolverLike,
) -> None:
    """Forward one proxy connection to a sandbox router.

    Reads the request head, validates the Host, resolves the sandbox's
    router and splices the connection. The guest router answers 404 and
    502 for unknown or down daemons itself.

    :param reader: Client-side stream reader.
    :param writer: Client-side stream writer.
    :param resolver: Sandbox-to-router resolver.
    """
    head = await read_head(reader)
    if head is None:
        writer.write(
            error_response(400, "Bad Request", "Malformed or oversized request head")
        )
        await writer.drain()
        writer.close()
        return
    host = extract_host(head)
    parsed = parse_proxy_host(host) if host is not None else None
    if parsed is None:
        writer.write(
            error_response(
                421,
                "Misdirected Request",
                "Use http://<daemon>.<sandbox>.localhost:<port>/ — list URLs "
                "with: msb-circus urls",
            )
        )
        await writer.drain()
        writer.close()
        return
    daemon, sandbox = parsed
    lookup = await resolver.resolve(sandbox)
    if lookup.status == "unknown":
        writer.write(error_response(404, "Not Found", f"No sandbox named {sandbox!r}"))
        await writer.drain()
        writer.close()
        return
    if lookup.status == "no-router" or lookup.address is None:
        writer.write(
            error_response(
                502,
                "Bad Gateway",
                f"Sandbox {sandbox!r} was created without the router port. "
                "Re-create it: msb-circus rm -f && msb-circus up",
            )
        )
        await writer.drain()
        writer.close()
        return
    address = lookup.address
    try:
        ur, uw = await asyncio.open_connection(*address, limit=MAX_HEAD_BYTES)
    except OSError:
        writer.write(
            error_response(
                502,
                "Bad Gateway",
                f"Sandbox {sandbox!r} does not answer. Is it running? Start "
                "it with: msb-circus up",
            )
        )
        await writer.drain()
        writer.close()
        return
    logger.debug("%s -> %s:%s (%s)", host, address[0], address[1], daemon)
    uw.write(head)
    await uw.drain()
    await splice(reader, writer, ur, uw)


async def serve(port: int, resolver: RouterResolver) -> None:
    """Run the proxy server until cancelled.

    Binds ``127.0.0.1`` (required) and ``::1`` (best effort; browsers may
    try ``::1`` first for ``*.localhost``). Never binds a wildcard
    address.

    :param port: Host port to listen on.
    :param resolver: Sandbox-to-router resolver.
    """
    loop = asyncio.get_running_loop()
    background: set[asyncio.Task[None]] = set()

    def _accepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = loop.create_task(_guarded(reader, writer, resolver))
        background.add(task)
        task.add_done_callback(background.discard)

    servers = [
        await asyncio.start_server(
            _accepted, host="127.0.0.1", port=port, limit=MAX_HEAD_BYTES
        )
    ]
    try:
        v6 = await asyncio.start_server(
            _accepted, host="::1", port=port, limit=MAX_HEAD_BYTES
        )
        servers.append(v6)
    except OSError as e:
        logger.debug("IPv6 proxy listener on [::1]:%s not available: %s", port, e)
    # The CLI command already echoes the listening line to the terminal;
    # a per-connection log would be too noisy, so this stays at debug.
    try:
        await asyncio.gather(*(server.serve_forever() for server in servers))
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()


async def _guarded(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    resolver: _RouterResolverLike,
) -> None:
    """Run one handler and log instead of raising, so one bad connection
    never kills the server."""
    try:
        await handle(reader, writer, resolver)
    except Exception:
        logger.exception("Proxy connection handler failed")
        with contextlib.suppress(Exception):
            writer.close()
