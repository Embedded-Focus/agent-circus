"""Environment variable resolution and redaction helpers."""

from __future__ import annotations

import os

from .exceptions import ConfigurationError

REDACTED = "<redacted>"


def resolve_passthrough(
    names: tuple[str, ...] | list[str],
    *,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve required host environment variables for pass-through.

    Fails fast before any ``msb`` invocation when a required variable is
    unset on the host (see plan decision 9).

    :param names: Variable names that must exist on the host.
    :param environ: Environment mapping, defaults to :data:`os.environ`.
    :returns: Mapping of variable name to value, ready to pass to ``msb``.
    :raises ConfigurationError: If any required variable is unset.
    """
    env = os.environ if environ is None else environ
    missing = [name for name in names if name not in env]
    if missing:
        raise ConfigurationError(
            "Missing required environment variable(s) on the host: "
            + ", ".join(missing)
        )
    return {name: env[name] for name in names}


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
