"""Tests for the agent-circus-update-templates maintainer script."""

import re
import urllib.error
from typing import Self
from unittest.mock import MagicMock

import pytest

from agent_circus.update_versions import (
    ALL_PINS,
    MSB_AGENTS_PINS,
    VersionPin,
    apply_version,
    build_report,
    fetch_latest_docker_digest,
    fetch_latest_github_asset_sha256,
    fetch_latest_npm_version,
    fetch_latest_tag,
    fetch_latest_version,
    normalize_tag,
    read_current_version,
)


@pytest.mark.parametrize(
    ("tag_name", "strip_prefix", "expected"),
    [
        ("0.22.0", "", "0.22.0"),
        ("v0.58.1", "v", "0.58.1"),
        ("rust-v0.144.1", "rust-v", "0.144.1"),
        ("v4.53.3", "", "v4.53.3"),
    ],
)
def test_normalize_tag(tag_name: str, strip_prefix: str, expected: str) -> None:
    assert normalize_tag(tag_name, strip_prefix) == expected


@pytest.mark.parametrize("pin", ALL_PINS, ids=[pin.name for pin in ALL_PINS])
def test_read_current_version_matches_real_template(pin: VersionPin) -> None:
    content = pin.file.read_text()
    version = read_current_version(content, pin)
    assert version


def _bump(version: str) -> str:
    """Return a version string that differs from *version* but keeps its shape.

    For dotted numeric versions (optionally ``v``-prefixed, optionally with a
    suffix such as ``-dind@sha256:...``) the last numeric component is
    incremented. For digest-shaped versions the last hex digit is flipped, so
    the result still satisfies the pin's pattern.
    """
    match = re.match(r"^(v?\d+(?:\.\d+)*)", version)
    if match is None:
        assert version[-1] in "0123456789abcdef"
        flipped = "0" if version[-1] != "0" else "1"
        return version[:-1] + flipped
    numeric = match.group(1)
    prefix = "v" if numeric.startswith("v") else ""
    parts = numeric[len(prefix) :].split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return prefix + ".".join(parts) + version[len(numeric) :]


def test_bump_handles_suffix_and_digest_versions() -> None:
    assert _bump("1.2.3") == "1.2.4"
    assert _bump("v1.2.3") == "v1.2.4"
    assert _bump("27.5.1-dind@sha256:" + "a" * 64) == "27.5.2-dind@sha256:" + "a" * 64
    assert _bump("sha256:" + "a" * 63 + "f") == "sha256:" + "a" * 63 + "0"


def _outside_version_spans(text: str, matches: list[re.Match[str]]) -> str:
    """Return *text* with all captured version spans removed.

    :param text: Full text the matches were found in.
    :param matches: Matches whose group 1 spans are cut out.
    :returns: Remaining text between the spans, joined.
    """
    pieces: list[str] = []
    pos = 0
    for match in matches:
        start = match.start(0) + (match.start(1) - match.start(0))
        end = match.start(0) + (match.end(1) - match.start(0))
        pieces.append(text[pos:start])
        pos = end
    pieces.append(text[pos:])
    return "".join(pieces)


@pytest.mark.parametrize("pin", ALL_PINS, ids=[pin.name for pin in ALL_PINS])
def test_apply_version_round_trips_without_other_changes(pin: VersionPin) -> None:
    content = pin.file.read_text()
    current = read_current_version(content, pin)
    new_version = _bump(current)

    updated = apply_version(content, pin, new_version)

    # Every occurrence of the pin's pattern is bumped (some Dockerfiles
    # reference the same image more than once), and the version count is
    # unchanged.
    matches = list(pin.pattern.finditer(content))
    updated_matches = list(pin.pattern.finditer(updated))
    assert len(updated_matches) == len(matches) > 0
    assert [match.group(1) for match in updated_matches] == [new_version] * len(matches)
    # Everything outside the captured version spans is untouched.
    assert _outside_version_spans(content, matches) == _outside_version_spans(
        updated, updated_matches
    )


def test_apply_version_raises_when_pattern_missing() -> None:
    pin = MSB_AGENTS_PINS[0]
    with pytest.raises(ValueError, match=r"node \(msb-agents\)"):
        apply_version("no version here", pin, "1.0.0")


def test_msb_pins_cover_every_pinned_component_in_msb_dockerfile() -> None:
    # Each component pinned in Dockerfile.msb-agents is tracked; the parametrized
    # read/apply tests above verify the patterns actually match the file.
    msb_pin_names = {pin.name for pin in MSB_AGENTS_PINS}
    assert msb_pin_names == {
        "node (msb-agents)",
        "uv (msb-agents)",
        "docker (msb-agents)",
        "opencode (msb-agents)",
        "skills (msb-agents)",
        "claude-code (msb-agents)",
        "claude-agent-acp (msb-agents)",
        "codex (msb-agents)",
        "codex-acp (msb-agents)",
        "pi (msb-agents)",
        "pi-acp (msb-agents)",
        "s6-overlay (msb-agents)",
        "s6-overlay noarch sha256 (msb-agents)",
        "s6-overlay x86_64 sha256 (msb-agents)",
        "s6-overlay aarch64 sha256 (msb-agents)",
    }


def test_s6_overlay_version_pin_matches_all_three_add_urls() -> None:
    # The tag appears once per ADD URL; apply_version must rewrite all of
    # them together, or the sha256sum -c build check breaks.
    pin = next(pin for pin in MSB_AGENTS_PINS if pin.name == "s6-overlay (msb-agents)")
    assert pin.source == "github"
    content = pin.file.read_text()
    assert len(list(pin.pattern.finditer(content))) == 3


def test_apply_version_updates_all_duplicate_image_references() -> None:
    # The msb-agents Dockerfile copies from the dind image twice; both
    # tag@digest references must be rewritten together.
    pin = next(pin for pin in ALL_PINS if pin.name == "docker (msb-agents)")
    content = pin.file.read_text()
    assert len(list(pin.pattern.finditer(content))) == 2

    new_version = _bump(read_current_version(content, pin))
    updated = apply_version(content, pin, new_version)

    assert updated.count(new_version) == 2


def test_fetch_latest_docker_digest_returns_hub_api_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = (
        b'{"digest": "sha256:' + b"a" * 64 + b'", "tag": "26-slim"}'
    )
    urlopen = MagicMock(return_value=response)
    monkeypatch.setattr("agent_circus.update_versions.urllib.request.urlopen", urlopen)

    digest = fetch_latest_docker_digest("docker.io/library/node", "26-slim")

    assert digest == "sha256:" + "a" * 64
    url = urlopen.call_args.args[0]
    assert url == "https://hub.docker.com/v2/repositories/library/node/tags/26-slim"


def test_fetch_latest_docker_digest_raises_without_hub_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b'{"tag": "26-slim"}'
    urlopen = MagicMock(return_value=response)
    monkeypatch.setattr("agent_circus.update_versions.urllib.request.urlopen", urlopen)

    with pytest.raises(ValueError, match="no digest"):
        fetch_latest_docker_digest("docker.io/library/node", "26-slim")


def test_fetch_ghcr_digest_returns_multiarch_content_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_response = MagicMock()
    token_response.__enter__.return_value = token_response
    token_response.read.return_value = b'{"token": "tok-123"}'
    manifest_response = MagicMock()
    manifest_response.__enter__.return_value = manifest_response
    manifest_response.headers.get.return_value = "sha256:" + "a" * 64
    urlopen = MagicMock(side_effect=[token_response, manifest_response])
    monkeypatch.setattr("agent_circus.update_versions.urllib.request.urlopen", urlopen)

    digest = fetch_latest_docker_digest("ghcr.io/astral-sh/uv", "0.13.6")

    assert digest == "sha256:" + "a" * 64
    token_url = urlopen.call_args_list[0].args[0]
    assert token_url.startswith("https://ghcr.io/token?")
    assert "repository%3Aastral-sh%2Fuv%3Apull" in token_url
    manifest_request = urlopen.call_args_list[1].args[0]
    assert (
        manifest_request.full_url == "https://ghcr.io/v2/astral-sh/uv/manifests/0.13.6"
    )
    assert manifest_request.headers["Authorization"] == "Bearer tok-123"
    assert (
        "application/vnd.docker.distribution.manifest.list.v2+json"
        in manifest_request.headers["Accept"]
    )


def test_fetch_latest_docker_digest_rejects_unsupported_registry() -> None:
    with pytest.raises(ValueError, match="Unsupported registry"):
        fetch_latest_docker_digest("quay.io/example/image", "latest")


def test_fetch_ghcr_digest_raises_without_digest_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_response = MagicMock()
    token_response.__enter__.return_value = token_response
    token_response.read.return_value = b'{"token": "tok-123"}'
    manifest_response = MagicMock()
    manifest_response.__enter__.return_value = manifest_response
    manifest_response.headers.get.return_value = None
    urlopen = MagicMock(side_effect=[token_response, manifest_response])
    monkeypatch.setattr("agent_circus.update_versions.urllib.request.urlopen", urlopen)

    with pytest.raises(ValueError, match="no digest"):
        fetch_latest_docker_digest("ghcr.io/astral-sh/uv", "0.13.6")


def test_fetch_latest_version_routes_registry_digest_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = next(pin for pin in ALL_PINS if pin.name == "node (msb-agents)")
    digest_mock = MagicMock(return_value="sha256:" + "b" * 64)
    monkeypatch.setattr(
        "agent_circus.update_versions.fetch_latest_docker_digest", digest_mock
    )

    latest = fetch_latest_version(MagicMock(), pin)

    assert latest == "sha256:" + "b" * 64
    digest_mock.assert_called_once_with("docker.io/library/node", "26-slim")


def test_fetch_latest_version_routes_github_image_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = next(pin for pin in ALL_PINS if pin.name == "docker (msb-agents)")
    monkeypatch.setattr(
        "agent_circus.update_versions.fetch_latest_tag",
        lambda gh, repo: "docker-v29.9.2",
    )
    digest_mock = MagicMock(return_value="sha256:" + "c" * 64)
    monkeypatch.setattr(
        "agent_circus.update_versions.fetch_latest_docker_digest", digest_mock
    )

    latest = fetch_latest_version(MagicMock(), pin)

    assert latest == f"29.9.2-dind@sha256:{'c' * 64}"
    digest_mock.assert_called_once_with("docker.io/library/docker", "29.9.2-dind")


def test_fetch_latest_tag_returns_tag_name() -> None:
    gh = MagicMock()
    gh.get_repo.return_value.get_latest_release.return_value.tag_name = "v1.2.3"

    tag = fetch_latest_tag(gh, "owner/repo")

    assert tag == "v1.2.3"
    gh.get_repo.assert_called_once_with("owner/repo")


def test_fetch_latest_github_asset_sha256_parses_release_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = MagicMock()
    gh.get_repo.return_value.get_latest_release.return_value.tag_name = "v3.2.3.2"

    class Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return b"" + b"a" * 64 + b"  s6-overlay-noarch.tar.xz\n"

    urlopen = MagicMock(return_value=Response())
    monkeypatch.setattr("agent_circus.update_versions.urllib.request.urlopen", urlopen)

    digest = fetch_latest_github_asset_sha256(
        gh, "just-containers/s6-overlay", "s6-overlay-noarch.tar.xz.sha256"
    )

    assert digest == "a" * 64
    url = urlopen.call_args.args[0]
    assert url == (
        "https://github.com/just-containers/s6-overlay/releases/download/"
        "v3.2.3.2/s6-overlay-noarch.tar.xz.sha256"
    )


def test_fetch_latest_github_asset_sha256_rejects_malformed_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = MagicMock()
    gh.get_repo.return_value.get_latest_release.return_value.tag_name = "v1.0"

    class Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return b"not a sha256sum line\n"

    urlopen = MagicMock(return_value=Response())
    monkeypatch.setattr("agent_circus.update_versions.urllib.request.urlopen", urlopen)

    with pytest.raises(ValueError, match="not a sha256sum line"):
        fetch_latest_github_asset_sha256(
            gh, "just-containers/s6-overlay", "s6-overlay-noarch.tar.xz.sha256"
        )


def test_fetch_latest_version_routes_github_asset_sha256_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = next(
        pin
        for pin in MSB_AGENTS_PINS
        if pin.source == "github-asset-sha256"
        and pin.name == "s6-overlay noarch sha256 (msb-agents)"
    )
    fetch = MagicMock(return_value="a" * 64)
    monkeypatch.setattr(
        "agent_circus.update_versions.fetch_latest_github_asset_sha256", fetch
    )

    latest = fetch_latest_version(MagicMock(), pin)

    assert latest == "a" * 64
    fetch.assert_called_once()
    args = fetch.call_args.args
    assert args[1] == "just-containers/s6-overlay"
    assert args[2] == "s6-overlay-noarch.tar.xz.sha256"


def test_fetch_latest_npm_version_returns_latest_dist_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return b'{"dist-tags": {"latest": "12.0.2"}}'

    urlopen = MagicMock(return_value=Response())
    monkeypatch.setattr("agent_circus.update_versions.urllib.request.urlopen", urlopen)

    latest = fetch_latest_npm_version("npm")

    assert latest == "12.0.2"
    urlopen.assert_called_once_with("https://registry.npmjs.org/npm", timeout=30)


def test_fetch_latest_version_routes_npm_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    npm_pin = next(pin for pin in MSB_AGENTS_PINS if pin.source == "npm")
    fetch = MagicMock(return_value="2.3.2")
    monkeypatch.setattr("agent_circus.update_versions.fetch_latest_npm_version", fetch)

    latest = fetch_latest_version(MagicMock(), npm_pin)

    assert latest == "2.3.2"
    fetch.assert_called_once_with(npm_pin.repo)


def test_build_report_records_error_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_fetch(gh: object, pin: VersionPin) -> str:
        calls.append(pin.repo)
        if pin == ALL_PINS[0]:
            raise urllib.error.URLError("Not Found")
        return normalize_tag("v9.9.9", pin.strip_prefix)

    monkeypatch.setattr("agent_circus.update_versions.fetch_latest_version", fake_fetch)

    results = build_report(ALL_PINS, gh=MagicMock())

    assert len(results) == len(ALL_PINS)
    assert len(calls) == len(ALL_PINS)

    failed = results[0]
    assert failed.error is not None
    assert failed.latest is None
    assert not failed.outdated

    for pin, result in zip(ALL_PINS[1:], results[1:], strict=True):
        assert result.error is None
        assert result.latest == normalize_tag("v9.9.9", pin.strip_prefix)
