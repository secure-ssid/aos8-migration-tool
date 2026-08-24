"""Shared HTTP primitives for the API clients.

Kept deliberately tiny: the four clients are intentionally separate (they
target four unrelated API surfaces), so only genuinely identical helpers
live here.
"""
import os
from urllib.parse import urlsplit

# Hosts that may receive Central/GreenLake credentials (review finding 10):
# a mistyped base URL must never ship a bearer token to an unintended or
# cleartext endpoint. Suffixes are matched on the registrable domain.
ALLOWED_HOST_SUFFIXES = (
    "arubanetworks.com",    # New Central regional APIs + Classic API gateway
    "cloud.hpe.com",        # GreenLake SSO token endpoint
    "greenlake.hpe.com",    # GreenLake global API
)

# The test harness and local API mocks bind plain HTTP on loopback, so
# loopback hosts are always allowed regardless of scheme.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

_DEV_MODE_VALUES = {"1", "true", "yes", "on"}


def dev_mode() -> bool:
    """Explicit dev/test opt-out.

    AOS8_DEV_MODE=true permits cleartext http:// and non-allowlisted hosts
    (local API mocks, lab-internal endpoints). Production operators must
    NOT set it: without it, http:// and unknown hosts are refused outright.
    """
    return os.environ.get("AOS8_DEV_MODE", "").strip().lower() in _DEV_MODE_VALUES


def _host_allowed(host: str) -> bool:
    h = (host or "").lower()
    if h in _LOOPBACK_HOSTS:
        return True
    return any(h == sfx or h.endswith("." + sfx) for sfx in ALLOWED_HOST_SUFFIXES)


def normalize_base(url: str) -> str:
    """Scheme + host[:port] only, gated by a host/scheme allowlist.

- Schemeless input defaults to https.
- Explicit http:// is refused UNLESS the host is loopback (test harness /
  local API mocks) or AOS8_DEV_MODE is set. A bearer token must never ride
  cleartext to a real endpoint.
- A host outside the HPE/Aruba allowlist is refused unless AOS8_DEV_MODE.
- Any path/query/fragment is stripped — operators paste '.../api/' and
  every f"{base}{path}" would 404.
- '' / blank input stays '' (callers treat it as 'unset/disabled').

Raises ValueError when the URL is not one we may send credentials to."""
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if not parts.netloc:
        raise ValueError(f"base URL '{url}' has no host")
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme '{parts.scheme}' in base URL")
    if scheme == "http" and host not in _LOOPBACK_HOSTS and not dev_mode():
        raise ValueError(
            f"refusing cleartext http:// base URL '{url}' — HTTPS is required "
            "(set AOS8_DEV_MODE only for a local dev/test endpoint)")
    if not _host_allowed(host) and not dev_mode():
        raise ValueError(
            f"refusing base URL '{url}': '{host}' is not an allowed "
            "HPE/Aruba Central or GreenLake host (set AOS8_DEV_MODE only for "
            "a local dev/test endpoint)")
    return f"{scheme}://{parts.netloc}"