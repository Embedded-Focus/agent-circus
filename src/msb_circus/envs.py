"""Environment variable resolution and redaction helpers."""

from __future__ import annotations

import fnmatch
import os
import re

from .exceptions import ConfigurationError

REDACTED = "<redacted>"

#: Characters that turn a plain name into a glob pattern (like agent-circus).
_GLOB_CHARS = "*?["


def _matches_glob(name: str, pattern: str) -> bool:
    """Case-insensitively match *name* against an fnmatch glob *pattern*."""
    return fnmatch.fnmatch(name.lower(), pattern.lower())


def resolve_passthrough(
    names: tuple[str, ...] | list[str],
    *,
    environ: dict[str, str] | None = None,
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
