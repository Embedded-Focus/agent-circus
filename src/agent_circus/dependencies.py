"""Immutable dependency selections independent of installed template recipes."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import tomli_w
from github import Auth, Github
from github.GithubException import GithubException
from packaging.version import Version
from requests.exceptions import RequestException

from .config import AVAILABLE_SERVICES, resolve_config
from .exceptions import ConfigurationError
from .templates import IGNORED_TEMPLATE_NAMES, template_dir_context
from .update_versions import (
    PINS,
    apply_version,
    fetch_latest_version,
    read_current_version,
)

SCHEMA = 1
FILES = ("versions.toml", "pyproject.toml", "uv.lock")


def storage_dir(workspace: Path) -> Path:
    """Return the selection store for the effective template mode.

    :param workspace: Project directory.
    :returns: Project-local or user-wide durable storage path.
    """
    deployed = resolve_config(workspace)
    if deployed is not None:
        return deployed / "dependencies"
    return (
        Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        / "agent-circus/dependencies"
    )


def versions(directory: Path) -> dict[str, str]:
    """Extract the supported tool pins from a template directory.

    :param directory: Template directory.
    :returns: Tool names mapped to versions.
    """
    pinned = {
        p.name: read_current_version((directory / p.file.name).read_text(), p)
        for p in PINS
    }
    # The Python requirement is a lower bound; report the installed lock version.
    for package in tomllib.loads((directory / "uv.lock").read_text()).get(
        "package", []
    ):
        if package["name"] == "mistral-vibe":
            pinned["mistral-vibe"] = package["version"]
            break
    return pinned


def recipe_id(directory: Path) -> str:
    """Fingerprint recipes while excluding supported version values.

    :param directory: Template directory.
    :returns: Compatibility fingerprint.
    """
    contents = []
    for name in ("Dockerfile", "compose.yaml", "pyproject.toml"):
        content = (directory / name).read_text()
        for pin in PINS:
            if pin.file.name == name:
                if len(list(pin.pattern.finditer(content))) != 1:
                    raise ConfigurationError(
                        f"Unsupported dependency recipe in {name}: expected one {pin.name} pin."
                    )
                content = apply_version(content, pin, "VERSION")
        contents.append(content)
    return hashlib.sha256("\0".join(contents).encode()).hexdigest()


def check_deployment(directory: Path) -> None:
    """Reject recipes that cannot safely consume the bundled dependency format.

    :param directory: Deployed template directory.
    :raises ConfigurationError: If recipes have unsupported customizations.
    """
    with template_dir_context() as baseline:
        if recipe_id(directory) != recipe_id(baseline):
            raise ConfigurationError(
                "Deployed dependency recipes differ from this release. Back up your "
                "customizations, redeploy with 'agent-circus init --deploy --force', "
                "and reapply customizations through hooks before using deps."
            )


def snapshot(directory: Path, source: str) -> dict[str, str]:
    """Capture versions and complete Python resolution as immutable file contents.

    :param directory: Source template directory.
    :param source: Human-readable provenance.
    :returns: Snapshot payload.
    """
    return {
        "versions.toml": tomli_w.dumps(
            {
                "schema": SCHEMA,
                "recipe": recipe_id(directory),
                "versions": versions(directory),
            }
        ),
        "pyproject.toml": (directory / "pyproject.toml").read_text(),
        "uv.lock": (directory / "uv.lock").read_text(),
        "source": source,
    }


def snapshot_id(payload: dict[str, str]) -> str:
    """Compute a content identity excluding provenance and timestamps.

    :param payload: Snapshot file contents.
    :returns: SHA-256 identifier.
    """
    return hashlib.sha256(
        json.dumps({n: payload[n] for n in FILES}, sort_keys=True).encode()
    ).hexdigest()


def selection(root: Path) -> dict[str, str | None]:
    """Read the active and previous selection without creating storage.

    :param root: Snapshot store.
    :returns: Selection pointers; null active means the installed baseline.
    """
    path = root / "selection.json"
    if not path.exists():
        return {"active": None, "previous": None}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) != {"active", "previous"}:
        raise ConfigurationError("Invalid dependency selection.json.")
    for value in data.values():
        if value is not None and (
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        ):
            raise ConfigurationError("Invalid dependency snapshot identifier.")
    return data


def read_snapshot(root: Path, identifier: str) -> dict[str, str]:
    """Read and verify a stored snapshot.

    :param root: Snapshot store.
    :param identifier: Content hash.
    :returns: Verified file contents and provenance.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ConfigurationError("Invalid dependency snapshot identifier.")
    directory = root / "snapshots" / identifier
    payload = {name: (directory / name).read_text() for name in FILES}
    payload["source"] = json.loads((directory / "metadata.json").read_text())["source"]
    if snapshot_id(payload) != identifier:
        raise ConfigurationError(f"Dependency snapshot {identifier} is corrupted.")
    return payload


def current(workspace: Path) -> dict[str, str]:
    """Read the effective snapshot, falling back to the current recipes.

    :param workspace: Project directory.
    :returns: Selected snapshot payload.
    """
    root = storage_dir(workspace)
    identifier = selection(root)["active"]
    if identifier:
        return read_snapshot(root, identifier)
    deployed = resolve_config(workspace)
    if deployed is not None:
        payload = snapshot(deployed, "deployed template versions")
        with template_dir_context() as baseline:
            bundled = snapshot(baseline, "release-tested baseline")
        return bundled if snapshot_id(payload) == snapshot_id(bundled) else payload
    with template_dir_context() as baseline:
        return snapshot(baseline, "release-tested baseline")


@contextlib.contextmanager
def store_lock(root: Path) -> Iterator[None]:
    """Serialize selection mutations across processes.

    :param root: Store to lock.
    :yields: Once an exclusive lock is acquired.
    """
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save_snapshot(root: Path, payload: dict[str, str]) -> str:
    """Publish immutable snapshot contents before selecting them.

    :param root: Store locked by the caller.
    :param payload: Snapshot file contents.
    :returns: Snapshot identity.
    """
    identifier = snapshot_id(payload)
    snapshots = root / "snapshots"
    snapshots.mkdir(exist_ok=True)
    destination = snapshots / identifier
    if not destination.exists():
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            stage = Path(temporary) / "snapshot"
            stage.mkdir()
            for name in FILES:
                (stage / name).write_text(payload[name])
            (stage / "metadata.json").write_text(
                json.dumps(
                    {
                        "source": payload["source"],
                        "created": datetime.now(UTC).isoformat(),
                    }
                )
            )
            stage.rename(destination)
    else:
        read_snapshot(root, identifier)
    return identifier


def activate(root: Path, active: str | None, previous: str) -> None:
    """Atomically replace the selection pointer.

    :param root: Store locked by the caller.
    :param active: New selection, or null for bundled/default recipes.
    :param previous: Materialized previous selection.
    """
    with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False) as stream:
        path = Path(stream.name)
        json.dump({"active": active, "previous": previous}, stream)
    try:
        path.replace(root / "selection.json")
    finally:
        path.unlink(missing_ok=True)


def apply_snapshot(directory: Path, payload: dict[str, str]) -> None:
    """Apply a compatible snapshot to a disposable build context.

    :param directory: Disposable template copy.
    :param payload: Selected snapshot.
    :raises ConfigurationError: If the recipe format is incompatible.
    """
    manifest = tomllib.loads(payload["versions.toml"])
    if manifest.get("schema") != SCHEMA or manifest.get("recipe") != recipe_id(
        directory
    ):
        raise ConfigurationError(
            "Selected dependencies are incompatible with these templates. Use 'deps reset' or migrate deployed templates."
        )
    pinned = manifest.get("versions", {})
    if set(pinned) != {p.name for p in PINS}:
        raise ConfigurationError("Dependency snapshot has an unsupported tool set.")
    for pin in PINS:
        stable_version(pinned[pin.name])
        path = directory / pin.file.name
        path.write_text(apply_version(path.read_text(), pin, pinned[pin.name]))
    for name in ("pyproject.toml", "uv.lock"):
        (directory / name).write_text(payload[name])


def stable_version(value: str) -> tuple[int, ...]:
    """Parse supported stable release numbers without accepting prereleases.

    :param value: Upstream version, optionally prefixed by v.
    :returns: Numeric version tuple.
    :raises ConfigurationError: If the version is unsupported or a prerelease.
    """
    if not isinstance(value, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise ConfigurationError(f"Unsupported stable version: {value!r}")
    return tuple(int(part) for part in value.removeprefix("v").split("."))


def github_client() -> Github:
    """Create a host-only discovery client using optional environment credentials.

    :returns: GitHub client with bounded network waits and no retries.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    return Github(auth=Auth.Token(token) if token else None, retry=0, timeout=30)


def resolve_upstream(directory: Path) -> list[str]:
    """Resolve stable non-decreasing versions into a temporary recipe copy.

    :param directory: Disposable recipe directory.
    :returns: Human-readable changes.
    :raises ConfigurationError: If any lookup or validation fails.
    """
    changes = []
    installed = versions(directory)
    with github_client() as gh:
        for original in PINS:
            pin = replace(original, file=directory / original.file.name)
            before = installed[pin.name]
            try:
                after = fetch_latest_version(gh, pin)
            except GithubException as error:
                raise ConfigurationError(
                    f"GitHub lookup failed for {pin.name} (HTTP {error.status}). Check GITHUB_TOKEN/GH_TOKEN and API rate limits."
                ) from None
            except (OSError, ValueError, RequestException):
                # Never render upstream exceptions: they may contain credentials.
                raise ConfigurationError(
                    f"Upstream lookup failed for {pin.name}. Check network access and retry."
                ) from None
            if stable_version(after) < stable_version(before):
                changes.append(
                    f"{pin.name}: {before} (keeping current; upstream reports {after})"
                )
                pin.file.write_text(apply_version(pin.file.read_text(), pin, before))
                continue
            changes.append(f"{pin.name}: {before} -> {after}")
            pin.file.write_text(apply_version(pin.file.read_text(), pin, after))
    return changes


def lock_python(directory: Path) -> None:
    """Upgrade Python resolution without installing a host environment.

    :param directory: Disposable Python project.
    :raises ConfigurationError: If uv is absent or resolution fails.
    """
    if shutil.which("uv") is None:
        raise ConfigurationError(
            "Dependency updates require host uv. Install uv and retry."
        )
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {"UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV", "GITHUB_TOKEN", "GH_TOKEN"}
    }
    try:
        subprocess.run(
            ["uv", "lock", "--upgrade", "--prerelease", "disallow", "--no-config"],
            cwd=directory,
            env=env,
            check=True,
            capture_output=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        raise ConfigurationError(
            "Python dependency resolution failed. Check uv, Python 3.14 availability, and registry access; selection is unchanged."
        ) from None


def validate_python(before: str, after: str) -> None:
    """Reject Python downgrades and newly selected prereleases.

    :param before: Previous Python lock contents.
    :param after: Newly resolved Python lock contents.
    :raises ConfigurationError: If an existing package moves backward.
    """

    def packages(content: str) -> dict[str, set[Version]]:
        """Collect registry versions by package name from a uv lockfile."""
        result: dict[str, set[Version]] = {}
        for package in tomllib.loads(content).get("package", []):
            if "registry" in package.get("source", {}):
                result.setdefault(package["name"], set()).add(
                    Version(package["version"])
                )
        return result

    old = packages(before)
    new = packages(after)
    for name, resolved in new.items():
        for version in resolved:
            if version in old.get(name, set()):
                continue
            if version.is_prerelease or version.is_devrelease:
                raise ConfigurationError(
                    f"Python resolution selected prerelease {name} {version}; selection is unchanged."
                )
            if name in old and version < max(old[name]):
                raise ConfigurationError(
                    f"Python resolution would downgrade {name}; selection is unchanged."
                )


@contextlib.contextmanager
def recipe_copy(workspace: Path) -> Iterator[Path]:
    """Copy effective templates without changing installed or deployed files.

    :param workspace: Project directory.
    :yields: Disposable template directory.
    """
    with template_dir_context() as baseline, tempfile.TemporaryDirectory() as temporary:
        source = resolve_config(workspace) or baseline
        destination = Path(temporary)
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(*IGNORED_TEMPLATE_NAMES, "dependencies"),
        )
        yield destination


def update(workspace: Path, dry_run: bool = False) -> list[str]:
    """Resolve a complete candidate and atomically select it unless previewing.

    :param workspace: Project directory.
    :param dry_run: Resolve tool versions only without storage or Python locking.
    :returns: Version report.
    """
    root = storage_dir(workspace)
    with contextlib.nullcontext() if dry_run else store_lock(root):
        deployed = resolve_config(workspace)
        if deployed is not None:
            check_deployment(deployed)
        before = current(workspace)
        with recipe_copy(workspace) as candidate:
            apply_snapshot(candidate, before)
            report = resolve_upstream(candidate)
            if dry_run:
                return report + [
                    "Preview only; Python transitive updates are resolved during deps update."
                ]
            lock_python(candidate)
            validate_python(before["uv.lock"], (candidate / "uv.lock").read_text())
            after = snapshot(candidate, "locally resolved upstream versions")
        previous = save_snapshot(root, before)
        identifier = save_snapshot(root, after)
        if identifier != previous:
            activate(root, identifier, previous)
            report.append(
                f"Selected {identifier[:12]}. Rebuild images and recreate containers to use it."
            )
        else:
            report.append("Dependencies are already up to date.")
        return report


def restore(workspace: Path, reset: bool = False) -> str:
    """Select the installed baseline or the previous immutable snapshot.

    :param workspace: Project directory.
    :param reset: Select the installed release baseline instead of previous.
    :returns: Result message.
    """
    root = storage_dir(workspace)
    with store_lock(root):
        before = current(workspace)
        if reset:
            with template_dir_context() as baseline:
                target = snapshot(baseline, "release-tested baseline")
        else:
            identifier = selection(root)["previous"]
            if identifier is None:
                raise ConfigurationError(
                    "No previous dependency selection is available."
                )
            target = read_snapshot(root, identifier)
        with recipe_copy(workspace) as candidate:
            apply_snapshot(candidate, target)
        previous = save_snapshot(root, before)
        identifier = save_snapshot(root, target)
        # Deployed recipes may retain older pins, so reset selects the saved baseline.
        active = None if reset and resolve_config(workspace) is None else identifier
        if snapshot_id(before) == identifier and selection(root)["active"] == active:
            return "Dependency selection is unchanged."
        activate(root, active, previous)
        return f"Selected {identifier[:12]}. Rebuild images and recreate containers to use it."


@contextlib.contextmanager
def deployed_override(workspace: Path) -> Iterator[Path | None]:
    """Prepare a build-only override while preserving deployed Compose path semantics.

    :param workspace: Project directory.
    :yields: Temporary Compose override path, or none without a selection.
    """
    if selection(storage_dir(workspace))["active"] is None:
        yield None
        return
    deployed = resolve_config(workspace)
    assert deployed is not None
    check_deployment(deployed)
    with recipe_copy(workspace) as candidate:
        apply_snapshot(candidate, current(workspace))
        args = {
            p.pattern.pattern.split(":")[0]: read_current_version(
                (candidate / p.file.name).read_text(), p
            )
            for p in PINS
            if p.file.name == "compose.yaml"
        }
        path = candidate / "dependency-override.json"
        path.write_text(
            json.dumps(
                {
                    "services": {
                        service: {"build": {"context": str(candidate), "args": args}}
                        for service in AVAILABLE_SERVICES
                    }
                }
            )
        )
        yield path
