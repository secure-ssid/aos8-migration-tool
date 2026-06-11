"""
Structured audit logging for sensitive actions.

This tool creates and DELETES objects in live customer tenants, so a shared
deployment needs a record of who did what. Each event is emitted as a single
JSON line to stdout, where the Docker farm's log pipeline collects it, tied to
the authenticated operator (lib.identity). Best-effort: auditing never raises
into the UI.
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


def record(action: str, user: str | None = None, **fields) -> None:
    """Emit one audit event as a JSON line.

    `action` is a short verb, e.g. 'provision', 'cutover', 'cleanup', 'claim'.
    Extra keyword fields (tenant base, target, counts, ok/failed) are merged in;
    None values are dropped."""
    try:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "audit": action,
            "user": user or "unknown",
        }
        event.update({k: v for k, v in fields.items() if v is not None})
        _logger.info(json.dumps(event, default=str))
    except Exception:
        pass  # auditing must never break the migration workflow
