"""circus-router: per-connection HTTP router inside the agent-circus guest.

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

The router also answers its own status endpoint on host ``_circus`` (a
name the daemon grammar rejects, so it can never clash and a browser
cannot reach it through the central proxy): ``GET /status`` returns a
JSON object with the router's start time, its routes, the TCP
reachability of each routed port, the output of a daemon status
script, and the use of the docker data disk when it is a separate
filesystem. The central proxy's dashboard polls this endpoint per sandbox.
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
import time

MAX_HEAD_BYTES = 64 * 1024
HEAD_TIMEOUT_S = 10.0
CHUNK = 64 * 1024
#: How long a blocked connect to a daemon may take, in seconds.
CONNECT_TIMEOUT_S = 10.0

#: Host header value of the router's own status endpoint. The underscore
#: keeps it out of the daemon name grammar.
STATUS_HOST = "_circus"
#: Path of the router's status endpoint.
STATUS_PATH = "/status"
#: Version of the status JSON payload. Bump on incompatible changes.
STATUS_API = 1
#: Seconds to wait for a reachability probe connect.
PROBE_TIMEOUT_S = 0.5
#: Seconds to wait for the daemon status script.
STATUS_SCRIPT_TIMEOUT_S = 3.0

#: Guest path of the docker data disk monitored by the status endpoint.
#: Reported only while it is a separate mount, so the root filesystem is
#: never reported twice.
DOCKER_DATA_PATH = "/var/lib/docker"

#: Router process start time (epoch seconds), read by the status endpoint.
STARTED_AT = time.time()

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
    on every lookup, so agent-circus can rewrite routes without restarting
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

    def snapshot(self) -> dict[str, int]:
        """Return a copy of the current route mapping, reloading if stale.

        :returns: Mapping of daemon name to guest TCP port.
        """
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            mtime = None
        if mtime != self._mtime_ns:
            self._mtime_ns = mtime
            self._load()
        return dict(self._routes)


def request_target(head: bytes) -> tuple[str, str] | None:
    """Return ``(method, path)`` from the request line, or None.

    :param head: Raw request head bytes.
    :returns: Method and path (query string kept), or ``None`` when
        the request line has not exactly three space-separated parts.
    """
    request_line = head.split(b"\r\n", 1)[0]
    parts = request_line.split(b" ")
    if len(parts) != 3:
        return None
    try:
        return parts[0].decode("ascii"), parts[1].decode("latin-1")
    except UnicodeDecodeError:
        return None


async def probe_port(port: int) -> bool:
    """Check whether a TCP port on 127.0.0.1 accepts a connection.

    :param port: Guest TCP port to probe.
    :returns: ``True`` when the connect succeeded (the connection is
        closed again at once), ``False`` on timeout or connection error.
    """
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), PROBE_TIMEOUT_S
        )
    except (TimeoutError, OSError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


async def run_status_script(path: str) -> str | None:
    """Run the daemon status script and return its stdout.

    :param path: Guest path of the status script.
    :returns: The script's stdout decoded with errors replaced, or
        ``None`` when the script is missing, cannot run or times out.
    """
    if not await asyncio.to_thread(os.path.isfile, path):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "sh",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), STATUS_SCRIPT_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return None
    return stdout.decode("utf-8", errors="replace")


def usage_from_statvfs(st: os.statvfs_result) -> dict[str, int] | None:
    """Convert one ``os.statvfs`` result into byte counts.

    ``available_bytes`` counts only blocks unprivileged processes may
    use (``f_bavail``), so used plus available can be smaller than
    total: the difference is reserved for root.

    :param st: Result of ``os.statvfs``.
    :returns: ``total_bytes``, ``used_bytes`` and ``available_bytes``;
        ``None`` when the block size is not positive (the counts would
        be meaningless).
    """
    block_size = st.f_frsize
    if block_size <= 0:
        return None
    total_bytes = st.f_blocks * block_size
    free_bytes = st.f_bfree * block_size
    available_bytes = st.f_bavail * block_size
    return {
        "total_bytes": total_bytes,
        "used_bytes": max(total_bytes - free_bytes, 0),
        "available_bytes": available_bytes,
    }


def statvfs_usage(path: str) -> dict[str, int] | None:
    """Inspect one filesystem and return its byte counts.

    :param path: Guest path of the mount point.
    :returns: The counts (see :func:`usage_from_statvfs`), or ``None``
        when inspection fails; the status payload then omits the entry.
    """
    try:
        return usage_from_statvfs(os.statvfs(path))
    except OSError:
        return None


def collect_filesystems() -> dict[str, dict[str, int]]:
    """Report the docker data disk when it is a separate filesystem.

    :returns: Mapping with one ``total_bytes``/``used_bytes``/
        ``available_bytes`` entry for :data:`DOCKER_DATA_PATH`, or an
        empty dict when the path is not a separate mount or cannot be
        inspected.
    """
    if not os.path.ismount(DOCKER_DATA_PATH):
        return {}
    usage = statvfs_usage(DOCKER_DATA_PATH)
    return {DOCKER_DATA_PATH: usage} if usage is not None else {}


async def status_payload(
    routes: Routes,
    status_script: str,
    filesystems: dict[str, dict[str, int]] | None = None,
) -> dict:
    """Build the JSON payload of the status endpoint.

    :param routes: Route table.
    :param status_script: Guest path of the daemon status script.
    :param filesystems: Filesystem usage entries; ``None`` collects the
        docker data disk live.
    :returns: The payload with the API version, the router start time,
        the routes, per-route reachability, the daemon status output and
        the optional filesystem entries.
    """
    snapshot = routes.snapshot()
    reachable = dict(
        zip(snapshot, await asyncio.gather(*(probe_port(p) for p in snapshot.values())))
    )
    return {
        "api": STATUS_API,
        "started_at": STARTED_AT,
        "routes": snapshot,
        "reachable": reachable,
        "daemon_status": await run_status_script(status_script),
        "filesystems": collect_filesystems() if filesystems is None else filesystems,
    }


async def handle_status(
    head: bytes,
    writer: asyncio.StreamWriter,
    routes: Routes,
    status_script: str,
) -> None:
    """Answer one connection to the status endpoint and close it.

    :param head: Raw request head bytes.
    :param writer: Client-side stream writer.
    :param routes: Route table.
    :param status_script: Guest path of the daemon status script.
    """
    target = request_target(head)
    if target == ("GET", STATUS_PATH):
        payload = await status_payload(routes, status_script)
        body = json.dumps(payload).encode()
        writer.write(
            (
                f"HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            + body
        )
    else:
        writer.write(error_response(404, "Not Found", "Unknown status path"))
    await writer.drain()
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    routes: Routes,
    status_script: str = "/run/circus-router/status.sh",
) -> None:
    """Route one connection to its daemon, or answer with an error.

    Reads the request head, resolves the daemon from the Host header and
    splices the client connection to ``127.0.0.1:<port>``. Connections
    addressed to the router itself (host ``_circus``) are answered by
    the status endpoint.

    :param reader: Client-side stream reader.
    :param writer: Client-side stream writer.
    :param routes: Route table.
    :param status_script: Guest path of the daemon status script.
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
    if host is not None and host.lower().split(":", 1)[0] == STATUS_HOST:
        await handle_status(head, writer, routes, status_script)
        return
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
                f"See: agent-circus daemon logs {name}",
            )
        )
        await writer.drain()
        writer.close()
        return
    logger.info("%s -> %s:%s (%s)", peer, "127.0.0.1", port, name)
    uw.write(head)
    await uw.drain()
    await splice(reader, writer, ur, uw)


async def serve(port: int, routes: Routes, status_script: str) -> None:
    """Run the router server forever.

    :param port: TCP port to listen on (all interfaces).
    :param routes: Route table shared by all connections.
    :param status_script: Guest path of the daemon status script.
    """
    server = await asyncio.start_server(
        lambda r, w: _guarded(r, w, routes, status_script),
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
    status_script: str,
) -> None:
    """Run one handler and log instead of raising, so one bad
    connection never kills the server."""
    try:
        await handle(reader, writer, routes, status_script)
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
    parser.add_argument("--status-script", default="/run/circus-router/status.sh")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.port, Routes(args.routes), args.status_script))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
