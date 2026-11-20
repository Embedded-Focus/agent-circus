"""Daily upgrade notice for agent-circus 1.x users."""

import os
import time as _time
from pathlib import Path

import typer

UPGRADE_NOTICE_ENV_VAR = "AGENT_CIRCUS_NO_UPGRADE_NOTICE"

UPGRADE_NOTICE_MESSAGE = (
    "agent-circus 2 replaces this Docker/Podman version. To stay on it, pin "
    "agent-circus<2. Set AGENT_CIRCUS_NO_UPGRADE_NOTICE=1 to hide this note."
)


def notice_state_file() -> Path:
    """Return the path of the upgrade notice state file.

    Uses ``$XDG_STATE_HOME/agent-circus/.upgrade-notice``, falling back
    to ``~/.local/state/agent-circus/.upgrade-notice`` when
    ``XDG_STATE_HOME`` is unset, empty, or not absolute. The leading dot
    keeps the file apart from the per-project directories next to it.

    :returns: Path to the state file.
    :rtype: Path
    """
    xdg = os.environ.get("XDG_STATE_HOME")
    base = (
        Path(xdg)
        if xdg and Path(xdg).is_absolute()
        else Path.home() / ".local" / "state"
    )
    return base / "agent-circus" / ".upgrade-notice"


def should_show_notice(state_file: Path, now: float, interval: float = 86400.0) -> bool:
    """Decide whether the upgrade notice should be shown.

    The notice is suppressed when ``AGENT_CIRCUS_NO_UPGRADE_NOTICE`` is
    set to a non-empty value, or when the state file was modified less
    than ``interval`` seconds before ``now``.

    :param state_file: Path of the state file recording the last notice.
    :type state_file: Path
    :param now: Current time as a Unix timestamp.
    :type now: float
    :param interval: Minimum seconds between notices.
    :type interval: float
    :returns: ``True`` if the notice should be shown.
    :rtype: bool
    """
    if os.environ.get(UPGRADE_NOTICE_ENV_VAR):
        return False
    try:
        last = state_file.stat().st_mtime
    except OSError:
        return True
    return now - last >= interval


def maybe_show_upgrade_notice() -> None:
    """Show the upgrade notice on stderr at most once per day.

    When the notice is shown, the state file's parent directory is
    created and the file is touched so the next notice is delayed. Any
    ``OSError`` while reading or writing the state file is ignored; the
    notice is then shown and not recorded.
    """
    state_file = notice_state_file()
    if not should_show_notice(state_file, _time.time()):
        return
    typer.echo(UPGRADE_NOTICE_MESSAGE, err=True)
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.touch()
    except OSError:
        pass
