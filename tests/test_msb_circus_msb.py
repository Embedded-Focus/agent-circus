"""Tests for msb binary resolution, version probing, and the invoker."""

import subprocess
from pathlib import Path

import pytest

import msb_circus.msb as msb_module
from msb_circus.exceptions import MsbCommandError, MsbNotFoundError
from msb_circus.msb import MsbInvoker, check_version, probe_version, resolve_msb_binary


def test_resolve_msb_binary_prefers_config_path(tmp_path):
    binary = tmp_path / "msb"
    binary.touch()
    assert resolve_msb_binary(str(binary)) == binary


def test_resolve_msb_binary_rejects_missing_config_path(tmp_path):
    with pytest.raises(MsbNotFoundError, match="does not exist"):
        resolve_msb_binary(str(tmp_path / "absent"))


def test_resolve_msb_binary_falls_back_to_bundled(monkeypatch):
    bundled = Path("/bundled/msb")
    monkeypatch.setattr(msb_module, "_bundled_msb_path", lambda: bundled)
    monkeypatch.setattr(msb_module.shutil, "which", lambda name: None)
    assert resolve_msb_binary(None) == bundled


def test_resolve_msb_binary_falls_back_to_path(monkeypatch):
    monkeypatch.setattr(msb_module, "_bundled_msb_path", lambda: None)
    monkeypatch.setattr(msb_module.shutil, "which", lambda name: "/usr/bin/msb")
    assert resolve_msb_binary(None) == Path("/usr/bin/msb")


def test_resolve_msb_binary_raises_when_nothing_found(monkeypatch):
    monkeypatch.setattr(msb_module, "_bundled_msb_path", lambda: None)
    monkeypatch.setattr(msb_module.shutil, "which", lambda name: None)
    with pytest.raises(MsbNotFoundError, match="No msb binary found"):
        resolve_msb_binary(None)


def test_probe_version_parses_semver(tmp_path):
    fake = tmp_path / "msb"
    fake.write_text("#!/bin/sh\necho 'msb 0.7.11'\n")
    fake.chmod(0o755)
    assert probe_version(fake) == "0.7.11"


def test_probe_version_returns_none_on_garbage(tmp_path):
    fake = tmp_path / "msb"
    fake.write_text("#!/bin/sh\necho 'nothing useful'\n")
    fake.chmod(0o755)
    assert probe_version(fake) is None


def test_check_version_warns_on_drift(tmp_path, caplog):
    import logging

    fake = tmp_path / "msb"
    fake.write_text("#!/bin/sh\necho 'msb 9.9.9'\n")
    fake.chmod(0o755)
    with caplog.at_level(logging.WARNING):
        version = check_version(fake, expected="0.7.11")
    assert version == "9.9.9"
    assert any("differs" in record.getMessage() for record in caplog.records)


def test_check_version_skips_drift_check_for_bundled_binary(
    tmp_path, monkeypatch, caplog
):
    import logging

    fake_bundled = tmp_path / "bundled-msb"
    fake_bundled.write_text("#!/bin/sh\necho 'msb 9.9.9'\n")
    fake_bundled.chmod(0o755)
    monkeypatch.setattr(msb_module, "_bundled_msb_path", lambda: fake_bundled)
    with caplog.at_level(logging.WARNING):
        version = check_version(fake_bundled, expected="0.7.11")
    assert version == "9.9.9"
    assert not any("differs" in record.getMessage() for record in caplog.records)


def test_check_version_passes_when_versions_match(tmp_path, caplog):
    import logging

    fake = tmp_path / "msb"
    fake.write_text("#!/bin/sh\necho 'msb 0.7.12'\n")
    fake.chmod(0o755)
    with caplog.at_level(logging.WARNING):
        version = check_version(fake, expected="0.7.12")
    assert version == "0.7.12"
    assert not any("differs" in record.getMessage() for record in caplog.records)


def test_expected_msb_version_follows_installed_distribution():
    """The expected version tracks the installed microsandbox wheel."""
    pytest.importorskip("microsandbox")
    from importlib.metadata import version as dist_version

    assert msb_module.expected_msb_version() == dist_version("microsandbox")


def test_invoker_raises_command_error_with_stderr(tmp_path):
    fake = tmp_path / "msb"
    fake.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    fake.chmod(0o755)
    invoker = MsbInvoker(fake)
    with pytest.raises(MsbCommandError) as exc_info:
        invoker.run(["frobnicate"])
    assert exc_info.value.returncode == 3
    assert "boom" in exc_info.value.stderr


def test_invoker_returns_completed_process_on_success(tmp_path):
    fake = tmp_path / "msb"
    fake.write_text("#!/bin/sh\necho ok\n")
    fake.chmod(0o755)
    result = MsbInvoker(fake).run(["list"])
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"


def test_invoker_appends_remediation_hint_for_storage_perm_error(tmp_path):
    fake = tmp_path / "msb"
    fake.write_text(
        "#!/bin/sh\n"
        "echo 'error: io error: catalog ancestor /home/x/.microsandbox "
        "permits path substitution; use a storage root protected from "
        "other accounts' >&2\nexit 1\n"
    )
    fake.chmod(0o755)
    with pytest.raises(MsbCommandError) as exc_info:
        MsbInvoker(fake).run(["list"])
    assert "permits path substitution" in str(exc_info.value)
    assert "chmod g-w,o-w" in str(exc_info.value)


def test_remediation_hint_empty_for_unknown_errors():
    from msb_circus.msb import remediation_hint

    assert remediation_hint("some unrelated failure") == ""


def test_invoker_reports_missing_binary(tmp_path):
    invoker = MsbInvoker(tmp_path / "absent")
    with pytest.raises(MsbCommandError, match="Failed to execute"):
        invoker.run(["list"])


def test_invoker_times_out(tmp_path):
    fake = tmp_path / "msb"
    fake.write_text("#!/bin/sh\nsleep 5\n")
    fake.chmod(0o755)
    with pytest.raises(MsbCommandError, match="timed out"):
        MsbInvoker(fake).run(["list"], timeout=0.2)


def test_invoker_run_never_reads_host_stdin(tmp_path, monkeypatch):
    """Helper invocations must not consume the host's stdin descriptor.

    Piped ``msb exec`` reads host stdin unconditionally (forwarding it to the
    guest). When msb-circus probes the sandbox or writes the pi trust file,
    an inherited stdin would steal bytes from a concurrent ACP stream — for
    example the editor's initialize request.
    """
    recorded: dict = {}
    real_run = msb_module.subprocess.run

    def fake_run(argv, **kwargs):
        recorded["stdin"] = kwargs.get("stdin")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(msb_module.subprocess, "run", fake_run)
    fake = tmp_path / "msb"
    fake.write_text("#!/bin/sh\necho ok\n")
    fake.chmod(0o755)
    MsbInvoker(fake).run(["list"])
    assert recorded["stdin"] is subprocess.DEVNULL


def test_version_probe_uses_real_bundled_binary_when_available():
    """Smoke check: the bundled msb reports the installed wheel version."""
    pytest.importorskip("microsandbox")
    from importlib.metadata import version as dist_version

    binary = resolve_msb_binary(None)
    result = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert dist_version("microsandbox") in result.stdout
