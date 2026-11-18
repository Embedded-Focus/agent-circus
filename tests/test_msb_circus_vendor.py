"""Guards for the vendored xterm.js files (hashes, license, serving)."""

import hashlib
import re
from importlib import resources
from pathlib import Path

from msb_circus.dashboard_http import STATIC_FILES

VENDOR_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "msb_circus"
    / "templates"
    / "dashboard"
    / "vendor"
    / "xterm"
)


def _recorded_hashes() -> dict[str, str]:
    """Parse the file-hash table of VENDORED.md.

    :returns: Mapping of file name to SHA-256 hex digest.
    """
    text = (VENDOR_DIR / "VENDORED.md").read_text()
    hashes: dict[str, str] = {}
    for row in text.splitlines():
        match = re.match(r"^\| `([^`]+)` \|[^|]+\| `([0-9a-f]{64})` \|$", row)
        if match:
            hashes[match.group(1)] = match.group(2)
    return hashes


def test_vendored_files_match_recorded_hashes():
    hashes = _recorded_hashes()
    assert set(hashes) == {
        "xterm.js",
        "xterm.css",
        "addon-fit.js",
        "LICENSE.xterm",
        "LICENSE.addon-fit",
    }
    for name, expected in hashes.items():
        content = (VENDOR_DIR / name).read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected, name


def test_vendored_files_have_no_source_map_references():
    for name in ("xterm.js", "addon-fit.js"):
        assert "sourceMappingURL" not in (VENDOR_DIR / name).read_text(), name


def test_vendored_license_present():
    for name in ("LICENSE.xterm", "LICENSE.addon-fit"):
        text = (VENDOR_DIR / name).read_text()
        assert "MIT" in text
        assert "xterm" in text.lower()


def test_vendor_files_are_served():
    for path in (
        "/vendor/xterm/xterm.js",
        "/vendor/xterm/xterm.css",
        "/vendor/xterm/addon-fit.js",
    ):
        assert path in STATIC_FILES
        file_name, _content_type = STATIC_FILES[path]
        resource = resources.files("msb_circus").joinpath(
            f"templates/dashboard/{file_name}"
        )
        assert resource.is_file(), f"missing packaged asset for {path}"
