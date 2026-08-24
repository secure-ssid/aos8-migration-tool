"""Audit durability: record() must surface a write failure — compliance
evidence is not guaranteed when failures are silently swallowed."""
import json
import logging

import pytest

from lib import audit
from lib.audit import AuditWriteError


class _RaisingHandler:
    """A logging handler whose emit() always fails, simulating a broken
    log pipeline (closed stdout, full disk, permission error)."""

    def emit(self, record):
        raise OSError("log pipeline down")


def test_record_raises_when_logger_fails(monkeypatch):
    monkeypatch.setattr(audit._logger, "handlers", [_RaisingHandler()])
    with pytest.raises(AuditWriteError):
        audit.record("provision", user="op@hpe.com", tenant="x", ok=True)


def test_record_emits_one_json_line():
    lines = []

    class Collect(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(audit._logger, "handlers", [Collect()])
    try:
        audit.record("cutover", user="op@hpe.com", tenant="t1", ok=True)
    finally:
        monkeypatch.undo()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["audit"] == "cutover"
    assert event["user"] == "op@hpe.com"
    assert event["tenant"] == "t1"
    assert event["ok"] is True
    assert event["ts"]
