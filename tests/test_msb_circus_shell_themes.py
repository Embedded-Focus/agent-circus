"""Static checks for the browser shell color themes."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

THEMES_FILE = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "msb_circus"
    / "templates"
    / "dashboard"
    / "shell-themes.js"
)
PREFIX = "window.MSB_SHELL_THEMES = "
BASE_KEYS = {
    "background",
    "foreground",
    "cursor",
    "cursorAccent",
    "selectionBackground",
}
ANSI_NAMES = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")
ANSI_KEYS = set(ANSI_NAMES) | {"bright" + n.capitalize() for n in ANSI_NAMES}
HEX = re.compile(r"^#[0-9a-f]{6}([0-9a-f]{2})?$")


def _themes() -> list[dict]:
    """Return the theme list parsed from shell-themes.js.

    :returns: The value of ``window.MSB_SHELL_THEMES``.
    :raises AssertionError: If the file does not have the expected shape.
    """
    text = THEMES_FILE.read_text()
    # Anchor at a line start: the file's leading comment also
    # contains the PREFIX text.
    match = re.search("^" + re.escape(PREFIX), text, re.MULTILINE)
    assert match, "missing the window.MSB_SHELL_THEMES assignment"
    body = text[match.end() :].rstrip()
    assert body.endswith(";"), "the assignment must end with ';'"
    return json.loads(body[:-1])


def _luminance(color: str) -> float:
    """Return the WCAG 2 relative luminance of a ``#rrggbb[aa]`` color.

    :param color: Hex color; an alpha part is ignored.
    :returns: Luminance between 0.0 (black) and 1.0 (white).
    """
    channels = []
    for i in (1, 3, 5):
        c = int(color[i : i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(a: str, b: str) -> float:
    """Return the WCAG 2 contrast ratio of two hex colors.

    :param a: First color.
    :param b: Second color.
    :returns: Ratio between 1.0 and 21.0.
    """
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_shell_themes_file_is_a_json_array():
    themes = _themes()
    assert isinstance(themes, list) and themes, (
        "MSB_SHELL_THEMES must be a non-empty array"
    )
    for theme in themes:
        assert isinstance(theme, dict), f"{theme!r} is not an object"
        assert set(theme) == {"id", "label", "group", "scheme", "source", "colors"}, (
            f"{theme.get('id')}: unexpected keys {sorted(theme)}"
        )


def test_shell_theme_ids_are_unique_and_safe():
    ids = [theme["id"] for theme in _themes()]
    for theme_id in ids:
        assert re.fullmatch(r"[a-z0-9-]+", theme_id), f"bad id {theme_id!r}"
        assert theme_id != "auto", f"id {theme_id!r} collides with the Auto choice"
    assert len(ids) == len(set(ids)), "duplicate theme ids"


def test_shell_theme_labels_are_unique():
    labels = [theme["label"] for theme in _themes()]
    assert len(labels) == len(set(labels)), "duplicate theme labels"


def test_shell_theme_groups_are_known_and_contiguous():
    groups = [theme["group"] for theme in _themes()]
    for group in groups:
        assert group in ("Dashboard", "Modus", "Other"), f"unknown group {group!r}"
    blocks = [groups[0]] + [g for i, g in enumerate(groups[1:]) if g != groups[i]]
    assert blocks == ["Dashboard", "Modus", "Other"], f"groups not contiguous: {blocks}"


def test_shell_themes_include_the_dashboard_defaults():
    by_id = {theme["id"]: theme for theme in _themes()}
    assert by_id["msb-light"]["scheme"] == "light", "msb-light must be light"
    assert by_id["msb-dark"]["scheme"] == "dark", "msb-dark must be dark"
    assert by_id["msb-light"]["group"] == "Dashboard", (
        "msb-light must be a Dashboard theme"
    )
    assert by_id["msb-dark"]["group"] == "Dashboard", (
        "msb-dark must be a Dashboard theme"
    )


def test_shell_theme_colors_are_valid():
    for theme in _themes():
        colors = theme["colors"]
        assert BASE_KEYS <= set(colors), f"{theme['id']}: missing base keys"
        unknown = set(colors) - BASE_KEYS - ANSI_KEYS
        assert not unknown, f"{theme['id']}: unknown color keys {sorted(unknown)}"
        for key, value in colors.items():
            assert isinstance(value, str) and HEX.fullmatch(value), (
                f"{theme['id']}.{key}: bad color {value!r}"
            )


def test_shell_theme_ansi_colors_are_all_or_nothing():
    for theme in _themes():
        present = ANSI_KEYS & set(theme["colors"])
        assert len(present) in (0, len(ANSI_KEYS)), (
            f"{theme['id']}: partial ANSI palette ({len(present)} of {len(ANSI_KEYS)})"
        )
        if not present:
            assert theme["id"] in ("msb-light", "msb-dark"), (
                f"{theme['id']}: only the msb themes may lack ANSI colors"
            )


def test_shell_theme_cursor_accent_is_the_background():
    for theme in _themes():
        colors = theme["colors"]
        assert colors["cursorAccent"] == colors["background"], (
            f"{theme['id']}: cursorAccent must equal the background"
        )


def test_shell_theme_scheme_matches_the_background():
    for theme in _themes():
        expected = (
            "light" if _luminance(theme["colors"]["background"]) >= 0.4 else "dark"
        )
        assert theme["scheme"] == expected, (
            f"{theme['id']}: scheme {theme['scheme']!r} does not match its background"
        )


def test_shell_theme_foreground_is_readable():
    for theme in _themes():
        ratio = _contrast(theme["colors"]["foreground"], theme["colors"]["background"])
        # The threshold is 4.0, not 4.5, because Solarized is designed
        # with low contrast (Solarized Light 4.13:1, Solarized Dark 4.75:1).
        assert ratio >= 4.0, f"{theme['id']}: contrast {ratio:.2f}:1 is below 4.0"


def test_shell_theme_sources_are_named():
    for theme in _themes():
        assert isinstance(theme["source"], str) and theme["source"], (
            f"{theme['id']}: missing source"
        )


def test_shell_theme_values_match_upstream():
    by_id = {theme["id"]: theme["colors"] for theme in _themes()}
    assert by_id["dracula"]["background"] == "#282a36"
    assert by_id["modus-vivendi"]["blue"] == "#2fafff"
    assert by_id["modus-operandi"]["selectionBackground"] == "#bdbdbd"
    assert by_id["nord"]["cyan"] == "#88c0d0"
    assert by_id["solarized-dark"]["brightBlack"] == "#002b36"
    assert by_id["catppuccin-mocha"]["brightRed"] == "#f37799"
    assert by_id["gruvbox-light"]["brightBlue"] == "#076678"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_shell_themes_js_parses():
    result = subprocess.run(
        ["node", "--check", str(THEMES_FILE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
