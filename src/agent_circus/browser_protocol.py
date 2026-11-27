"""Protocol version shared by the bundled browser pages and the proxy.

One integer major version covers all communication between the bundled
browser pages and the proxy: the ``/api/state`` and ``/api/logs`` HTTP
endpoints and the ``/api/shell`` WebSocket. HTTP clients send the
version in the ``X-Agent-Circus-Protocol-Version`` request header; the
``/api/state`` response repeats it in the top-level
``protocol_version`` field; a shell client sends it as the
``protocol_version`` field of its first ``hello`` message.

The WebSocket shell also negotiates a subprotocol during the HTTP
upgrade. A client requests :data:`BROWSER_PROTOCOL_SUBPROTOCOL` in the
``Sec-WebSocket-Protocol`` header. A proxy that supports the version
echoes the selected value in its ``101`` response; an old proxy echoes
nothing, so the client can detect the mismatch before it sends its
first message. A client without an offer still completes the upgrade
and is rejected through the versioned ``hello`` message instead.

The package version (``agent_circus.__version__``) is separate: a
package release does not imply a protocol change. Increase
:data:`BROWSER_PROTOCOL_VERSION` only for a breaking change, such as
removing or renaming a field or changing a message shape. Adding an
optional field to a JSON object is not a breaking change.

Clients must ignore unknown JSON fields, so an optional field can be
added without a version increase.

This module is the authoritative description of these rules. The
JavaScript pages (``dashboard.js`` and ``shell.js``) hold a copy of the
version number and must be kept in sync; a static test compares the
copies with the Python constant.
"""

from __future__ import annotations

import re
from typing import Any

#: Major version of the browser protocol (HTTP API and shell hello).
BROWSER_PROTOCOL_VERSION = 1

#: HTTP request header that carries :data:`BROWSER_PROTOCOL_VERSION`.
BROWSER_PROTOCOL_HEADER = "X-Agent-Circus-Protocol-Version"

#: WebSocket subprotocol that a shell client offers during the upgrade.
#: Derived from the version, so a version increase changes the value.
BROWSER_PROTOCOL_SUBPROTOCOL = f"agent-circus.v{BROWSER_PROTOCOL_VERSION}"

#: Private WebSocket close code for an incompatible application
#: protocol. The 4xxx range is application specific and never collides
#: with the registered RFC 6455 codes.
INCOMPATIBLE_PROTOCOL_CLOSE_CODE = 4406

#: Message shown to the user after a version mismatch. A page that
#: contains incompatible JavaScript can only be fixed by a full reload,
#: so the message asks for one.
INCOMPATIBLE_PROTOCOL_MESSAGE = (
    "This page is incompatible with the running proxy. Reload the page."
)

#: One base-10 integer, nothing else (a version header is a string).
_DECIMAL_INTEGER = re.compile(r"[0-9]+")


def parse_header_version(value: str | None) -> int | None:
    """Return the protocol version from an HTTP header value.

    The parser is total: it returns a value for every input and never
    raises. A very long numeric string would otherwise make ``int``
    raise, and the proxy must answer a malformed request with the
    structured 409 error, not with a connection failure.

    :param value: Header value (already stripped), or ``None`` when the
        header is missing.
    :returns: The version integer, or ``None`` when the value is
        missing or is not one base-10 integer.
    """
    if value is None or not _DECIMAL_INTEGER.fullmatch(value):
        return None
    try:
        return int(value, 10)
    except ValueError:
        return None


def parse_hello_version(value: Any) -> int | None:
    """Return the protocol version from a shell hello message field.

    :param value: The ``protocol_version`` field of the hello object.
    :returns: The version integer, or ``None`` when the field is
        absent, a Boolean, or not an integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
