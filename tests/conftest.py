"""Shared test isolation fixtures."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent_circus.cli as cli_module


@pytest.fixture
def fake_msb(monkeypatch):
    """Patch the CLI to use a fake msb binary returning canned JSON.

    ``list`` calls return ``calls["stdout"]``, ``inspect`` calls return
    ``calls["inspect_stdout"]`` (default: no ports). Every invocation is
    recorded in ``calls["invocations"]``.
    """
    calls: dict[str, Any] = {"invocations": []}

    class FakeInvoker:
        def __init__(self, binary: Path) -> None:
            calls["binary"] = binary

        def run(self, args, **kwargs):
            calls["args"] = args
            calls["invocations"].append(args)
            stdout = (
                calls["inspect_stdout"]
                if args and args[0] == "inspect"
                else calls["stdout"]
            )
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def setup(stdout: str) -> None:
        calls["stdout"] = stdout
        calls.setdefault("inspect_stdout", '{"config": {}}')
        monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
        monkeypatch.setattr(cli_module, "check_version", lambda binary: "0.7.12")
        monkeypatch.setattr(cli_module, "MsbInvoker", FakeInvoker)

    setup("[]")
    return calls


@pytest.fixture(autouse=True)
def isolate_user_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests independent of the invoking user's Agent Circus state.

    Config isolation also covers ``XDG_CONFIG_HOME``: on hosts with a real
    ``~/.config/agent-circus/config.toml``, unsupported-key warnings from
    that file would otherwise leak into CLI output captured by
    ``CliRunner`` and break exact-output assertions.

    :param tmp_path: Per-test temporary directory.
    :param monkeypatch: Pytest environment patch helper.
    :returns: None.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
