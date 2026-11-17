"""Tests for the minimal WebSocket server (msb_circus.websocket)."""

import asyncio
import socket
from typing import TYPE_CHECKING, cast

import pytest
import websockets.asyncio.client
import websockets.exceptions

if TYPE_CHECKING:
    from collections.abc import Callable

    from websockets.typing import Origin

from msb_circus.websocket import (
    MAX_MESSAGE_BYTES,
    OP_BINARY,
    OP_TEXT,
    HandshakeError,
    WebSocket,
    WebSocketClosed,
    accept_key,
    check_upgrade,
    encode_frame,
    handshake_response,
    parse_headers,
)

GOOD_HEAD = (
    b"GET /api/shell HTTP/1.1\r\n"
    b"Host: localhost:7700\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: keep-alive, Upgrade\r\n"
    b"Sec-WebSocket-Version: 13\r\n"
    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
)


def test_accept_key_matches_rfc_example():
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_check_upgrade_accepts_browser_request():
    assert check_upgrade(GOOD_HEAD) == "dGhlIHNhbXBsZSBub25jZQ=="


def test_parse_headers_lowercases_and_keeps_last():
    head = b"GET / HTTP/1.1\r\nX-A: one\r\nx-a: two\r\n\r\n"
    assert parse_headers(head) == {"x-a": "two"}


@pytest.mark.parametrize(
    "head",
    [
        b"POST / HTTP/1.1\r\n" + GOOD_HEAD.split(b"\r\n", 1)[1],
        b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",
        GOOD_HEAD.replace(b"Sec-WebSocket-Version: 13", b"Sec-WebSocket-Version: 8"),
        GOOD_HEAD.replace(b"dGhlIHNhbXBsZSBub25jZQ==", b"tooshort"),
        GOOD_HEAD.replace(
            b"Connection: keep-alive, Upgrade", b"Connection: keep-alive"
        ),
    ],
)
def test_check_upgrade_rejects_bad_requests(head):
    with pytest.raises(HandshakeError):
        check_upgrade(head)


def test_handshake_response_contains_accept():
    response = handshake_response("dGhlIHNhbXBsZSBub25jZQ==")
    assert response.startswith(b"HTTP/1.1 101 Switching Protocols\r\n")
    assert b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in response


def test_encode_frame_lengths():
    for size, expected_header in ((0, 2), (125, 2), (126, 4), (65535, 4), (65536, 10)):
        frame = encode_frame(OP_BINARY, b"x" * size)
        assert len(frame) == expected_header + size, size
        assert frame[0] == 0x80 | OP_BINARY
        if size <= 125:
            assert frame[1] == size
        elif size <= 0xFFFF:
            assert frame[1] == 126


# ---- integration with the websockets client -------------------------------


class WsServer:
    """A real asyncio server speaking msb_circus.websocket.WebSocket."""

    def __init__(self):
        self.handler: Callable | None = None
        self.server: asyncio.AbstractServer | None = None
        self.port: int | None = None

    async def start(self, handler) -> None:
        self.handler = handler
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        assert self.server is not None
        self.port = self.server.sockets[0].getsockname()[1]

    async def _accept(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        key = check_upgrade(head)
        writer.write(handshake_response(key))
        await writer.drain()
        ws = WebSocket(reader, writer)
        try:
            handler = self.handler
            assert handler is not None
            await handler(ws)
        except WebSocketClosed:
            pass
        finally:
            if not writer.is_closing():
                writer.close()

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()


def _run_server_test(handler, client_coro):
    async def scenario():
        server = WsServer()
        await server.start(handler)
        try:
            return await client_coro(server.port)
        finally:
            await server.stop()

    return asyncio.run(scenario())


async def _connect(port):
    return await websockets.asyncio.client.connect(
        f"ws://127.0.0.1:{port}/api/shell",
        origin=cast("Origin", "http://localhost:7700"),
        open_timeout=5,
    )


def test_echo_text_and_binary():
    async def handler(ws):
        while True:
            try:
                opcode, payload = await ws.recv()
            except WebSocketClosed:
                return
            if opcode == OP_TEXT:
                await ws.send_text(payload.decode())
            else:
                await ws.send_binary(payload)

    async def client(port):
        async with await _connect(port) as ws:
            await ws.send("hello")
            assert await ws.recv() == "hello"
            await ws.send(b"\x00\x01\x02")
            assert await ws.recv() == b"\x00\x01\x02"

    _run_server_test(handler, client)


def test_large_binary_message_roundtrip():
    async def handler(ws):
        _opcode, payload = await ws.recv()
        await ws.send_binary(payload)

    async def client(port):
        payload = bytes(range(256)) * 2000  # 500 kB
        async with await _connect(port) as ws:
            await ws.send(payload)
            received = await ws.recv()
            assert received == payload

    _run_server_test(handler, client)


def test_fragmented_message_is_joined():
    async def handler(ws):
        opcode, payload = await ws.recv()
        assert opcode == OP_TEXT
        await ws.send_text(payload.decode())

    async def client(port):
        async with await _connect(port) as ws:
            await ws.send(["hello ", "fragmented ", "world"])
            assert await ws.recv() == "hello fragmented world"

    _run_server_test(handler, client)


def test_ping_gets_pong():
    async def handler(ws):
        await ws.recv()

    async def client(port):
        async with await _connect(port) as ws:
            pong = await ws.ping()
            await pong  # resolves when the pong arrives
            await ws.close()

    _run_server_test(handler, client)


def test_oversized_message_closes_with_1009():
    async def handler(ws):
        try:
            await ws.recv()
        except WebSocketClosed:
            pass

    async def client(port):
        async with await _connect(port) as ws:
            await ws.send(b"x" * (MAX_MESSAGE_BYTES + 1))
            try:
                await asyncio.wait_for(ws.recv(), 5)
                received_after = True
            except websockets.exceptions.ConnectionClosed as e:
                received_after = False
                assert e.rcvd is None or e.rcvd.code == 1009
            assert not received_after

    _run_server_test(handler, client)


def test_client_close_ends_recv():
    async def handler(ws):
        await ws.recv()
        # After the client closed, the next recv raises WebSocketClosed.
        await ws.close()

    async def client(port):
        async with await _connect(port) as ws:
            await ws.send("bye")
            await ws.close()

    _run_server_test(handler, client)


# ---- raw socket tests (frames the library refuses to send) -----------------


def _client_socket(port):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    head = GOOD_HEAD.replace(
        b"Host: localhost:7700", f"Host: 127.0.0.1:{port}".encode()
    )
    sock.sendall(head)
    response = b""
    while not response.endswith(b"\r\n\r\n"):
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    assert response.startswith(b"HTTP/1.1 101")
    return sock


def _masked_client_frame(opcode: int, payload: bytes) -> bytes:
    mask = b"\x11\x22\x33\x44"
    length = len(payload)
    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([0x80 | length])
    else:
        header += bytes([0x80 | 126]) + len(payload).to_bytes(2, "big")
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return header + mask + masked


def _read_close_code(sock) -> int | None:
    """Read frames until a close frame; return its code."""
    data = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except (ConnectionError, OSError):
            return None
        if not chunk:
            return None
        data += chunk
        while len(data) >= 2:
            length = data[1] & 0x7F
            offset = 2
            if length == 126:
                if len(data) < 4:
                    break
                length = int.from_bytes(data[2:4], "big")
                offset = 4
            if len(data) < offset + length:
                break
            opcode = data[0] & 0x0F
            payload = data[offset : offset + length]
            data = data[offset + length :]
            if opcode == 0x8:
                return int.from_bytes(payload[:2], "big") if payload else None
        if len(data) > 1 << 20:
            return None


def test_unmasked_frame_closes_with_1002():
    async def handler(ws):
        try:
            await ws.recv()
        except WebSocketClosed:
            pass

    async def client(port):
        def scenario():
            sock = _client_socket(port)
            try:
                # Send a client frame without the MASK bit.
                frame = bytes([0x80 | OP_TEXT, 5]) + b"hello"
                sock.sendall(frame)
                return _read_close_code(sock)
            finally:
                sock.close()

        return await asyncio.to_thread(scenario)

    assert _run_server_test(handler, client) == 1002


def test_invalid_utf8_text_closes_with_1007():
    async def handler(ws):
        try:
            await ws.recv()
        except WebSocketClosed:
            pass

    async def client(port):
        def scenario():
            sock = _client_socket(port)
            try:
                sock.sendall(_masked_client_frame(OP_TEXT, b"\xff\xfe\xfd"))
                return _read_close_code(sock)
            finally:
                sock.close()

        return await asyncio.to_thread(scenario)

    assert _run_server_test(handler, client) == 1007
