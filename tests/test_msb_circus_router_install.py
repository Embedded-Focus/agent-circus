"""Tests for the router install helpers (msb_circus.router)."""

import base64
import json
import subprocess
from pathlib import Path

from msb_circus.config import ROUTER_GUEST_PORT
from msb_circus.router import (
    EXIT_NO_PYTHON,
    ROUTER_SCRIPT_ENV_VAR,
    ROUTER_SERVICE,
    build_router_install_script,
    encode_router_script,
    router_script_source,
)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "src" / "msb_circus" / "templates"


def test_install_script_writes_routes_json():
    script = build_router_install_script({"web": 4096, "api": 4097})
    assert "printf '%s' '{\"api\": 4097, \"web\": 4096}' " in script


def test_install_script_uses_router_guest_port():
    script = build_router_install_script({"web": 4096})
    assert f"--port {ROUTER_GUEST_PORT}" in script
    assert "7780" not in script.replace(f"--port {ROUTER_GUEST_PORT}", "")


def test_install_script_checks_python3_with_exit_6():
    script = build_router_install_script({})
    assert "command -v python3" in script
    assert f"exit {EXIT_NO_PYTHON}" in script


def test_install_script_contains_svscan_wait():
    script = build_router_install_script({})
    assert "until /command/s6-svscanctl -a /run/service" in script
    assert f"s={ROUTER_SERVICE}" in script


def test_encode_router_script_roundtrips():
    encoded = encode_router_script()
    assert list(encoded) == [ROUTER_SCRIPT_ENV_VAR]
    assert base64.b64decode(encoded[ROUTER_SCRIPT_ENV_VAR]).decode() == (
        router_script_source()
    )


def test_install_script_runs_in_sh_syntax_check():
    for routes in ({}, {"web": 4096, "opencode-web": 4097}):
        script = build_router_install_script(routes)
        assert subprocess.run(["sh", "-n", "-c", script], check=False).returncode == 0


def test_router_script_source_is_standalone():
    """The guest router must not import msb_circus and must stay stdlib-only."""
    source = router_script_source()
    assert "msb_circus" not in source.replace("circus-router", "").replace(
        "circus_router", ""
    )
    assert "import msb_circus" not in source


def test_install_script_routes_json_matches_expected_content():
    routes = {"web": 4096}
    script = build_router_install_script(routes)
    line = next(l for l in script.splitlines() if "routes.json.tmp" in l)
    expected = json.dumps(routes, sort_keys=True)
    assert f"'{expected}'" in line


def test_image_puts_python3_on_the_default_path():
    """The router probe runs in a plain msb exec shell, which does not
    carry the image ENV: python3 must be reachable without it."""
    dockerfile = (TEMPLATE_DIR / "Dockerfile.msb-agents").read_text()
    # uv installs under /opt/uv-python/cpython-<ver>-<arch>-gnu/ and keeps a
    # stable cpython-3.14-<arch>-gnu symlink; it never creates
    # /opt/uv-python/bin, so the symlink must target the real bin dir.
    assert (
        "ln -sf /opt/uv-python/cpython-3.14-*/bin/python3 /usr/local/bin/python3"
        in dockerfile
    )
    assert "ln -sf /opt/uv-python/bin/" not in dockerfile
    # The image ENV must not promise a PATH entry that does not exist.
    assert "PATH=/opt/uv-python/bin" not in dockerfile
