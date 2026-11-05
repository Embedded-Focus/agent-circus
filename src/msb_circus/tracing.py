"""Lightweight execution traces for ``msb-circus exec``."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, TextIO, cast

from . import __version__
from .config import MsbConfig, TracingConfig

TRACE_FORMAT_VERSION = 1

_PROCESS_SAMPLER = r"""
import json
import os
import time

interval = float(__import__('sys').argv[1])

def read_text(path):
    try:
        return open(path, encoding='utf-8', errors='replace').read().strip()
    except OSError:
        return None

def read_int(path):
    value = read_text(path)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None

while True:
    processes = []
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        base = '/proc/' + name
        stat = read_text(base + '/stat')
        if stat is None or ')' not in stat:
            continue
        tail = stat[stat.rfind(')') + 2:].split()
        if len(tail) < 22:
            continue
        item = {
            'pid': int(name),
            'ppid': int(tail[1]),
            'name': read_text(base + '/comm'),
            'state': tail[0],
            'user_ticks': int(tail[11]),
            'system_ticks': int(tail[12]),
            'threads': int(tail[17]),
            'virtual_bytes': int(tail[20]),
            'resident_pages': int(tail[21]),
            'fd_count': None,
        }
        try:
            item['fd_count'] = len(os.listdir(base + '/fd'))
        except OSError:
            pass
        io_data = read_text(base + '/io')
        if io_data is not None:
            for line in io_data.splitlines():
                key, _, value = line.partition(':')
                if key in ('read_bytes', 'write_bytes'):
                    try:
                        item[key] = int(value.strip())
                    except ValueError:
                        pass
        processes.append(item)
    print(json.dumps({
        'time_ns': time.time_ns(),
        'monotonic_ns': time.monotonic_ns(),
        'clock_ticks_per_second': os.sysconf('SC_CLK_TCK'),
        'page_size': os.sysconf('SC_PAGE_SIZE'),
        'processes': processes,
    }, separators=(',', ':')), flush=True)
    time.sleep(interval)
"""


@dataclass
class _RunState:
    """Track one repeated byte across trace chunks."""

    last_byte: int | None = None
    count: int = 0
    offset: int = 0
    run_start: int = 0


def _utc_now() -> str:
    """Return the current UTC time in ISO 8601 format.

    :returns: Current UTC time.
    """
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _secure_text_file(path: Path, mode: str = "w") -> TextIO:
    """Open a private UTF-8 text file.

    :param path: File path.
    :param mode: Text open mode.
    :returns: Open text file.
    """
    file = cast(TextIO, path.open(mode, encoding="utf-8"))
    path.chmod(0o600)
    return file


def _secure_binary_file(path: Path) -> BinaryIO:
    """Open a private binary file.

    :param path: File path.
    :returns: Open binary file.
    """
    file = path.open("wb")
    path.chmod(0o600)
    return file


class TraceSession:
    """Own trace files and collectors for one agent execution."""

    def __init__(
        self,
        *,
        workspace: Path,
        root: Path,
        config: TracingConfig,
        manifest: dict[str, Any],
    ) -> None:
        """Create a trace directory and its initial files.

        :param workspace: Project workspace.
        :param root: Trace root directory.
        :param config: Effective trace settings.
        :param manifest: Sanitized execution metadata.
        """
        self.workspace = workspace
        self.config = config
        trace_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        trace_id += f"-{secrets.token_hex(2)}"
        root_exists = root.exists()
        root.mkdir(parents=True, exist_ok=True)
        if not root_exists:
            root.chmod(0o700)
        self.directory = root / trace_id
        self.directory.mkdir(mode=0o700)
        self.trace_id = trace_id
        self.started_at = _utc_now()
        self.started_monotonic_ns = time.monotonic_ns()
        self._lock = threading.Lock()
        self._events = _secure_text_file(self.directory / "events.jsonl")
        self._transport = _secure_text_file(self.directory / "acp-transport.jsonl")
        self._collector_errors = _secure_text_file(
            self.directory / "collector-errors.log", "a"
        )
        self._collectors: list[subprocess.Popen[bytes]] = []
        self._collector_files: list[TextIO] = []
        self._run_states: dict[str, _RunState] = {}
        self._sequences: dict[str, int] = {}
        self._incidents = 0
        self._payload_files: dict[str, BinaryIO] = {}
        self._payload_bytes: dict[str, int] = {}
        self._payload_limited: set[str] = set()
        self._log_handler: logging.FileHandler | None = None

        if config.capture_payloads:
            payload_dir = self.directory / "payloads"
            payload_dir.mkdir(mode=0o700)
            for direction, filename in {
                "client-to-agent": "client-to-agent.bin",
                "agent-to-client": "agent-to-client.bin",
                "agent-stderr": "agent-stderr.bin",
            }.items():
                self._payload_files[direction] = _secure_binary_file(
                    payload_dir / filename
                )
                self._payload_bytes[direction] = 0

        document = {
            "trace_format_version": TRACE_FORMAT_VERSION,
            "trace_id": trace_id,
            "started_at": self.started_at,
            "msb_circus_version": __version__,
            **manifest,
        }
        self._write_json(self.directory / "manifest.json", document)
        self._write_latest(root, trace_id)
        self._add_log_handler()
        self.record_event("trace_started", directory=str(self.directory))

    @classmethod
    def create(
        cls,
        *,
        workspace: Path,
        root: Path,
        config: TracingConfig,
        msb_config: MsbConfig,
        sandbox: str,
        command: list[str],
        acp: bool,
        workdir: str,
        env_names: list[str],
        msb_version: str | None,
    ) -> TraceSession:
        """Create a trace with sanitized execution metadata.

        :param workspace: Project workspace.
        :param root: Trace root directory.
        :param config: Effective trace settings.
        :param msb_config: Effective msb-circus configuration.
        :param sandbox: Sandbox name.
        :param command: Agent command. Only its executable name is saved.
        :param acp: Whether the execution uses ACP.
        :param workdir: Guest working directory.
        :param env_names: Names of passed environment variables.
        :param msb_version: Detected msb version.
        :returns: New trace session.
        """
        manifest = {
            "workspace": str(workspace),
            "sandbox": sandbox,
            "mode": "acp" if acp else "terminal",
            "command": Path(command[0]).name if command else None,
            "workdir": workdir,
            "environment_names": sorted(env_names),
            "msb_version": msb_version,
            "sandbox_resources": {
                "memory": msb_config.sandbox.memory,
                "cpus": msb_config.sandbox.cpus,
                "root_disk": msb_config.sandbox.root_disk,
                "docker_disk": msb_config.sandbox.docker_disk,
            },
            "tracing": asdict(config),
        }
        return cls(workspace=workspace, root=root, config=config, manifest=manifest)

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        """Write one private JSON document.

        :param path: Destination path.
        :param value: JSON object.
        """
        with _secure_text_file(path) as file:
            json.dump(value, file, indent=2, sort_keys=True)
            file.write("\n")

    def _write_latest(self, root: Path, trace_id: str) -> None:
        """Write the ID of the newest trace.

        :param root: Trace root directory.
        :param trace_id: New trace ID.
        """
        latest = root / "latest"
        with _secure_text_file(latest) as file:
            file.write(trace_id + "\n")

    def _add_log_handler(self) -> None:
        """Copy msb-circus logs into this trace."""
        path = self.directory / "msb-circus.log"
        handler = logging.FileHandler(path)
        path.chmod(0o600)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(handler)
        self._log_handler = handler

    def record_event(self, event: str, **fields: Any) -> None:
        """Write a trace event.

        :param event: Event name.
        :param fields: Extra JSON fields.
        """
        entry = {
            "time": _utc_now(),
            "monotonic_ns": time.monotonic_ns(),
            "event": event,
            **fields,
        }
        with self._lock:
            self._events.write(json.dumps(entry, separators=(",", ":")) + "\n")
            self._events.flush()

    def record_stream(self, direction: str, data: bytes) -> None:
        """Record metadata for one ACP stream chunk.

        :param direction: Stream direction.
        :param data: Exact bytes that pass through the bridge.
        """
        if not data:
            return
        incidents: list[dict[str, int]] = []
        with self._lock:
            state = self._run_states.setdefault(direction, _RunState())
            sequence = self._sequences.get(direction, 0) + 1
            self._sequences[direction] = sequence
            longest_byte: int | None = None
            longest_count = 0
            longest_offset = state.offset
            for byte in data:
                if byte == state.last_byte:
                    state.count += 1
                else:
                    state.last_byte = byte
                    state.count = 1
                    state.run_start = state.offset
                if state.count > longest_count:
                    longest_byte = byte
                    longest_count = state.count
                    longest_offset = state.run_start
                if state.count == self.config.repetition_threshold:
                    incidents.append(
                        {
                            "byte": byte,
                            "count": state.count,
                            "offset": state.run_start,
                        }
                    )
                state.offset += 1

            entry: dict[str, Any] = {
                "time": _utc_now(),
                "monotonic_ns": time.monotonic_ns(),
                "direction": direction,
                "sequence": sequence,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "newlines": data.count(b"\n"),
                "stream_offset": state.offset - len(data),
            }
            if longest_byte is not None:
                entry["longest_repeated_byte"] = {
                    "byte": longest_byte,
                    "count": longest_count,
                    "offset": longest_offset,
                }
            self._transport.write(json.dumps(entry, separators=(",", ":")) + "\n")
            self._transport.flush()
            self._write_payload_locked(direction, data)
            self._incidents += len(incidents)

        for incident in incidents:
            self.record_event(
                "repetition_detected",
                direction=direction,
                sequence=sequence,
                **incident,
            )

    def _write_payload_locked(self, direction: str, data: bytes) -> None:
        """Write raw bytes while the trace lock is held.

        :param direction: Stream direction.
        :param data: Stream bytes.
        """
        file = self._payload_files.get(direction)
        if file is None or direction in self._payload_limited:
            return
        used = self._payload_bytes[direction]
        remaining = self.config.max_payload_bytes - used
        if remaining > 0:
            chunk = data[:remaining]
            file.write(chunk)
            file.flush()
            self._payload_bytes[direction] += len(chunk)
        if len(data) > remaining:
            self._payload_limited.add(direction)

    def start_collectors(self, binary: Path, sandbox: str) -> None:
        """Start VM, runtime, and guest process collectors.

        :param binary: Resolved msb binary.
        :param sandbox: Sandbox name.
        """
        interval = f"{self.config.sample_interval:g}s"
        self._start_collector(
            "vm_metrics",
            [
                str(binary),
                "metrics",
                sandbox,
                "--follow",
                "--interval",
                interval,
            ],
            "vm-metrics.jsonl",
        )
        self._start_collector(
            "msb_system_logs",
            [
                str(binary),
                "logs",
                sandbox,
                "--follow",
                "--source",
                "system",
                "--json",
            ],
            "msb-system.jsonl",
        )
        self._start_collector(
            "guest_processes",
            [
                str(binary),
                "exec",
                "--no-tty",
                "--no-stdin",
                "--quiet",
                sandbox,
                "--",
                "uv",
                "run",
                "--no-project",
                "--offline",
                "--no-python-downloads",
                "--managed-python",
                "python",
                "-u",
                "-c",
                _PROCESS_SAMPLER,
                str(self.config.sample_interval),
            ],
            "processes.jsonl",
        )

    def _start_collector(self, name: str, argv: list[str], filename: str) -> None:
        """Start one collector and record a failure without stopping the agent.

        :param name: Collector name.
        :param argv: Collector command.
        :param filename: Trace output file.
        """
        try:
            output = _secure_text_file(self.directory / filename)
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=self._collector_errors,
            )
        except OSError as error:
            if "output" in locals():
                output.close()
            self.record_event(
                "collector_start_failed", collector=name, error=str(error)
            )
            return
        self._collector_files.append(output)
        self._collectors.append(process)
        self.record_event("collector_started", collector=name, pid=process.pid)

    def finish(self, returncode: int, reason: str = "process_exit") -> None:
        """Stop collectors and write the trace summary.

        :param returncode: Agent process exit code.
        :param reason: End reason.
        """
        self.record_event("trace_stopping", returncode=returncode, reason=reason)
        for process in self._collectors:
            if process.poll() is None:
                process.terminate()
        for process in self._collectors:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        for file in self._collector_files:
            file.close()
        self._collector_errors.close()
        for file in self._payload_files.values():
            file.close()

        summary = {
            "trace_format_version": TRACE_FORMAT_VERSION,
            "trace_id": self.trace_id,
            "started_at": self.started_at,
            "ended_at": _utc_now(),
            "duration_seconds": (time.monotonic_ns() - self.started_monotonic_ns)
            / 1_000_000_000,
            "returncode": returncode,
            "reason": reason,
            "repetition_incidents": self._incidents,
            "stream_chunks": self._sequences,
            "payload_bytes": self._payload_bytes,
            "payload_limit_reached": sorted(self._payload_limited),
            **self._resource_summary(),
        }
        self._write_json(self.directory / "summary.json", summary)
        self._events.close()
        self._transport.close()
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler.close()

    def _resource_summary(self) -> dict[str, Any]:
        """Read compact resource totals from collector output.

        :returns: Resource summary fields.
        """
        maximum_cpu: float | None = None
        maximum_memory: int | None = None
        metrics_path = self.directory / "vm-metrics.jsonl"
        if metrics_path.is_file():
            with metrics_path.open(encoding="utf-8", errors="replace") as metrics_file:
                for line in metrics_file:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    objects = value if isinstance(value, list) else [value]
                    for item in objects:
                        if not isinstance(item, dict):
                            continue
                        metrics = item.get("metrics", item)
                        if not isinstance(metrics, dict):
                            continue
                        cpu = metrics.get("cpu_percent")
                        memory = metrics.get("memory_bytes")
                        if isinstance(cpu, int | float) and not isinstance(cpu, bool):
                            maximum_cpu = max(maximum_cpu or cpu, float(cpu))
                        if isinstance(memory, int) and not isinstance(memory, bool):
                            maximum_memory = max(maximum_memory or memory, memory)
        return {
            "maximum_vm_cpu_percent": maximum_cpu,
            "maximum_vm_memory_bytes": maximum_memory,
        }
