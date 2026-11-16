"""Host-side helpers that install the circus-router service in the guest.

The router itself (``templates/circus_router.py``) is a standalone
stdlib script. This module base64-encodes it for transport into the
guest and renders the POSIX ``sh`` install script that writes the
router script, the routes file and the s6 service directory.

Like the daemons, the router is installed again on every ``up`` or
``start``: ``/run`` is reset on every boot, so ``/run/circus-router``
and ``/run/service/circus-router`` must be recreated. A routes change
needs no restart (the router reloads the file on mtime change); a
changed router script restarts the service.
"""

from __future__ import annotations

import base64
import json
import shlex
from importlib import resources
from typing import TYPE_CHECKING

from .config import ROUTER_GUEST_PORT
from .daemons import svscan_wait_lines

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Guest directory of the router script and its routes file.
ROUTER_DIR = "/run/circus-router"
#: s6 service directory of the router.
ROUTER_SERVICE = "/run/service/circus-router"
#: Guest directory of the router's s6-log output.
ROUTER_LOG_DIR = "/var/log/circus-router"
#: Host env var carrying the base64-encoded router script into the guest.
ROUTER_SCRIPT_ENV_VAR = "CIRCUS_ROUTER_SCRIPT"
#: Install script exit code when the guest has no python3.
EXIT_NO_PYTHON = 6


def router_script_source() -> str:
    """Return the text of templates/circus_router.py.

    :returns: The router script text.
    """
    return (
        resources.files("msb_circus").joinpath("templates/circus_router.py").read_text()
    )


def encode_router_script() -> dict[str, str]:
    """Return the env mapping that ships the router script into the guest.

    :returns: ``{ROUTER_SCRIPT_ENV_VAR: base64 of the router script}``.
    """
    return {
        ROUTER_SCRIPT_ENV_VAR: base64.b64encode(router_script_source().encode()).decode(
            "ascii"
        )
    }


def _render_router_run_script() -> str:
    """Render the s6 ``run`` script that starts the router.

    :returns: The complete ``run`` script text with a final newline.
    """
    return (
        "#!/command/with-contenv sh\n"
        "exec 2>&1\n"
        f"exec python3 {ROUTER_DIR}/router.py --port {ROUTER_GUEST_PORT} "
        f"--routes {ROUTER_DIR}/routes.json\n"
    )


def _render_router_log_run_script() -> str:
    """Render the s6 ``log/run`` script for the router's logs.

    Mirrors the daemon logger in :mod:`msb_circus.daemons`: it stamps
    every line in UTC via ``s6-log``'s ``T`` action.

    :returns: The complete logger ``run`` script text with a final newline.
    """
    logd = shlex.quote(ROUTER_LOG_DIR)
    return (
        "#!/bin/sh\n"
        f"mkdir -p {logd}\n"
        "export TZ=UTC0\n"
        f"exec /command/s6-log -b n10 s1000000 T {logd}\n"
    )


def build_router_install_script(routes: Mapping[str, int]) -> str:
    """Render the POSIX sh script that installs or updates the router.

    The script waits for s6-svscan, checks for ``python3`` (exit code
    :data:`EXIT_NO_PYTHON` otherwise), writes the base64-decoded router
    script and the sorted JSON routes file, creates the s6 service
    directory on first install (with ``s6-svscanctl -an``), and restarts
    the service when the router script changed.

    :param routes: Mapping of daemon name to guest TCP port.
    :returns: The complete install script text with a final newline.
    """
    routes_json = shlex.quote(json.dumps(dict(routes), sort_keys=True))
    s = ROUTER_SERVICE
    t = f"{ROUTER_SERVICE}.new"
    lines = [
        "set -eu",
        *svscan_wait_lines(),
        (
            "command -v python3 >/dev/null 2>&1 || { "
            'echo "python3 not found in the sandbox" >&2; '
            f"exit {EXIT_NO_PYTHON}; }}"
        ),
        "umask 022",
        f"d={ROUTER_DIR}",
        'mkdir -p "$d"',
        f'printf \'%s\' "${{{ROUTER_SCRIPT_ENV_VAR}}}" | base64 -d > "$d/router.py.tmp"',
        "changed=0",
        'if cmp -s "$d/router.py.tmp" "$d/router.py"; then rm -f "$d/router.py.tmp"',
        'else mv "$d/router.py.tmp" "$d/router.py"; changed=1; fi',
        f"printf '%s' {routes_json} > \"$d/routes.json.tmp\"",
        'mv "$d/routes.json.tmp" "$d/routes.json"',
        f"s={s}",
        'if [ ! -d "$s" ]; then',
        f"    t={t}",
        '    rm -rf "$t"; mkdir -p "$t/log"',
        "    install_file() {",
        '        printf \'%s\' "$3" > "$1.tmp"',
        '        chmod "$2" "$1.tmp"',
        '        mv "$1.tmp" "$1"',
        "    }",
        '    install_file "$t/run" 755 ' + shlex.quote(_render_router_run_script()),
        (
            '    install_file "$t/log/run" 755 '
            + shlex.quote(_render_router_log_run_script())
        ),
        '    mv "$t" "$s"',
        "    /command/s6-svscanctl -an /run/service",
        'elif [ "$changed" = 1 ]; then',
        '    /command/s6-svc -r "$s"',
        "fi",
    ]
    return "\n".join(lines) + "\n"
