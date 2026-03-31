"""Tests for CA certificate forwarding."""

import json
from pathlib import Path

from agent_circus.config import (
    AVAILABLE_SERVICES,
    build_ca_certs_override,
    match_files,
)

# ---------------------------------------------------------------------------
# match_files
# ---------------------------------------------------------------------------


def test_match_files_glob_match(tmp_path: Path) -> None:
    (tmp_path / "corp-root.crt").write_text("cert")
    result = match_files(str(tmp_path), ["corp-*.crt"])
    assert str(tmp_path / "corp-root.crt") in result


def test_match_files_glob_no_match(tmp_path: Path) -> None:
    (tmp_path / "unrelated.crt").write_text("cert")
    result = match_files(str(tmp_path), ["corp-*.crt"])
    assert result == []


def test_match_files_regex_prefix(tmp_path: Path) -> None:
    (tmp_path / "internal-ca.crt").write_text("cert")
    result = match_files(str(tmp_path), ["re:internal"])
    assert str(tmp_path / "internal-ca.crt") in result


def test_match_files_only_files_not_dirs(tmp_path: Path) -> None:
    subdir = tmp_path / "corp-subdir.crt"
    subdir.mkdir()
    result = match_files(str(tmp_path), ["corp-*.crt"])
    assert result == []


def test_match_files_missing_dir_returns_empty() -> None:
    result = match_files("/nonexistent/path", ["*.crt"])
    assert result == []


def test_match_files_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "Corp-Root.CRT").write_text("cert")
    result = match_files(str(tmp_path), ["corp-*.crt"])
    assert len(result) == 1


def test_match_files_returns_sorted(tmp_path: Path) -> None:
    for name in ["z.crt", "a.crt", "m.crt"]:
        (tmp_path / name).write_text("cert")
    result = match_files(str(tmp_path), ["*.crt"])
    assert result == sorted(result)


# ---------------------------------------------------------------------------
# build_ca_certs_override
# ---------------------------------------------------------------------------


def test_build_ca_certs_override_structure(tmp_path: Path) -> None:
    cert = str(tmp_path / "corp-root.crt")
    result = json.loads(build_ca_certs_override([cert]))
    for svc in AVAILABLE_SERVICES:
        volumes = result["services"][svc]["volumes"]
        assert any("/run/ca-host/corp-root.crt:ro" in v for v in volumes)


def test_build_ca_certs_override_all_services(tmp_path: Path) -> None:
    result = json.loads(build_ca_certs_override([str(tmp_path / "ca.crt")]))
    assert set(result["services"].keys()) == set(AVAILABLE_SERVICES)


def test_build_ca_certs_override_returns_valid_json(tmp_path: Path) -> None:
    output = build_ca_certs_override([str(tmp_path / "a.crt"), str(tmp_path / "b.crt")])
    parsed = json.loads(output)
    assert isinstance(parsed, dict)
