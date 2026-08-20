"""Tests for utility helpers."""

import logging
from io import StringIO

import agent_circus.utils
from agent_circus.utils import setup_logging


def test_setup_logging_writes_stream_handler_to_stderr(monkeypatch) -> None:
    """Logging must not pollute stdout, which may carry agent protocols."""
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    stderr = StringIO()
    stdout = StringIO()
    monkeypatch.setattr(agent_circus.utils.sys, "stderr", stderr)
    monkeypatch.setattr(agent_circus.utils.sys, "stdout", stdout)
    try:
        root.handlers[:] = []
        setup_logging(level="DEBUG")

        stream_handlers = [
            handler
            for handler in root.handlers
            if isinstance(handler, logging.StreamHandler)
        ]
        streams = {
            handler.stream
            for handler in stream_handlers
            if getattr(handler, "stream", None) is not None
        }
        assert stderr in streams
        assert stdout not in streams
    finally:
        root.handlers[:] = old_handlers
        root.setLevel(old_level)
