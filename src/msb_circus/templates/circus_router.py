"""circus-router: per-connection HTTP router inside the msb-circus guest.

This script runs in the sandbox guest under the guest's ``python3`` (the
standard library only; Python 3.11+). It listens on ``0.0.0.0:7780`` as
an s6 service. For every connection it reads the first request head,
takes the first ``Host`` label (e.g. ``opencode-web`` from
``opencode-web.agents-foo.localhost``), looks it up in a JSON routes
file, and splices the connection to ``127.0.0.1:<port>``.

Routing happens per connection, not per request: after the first request
head, bytes are copied both ways without interpretation. This handles
WebSockets, SSE and chunked bodies. The shortcut: a client that reuses
one keep-alive connection for a different Host stays on the first
backend; browsers do not do this over plain HTTP/1.1.

The ``Host`` header is forwarded unchanged. Dev servers with a strict
host allow-list (e.g. Vite's ``server.allowedHosts``) may reject
``*.localhost`` names; Vite allows ``.localhost`` subdomains by default.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import sys

MAX_HEAD_BYTES = 64 * 1024
HEAD_TIMEOUT_S = 10.0
CHUNK = 64 * 1024
#: How long a blocked connect to a daemon may take, in seconds.
CONNECT_TIMEOUT_S = 10.0

_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

logger = logging.getLogger("circus-router")


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


def daemon_label(host: str) -> str | None:
    """Return the first DNS label of *host* (port stripped, lowercased).

    :param host: Host header value, e.g. ``opencode-web.agents-foo.localhost:7700``.
    :returns: The first label if it matches the daemon name grammar,
        else ``None``.
    """
    label = host.lower().split(":", 1)[0].split(".", 1)[0]
    return label if _LABEL.fullmatch(label) else None


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
    :param ur: Upstream (daemon) stream reader.
    :param uw: Upstream (daemon) stream writer.
    """
    try:
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
    finally:
        for w in (uw, cw):
            w.close()
            with contextlib.suppress(Exception):
                await w.wait_closed()


class Routes:
    """Daemon routes, reloaded from a JSON file when it changes.

    The file maps daemon names to guest TCP ports. The mtime is checked
    on every lookup, so msb-circus can rewrite routes without restarting
    the router. A missing or invalid file gives an empty mapping.
    """

    def __init__(self, path: str) -> None:
        """Store the routes file path.

        :param path: Guest path of the JSON routes file.
        """
        self.path = path
        self._mtime_ns: int | None = None
        self._routes: dict[str, int] = {}
        self._warned = False

    def _load(self) -> None:
        """Reload the routes file into the cached mapping."""
        try:
            with open(self.path, "rb") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            if not self._warned:
                logger.warning("Cannot read routes file %s: %s", self.path, e)
                self._warned = True
            self._routes = {}
            return
        self._warned = False
        routes: dict[str, int] = {}
        if isinstance(data, dict):
            for name, port in data.items():
                if (
                    isinstance(name, str)
                    and isinstance(port, int)
                    and not isinstance(port, bool)
                    and 1 <= port <= 65535
                ):
                    routes[name] = port
        self._routes = routes

    def get(self, name: str) -> int | None:
        """Return the port of a daemon, reloading the file when it changed.

        :param name: Daemon name.
        :returns: The guest TCP port, or ``None`` when the name is not
            routed.
        """
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            mtime = None
        if mtime != self._mtime_ns:
            self._mtime_ns = mtime
            self._load()
        return self._routes.get(name)


async def handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    routes: Routes,
) -> None:
    """Route one connection to its daemon, or answer with an error.

    Reads the request head, resolves the daemon from the Host header and
    splices the client connection to ``127.0.0.1:<port>``.

    :param reader: Client-side stream reader.
    :param writer: Client-side stream writer.
    :param routes: Route table.
    """
    peer = writer.get_extra_info("peername")
    head = await read_head(reader)
    if head is None:
        writer.write(
            error_response(400, "Bad Request", "Malformed or oversized request head")
        )
        await writer.drain()
        writer.close()
        return
    host = extract_host(head)
    name = daemon_label(host) if host is not None else None
    if name is None:
        writer.write(
            error_response(400, "Bad Request", "Missing or invalid Host header")
        )
        await writer.drain()
        writer.close()
        return
    port = routes.get(name)
    if port is None:
        writer.write(
            error_response(
                404, "Not Found", f"No exposed daemon named {name!r} in this sandbox"
            )
        )
        await writer.drain()
        writer.close()
        return
    try:
        ur, uw = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), CONNECT_TIMEOUT_S
        )
    except (TimeoutError, OSError):
        writer.write(
            error_response(
                502,
                "Bad Gateway",
                f"Daemon {name!r} does not accept connections on port {port}. "
                f"See: msb-circus daemon logs {name}",
            )
        )
        await writer.drain()
        writer.close()
        return
    logger.info("%s -> %s:%s (%s)", peer, "127.0.0.1", port, name)
    uw.write(head)
    await uw.drain()
    await splice(reader, writer, ur, uw)


async def serve(port: int, routes: Routes) -> None:
    """Run the router server forever.

    :param port: TCP port to listen on (all interfaces).
    :param routes: Route table shared by all connections.
    """
    server = await asyncio.start_server(
        lambda r, w: _guarded(r, w, routes),
        host="0.0.0.0",
        port=port,
        limit=MAX_HEAD_BYTES,
    )
    logger.info("circus-router listening on 0.0.0.0:%s", port)
    async with server:
        await server.serve_forever()


async def _guarded(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    routes: Routes,
) -> None:
    """Run one handler and log instead of raising, so one bad
    connection never kills the server."""
    try:
        await handle(reader, writer, routes)
    except Exception:
        logger.exception("Connection handler failed")


def main() -> None:
    """Parse the command line and run the router server."""
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="circus-router HTTP router")
    parser.add_argument("--port", type=int, default=7780)
    parser.add_argument("--routes", default="/run/circus-router/routes.json")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.port, Routes(args.routes)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
