"""Environment variable resolution and redaction helpers."""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

from .config import SSH_AGENT_GUEST_SOCKET
from .exceptions import ConfigurationError

if TYPE_CHECKING:
    from .config import MsbConfig

REDACTED = "<redacted>"

#: Characters that turn a plain name into a glob pattern (like agent-circus).
_GLOB_CHARS = "*?["

#: Valid environment variable names (POSIX shell identifiers).
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _matches_glob(name: str, pattern: str) -> bool:
    """Case-insensitively match *name* against an fnmatch glob *pattern*."""
    return fnmatch.fnmatch(name.lower(), pattern.lower())


def resolve_passthrough(
    names: tuple[str, ...] | list[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve host environment variables for pass-through.

    Entries are resolved like agent-circus's ``filter_env``: names prefixed
    with ``re:`` are case-insensitive Python regexes (via :func:`re.search`),
    entries containing glob characters (``*``, ``?``, ``[``) are fnmatch
    globs, and everything else is a literal variable name. Pattern entries
    expand to every matching variable currently set on the host; a pattern
    that matches nothing is not an error. Literal names are required: they
    fail fast before any ``msb`` invocation when unset on the host (see
    plan decision 9).

    :param names: Variable names or glob/``re:``-prefixed patterns.
    :param environ: Environment mapping, defaults to :data:`os.environ`.
    :returns: Mapping of variable name to value, ready to pass to ``msb``.
    :raises ConfigurationError: If any required literal variable is unset.
    """
    env = os.environ if environ is None else environ
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        if name.startswith("re:"):
            regex = re.compile(name[3:], re.IGNORECASE)
            resolved.update((var, env[var]) for var in sorted(env) if regex.search(var))
        elif any(ch in name for ch in _GLOB_CHARS):
            resolved.update(
                (var, env[var]) for var in sorted(env) if _matches_glob(var, name)
            )
        elif name in env:
            resolved[name] = env[name]
        else:
            missing.append(name)
    if missing:
        raise ConfigurationError(
            "Missing required environment variable(s) on the host: "
            + ", ".join(missing)
        )
    return resolved


def parse_env_assignments(pairs: list[str]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` CLI assignments into a mapping.

    :param pairs: Raw ``KEY=VALUE`` strings (the value may contain ``=``).
    :returns: Parsed mapping.
    :raises ConfigurationError: If a pair is malformed or has an empty key.
    """
    result: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ConfigurationError(
                f"Invalid environment assignment {pair!r}; expected KEY=VALUE"
            )
        key, value = pair.split("=", 1)
        if not key:
            raise ConfigurationError(
                f"Invalid environment assignment {pair!r}; key must not be empty"
            )
        result[key] = value
    return result


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """Return a log-safe copy of an environment mapping.

    :param env: Mapping to redact.
    :returns: Mapping with every value replaced by :data:`REDACTED`.
    """
    return dict.fromkeys(env, REDACTED)


def quote_shell_word(value: str) -> str:
    """Quote *value* as a shell word in double quotes, keeping ``$`` live.

    User-authored environment values are guest shell words: ``$VAR``
    expands when a guest shell evaluates the quoted string. ``$$`` yields
    a literal ``$`` (docker-compose convention), backslashes and double
    quotes are escaped. Everything else keeps POSIX double-quote
    semantics, so ``$(cmd)`` and backticks run inside the guest.

    :param value: Raw user-authored value.
    :returns: The value wrapped in double quotes, ready for an
        ``export KEY=...`` line or ``sh -c`` script.
    """
    out: list[str] = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "$" and value.startswith("$$", i):
            out.append("\\$")
            i += 2
        elif char in ("\\", '"'):
            out.append("\\" + char)
            i += 1
        else:
            out.append(char)
            i += 1
    return '"' + "".join(out) + '"'


def merged_env(
    config: MsbConfig,
    env_passthrough: tuple[str, ...],
    env: Mapping[str, str],
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], frozenset[str]]:
    """Return the environment of a daemon or hook, plus its shell words.

    In increasing precedence: the ``[env]``/``[msb.env]`` variables, the
    top-level ``env_passthrough`` variables, ``SSH_AUTH_SOCK`` (when
    ``[ssh]`` is configured), *env_passthrough*, and *env*.

    Values are classified by origin and content: user-authored values
    (``[env]``/``[msb.env]`` and *env*) containing ``$`` are guest shell
    words whose references expand in the guest; passthrough values stay
    literal even when they contain ``$`` (host secrets), and values
    without ``$`` are literal because quoting them would change nothing.
    A later source overrides both the value and the classification of a
    key.

    :param config: Resolved msb-circus configuration.
    :param env_passthrough: Extra passthrough patterns of the entry.
    :param env: Extra variables of the entry.
    :param environ: Host environment for passthrough resolution, defaults
        to :data:`os.environ` (injected by the web shell tests).
    :returns: Tuple of the merged environment and the names of the keys
        whose values are guest shell words.
    :raises ConfigurationError: If a passthrough variable is unset on the host.
    """
    merged: dict[str, str] = {}
    origins: dict[str, bool] = {}  # True: shell word, False: literal

    def put(values: Mapping[str, str], *, shell_word: bool) -> None:
        """Merge *values*, overriding value and origin of known keys."""
        merged.update(values)
        origins.update(dict.fromkeys(values, shell_word))

    put(config.env, shell_word=True)
    put(resolve_passthrough(config.env_passthrough, environ=environ), shell_word=False)
    if config.ssh:
        put({"SSH_AUTH_SOCK": SSH_AGENT_GUEST_SOCKET}, shell_word=False)
    put(resolve_passthrough(env_passthrough, environ=environ), shell_word=False)
    put(env, shell_word=True)
    return merged, frozenset(
        k for k, shell_word in origins.items() if shell_word and "$" in merged[k]
    )
