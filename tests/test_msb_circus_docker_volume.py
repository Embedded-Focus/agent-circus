"""Tests for the persistent docker volume helpers: config, builders, parsing."""

import json
from pathlib import Path

import pytest

from msb_circus.config import (
    docker_volume_name_for_workspace,
    parse_config,
    sandbox_name_for_workspace,
)
from msb_circus.exceptions import ConfigurationError
from msb_circus.sandbox import (
    VolumeInfo,
    build_run_args,
    build_volume_list_args,
    build_volume_remove_args,
    parse_inspect_disk_config,
    parse_volume_list_output,
)


def test_parse_config_accepts_docker_disk_persist():
    config = parse_config(
        {"msb": {"sandbox": {"docker_disk": "20G", "docker_disk_persist": True}}}
    )
    assert config.sandbox.docker_disk == "20G"
    assert config.sandbox.docker_disk_persist is True


def test_parse_config_defaults_docker_disk_persist_to_false():
    config = parse_config({})
    assert config.sandbox.docker_disk_persist is False


def test_parse_config_rejects_non_boolean_docker_disk_persist():
    with pytest.raises(ConfigurationError, match="must be a boolean"):
        parse_config({"msb": {"sandbox": {"docker_disk_persist": "yes"}}})


def test_parse_config_rejects_docker_disk_persist_without_docker_disk():
    with pytest.raises(ConfigurationError, match=r"requires msb.sandbox.docker_disk"):
        parse_config({"msb": {"sandbox": {"docker_disk_persist": True}}})


def test_parse_config_rejects_docker_disk_persist_with_flat_root_disk_only():
    with pytest.raises(ConfigurationError, match=r"requires msb.sandbox.docker_disk"):
        parse_config(
            {"msb": {"sandbox": {"root_disk": "flat:32G", "docker_disk_persist": True}}}
        )


def test_docker_volume_name_for_workspace_extends_sandbox_name():
    workspace = Path("/tmp/My Project")
    assert docker_volume_name_for_workspace(workspace) == (
        sandbox_name_for_workspace(workspace) + "-docker"
    )


def test_build_run_args_sends_docker_volume_as_mount_named():
    args = build_run_args(
        name="agents-x",
        image="img:1",
        docker_disk="20G",
        docker_volume="agents-x-docker",
    )
    index = args.index("--mount-named")
    assert args[index + 1] == "agents-x-docker:/var/lib/docker:kind=disk,size=20G"
    assert "--mount-owned" not in args


def test_build_run_args_rejects_docker_volume_without_docker_disk():
    with pytest.raises(ValueError):
        build_run_args(name="agents-x", image="img:1", docker_volume="v")


def test_build_volume_list_args():
    assert build_volume_list_args() == ["volume", "list", "--format", "json"]


def test_build_volume_remove_args():
    assert build_volume_remove_args("v") == ["volume", "remove", "v"]


VOLUME_LIST_OUTPUT = """[
  {
    "capacity_bytes": 5368709120,
    "created_at": "2026-11-12T14:17:39.766149553+00:00",
    "disk_format": "raw",
    "disk_fstype": "ext4",
    "kind": "disk",
    "name": "dk-test",
    "quota_mib": null,
    "used_bytes": 0
  }
]"""


def test_parse_volume_list_output_parses_real_output():
    assert parse_volume_list_output(VOLUME_LIST_OUTPUT) == [
        VolumeInfo(name="dk-test", kind="disk", capacity_bytes=5368709120)
    ]


def test_parse_volume_list_output_accepts_empty_array():
    assert parse_volume_list_output("[]") == []


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps([{"name": "v", "capacity_bytes": None}]),
        json.dumps([{"name": "v", "capacity_bytes": True}]),
        json.dumps([{"name": "v", "capacity_bytes": "big"}]),
    ],
)
def test_parse_volume_list_output_ignores_invalid_capacity_bytes(raw):
    assert parse_volume_list_output(raw) == [VolumeInfo(name="v")]


def test_parse_volume_list_output_rejects_invalid_json():
    with pytest.raises(ConfigurationError, match="JSON"):
        parse_volume_list_output("not json")


def test_parse_volume_list_output_rejects_top_level_object():
    with pytest.raises(ConfigurationError, match="expected a JSON array"):
        parse_volume_list_output(json.dumps({"volumes": []}))


def test_parse_volume_list_output_rejects_entry_without_name():
    with pytest.raises(ConfigurationError, match="index 0"):
        parse_volume_list_output(json.dumps([{"kind": "disk"}]))


NAMED_DOCKER_MOUNT = {
    "type": "Named",
    "name": "agents-foo-docker",
    "guest": "/var/lib/docker",
    "options": {"readonly": False, "noexec": False},
    "stat_virtualization": "strict",
    "host_permissions": "private",
    "follow_root_symlinks": False,
}


def test_parse_inspect_disk_config_extracts_named_docker_volume():
    output = json.dumps(
        {
            "config": {
                "spec": {
                    "mounts": [
                        {
                            "type": "Bind",
                            "host": "/workspace",
                            "guest": "/workspace",
                            "options": {"readonly": False, "noexec": False},
                        },
                        NAMED_DOCKER_MOUNT,
                    ]
                }
            }
        }
    )
    guest = parse_inspect_disk_config(output)
    assert guest.docker_volume_name == "agents-foo-docker"
    assert guest.docker_disk_size_mib is None
    assert guest.mounts_seen is True


def test_parse_inspect_disk_config_ignores_named_mount_at_other_guest_path():
    output = json.dumps(
        {
            "config": {
                "spec": {
                    "mounts": [
                        {
                            "type": "Named",
                            "name": "data-vol",
                            "guest": "/data",
                            "options": {"readonly": False, "noexec": False},
                        }
                    ]
                }
            }
        }
    )
    guest = parse_inspect_disk_config(output)
    assert guest.docker_volume_name is None
    assert guest.mounts_seen is True


def test_parse_inspect_disk_config_named_docker_volume_before_other_mounts():
    output = json.dumps(
        {
            "config": {
                "spec": {
                    "mounts": [
                        NAMED_DOCKER_MOUNT,
                        {
                            "type": "Bind",
                            "host": "/workspace",
                            "guest": "/workspace",
                            "options": {"readonly": False, "noexec": False},
                        },
                    ]
                }
            }
        }
    )
    guest = parse_inspect_disk_config(output)
    assert guest.docker_volume_name == "agents-foo-docker"
    assert guest.mounts_seen is True
