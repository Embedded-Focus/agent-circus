"""Shared test isolation fixtures."""

from pathlib import Path

import pytest


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
