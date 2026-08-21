"""Shared HTTP primitives for the API clients.

Kept deliberately tiny: the four clients are intentionally separate (they
target four unrelated API surfaces), so only genuinely identical helpers
live here.
"""
from urllib.parse import urlsplit


def normalize_base(url: str) -> str:
    """Scheme + host[:port] only. Schemeless input defaults to https; an
    explicit http:// is PRESERVED (the test harness and local API mocks bind
    plain HTTP on loopback). Any path/query/fragment is stripped — operators
    paste '.../api/' and every f"{base}{path}" would 404."""
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"
