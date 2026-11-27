"""Tests for the agent-circus volumes command and its formatters."""

import json

from typer.testing import CliRunner

from agent_circus.cli import app
from agent_circus.sandbox import VolumeInfo, parse_volume_list_output
from agent_circus.volumes import (
    format_capacity,
    format_volume_json,
    format_volume_table,
)

runner = CliRunner()


VOLUME_LIST_OUTPUT = json.dumps(
    [
        {"name": "agents-foo-docker", "kind": "disk", "capacity_bytes": 10737418240},
        {"name": "shared", "kind": "dir", "capacity_bytes": 500000000},
        {"name": "legacy", "capacity_bytes": None},
    ]
)


# ---- formatters ---------------------------------------------------------------


def test_format_capacity_whole_gib_uses_g_suffix():
    assert format_capacity(10737418240) == "10G"


def test_format_capacity_other_sizes_use_m_suffix():
    assert format_capacity(500000000) == "476M"
    assert format_capacity(12345) == "0M"


def test_format_volume_table_lists_name_kind_size():
    table = format_volume_table(parse_volume_list_output(VOLUME_LIST_OUTPUT))
    lines = table.splitlines()
    assert lines[0].split() == ["NAME", "KIND", "SIZE"]
    assert lines[1].startswith("agents-foo-docker")
    assert "disk" in lines[1]
    assert "10G" in lines[1]
    assert "476M" in lines[2]
    # Missing values render as the placeholder.
    assert "legacy" in lines[3] and "-" in lines[3]


def test_format_volume_table_columns_are_aligned():
    volumes = [
        VolumeInfo(name="a-very-long-volume-name", kind="disk", capacity_bytes=1024),
        VolumeInfo(name="x", kind="dir", capacity_bytes=None),
    ]
    lines = format_volume_table(volumes).splitlines()
    # Every row (except a short last cell) starts its KIND column at the
    # same offset, because cells are padded to the column width.
    assert {line.index("disk" if "disk" in line else "dir") for line in lines[1:]} == {
        len(lines[0].split("KIND")[0])
    }


def test_format_volume_table_empty_selection_keeps_header():
    assert format_volume_table([]) == "NAME  KIND  SIZE"


def test_format_volume_json_shape_is_stable():
    parsed = json.loads(
        format_volume_json(parse_volume_list_output(VOLUME_LIST_OUTPUT))
    )
    assert parsed == [
        {
            "name": "agents-foo-docker",
            "kind": "disk",
            "capacity_bytes": 10737418240,
        },
        {"name": "shared", "kind": "dir", "capacity_bytes": 500000000},
        {"name": "legacy", "kind": None, "capacity_bytes": None},
    ]


# ---- CLI command --------------------------------------------------------------


def test_volumes_table_lists_all_volumes(tmp_path, fake_msb):
    fake_msb["stdout"] = VOLUME_LIST_OUTPUT
    result = runner.invoke(app, ["volumes", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "agents-foo-docker" in result.output
    assert "legacy" in result.output
    # One msb call, the bulk volume list.
    assert fake_msb["invocations"] == [["volume", "list", "--format", "json"]]


def test_volumes_json_format_is_parseable(tmp_path, fake_msb):
    fake_msb["stdout"] = VOLUME_LIST_OUTPUT
    result = runner.invoke(
        app, ["volumes", "--workspace", str(tmp_path), "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["name"] == "agents-foo-docker"


def test_volumes_quiet_prints_names_only(tmp_path, fake_msb):
    fake_msb["stdout"] = VOLUME_LIST_OUTPUT
    result = runner.invoke(app, ["volumes", "--workspace", str(tmp_path), "--quiet"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["agents-foo-docker", "shared", "legacy"]


def test_volumes_empty_list_still_prints_header(tmp_path, fake_msb):
    fake_msb["stdout"] = "[]"
    result = runner.invoke(app, ["volumes", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "NAME  KIND  SIZE"


def test_volumes_rejects_unknown_format(tmp_path, fake_msb):
    fake_msb["stdout"] = VOLUME_LIST_OUTPUT
    result = runner.invoke(
        app, ["volumes", "--workspace", str(tmp_path), "--format", "yaml"]
    )
    assert result.exit_code == 1
    assert "unsupported --format" in result.output


def test_volumes_unparsable_output_is_a_clean_error(tmp_path, fake_msb, monkeypatch):
    """A ConfigurationError from the parser exits cleanly (no traceback)."""
    from agent_circus.exceptions import ConfigurationError

    fake_msb["stdout"] = ""

    def parse(_stdout):
        raise ConfigurationError("Unexpected msb volume list output shape")

    monkeypatch.setattr("agent_circus.cli.parse_volume_list_output", parse)
    result = runner.invoke(app, ["volumes", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "Error:" in result.output


def test_volumes_msb_command_error_exits_nonzero(tmp_path, fake_msb, monkeypatch):
    from agent_circus.exceptions import MsbCommandError

    fake_msb["stdout"] = ""

    def run(args, **kwargs):
        raise MsbCommandError("msb volume list failed", returncode=1, stderr="boom")

    monkeypatch.setattr(
        "agent_circus.cli.MsbInvoker", lambda binary: type("I", (), {"run": run})
    )
    result = runner.invoke(app, ["volumes", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "Error:" in result.output
