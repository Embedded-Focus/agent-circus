"""Connection accounting for the central proxy (pure, no I/O).

The tracker records one :class:`ConnectionRecord` per proxied
connection: who connected, which Host it named, the first request line,
the first response status, byte counters and the outcome. It also keeps
per-route totals and a fixed-length series of time buckets for the
dashboard's traffic sparkline.

Everything runs on the proxy's event loop thread, so there are no locks.
The clock is injectable for deterministic tests. Privacy: only the
request line is stored (query string removed); never headers or bodies.
"""

from __future__ import annotations

import math
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

#: Number of closed connections kept for the dashboard.
RECENT_LIMIT = 200
#: Width of one traffic bucket, in seconds.
BUCKET_SECONDS = 10
#: Number of traffic buckets kept (60 x 10 s = 10 minutes).
BUCKET_COUNT = 60
#: Longest path stored per connection.
MAX_PATH_CHARS = 120

_STATUS_LINE = re.compile(rb"^HTTP/1\.[01] (\d{3})")


def parse_request_line(head: bytes) -> tuple[str | None, str | None]:
    """Return ``(method, path)`` of a request head, query string removed.

    The path is cut at the first ``?`` or ``#`` and to MAX_PATH_CHARS.

    :param head: Raw request head bytes.
    :returns: Method and path, or ``(None, None)`` when the request line
        is malformed.
    """
    request_line = head.split(b"\r\n", 1)[0]
    parts = request_line.split(b" ")
    if len(parts) != 3:
        return None, None
    try:
        method = parts[0].decode("ascii")
        path = parts[1].decode("latin-1")
    except UnicodeDecodeError:
        return None, None
    path = path.partition("?")[0].partition("#")[0]
    return method, path[:MAX_PATH_CHARS]


def parse_response_status(chunk: bytes) -> int | None:
    """Return the status code if *chunk* starts with an HTTP/1.x status line.

    :param chunk: First bytes sent back to the client.
    :returns: The three-digit status code, or ``None`` when the chunk
        does not start with an HTTP/1.0 or HTTP/1.1 status line.
    """
    match = _STATUS_LINE.match(chunk)
    return int(match.group(1)) if match else None


@dataclass
class ConnectionRecord:
    """State of one proxied connection.

    :ivar id: Monotonically increasing connection id.
    :ivar started_at: Epoch seconds of the accept.
    :ivar client: ``"host:port"`` of the client, or ``"-"``.
    :ivar host: Raw Host header value, or ``None``.
    :ivar sandbox: Sandbox name from the Host, or ``None`` when rejected.
    :ivar daemon: Daemon name from the Host, or ``None`` when rejected.
    :ivar method: Method of the first request, or ``None``.
    :ivar path: Path of the first request (query string removed), or
        ``None``.
    :ivar status: First response status, or the proxy's own error code.
    :ivar outcome: ``open``, ``closed`` or ``rejected``.
    :ivar error: Proxy error text for rejected connections.
    :ivar ended_at: Epoch seconds of the close, or ``None`` while open.
    :ivar bytes_up: Bytes copied client -> daemon.
    :ivar bytes_down: Bytes copied daemon -> client.
    """

    id: int
    started_at: float
    client: str
    host: str | None
    sandbox: str | None
    daemon: str | None
    method: str | None
    path: str | None
    status: int | None = None
    outcome: Literal["open", "closed", "rejected"] = "open"
    error: str | None = None
    ended_at: float | None = None
    bytes_up: int = 0
    bytes_down: int = 0

    def to_json(self, now: float) -> dict[str, Any]:
        """Return the JSON shape of the record for the dashboard API.

        :param now: Current epoch seconds (for the duration of open
            connections).
        :returns: A JSON-serializable dict.
        """
        ended = self.ended_at
        duration = (ended if ended is not None else now) - self.started_at
        return {
            "id": self.id,
            "started_at": self.started_at,
            "ended_at": ended,
            "duration_s": round(duration, 3),
            "client": self.client,
            "host": self.host,
            "sandbox": self.sandbox,
            "daemon": self.daemon,
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "outcome": self.outcome,
            "error": self.error,
            "bytes_up": self.bytes_up,
            "bytes_down": self.bytes_down,
        }


@dataclass
class RouteStats:
    """Aggregated counters of one ``(sandbox, daemon)`` route.

    :ivar active: Currently open connections.
    :ivar total: Connections ever opened.
    :ivar errors: Rejected connections plus first responses >= 500.
    :ivar bytes_up: Bytes copied client -> daemon.
    :ivar bytes_down: Bytes copied daemon -> client.
    :ivar last_at: Epoch seconds of the last connection open.
    """

    active: int = 0
    total: int = 0
    errors: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    last_at: float | None = None


@dataclass
class _Bucket:
    """Mutable traffic counter of one time window.

    :ivar t: Bucket start in epoch seconds.
    :ivar connections: Connections opened in this window.
    :ivar errors: Errors counted in this window.
    :ivar bytes_up: Bytes copied client -> daemon in this window.
    :ivar bytes_down: Bytes copied daemon -> client in this window.
    """

    t: float
    connections: int = 0
    errors: int = 0
    bytes_up: int = 0
    bytes_down: int = 0


class ConnectionTracker:
    """Record proxied connections and aggregate traffic, in memory.

    All methods run on the proxy's event loop thread; no locking needed.
    The clock is injectable for tests.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        """Store the clock and start with empty state.

        :param clock: Epoch-seconds callable used for every timestamp.
        """
        self._clock = clock
        self._next_id = 1
        self._active: dict[int, ConnectionRecord] = {}
        self._recent: deque[ConnectionRecord] = deque(maxlen=RECENT_LIMIT)
        self._routes: dict[tuple[str, str], RouteStats] = {}
        self._buckets: deque[_Bucket] = deque(maxlen=BUCKET_COUNT)

    def _bucket(self, when: float) -> _Bucket:
        """Return the current bucket, extending the series if needed.

        :param when: Epoch seconds to bucket for.
        :returns: The bucket covering *when*.
        """
        key = math.floor(when / BUCKET_SECONDS) * BUCKET_SECONDS
        if not self._buckets or self._buckets[-1].t < key:
            self._buckets.append(_Bucket(t=key))
        return self._buckets[-1]

    def open(
        self,
        *,
        client: str,
        head: bytes | None,
        host: str | None,
        sandbox: str | None,
        daemon: str | None,
    ) -> ConnectionRecord:
        """Register a newly accepted connection.

        Counts one connection in the current bucket and, when both the
        sandbox and the daemon are known, one in that route's totals.

        :param client: ``"host:port"`` of the client.
        :param head: Raw request head, or ``None`` when none was read.
        :param host: Host header value, or ``None``.
        :param sandbox: Sandbox name, or ``None`` when the Host is not
            a proxy host.
        :param daemon: Daemon name, or ``None``.
        :returns: The new record; keep it and pass it to the other
            methods.
        """
        now = self._clock()
        method, path = parse_request_line(head) if head is not None else (None, None)
        record = ConnectionRecord(
            id=self._next_id,
            started_at=now,
            client=client,
            host=host,
            sandbox=sandbox,
            daemon=daemon,
            method=method,
            path=path,
        )
        self._next_id += 1
        self._active[record.id] = record
        self._bucket(now).connections += 1
        if sandbox is not None and daemon is not None:
            stats = self._routes.setdefault((sandbox, daemon), RouteStats())
            stats.active += 1
            stats.total += 1
            stats.last_at = now
        return record

    def reject(self, record: ConnectionRecord, status: int, error: str) -> None:
        """Mark a connection as rejected and close it.

        Counts one error in the current bucket and the route stats.

        :param record: The record to reject.
        :param status: HTTP status the proxy answered with.
        :param error: Short error text shown in the dashboard.
        """
        record.outcome = "rejected"
        record.status = status
        record.error = error
        now = self._clock()
        self._bucket(now).errors += 1
        if record.sandbox is not None and record.daemon is not None:
            self._routes[(record.sandbox, record.daemon)].errors += 1
        self.close(record)

    def add_up(self, record: ConnectionRecord, data: bytes) -> None:
        """Account for bytes copied client -> daemon.

        :param record: The record to update.
        :param data: The copied chunk.
        """
        record.bytes_up += len(data)
        self._bucket(self._clock()).bytes_up += len(data)
        if record.sandbox is not None and record.daemon is not None:
            self._routes[(record.sandbox, record.daemon)].bytes_up += len(data)

    def add_down(self, record: ConnectionRecord, data: bytes) -> None:
        """Account for bytes copied daemon -> client.

        The first chunk is inspected for an HTTP status line, which
        becomes the record's status.

        :param record: The record to update.
        :param data: The copied chunk.
        """
        if record.bytes_down == 0:
            record.status = parse_response_status(data)
        record.bytes_down += len(data)
        self._bucket(self._clock()).bytes_down += len(data)
        if record.sandbox is not None and record.daemon is not None:
            self._routes[(record.sandbox, record.daemon)].bytes_down += len(data)

    def close(self, record: ConnectionRecord) -> None:
        """Move a connection out of the active set. Idempotent.

        A connection whose first response status is >= 500 counts as an
        error (once).

        :param record: The record to close.
        """
        if record.id not in self._active:
            return
        del self._active[record.id]
        now = self._clock()
        record.ended_at = now
        if record.outcome != "rejected":
            record.outcome = "closed"
            if record.status is not None and record.status >= 500:
                self._bucket(now).errors += 1
                if record.sandbox is not None and record.daemon is not None:
                    self._routes[(record.sandbox, record.daemon)].errors += 1
        self._recent.appendleft(record)
        if record.sandbox is not None and record.daemon is not None:
            self._routes[(record.sandbox, record.daemon)].active -= 1

    def snapshot(self) -> dict[str, Any]:
        """Return the ``traffic`` object of the dashboard state API.

        :returns: Dict with ``active`` (oldest first), ``recent`` (newest
            first, at most 100), ``routes`` (sorted by sandbox, daemon)
            and ``series`` (exactly BUCKET_COUNT buckets, oldest first).
        """
        now = self._clock()
        current = math.floor(now / BUCKET_SECONDS) * BUCKET_SECONDS
        buckets: list[dict[str, Any]] = []
        by_key = {b.t: b for b in self._buckets}
        for offset in range(BUCKET_COUNT - 1, -1, -1):
            t = current - offset * BUCKET_SECONDS
            b = by_key.get(t)
            buckets.append(
                {
                    "t": t,
                    "connections": b.connections if b else 0,
                    "errors": b.errors if b else 0,
                    "bytes": (b.bytes_up + b.bytes_down) if b else 0,
                }
            )
        return {
            "active": [r.to_json(now) for r in self._active.values()],
            "recent": [r.to_json(now) for r in list(self._recent)[:100]],
            "routes": [
                {
                    "sandbox": sandbox,
                    "daemon": daemon,
                    "active": stats.active,
                    "total": stats.total,
                    "errors": stats.errors,
                    "bytes_up": stats.bytes_up,
                    "bytes_down": stats.bytes_down,
                    "last_at": stats.last_at,
                }
                for (sandbox, daemon), stats in sorted(self._routes.items())
            ],
            "series": {
                "bucket_s": BUCKET_SECONDS,
                "buckets": buckets,
            },
        }
