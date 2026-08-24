"""
Structured audit logging for sensitive actions.

This tool creates and DELETES objects in live customer tenants, so a shared
deployment needs a record of who did what. Each event is emitted as a single
JSON line to stdout, where the Docker farm's log pipeline collects it, tied to
the authenticated operator (lib.identity). A write failure RAISES
AuditWriteError: compliance evidence is not guaranteed unless the caller
surfaces the failure — a sensitive action must not proceed silently
un-audited (review: 'audit logging silently ignores all logging failures').
"""
import json
import logging
import sys
from datetime import datetime, timezone

_logger = logging.getLogger("aos8.audit")
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


class AuditWriteError(Exception):
    """The audit event could not be written. Callers must surface this — a
    sensitive action without a durable audit record is a compliance miss."""


def record(action: str, user: str | None = None, **fields) -> None:
    """Emit one audit event as a JSON line.

    `action` is a short verb, e.g. 'provision', 'cutover', 'cleanup', 'claim'.
    Extra keyword fields (tenant base, target, counts, ok/failed) are merged in;
    None values are dropped. Raises AuditWriteError when the event cannot be
    written."""
    try:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "audit": action,
            "user": user or "unknown",
        }
        event.update({k: v for k, v in fields.items() if v is not None})
        _logger.info(json.dumps(event, default=str))
    except Exception as e:
        raise AuditWriteError(f"audit record '{action}' failed: {e}") from e