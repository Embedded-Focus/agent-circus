"""Tests for lightweight msb-circus execution traces."""

from __future__ import annotations

import json
from pathlib import Path

from msb_circus.config import MsbConfig, TracingConfig
from msb_circus.tracing import TraceSession


def _json_lines(path):
    """Read a JSON Lines test file."""
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_trace_records_repetition_across_chunk_boundaries(tmp_path):
    config = TracingConfig(repetition_threshold=4)
    trace = TraceSession.create(
        workspace=tmp_path,
        root=tmp_path / ".agent-circus" / "traces",
        config=config,
        msb_config=MsbConfig(),
        sandbox="agents-test",
        command=["pi-acp", "--flag", "secret prompt"],
        acp=True,
        workdir="/workspace",
        env_names=["OPENAI_API_KEY"],
        msb_version="0.7.12",
    )

    trace.record_stream("agent-to-client", b"!!")
    trace.record_stream("agent-to-client", b"!!x")
    trace.finish(0)

    transport = _json_lines(trace.directory / "acp-transport.jsonl")
    assert transport[1]["longest_repeated_byte"] == {
        "byte": ord("!"),
        "count": 4,
        "offset": 0,
    }
    events = _json_lines(trace.directory / "events.jsonl")
    incidents = [e for e in events if e["event"] == "repetition_detected"]
    assert len(incidents) == 1
    assert incidents[0]["byte"] == ord("!")

    manifest = json.loads((trace.directory / "manifest.json").read_text())
    assert manifest["command"] == "pi-acp"
    assert "secret prompt" not in (trace.directory / "manifest.json").read_text()
    assert manifest["environment_names"] == ["OPENAI_API_KEY"]


def test_trace_payload_capture_stops_at_limit(tmp_path):
    config = TracingConfig(
        capture_payloads=True,
        max_payload_bytes=5,
        repetition_threshold=10,
    )
    trace = TraceSession.create(
        workspace=tmp_path,
        root=tmp_path / "traces",
        config=config,
        msb_config=MsbConfig(),
        sandbox="agents-test",
        command=["pi-acp"],
        acp=True,
        workdir="/workspace",
        env_names=[],
        msb_version=None,
    )

    trace.record_stream("agent-to-client", b"1234")
    trace.record_stream("agent-to-client", b"5678")
    trace.finish(0)

    payload = trace.directory / "payloads" / "agent-to-client.bin"
    assert payload.read_bytes() == b"12345"
    summary = json.loads((trace.directory / "summary.json").read_text())
    assert summary["payload_bytes"]["agent-to-client"] == 5
    assert summary["payload_limit_reached"] == ["agent-to-client"]


def test_trace_files_are_private(tmp_path):
    trace = TraceSession.create(
        workspace=tmp_path,
        root=tmp_path / "traces",
        config=TracingConfig(),
        msb_config=MsbConfig(),
        sandbox="agents-test",
        command=["pi"],
        acp=False,
        workdir="/workspace",
        env_names=[],
        msb_version=None,
    )
    trace.finish(0)

    assert trace.directory.stat().st_mode & 0o777 == 0o700
    assert (trace.directory / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "traces" / "latest").read_text().strip() == trace.trace_id


def test_process_collector_uses_preinstalled_uv_python(tmp_path, monkeypatch):
    trace = TraceSession.create(
        workspace=tmp_path,
        root=tmp_path / "traces",
        config=TracingConfig(),
        msb_config=MsbConfig(),
        sandbox="agents-test",
        command=["pi-acp"],
        acp=True,
        workdir="/workspace",
        env_names=[],
        msb_version=None,
    )
    collectors = []
    monkeypatch.setattr(
        trace,
        "_start_collector",
        lambda name, argv, filename: collectors.append((name, argv, filename)),
    )

    trace.start_collectors(Path("/usr/bin/msb"), "agents-test")
    trace.finish(0)

    process_collector = next(
        item for item in collectors if item[0] == "guest_processes"
    )
    assert process_collector[1][7:14] == [
        "uv",
        "run",
        "--no-project",
        "--offline",
        "--no-python-downloads",
        "--managed-python",
        "python",
    ]
