"""Tests for msb-provided disk support: builders, parsing, and drift."""

import json

import pytest

from msb_circus.config import docker_enabled, parse_config
from msb_circus.exceptions import ConfigurationError
from msb_circus.sandbox import (
    DOCKER_DATA_GUEST_PATH,
    build_run_args,
    parse_inspect_disk_config,
    parse_root_disk_spec,
    parse_size_mib,
)


def test_build_run_args_sends_docker_disk_as_mount_owned():
    args = build_run_args(
        name="agents-x",
        image="img:1",
        docker_disk="10G",
    )
    index = args.index("--mount-owned")
    assert args[index + 1] == f"{DOCKER_DATA_GUEST_PATH}:kind=disk,size=10G"
    assert "--root-disk" not in args


def test_build_run_args_sends_root_disk_verbatim():
    args = build_run_args(
        name="agents-x",
        image="img:1",
        root_disk="flat:32G",
    )
    index = args.index("--root-disk")
    assert args[index + 1] == "flat:32G"
    assert "--mount-owned" not in args


def test_build_run_args_sends_both_disks_when_configured():
    args = build_run_args(
        name="agents-x",
        image="img:1",
        docker_disk="10G",
        root_disk="flat:32G",
    )
    assert args.index("--root-disk") < args.index("--mount-owned")


def test_build_run_args_omits_disk_flags_by_default():
    args = build_run_args(name="agents-x", image="img:1")
    assert "--root-disk" not in args
    assert "--mount-owned" not in args


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("10G", 10240),
        ("500M", 500),
        ("512K", 0),
        ("1.5G", 1536),
        ("2tib", 2 * 1024 * 1024),
        ("10", None),
        ("flat:32G", None),
        ("", None),
        ("abc", None),
    ],
)
def test_parse_size_mib(spec, expected):
    assert parse_size_mib(spec) == expected


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("flat:32G", ("flat", 32768)),
        ("tmpfs:2G", ("tmpfs", 2048)),
        ("16G", ("managed", 16384)),
        ("./scratch.qcow2:format=qcow2,fstype=ext4", (None, None)),
        ("flat", (None, None)),
    ],
)
def test_parse_root_disk_spec(spec, expected):
    assert parse_root_disk_spec(spec) == expected


def test_parse_config_accepts_disk_keys():
    config = parse_config(
        {"msb": {"sandbox": {"root_disk": "flat:32G", "docker_disk": "10G"}}}
    )
    assert config.sandbox.root_disk == "flat:32G"
    assert config.sandbox.docker_disk == "10G"


def test_parse_config_defaults_disks_to_none():
    config = parse_config({})
    assert config.sandbox.root_disk is None
    assert config.sandbox.docker_disk is None


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"root_disk": ""}, "root_disk"),
        ({"root_disk": 5}, "root_disk"),
        ({"docker_disk": ""}, "docker_disk"),
        ({"docker_disk": []}, "docker_disk"),
    ],
)
def test_parse_config_rejects_invalid_disk_keys(table, message):
    with pytest.raises(ConfigurationError, match=message):
        parse_config({"msb": {"sandbox": table}})


@pytest.mark.parametrize(
    ("sandbox_table", "expected"),
    [
        ({}, False),
        ({"docker_disk": "10G"}, True),
        ({"root_disk": "flat:32G"}, True),
        ({"root_disk": "16G"}, False),
        ({"root_disk": "tmpfs:2G"}, False),
        ({"root_disk": "flat:32G", "docker_disk": "10G"}, True),
    ],
)
def test_docker_enabled(sandbox_table, expected):
    config = parse_config({"msb": {"sandbox": sandbox_table}})
    assert docker_enabled(config) is expected


INSPECT_WITH_DISKS = json.dumps(
    {
        "config": {
            "spec": {
                "root_disk": {"kind": "managed", "size_mib": 4096},
                "mounts": [
                    {
                        "type": "Bind",
                        "host": "/workspace",
                        "guest": "/workspace",
                        "options": {"readonly": False, "noexec": False},
                    },
                    {
                        "type": "Owned",
                        "guest": "/var/lib/docker",
                        "storage": {"kind": "disk", "capacity_mib": 10240},
                        "options": {"readonly": False, "noexec": False},
                    },
                ],
            }
        }
    }
)


def test_parse_inspect_disk_config_extracts_both_disks():
    guest = parse_inspect_disk_config(INSPECT_WITH_DISKS)
    assert guest.root_disk_kind == "managed"
    assert guest.root_disk_size_mib == 4096
    assert guest.docker_disk_size_mib == 10240
    assert guest.mounts_seen is True


def test_parse_inspect_disk_config_falls_back_to_active_config():
    document = json.loads(INSPECT_WITH_DISKS)
    output = json.dumps({"active_config": document["config"]})
    guest = parse_inspect_disk_config(output)
    assert guest.root_disk_kind == "managed"
    assert guest.docker_disk_size_mib == 10240


def test_parse_inspect_disk_config_without_disk_entries():
    output = json.dumps(
        {"config": {"spec": {"root_disk": {"kind": "managed", "size_mib": 4096}}}}
    )
    guest = parse_inspect_disk_config(output)
    assert guest.root_disk_kind == "managed"
    assert guest.root_disk_size_mib == 4096
    assert guest.docker_disk_size_mib is None
    assert guest.mounts_seen is False


def test_parse_inspect_disk_config_ignores_tmpfs_mount_as_root_disk():
    """A tmpfs *mount* must not be read as the tmpfs *root disk*."""
    output = json.dumps(
        {
            "config": {
                "spec": {
                    "mounts": [
                        {
                            "type": "Tmpfs",
                            "guest": "/scratch",
                            "size_mib": 512,
                            "options": {"readonly": False, "noexec": False},
                        },
                        {
                            "type": "Owned",
                            "guest": "/var/lib/docker",
                            "storage": {"kind": "disk", "capacity_mib": 10240},
                            "options": {"readonly": False, "noexec": False},
                        },
                    ]
                }
            }
        }
    )
    guest = parse_inspect_disk_config(output)
    assert guest.root_disk_kind is None
    assert guest.docker_disk_size_mib == 10240
    assert guest.mounts_seen is True


def test_parse_inspect_disk_config_ignores_owned_directory_storage():
    """A directory-backed owned mount must not set a docker disk size."""
    output = json.dumps(
        {
            "config": {
                "spec": {
                    "mounts": [
                        {
                            "type": "Owned",
                            "guest": "/private",
                            "storage": {"kind": "directory"},
                            "options": {"readonly": False, "noexec": False},
                        }
                    ]
                }
            }
        }
    )
    guest = parse_inspect_disk_config(output)
    assert guest.root_disk_kind is None
    assert guest.docker_disk_size_mib is None
    assert guest.mounts_seen is True


def test_parse_inspect_disk_config_unrecognized_shape_yields_nothing():
    guest = parse_inspect_disk_config(json.dumps({"config": {"unrelated": 1}}))
    assert guest.root_disk_kind is None
    assert guest.docker_disk_size_mib is None
    assert guest.mounts_seen is False


def test_parse_inspect_disk_config_rejects_invalid_json():
    with pytest.raises(ConfigurationError, match="JSON"):
        parse_inspect_disk_config("not json")


def test_parse_inspect_disk_config_rejects_missing_config():
    with pytest.raises(ConfigurationError, match="config"):
        parse_inspect_disk_config(json.dumps({"unrelated": {}}))
