"""Tests for the msb-circus build and import commands."""

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import msb_circus.cli as cli_module
from msb_circus.cli import app
from msb_circus.images import build_image_inspect_args, bundled_dockerfile_path

runner = CliRunner()


class FakeInvoker:
    """Records msb invocations; configurable cache probe and load failure."""

    behavior: ClassVar[dict] = {}

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append(args)
        if args[:2] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=self.behavior.get("inspect_rc", 1),
                stdout="",
                stderr="",
            )
        if args[0] == "load":
            if self.behavior.get("load_fail"):
                from msb_circus.exceptions import MsbCommandError

                raise MsbCommandError("msb load failed", returncode=1)
            self.behavior["loaded"] = args
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")


@pytest.fixture
def fake_msb(monkeypatch):
    """Patch the CLI invoker and record host (podman) subprocess calls."""
    FakeInvoker.behavior = {}
    host_calls: list[list[str]] = []

    def fake_host_run(args, **kwargs):
        host_calls.append(list(args))
        if "--output" in args:  # model podman save writing the tar
            Path(args[args.index("--output") + 1]).touch()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
    monkeypatch.setattr(cli_module, "check_version", lambda binary: "0.7.12")
    monkeypatch.setattr(cli_module, "MsbInvoker", FakeInvoker)
    monkeypatch.setattr(cli_module.subprocess, "run", fake_host_run)
    FakeInvoker.behavior["host_calls"] = host_calls
    return FakeInvoker.behavior


def test_build_skips_when_image_is_cached(tmp_path, fake_msb):
    fake_msb["inspect_rc"] = 0
    result = runner.invoke(app, ["build", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "already in the msb cache" in result.output
    assert fake_msb["calls"] == [
        ["image", "inspect", "docker.io/library/msb-agents:26"]
    ]
    assert fake_msb["host_calls"] == []


def test_build_uses_bundled_dockerfile_and_workspace_context(tmp_path, fake_msb):
    result = runner.invoke(app, ["build", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    build_call = fake_msb["host_calls"][0]
    assert build_call[0] == "podman"
    assert build_call[1] == "build"
    assert build_call[build_call.index("--file") + 1] == str(bundled_dockerfile_path())
    assert build_call[build_call.index("--target") + 1] == "agents"
    assert (
        build_call[build_call.index("--tag") + 1] == "docker.io/library/msb-agents:26"
    )
    assert build_call[-1] == str(tmp_path)
    # build -> save -> load order
    assert fake_msb["host_calls"][1][0] == "podman"
    assert fake_msb["host_calls"][1][1] == "save"
    assert fake_msb["calls"][-1][:2] == ["load", "--input"]


def test_build_removes_temp_tar_on_success(tmp_path, fake_msb):
    result = runner.invoke(app, ["build", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "archive removed" in result.output
    save_call = fake_msb["host_calls"][1]
    tar_path = Path(save_call[save_call.index("--output") + 1])
    assert not tar_path.exists()


def test_build_keeps_tar_when_load_fails(tmp_path, fake_msb):
    fake_msb["load_fail"] = True
    result = runner.invoke(app, ["build", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "Image archive kept at" in result.output
    save_call = fake_msb["host_calls"][1]
    tar_path = Path(save_call[save_call.index("--output") + 1])
    assert tar_path.exists()


def test_build_keep_tar_writes_to_requested_path(tmp_path, fake_msb):
    tar_path = tmp_path / "out" / "image.tar"
    result = runner.invoke(
        app, ["build", "--workspace", str(tmp_path), "--keep-tar", str(tar_path)]
    )
    assert result.exit_code == 0
    assert f"archive kept at {tar_path}" in result.output
    assert tar_path.exists()
    save_call = fake_msb["host_calls"][1]
    assert save_call[save_call.index("--output") + 1] == str(tar_path)


def test_build_force_skips_cache_probe(tmp_path, fake_msb):
    fake_msb["inspect_rc"] = 0  # would be skippable, but --force ignores it
    result = runner.invoke(app, ["build", "--workspace", str(tmp_path), "--force"])
    assert result.exit_code == 0
    assert fake_msb["calls"] == [
        call for call in fake_msb["calls"] if call[:2] != ["image", "inspect"]
    ]
    assert fake_msb["calls"][-1][:2] == ["load", "--input"]


def test_build_engine_option_switches_host_commands(tmp_path, fake_msb):
    result = runner.invoke(
        app, ["build", "--workspace", str(tmp_path), "--engine", "docker"]
    )
    assert result.exit_code == 0
    assert fake_msb["host_calls"][0][0] == "docker"
    assert fake_msb["host_calls"][1][0] == "docker"


def test_import_loads_given_tar(tmp_path, fake_msb):
    tar = tmp_path / "image.tar"
    tar.write_bytes(b"")
    result = runner.invoke(app, ["import", str(tar), "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert fake_msb["calls"] == [["load", "--input", str(tar)]]


def test_bundled_dockerfile_template_exists():
    path = bundled_dockerfile_path()
    assert path.is_file()
    content = path.read_text()
    assert "FROM docker.io/library/node:26-slim@" in content
    assert "FROM base AS agents" in content
    assert 'ENTRYPOINT ["/init"]' in content
    assert "CMD []" in content
    assert "docker-entrypoint.sh" not in content


def test_build_image_inspect_args():
    assert build_image_inspect_args("img:1") == ["image", "inspect", "img:1"]


def test_bundled_dockerfile_points_tls_clients_at_system_bundle():
    """Node and Python requests keep their own CA lists; point them at the
    system bundle so CAs that msb adds to the guest are trusted."""
    content = bundled_dockerfile_path().read_text()
    assert "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt" in content
    assert "REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt" in content
    assert "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" in content


def test_dockerfile_installs_ssh_bridge_packages():
    """Guard against the image, constants, and entrypoint drifting apart."""
    content = bundled_dockerfile_path().read_text()
    assert "openssh-client" in content
    assert "socat" in content
    assert "CIRCUS_SSH_AGENT_VSOCK_PORT" in content
    assert "VSOCK-CONNECT:2:" in content


def test_dockerfile_defines_s6_services():
    """The three services are wired into the s6 user bundle correctly."""
    content = bundled_dockerfile_path().read_text()
    for service in ("circus-ca-import", "circus-ssh-agent-bridge", "circus-dockerd"):
        assert service in content
    assert "user-bundles.d/user/contents.d" in content
    assert "circus-dockerd/dependencies.d/circus-ca-import" in content
    assert (
        'exec dockerd --shutdown-timeout "${CIRCUS_DOCKERD_SHUTDOWN_TIMEOUT:-20}"'
        in content
    )
    assert "/tmp/dockerd.log" in content
    assert "/tmp/ssh-agent-bridge.log" in content


def test_dockerfile_pins_s6_overlay():
    """The s6-overlay tarballs are pinned by version and sha256."""
    content = bundled_dockerfile_path().read_text()
    assert "v3.2.3.2" in content
    assert "5379750ed30a84bbd2e2dd74847ba6b5bd29cd0b2e3ea2ec58049b57eb2eda12" in content
    assert "e6befcc96a437a3831386ecfc51808c5d3e939dc5fe3c02ae9284599e8aa2408" in content
    assert "b17f17a82e7a515c682a91edaf2ffdabb73f891981b6c1fd712115693a2f8b4c" in content


def test_dockerfile_dockerd_default_matches_config():
    """The dockerd service default equals DEFAULT_DOCKER_STOP_TIMEOUT."""
    from msb_circus.config import (
        DEFAULT_DOCKER_STOP_TIMEOUT,
        DOCKERD_SHUTDOWN_TIMEOUT_ENV_VAR,
    )

    content = bundled_dockerfile_path().read_text()
    assert (
        f"${{{DOCKERD_SHUTDOWN_TIMEOUT_ENV_VAR}:-{DEFAULT_DOCKER_STOP_TIMEOUT}}}"
        in content
    )


def test_dockerfile_service_scripts_have_no_single_quotes():
    """Script lines are single-quoted printf arguments; an inner single
    quote would break the RUN command, so none may appear."""
    content = bundled_dockerfile_path().read_text()
    for line in (
        "'exec >>/tmp/dockerd.log 2>&1'",
        "'exec >>/tmp/ssh-agent-bridge.log 2>&1'",
        "'    exec sleep infinity'",
    ):
        assert content.count(line) >= 1
