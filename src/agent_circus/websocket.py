"""Minimal WebSocket server on asyncio streams (RFC 6455, server side).

The dashboard's shell endpoint needs a WebSocket server, but the shell
is an opt-in feature and agent-circus has no runtime dependencies beyond
msb itself. This module implements the server side of RFC 6455 on the
standard library only: the upgrade handshake, a frame codec and a
:class:`WebSocket` class for one connection. No extensions, no
compression. The handshake can echo one application-selected
subprotocol. The caller chooses the value.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import struct

#: GUID from RFC 6455, section 1.3.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
#: Largest accepted message (all fragments together), in bytes.
MAX_MESSAGE_BYTES = 1024 * 1024

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_CLOSE_NORMAL = 1000
_CLOSE_PROTOCOL_ERROR = 1002
_CLOSE_BAD_DATA = 1007
_CLOSE_TOO_BIG = 1009


class HandshakeError(Exception):
    """The request is not a valid WebSocket upgrade (answer 400)."""


class WebSocketClosed(Exception):
    """The peer closed the connection or broke the protocol."""


def accept_key(key: str) -> str:
    """Return the Sec-WebSocket-Accept value for a client key.

    :param key: The client's Sec-WebSocket-Key header value.
    :returns: The base64 accept value from RFC 6455 §1.3.
    """
    digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_headers(head: bytes) -> dict[str, str]:
    """Return the request headers, names lowercased, values stripped.

    :param head: Raw request head bytes (lines separated by CRLF).
    :returns: Mapping of lowercased header name to value. A header that
        appears twice keeps the last value.
    """
    headers: dict[str, str] = {}
    for line in head.split(b"\r\n")[1:]:
        name, sep, value = line.partition(b":")
        if sep:
            headers[name.strip().decode("latin-1").lower()] = value.strip().decode(
                "latin-1"
            )
    return headers


def check_upgrade(head: bytes) -> str:
    """Validate a WebSocket upgrade request head.

    :param head: Raw request head bytes.
    :returns: The Sec-WebSocket-Key.
    :raises HandshakeError: Method not GET, no ``Upgrade: websocket``,
        ``Connection`` without the ``upgrade`` token (comma list,
        case-insensitive), version not 13, or a key that is not base64
        of 16 bytes.
    """
    request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
    method = request_line.partition(" ")[0]
    if method != "GET":
        raise HandshakeError("not a GET request")
    headers = parse_headers(head)
    upgrade = headers.get("upgrade", "").lower()
    if upgrade != "websocket":
        raise HandshakeError("Upgrade: websocket missing")
    connection = {
        token.strip().lower() for token in headers.get("connection", "").split(",")
    }
    if "upgrade" not in connection:
        raise HandshakeError("Connection header lacks the upgrade token")
    if headers.get("sec-websocket-version") != "13":
        raise HandshakeError("unsupported WebSocket version")
    key = headers.get("sec-websocket-key", "")
    try:
        raw = base64.b64decode(key.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as e:
        raise HandshakeError("invalid Sec-WebSocket-Key") from e
    if len(raw) != 16:
        raise HandshakeError("invalid Sec-WebSocket-Key")
    return key


def handshake_response(key: str, subprotocol: str | None = None) -> bytes:
    """Return the ``101 Switching Protocols`` response head.

    :param key: The validated Sec-WebSocket-Key.
    :param subprotocol: The one selected subprotocol to echo back, or
        ``None`` when the request offered none and none was selected.
    :returns: The complete response head bytes.
    """
    head = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(key)}\r\n"
    )
    if subprotocol is not None:
        head += f"Sec-WebSocket-Protocol: {subprotocol}\r\n"
    return (head + "\r\n").encode()


def encode_frame(opcode: int, payload: bytes) -> bytes:
    """Encode one unmasked, final server frame.

    :param opcode: Frame opcode (OP_TEXT, OP_BINARY, OP_CLOSE, ...).
    :param payload: Frame payload.
    :returns: The complete frame bytes (7-, 16- or 64-bit length).
    """
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([length])
    elif length <= 0xFFFF:
        header += bytes([126]) + struct.pack(">H", length)
    else:
        header += bytes([127]) + struct.pack(">Q", length)
    return header + payload


def _unmask(payload: bytes, mask: bytes) -> bytes:
    """XOR *payload* with the 4-byte *mask*, fast for big payloads.

    Works on the whole buffer as one big integer instead of per byte.

    :param payload: Masked bytes.
    :param mask: Exactly 4 mask bytes.
    :returns: The unmasked bytes.
    """
    n = len(payload)
    if n == 0:
        return payload
    extended = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(extended, "big")).to_bytes(
        n, "big"
    )


class WebSocket:
    """Server side of one WebSocket connection over asyncio streams.

    Reads messages with :meth:`recv` (control frames are answered
    automatically) and writes data frames with :meth:`send_text` and
    :meth:`send_binary`. One instance serves one connection; all methods
    run on the proxy's event loop.
    """

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Store the streams of the upgraded connection.

        :param reader: Client-side stream reader.
        :param writer: Client-side stream writer.
        """
        self._reader = reader
        self._writer = writer
        self._close_sent = False

    async def _read_frame(self) -> tuple[int, bytes, bool]:
        """Read one frame, unmasked.

        :returns: ``(opcode, unmasked payload, fin)``.
        :raises WebSocketClosed: On EOF, connection error or a protocol
            violation (after answering with a close frame).
        """
        try:
            head = await self._reader.readexactly(2)
        except (
            asyncio.IncompleteReadError,
            ConnectionError,
            asyncio.LimitOverrunError,
        ):
            raise WebSocketClosed() from None
        fin = bool(head[0] & 0x80)
        if head[0] & 0x70:
            await self.close(_CLOSE_PROTOCOL_ERROR, "reserved bits set")
            raise WebSocketClosed()
        opcode = head[0] & 0x0F
        if not head[1] & 0x80:
            await self.close(_CLOSE_PROTOCOL_ERROR, "unmasked client frame")
            raise WebSocketClosed()
        length = head[1] & 0x7F
        if length == 126:
            try:
                length = struct.unpack(">H", await self._reader.readexactly(2))[0]
            except (asyncio.IncompleteReadError, ConnectionError):
                raise WebSocketClosed() from None
        elif length == 127:
            try:
                length = struct.unpack(">Q", await self._reader.readexactly(8))[0]
            except (asyncio.IncompleteReadError, ConnectionError):
                raise WebSocketClosed() from None
        if opcode >= 0x8 and (not fin or length > 125):
            await self.close(_CLOSE_PROTOCOL_ERROR, "bad control frame")
            raise WebSocketClosed()
        if length > MAX_MESSAGE_BYTES:
            await self.close(_CLOSE_TOO_BIG, "message too big")
            raise WebSocketClosed()
        try:
            mask = await self._reader.readexactly(4)
            masked = await self._reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError):
            raise WebSocketClosed() from None
        return opcode, _unmask(masked, mask), fin

    async def _read_message(self) -> tuple[int, bytes]:
        """Read one complete data message, joining fragments.

        :returns: ``(opcode, payload)`` of the message.
        :raises WebSocketClosed: On close, EOF, bad UTF-8 or protocol
            error.
        """
        buffer = bytearray()
        message_opcode: int | None = None
        while True:
            opcode, payload, fin = await self._read_frame()
            if opcode == OP_CLOSE:
                close_payload = payload[:2]
                code = (
                    struct.unpack(">H", close_payload)[0]
                    if len(close_payload) >= 2
                    else _CLOSE_NORMAL
                )
                await self._send_close_frame(code)
                raise WebSocketClosed()
            if opcode == OP_PING:
                await self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BINARY):
                if message_opcode is not None:
                    await self.close(
                        _CLOSE_PROTOCOL_ERROR, "new data frame in fragment"
                    )
                    raise WebSocketClosed()
                message_opcode = opcode
            elif opcode == OP_CONT:
                if message_opcode is None:
                    await self.close(
                        _CLOSE_PROTOCOL_ERROR, "continuation without start"
                    )
                    raise WebSocketClosed()
            else:
                await self.close(_CLOSE_PROTOCOL_ERROR, "unknown opcode")
                raise WebSocketClosed()
            buffer.extend(payload)
            if len(buffer) > MAX_MESSAGE_BYTES:
                await self.close(_CLOSE_TOO_BIG, "message too big")
                raise WebSocketClosed()
            if fin:
                if message_opcode == OP_TEXT:
                    try:
                        bytes(buffer).decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        await self.close(
                            _CLOSE_BAD_DATA, "invalid UTF-8 in text message"
                        )
                        raise WebSocketClosed() from None
                return message_opcode, bytes(buffer)

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        """Write one frame and drain.

        :param opcode: Frame opcode.
        :param payload: Frame payload.
        """
        self._writer.write(encode_frame(opcode, payload))
        await self._writer.drain()

    async def _send_close_frame(self, code: int) -> None:
        """Send a close frame once.

        :param code: Close status code.
        """
        if self._close_sent:
            return
        self._close_sent = True
        with_frame = encode_frame(OP_CLOSE, struct.pack(">H", code))
        try:
            self._writer.write(with_frame)
            await self._writer.drain()
        except (ConnectionError, OSError):
            pass

    async def recv(self) -> tuple[int, bytes]:
        """Return the next data message as ``(OP_TEXT or OP_BINARY, payload)``.

        Answers pings with pongs, ignores pongs, joins continuation
        frames, and answers a close frame with a close frame.

        :returns: Opcode and payload of the next data message.
        :raises WebSocketClosed: On close, EOF, or a protocol error
            (after sending close 1002 or 1009).
        """
        return await self._read_message()

    async def send_text(self, text: str) -> None:
        """Send *text* as one binary-safe text frame.

        :param text: UTF-8 text to send.
        """
        await self._send_frame(OP_TEXT, text.encode("utf-8"))

    async def send_binary(self, data: bytes) -> None:
        """Send *data* as one binary frame, then drain.

        :param data: Raw bytes to send.
        """
        await self._send_frame(OP_BINARY, data)

    async def close(self, code: int = _CLOSE_NORMAL, reason: str = "") -> None:
        """Send a close frame once, then close the writer.

        Idempotent; a second call only closes the writer again.

        :param code: Close status code (RFC 6455 §7.4).
        :param reason: Short close reason.
        """
        await self._send_close_frame(code)
        self._writer.close()
