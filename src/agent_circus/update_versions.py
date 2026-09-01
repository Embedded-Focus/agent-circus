"""Refresh pinned tool versions in the ``templates/agent-circus`` template.

Looks up the latest release for each pinned component and rewrites the
corresponding version string in the template's Dockerfile, compose.yaml, and
pyproject.toml. Most components are backed by GitHub releases; npm itself is
resolved from the npm registry.

Installed as its own console script (``agent-circus-update-templates``),
separate from the ``agent-circus`` CLI, since it edits template *source*
rather than acting on a deployed workspace.

Shortcoming: :data:`TEMPLATE_DIR` is derived from this module's own file
location, so it only resolves to the real template sources when run against
an editable/source checkout (e.g. via ``uv run``). Running it from an
installed wheel would edit the installed copy, not the repository.
"""

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import typer
from github import Auth, Github
from github.GithubException import GithubException

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "agent-circus"


@dataclass(frozen=True)
class VersionPin:
    """A single pinned version location tied to a GitHub repository.

    :param name: Human-readable component name.
    :param repo: GitHub repository as ``"owner/repo"``, or npm package name
        when :attr:`source` is ``"npm"``.
    :param file: Template file containing the pinned version.
    :param pattern: Regex whose first capture group spans exactly the
        version substring to read/replace.
    :param strip_prefix: Prefix removed from the release tag name before
        it is written into the file (e.g. ``"v"``, ``"rust-v"``). An empty
        string keeps the raw tag name.
    :param source: Version lookup backend.
    """

    name: str
    repo: str
    file: Path
    pattern: re.Pattern[str]
    strip_prefix: str = ""
    source: Literal["github", "npm"] = "github"


PINS: list[VersionPin] = [
    VersionPin(
        name="npm",
        repo="npm",
        file=TEMPLATE_DIR / "compose.yaml",
        pattern=re.compile(r"NPM_VERSION:\s*(\S+)"),
        source="npm",
    ),
    VersionPin(
        name="yq",
        repo="mikefarah/yq",
        file=TEMPLATE_DIR / "Dockerfile",
        pattern=re.compile(r"mikefarah/yq/releases/download/(v[0-9.]+)/yq_linux_amd64"),
    ),
    VersionPin(
        name="uv",
        repo="astral-sh/uv",
        file=TEMPLATE_DIR / "Dockerfile",
        pattern=re.compile(r"ghcr\.io/astral-sh/uv:(\S+)"),
    ),
    VersionPin(
        name="claude-code",
        repo="anthropics/claude-code",
        file=TEMPLATE_DIR / "compose.yaml",
        pattern=re.compile(r"CLAUDE_CODE_VERSION:\s*(\S+)"),
        strip_prefix="v",
    ),
    VersionPin(
        name="claude-agent-acp",
        repo="agentclientprotocol/claude-agent-acp",
        file=TEMPLATE_DIR / "compose.yaml",
        pattern=re.compile(r"CLAUDE_AGENT_ACP_VERSION:\s*(\S+)"),
        strip_prefix="v",
    ),
    VersionPin(
        name="bun",
        repo="oven-sh/bun",
        file=TEMPLATE_DIR / "compose.yaml",
        pattern=re.compile(r"BUN_VERSION:\s*(\S+)"),
        strip_prefix="bun-v",
    ),
    VersionPin(
        name="claude-mem",
        repo="thedotmack/claude-mem",
        file=TEMPLATE_DIR / "compose.yaml",
        pattern=re.compile(r"CLAUDE_MEM_VERSION:\s*(\S+)"),
        strip_prefix="v",
    ),
    VersionPin(
        name="codex",
        repo="openai/codex",
        file=TEMPLATE_DIR / "compose.yaml",
        pattern=re.compile(r"CODEX_VERSION:\s*(\S+)"),
        strip_prefix="rust-v",
    ),
    VersionPin(
        name="codex-acp",
        repo="agentclientprotocol/codex-acp",
        file=TEMPLATE_DIR / "compose.yaml",
        pattern=re.compile(r"CODEX_ACP_VERSION:\s*(\S+)"),
        strip_prefix="v",
    ),
    VersionPin(
        name="opencode",
        repo="sst/opencode",
        file=TEMPLATE_DIR / "compose.yaml",
        pattern=re.compile(r"OPENCODE_VERSION:\s*(\S+)"),
        strip_prefix="v",
    ),
    VersionPin(
        name="mistral-vibe",
        repo="mistralai/mistral-vibe",
        file=TEMPLATE_DIR / "pyproject.toml",
        pattern=re.compile(r"mistral-vibe>=([0-9.]+)"),
        strip_prefix="v",
    ),
]


def normalize_tag(tag_name: str, strip_prefix: str) -> str:
    """Strip a known prefix from a GitHub release tag name.

    :param tag_name: Raw tag name as returned by the GitHub API.
    :param strip_prefix: Prefix to remove, or ``""`` to keep the tag as-is.
    :returns: Normalized version string.
    """
    if not strip_prefix:
        return tag_name
    return tag_name.removeprefix(strip_prefix)


def read_current_version(content: str, pin: VersionPin) -> str:
    """Extract the currently pinned version from file content.

    :param content: Full text of :attr:`VersionPin.file`.
    :param pin: Pin whose pattern locates the version.
    :returns: Currently pinned version string.
    :raises ValueError: If the pattern does not match *content*.
    """
    match = pin.pattern.search(content)
    if match is None:
        raise ValueError(
            f"Could not locate current version for {pin.name!r} in {pin.file}"
        )
    return match.group(1)


def apply_version(content: str, pin: VersionPin, new_version: str) -> str:
    """Return *content* with the pinned version replaced by *new_version*.

    :param content: Full text of :attr:`VersionPin.file`.
    :param pin: Pin whose pattern locates the version.
    :param new_version: Replacement version string.
    :returns: Updated file content.
    :raises ValueError: If the pattern does not match *content*.
    """
    match = pin.pattern.search(content)
    if match is None:
        raise ValueError(
            f"Could not locate current version for {pin.name!r} in {pin.file}"
        )
    start, end = match.span(1)
    return content[:start] + new_version + content[end:]


def fetch_latest_tag(gh: Github, repo: str) -> str:
    """Return the tag name of the latest GitHub release for *repo*.

    :param gh: Authenticated or anonymous GitHub client.
    :param repo: Repository as ``"owner/repo"``.
    :returns: Latest release's raw tag name.
    :raises GithubException: If the repository or its latest release
        cannot be resolved.
    """
    return gh.get_repo(repo).get_latest_release().tag_name


def fetch_latest_npm_version(package: str) -> str:
    """Return the npm registry ``latest`` dist-tag for *package*.

    :param package: npm package name.
    :returns: Latest published version string.
    :raises ValueError: If the registry response does not contain a latest tag.
    :raises urllib.error.URLError: If the registry request fails.
    """
    url = f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='@/')}"
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read()
    metadata = json.loads(data)
    latest = metadata.get("dist-tags", {}).get("latest")
    if not isinstance(latest, str) or not latest:
        raise ValueError(f"Could not locate npm latest dist-tag for {package!r}")
    return latest


def fetch_latest_version(gh: Github, pin: VersionPin) -> str:
    """Return the latest normalized version for *pin*.

    :param gh: Authenticated or anonymous GitHub client.
    :param pin: Pin to check.
    :returns: Latest normalized version string.
    """
    if pin.source == "npm":
        return fetch_latest_npm_version(pin.repo)
    tag = fetch_latest_tag(gh, pin.repo)
    return normalize_tag(tag, pin.strip_prefix)


@dataclass
class PinResult:
    """Outcome of checking one :class:`VersionPin` against GitHub.

    :param pin: The pin that was checked.
    :param current: Version currently pinned in the template file.
    :param latest: Latest version available, or ``None`` on lookup failure.
    :param error: Error message from a failed GitHub lookup, if any.
    """

    pin: VersionPin
    current: str
    latest: str | None
    error: str | None = None

    @property
    def outdated(self) -> bool:
        """Whether a newer version is available.

        :returns: ``True`` if the lookup succeeded and differs from current.
        """
        return self.latest is not None and self.latest != self.current


def build_report(pins: list[VersionPin], gh: Github) -> list[PinResult]:
    """Check every pin's current vs. latest version.

    Lookup failures for one pin do not prevent the others from being
    checked; they are recorded on the resulting :attr:`PinResult.error`.

    :param pins: Pins to check.
    :param gh: Authenticated or anonymous GitHub client.
    :returns: One :class:`PinResult` per pin, in the same order.
    """
    results: list[PinResult] = []
    for pin in pins:
        content = pin.file.read_text()
        current = read_current_version(content, pin)
        try:
            latest = fetch_latest_version(gh, pin)
        except (GithubException, urllib.error.URLError, ValueError) as e:
            results.append(
                PinResult(pin=pin, current=current, latest=None, error=str(e))
            )
            continue
        results.append(PinResult(pin=pin, current=current, latest=latest))
    return results


def apply_changes(results: list[PinResult]) -> list[Path]:
    """Write outdated pins' latest versions to their template files.

    Each affected file is read once, has all of its outdated pins applied in
    memory, and is written back once — several pins commonly share a file
    (e.g. compose.yaml), so per-pin read/write would repeatedly rewrite the
    same file and misreport how many files changed.

    :param results: Report produced by :func:`build_report`.
    :returns: Paths of files actually modified, one entry per file.
    """
    outdated = [result for result in results if result.outdated]

    changed: list[Path] = []
    for file in dict.fromkeys(result.pin.file for result in outdated):
        content = file.read_text()
        for result in outdated:
            if result.pin.file != file:
                continue
            assert result.latest is not None
            content = apply_version(content, result.pin, result.latest)
        file.write_text(content)
        changed.append(file)
    return changed


def sync_template_lockfile(template_dir: Path = TEMPLATE_DIR) -> None:
    """Upgrade the template lockfile and sync its environment.

    ``UV_PROJECT_ENVIRONMENT``/``VIRTUAL_ENV`` are stripped from the
    subprocess environment: dev shells for *this* repo (e.g. devenv) commonly
    export one of these pointing at the main project's own venv.  Strip them so
    uv operates only on the template project in *template_dir*.

    :param template_dir: Directory containing the template's own
        ``pyproject.toml``/``uv.lock`` pair.
    :raises subprocess.CalledProcessError: If ``uv lock -U`` or ``uv sync``
        exits non-zero.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV"}
    }
    subprocess.run(["uv", "lock", "-U"], cwd=template_dir, check=True, env=env)
    subprocess.run(["uv", "sync"], cwd=template_dir, check=True, env=env)


def format_report(results: list[PinResult]) -> str:
    """Render a plain-text table of current vs. latest versions.

    :param results: Report produced by :func:`build_report`.
    :returns: Aligned multi-line table.
    """
    name_width = max(len(r.pin.name) for r in results)
    current_width = max(len(r.current) for r in results)
    lines = []
    for r in results:
        status = r.error or ("update available" if r.outdated else "up to date")
        latest = r.latest if r.latest is not None else "?"
        lines.append(
            f"{r.pin.name:<{name_width}}  {r.current:<{current_width}}  {latest:<{current_width}}  {status}"
        )
    return "\n".join(lines)


def main(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Write the latest versions to the template files. "
            "Without this flag, only a report is printed.",
        ),
    ] = False,
) -> None:
    """Check and optionally update pinned versions in the agent-circus template."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    auth = Auth.Token(token) if token else None
    # PyGithub's default retry policy backs off and blocks for minutes on a
    # rate-limited response; this is a manually-run report/apply tool, so
    # surfacing the failure immediately (via GithubException, per pin) beats
    # hanging.
    gh = Github(auth=auth, retry=0)

    results = build_report(PINS, gh)
    typer.echo(format_report(results))

    if apply:
        changed = apply_changes(results)
        typer.echo(f"\nUpdated {len(changed)} file(s).")

        typer.echo(
            "Updating template lockfile (uv lock -U) and environment (uv sync)..."
        )
        try:
            sync_template_lockfile()
        except subprocess.CalledProcessError as e:
            typer.echo(f"Template lockfile/environment update failed: {e}", err=True)
            raise typer.Exit(code=1) from e

    if any(r.error for r in results):
        raise typer.Exit(code=1)


def run() -> None:
    """Entry point for the ``agent-circus-update-templates`` console script."""
    typer.run(main)


if __name__ == "__main__":
    run()
