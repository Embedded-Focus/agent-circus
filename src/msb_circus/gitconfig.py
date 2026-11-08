"""Forwarding of the host git configuration into the sandbox (``[git]``).

msb-circus copies the host gitconfig and an optional SSH public signing
key into the guest and writes a managed gitconfig that includes the host
copy and sets ``user.signingkey``. The guest's ``/root/.gitconfig`` only
receives one managed include block at the top, so agents keep their own
``git config --global`` settings. No private key is ever copied; commit
signing with ``gpg.format = ssh`` uses the forwarded SSH agent.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from .config import GitConfig

#: Guest directory of the msb-circus managed git files.
GIT_GUEST_DIR = "/root/.config/msb-circus/git"
#: Guest copy of the host gitconfig, included by the managed file.
GIT_GUEST_HOST_CONFIG = "/root/.config/msb-circus/git/host.gitconfig"
#: Guest copy of the host SSH public signing key.
GIT_GUEST_SIGNING_KEY = "/root/.config/msb-circus/git/signingkey.pub"
#: Guest gitconfig written by msb-circus (include + signingkey).
GIT_GUEST_MANAGED_CONFIG = "/root/.config/msb-circus/git/gitconfig"
#: Guest global gitconfig receiving the managed include block.
GIT_GUEST_GLOBAL_CONFIG = "/root/.gitconfig"

#: Prefixes of OpenSSH public key types accepted as signing keys.
_PUBLIC_KEY_PREFIXES: tuple[str, ...] = ("ssh-", "ecdsa-sha2-", "sk-")
#: Upper bound on the size of a plausible public key file.
_MAX_PUBLIC_KEY_BYTES = 16384


def default_gitconfig_candidates(
    environ: Mapping[str, str] | None = None,
) -> list[Path]:
    """Return the default host gitconfig locations in lookup order.

    :param environ: Environment mapping for ``$HOME`` and
        ``$XDG_CONFIG_HOME``, defaults to :data:`os.environ`.
    :returns: ``~/.gitconfig`` first, then ``$XDG_CONFIG_HOME/git/config``
        (default ``~/.config/git/config``).
    """
    env = os.environ if environ is None else environ
    xdg = env.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path("~/.config")
    return [
        Path("~/.gitconfig").expanduser(),
        base.expanduser() / "git" / "config",
    ]


def resolve_host_gitconfig(
    git: GitConfig, environ: Mapping[str, str] | None = None
) -> tuple[Path | None, int | None, str | None]:
    """Find the host gitconfig to forward.

    With an explicit ``config_path``, only that file is considered and a
    missing file yields a WARNING-level message. Without one, the default
    candidates are probed in order and an empty result yields an
    INFO-level message. The function does not log; the caller does.

    :param git: Resolved git settings.
    :param environ: Environment mapping, defaults to :data:`os.environ`.
    :returns: ``(path, log_level, message)``; ``(path, None, None)`` when
        found, else ``(None, logging.INFO or logging.WARNING, message)``.
    """
    if git.config_path is not None:
        path = Path(git.config_path)
        if path.is_file():
            return path, None, None
        return (
            None,
            logging.WARNING,
            (
                f"git.config_path {git.config_path} not found on the host; "
                "the sandbox gets no host git config"
            ),
        )
    candidates = default_gitconfig_candidates(environ)
    for path in candidates:
        if path.is_file():
            return path, None, None
    looked = ", ".join(str(path) for path in candidates)
    return (
        None,
        logging.INFO,
        (
            f"No host git config found (looked at {looked}); "
            "the sandbox gets no host git config"
        ),
    )


def check_ssh_public_key(path: Path) -> str | None:
    """Check that a host file looks like one OpenSSH public key.

    A file that contains ``PRIVATE KEY`` is always rejected, and only a
    single non-comment line whose first field starts with a known OpenSSH
    key-type prefix passes. The file is never returned or copied by the
    caller when a reason is given.

    :param path: Host path of the candidate key file.
    :returns: ``None`` when the file looks like one public key, else a
        short reason string.
    """
    if not path.is_file():
        return "not found"
    try:
        if path.stat().st_size > _MAX_PUBLIC_KEY_BYTES:
            return "too large for a public key"
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return "not a text file"
    except OSError as e:
        return f"not readable ({e})"
    if "PRIVATE KEY" in text:
        return "looks like a private key"
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if len(lines) != 1:
        return "must contain exactly one public key"
    fields = lines[0].split()
    if len(fields) < 2 or not fields[0].startswith(_PUBLIC_KEY_PREFIXES):
        return "not an OpenSSH public key"
    return None


def render_managed_gitconfig(
    include_host_config: bool, include_signing_key: bool
) -> str:
    """Render the managed gitconfig written into the guest.

    The file carries an ``[include]`` of the forwarded host gitconfig and
    a ``[user] signingkey`` pointing at the copied public key, only for
    the parts requested. Indentation follows git style (one tab).

    :param include_host_config: Include the host gitconfig copy.
    :param include_signing_key: Set ``user.signingkey``.
    :returns: Managed file text ending with a newline.
    :raises ValueError: If both flags are ``False`` (nothing to write).
    """
    if not include_host_config and not include_signing_key:
        raise ValueError("at least one of the parts is required")
    lines = [
        "# Managed by msb-circus; rewritten on every up, start and exec. Do not edit."
    ]
    if include_host_config:
        lines += ["[include]", f"\tpath = {GIT_GUEST_HOST_CONFIG}"]
    if include_signing_key:
        lines += ["[user]", f"\tsigningkey = {GIT_GUEST_SIGNING_KEY}"]
    return "\n".join(lines) + "\n"
