"""Tests for the msb-circus urls command."""

import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import agent_circus.cli as cli_module
from agent_circus.cli import app
from agent_circus.config import sandbox_name_for_workspace

runner = CliRunner()


class FakeInvoker:
    """Records invocations; scripted inspect/list behavior for urls."""

    behavior: ClassVar[dict] = {}

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append(args)
        if args[0] == "inspect":
            return SimpleNamespace(
                returncode=self.behavior.get("inspect_rc", 0),
                stdout=self.behavior.get("inspect_json", "{}"),
                stderr="",
            )
        if args[0] == "list":
            return SimpleNamespace(
                returncode=0,
                stdout=self.behavior.get("list_output", json.dumps([])),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")


@pytest.fixture
def fake_msb(tmp_path, monkeypatch):
    """Patch the CLI invoker; provide a workspace config with daemons."""
    FakeInvoker.behavior = {}
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(
        b"[[msb.daemons]]\n"
        b'name = "web"\n'
        b'command = ["python", "-m", "http.server", "4096"]\n'
        b"port = 4096\n\n"
        b"[[msb.daemons]]\n"
        b'name = "hidden"\n'
        b'command = ["sleep", "1"]\n'
        b"port = 4097\n"
        b"expose = false\n\n"
        b"[[msb.daemons]]\n"
        b'name = "noport"\n'
        b'command = ["sleep", "1"]\n\n'
        b"[[port_forwards]]\n"
        b"container_port = 4096\n"
        b"host_port = 14096\n"
    )
    monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
    monkeypatch.setattr(cli_module, "check_version", lambda binary: "0.7.12")
    monkeypatch.setattr(cli_module, "MsbInvoker", FakeInvoker)
    return FakeInvoker.behavior


def test_urls_lists_exposed_daemons_only(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    fake_msb["inspect_json"] = json.dumps(
        {
            "config": {
                "network": {
                    "ports": [
                        {
                            "guest_port": 7780,
                            "host_bind": "127.0.0.1",
                            "host_port": 51234,
                            "protocol": "tcp",
                        }
                    ]
                }
            }
        }
    )
    result = runner.invoke(app, ["urls", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "NAME" in result.output
    lines = {line.split()[0]: line for line in result.output.splitlines()[1:] if line}
    assert "web" in lines
    assert f"http://web.{name}.localhost:7700/" in lines["web"]
    assert "http://127.0.0.1:14096" in lines["web"]
    # Hidden and port-less daemons have no row.
    assert "hidden" not in lines
    assert "noport" not in lines


def test_urls_json_output_is_clean(tmp_path, fake_msb):
    """stdout parses as JSON; notes, when present, appear on stderr."""
    name = sandbox_name_for_workspace(tmp_path)
    fake_msb["inspect_json"] = json.dumps(
        {
            "config": {
                "network": {
                    "ports": [
                        {
                            "guest_port": 7780,
                            "host_bind": "127.0.0.1",
                            "host_port": 51234,
                            "protocol": "tcp",
                        }
                    ]
                }
            }
        }
    )
    # A listening port so the proxy probe succeeds and no note is emitted.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        listen_port = listener.getsockname()[1]
        result = runner.invoke(
            app,
            [
                "urls",
                "--format",
                "json",
                "--proxy-port",
                str(listen_port),
                "--workspace",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload == [
            {
                "name": "web",
                "port": 4096,
                "url": f"http://web.{name}.localhost:{listen_port}/",
                "direct_url": "http://127.0.0.1:14096",
            }
        ]
        # With a note emitted, it lands on stderr, not in the JSON text.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        free_port = blocker.getsockname()[1]
        blocker.close()
        noted = runner.invoke(
            app,
            [
                "urls",
                "--format",
                "json",
                "--proxy-port",
                str(free_port),
                "--workspace",
                str(tmp_path),
            ],
        )
        assert noted.exit_code == 0
        assert "the proxy is not running" in noted.stderr
        # The last stdout line is still the pure JSON payload (the note
        # went to stderr); its URLs use the free port given via --proxy-port.
        noted_payload = json.loads(noted.output.splitlines()[-1])
        assert noted_payload == [
            {
                "name": "web",
                "port": 4096,
                "url": f"http://web.{name}.localhost:{free_port}/",
                "direct_url": "http://127.0.0.1:14096",
            }
        ]
    finally:
        listener.close()


def test_urls_notes_missing_router_forward(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    fake_msb["inspect_json"] = json.dumps(
        {
            "config": {
                "network": {
                    "ports": [
                        {
                            "guest_port": 4096,
                            "host_bind": "127.0.0.1",
                            "host_port": 14096,
                            "protocol": "tcp",
                        }
                    ]
                }
            }
        }
    )
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    result = runner.invoke(app, ["urls", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "created without the router port" in result.stderr


def test_urls_notes_missing_sandbox(tmp_path, fake_msb):
    fake_msb["inspect_json"] = "{}"
    fake_msb["list_output"] = json.dumps([])
    result = runner.invoke(app, ["urls", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "does not exist yet" in result.stderr


def test_urls_notes_proxy_not_running(tmp_path, fake_msb):
    """A free port that nobody listens on triggers the proxy note."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    free_port = blocker.getsockname()[1]
    blocker.close()
    fake_msb["inspect_json"] = json.dumps(
        {
            "config": {
                "network": {
                    "ports": [
                        {
                            "guest_port": 7780,
                            "host_bind": "127.0.0.1",
                            "host_port": 51234,
                            "protocol": "tcp",
                        }
                    ]
                }
            }
        }
    )
    result = runner.invoke(
        app,
        ["urls", "--proxy-port", str(free_port), "--workspace", str(tmp_path)],
    )
    assert result.exit_code == 0
    assert "the proxy is not running" in result.stderr


def test_urls_without_exposed_daemons_prints_hint(tmp_path):
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(
        b'[[msb.daemons]]\nname = "db"\ncommand = ["sleep", "1"]\n'
    )
    result = runner.invoke(app, ["urls", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "No exposed daemons" in result.output
    assert "exposed by default" in result.output
