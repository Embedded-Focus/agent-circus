"""Refresh pinned tool versions in the bundled agent templates.

Covers the ``templates/agent-circus`` template (Dockerfile, compose.yaml,
pyproject.toml) and the msb-agents Dockerfile
(``src/msb_circus/templates/Dockerfile.msb-agents``). Looks up the latest
release for each pinned component and rewrites the corresponding version
string. Most components are backed by GitHub releases; npm-packaged
components (npm itself, the OpenCode CLI) are resolved from the npm registry;
container image pins are resolved through the registry API, so a new tag is
always accompanied by its fresh multi-arch manifest-list digest.

Installed as its own console script (``agent-circus-update-templates``),
separate from the ``agent-circus`` CLI, since it edits template *source*
rather than acting on a deployed workspace.

Shortcomings: template directories are derived from this module's own file
location, so they only resolve to the real template sources when run against
an editable/source checkout (e.g. via ``uv run``). Running it from an
installed wheel would edit the installed copy, not the repository. The node
base image in the msb-agents Dockerfile is digest-refreshed in place; its
major tag (26-slim) is never bumped automatically.
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

MSB_AGENTS_DOCKERFILE = (
    Path(__file__).resolve().parent.parent
    / "msb_circus"
    / "templates"
    / "Dockerfile.msb-agents"
)


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
    :param image: Full container image reference including registry host
        (e.g. ``docker.io/library/node``, ``ghcr.io/astral-sh/uv``); used by
        the digest-backed sources.
    :param tag: Fixed tag to resolve the digest for (source
        ``"registry-digest"`` only).
    :param tag_template: Tag built from the resolved version (source
        ``"github-image"`` only), e.g. ``"{version}-dind"``.
    :param source: Version lookup backend.
    """

    name: str
    repo: str
    file: Path
    pattern: re.Pattern[str]
    strip_prefix: str = ""
    image: str = ""
    tag: str = ""
    tag_template: str = ""
    source: Literal["github", "npm", "github-image", "registry-digest"] = "github"


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
    # Resolved from npm rather than GitHub: the template installs
    # @opencode/cli, and the repo's GitHub "latest release" still tracks the
    # v1 line even after the v2 series shipped.
    VersionPin(
        name="opencode",
        repo="@opencode/cli",
        file=TEMPLATE_DIR / "compose.yaml",
        pattern=re.compile(r"OPENCODE_VERSION:\s*(\S+)"),
        source="npm",
    ),
    VersionPin(
        name="mistral-vibe",
        repo="mistralai/mistral-vibe",
        file=TEMPLATE_DIR / "pyproject.toml",
        pattern=re.compile(r"mistral-vibe>=([0-9.]+)"),
        strip_prefix="v",
    ),
    # Resolved from npm: the image installs the published CLI, not a GitHub
    # release, and pi-acp is published without version tags.
    VersionPin(
        name="pi",
        repo="@earendil-works/pi-coding-agent",
        file=TEMPLATE_DIR / "compose.yaml",
        pattern=re.compile(r"PI_VERSION:\s*(\S+)"),
        source="npm",
    ),
    VersionPin(
        name="pi-acp",
        repo="pi-acp",
        file=TEMPLATE_DIR / "compose.yaml",
        pattern=re.compile(r"PI_ACP_VERSION:\s*(\S+)"),
        source="npm",
    ),
]

# Pins in the msb-agents Dockerfile (src/msb_circus/templates/). Kept separate
# from PINS: agent-circus dependency management (dependencies.py) resolves
# pins by filename inside the agent-circus template directory, which does not
# contain the msb-agents Dockerfile. The updater (see ALL_PINS) covers both.
MSB_AGENTS_PINS: list[VersionPin] = [
    # The node base image is digest-pinned only: the multi-arch manifest-list
    # digest is refreshed in place, but the node major tag (26-slim) itself is
    # never bumped automatically — bumping a major is a deliberate manual act.
    VersionPin(
        name="node (msb-agents)",
        repo="",
        file=MSB_AGENTS_DOCKERFILE,
        pattern=re.compile(r"node:26-slim@(sha256:[0-9a-f]{64})"),
        source="registry-digest",
        image="docker.io/library/node",
        tag="26-slim",
    ),
    # Tag and digest move together: the latest uv release tag is resolved from
    # GitHub, then its multi-arch digest from GHCR, so the tag@digest pair
    # stays consistent.
    VersionPin(
        name="uv (msb-agents)",
        repo="astral-sh/uv",
        file=MSB_AGENTS_DOCKERFILE,
        pattern=re.compile(r"ghcr\.io/astral-sh/uv:([0-9][0-9.]*@sha256:[0-9a-f]{64})"),
        source="github-image",
        image="ghcr.io/astral-sh/uv",
        tag_template="{version}",
    ),
    # Latest moby release drives the tag ({version}-dind), then the matching
    # docker.io official image tag is resolved to its multi-arch digest. If
    # the official image lags the moby release, the digest lookup fails and
    # the pin is reported with an error rather than half-updated.
    VersionPin(
        name="docker (msb-agents)",
        repo="moby/moby",
        file=MSB_AGENTS_DOCKERFILE,
        pattern=re.compile(
            r"docker\.io/library/docker:([0-9][0-9.]*-dind@sha256:[0-9a-f]{64})"
        ),
        strip_prefix="docker-v",
        source="github-image",
        image="docker.io/library/docker",
        tag_template="{version}-dind",
    ),
    VersionPin(
        name="opencode (msb-agents)",
        repo="@opencode/cli",
        file=MSB_AGENTS_DOCKERFILE,
        pattern=re.compile(r"@opencode/cli@([0-9][0-9.]*)"),
        source="npm",
    ),
    VersionPin(
        name="claude-code (msb-agents)",
        repo="@anthropic-ai/claude-code",
        file=MSB_AGENTS_DOCKERFILE,
        pattern=re.compile(r"@anthropic-ai/claude-code@([0-9][0-9.]*)"),
        source="npm",
    ),
    VersionPin(
        name="claude-agent-acp (msb-agents)",
        repo="@agentclientprotocol/claude-agent-acp",
        file=MSB_AGENTS_DOCKERFILE,
        pattern=re.compile(r"@agentclientprotocol/claude-agent-acp@([0-9][0-9.]*)"),
        source="npm",
    ),
    VersionPin(
        name="codex (msb-agents)",
        repo="@openai/codex",
        file=MSB_AGENTS_DOCKERFILE,
        pattern=re.compile(r"@openai/codex@([0-9][0-9.]*)"),
        source="npm",
    ),
    VersionPin(
        name="codex-acp (msb-agents)",
        repo="@agentclientprotocol/codex-acp",
        file=MSB_AGENTS_DOCKERFILE,
        pattern=re.compile(r"@agentclientprotocol/codex-acp@([0-9][0-9.]*)"),
        source="npm",
    ),
    VersionPin(
        name="pi (msb-agents)",
        repo="@earendil-works/pi-coding-agent",
        file=MSB_AGENTS_DOCKERFILE,
        pattern=re.compile(r"pi-coding-agent@([0-9][0-9.]*)"),
        source="npm",
    ),
    VersionPin(
        name="pi-acp (msb-agents)",
        repo="pi-acp",
        file=MSB_AGENTS_DOCKERFILE,
        pattern=re.compile(r"(?<![\w.-])pi-acp@([0-9][0-9.]*)"),
        source="npm",
    ),
]

# Every tracked pin, in report order: the agent-circus template first, then
# the msb-agents Dockerfile. This is what the updater checks; PINS alone
# remains the agent-circus recipe used by dependency management.
ALL_PINS: list[VersionPin] = [*PINS, *MSB_AGENTS_PINS]


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
    """Return *content* with every occurrence of the pinned version replaced.

    Some Dockerfiles reference the same image more than once (e.g. two
    ``COPY --from`` lines sharing one digest-pinned source image), so *all*
    matches of the pin's pattern are rewritten, not just the first.

    :param content: Full text of :attr:`VersionPin.file`.
    :param pin: Pin whose pattern locates the version.
    :param new_version: Replacement version string.
    :returns: Updated file content.
    :raises ValueError: If the pattern does not match *content*.
    """
    if pin.pattern.search(content) is None:
        raise ValueError(
            f"Could not locate current version for {pin.name!r} in {pin.file}"
        )

    def _replace(match: re.Match[str]) -> str:
        start, end = match.span(1)
        return (
            match.group(0)[: start - match.start()]
            + new_version
            + match.group(0)[end - match.start() :]
        )

    return pin.pattern.sub(_replace, content)


def fetch_latest_tag(gh: Github, repo: str) -> str:
    """Return the tag name of the latest GitHub release for *repo*.

    :param gh: Authenticated or anonymous GitHub client.
    :param repo: Repository as ``"owner/repo"``.
    :returns: Latest release's raw tag name.
    :raises GithubException: If the repository or its latest release
        cannot be resolved.
    """
    return gh.get_repo(repo).get_latest_release().tag_name


# Registry endpoint and anonymous token issuer for GHCR, keyed by registry
# host. docker.io is handled via the public Docker Hub REST API instead (see
# :func:`_fetch_docker_hub_digest`).
_GHCR_API = "https://ghcr.io"
_GHCR_TOKEN_URL = "https://ghcr.io/token"

# Manifest media types requested in preference order; the first two are
# multi-arch manifest lists/indices, so the returned Docker-Content-Digest is
# the multi-arch digest suitable for digest-pinned FROM/COPY --from lines.
_MANIFEST_MEDIA_TYPES = (
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
)


def _fetch_docker_hub_digest(repo: str, tag: str) -> str:
    """Resolve the digest of a Docker Hub image tag via the public Hub API.

    Used for ``docker.io`` images instead of the registry token dance: the
    Hub REST API is anonymous and returns the tag's multi-arch manifest-list
    digest directly.

    :param repo: Hub repository path (``library/node``).
    :param tag: Tag to resolve.
    :returns: Digest string (``sha256:...``).
    :raises ValueError: If the API response carries no digest.
    :raises urllib.error.URLError: If the request fails (including HTTP
        errors such as a tag that does not exist).
    """
    url = f"https://hub.docker.com/v2/repositories/{repo}/tags/{tag}"
    with urllib.request.urlopen(url, timeout=30) as response:
        data = json.loads(response.read())
    digest = data.get("digest")
    if not isinstance(digest, str) or not digest:
        raise ValueError(f"Docker Hub returned no digest for {repo}:{tag}")
    return digest


def _fetch_ghcr_digest(repo: str, tag: str) -> str:
    """Resolve the digest of a GHCR image tag via the registry API.

    Performs an anonymous pull-scoped token exchange and requests the tag's
    manifest with manifest-list/OCI-index media types so the returned
    ``Docker-Content-Digest`` header is the multi-arch manifest-list digest.

    :param repo: GHCR repository path (``astral-sh/uv``).
    :param tag: Tag to resolve.
    :returns: Digest string (``sha256:...``).
    :raises ValueError: If the registry response carries no digest header.
    :raises urllib.error.URLError: If a registry request fails (including
        HTTP errors such as a tag that does not exist).
    """
    scope = urllib.parse.urlencode({"scope": f"repository:{repo}:pull"})
    with urllib.request.urlopen(f"{_GHCR_TOKEN_URL}?{scope}", timeout=30) as response:
        token = json.loads(response.read())["token"]

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": ", ".join(_MANIFEST_MEDIA_TYPES),
    }
    request = urllib.request.Request(
        f"{_GHCR_API}/v2/{repo}/manifests/{tag}", headers=headers
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        digest = response.headers.get("Docker-Content-Digest")
    if not digest:
        raise ValueError(f"GHCR returned no digest for {repo}:{tag}")
    return digest


def fetch_latest_docker_digest(image: str, tag: str) -> str:
    """Resolve the multi-arch digest of *image:tag* from its registry.

    :param image: Full image reference including registry host
        (``docker.io/library/node``, ``ghcr.io/astral-sh/uv``).
    :param tag: Tag to resolve.
    :returns: Digest string (``sha256:...``).
    :raises ValueError: If the registry is unsupported or the response
        carries no digest.
    :raises urllib.error.URLError: If a registry request fails (including
        HTTP errors such as a tag that does not exist).
    """
    registry_host, _, repo = image.partition("/")
    if registry_host == "docker.io":
        return _fetch_docker_hub_digest(repo, tag)
    if registry_host == "ghcr.io":
        return _fetch_ghcr_digest(repo, tag)
    raise ValueError(f"Unsupported registry for image {image!r}")


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
    :returns: Latest normalized version string. For digest-backed sources
        this is the tag@digest composite (or bare digest) to write into the
        template.
    :raises ValueError: If the registry is unsupported.
    :raises urllib.error.URLError: If a registry request fails.
    :raises GithubException: If the GitHub lookup fails.
    """
    if pin.source == "npm":
        return fetch_latest_npm_version(pin.repo)
    if pin.source == "registry-digest":
        return fetch_latest_docker_digest(pin.image, pin.tag)
    if pin.source == "github-image":
        version = normalize_tag(fetch_latest_tag(gh, pin.repo), pin.strip_prefix)
        tag = pin.tag_template.format(version=version)
        digest = fetch_latest_docker_digest(pin.image, tag)
        return f"{tag}@{digest}"
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
    """Check and optionally update pinned versions in the bundled templates."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    auth = Auth.Token(token) if token else None
    # PyGithub's default retry policy backs off and blocks for minutes on a
    # rate-limited response; this is a manually-run report/apply tool, so
    # surfacing the failure immediately (via GithubException, per pin) beats
    # hanging.
    gh = Github(auth=auth, retry=0)

    results = build_report(ALL_PINS, gh)
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
