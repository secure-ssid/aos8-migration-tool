"""
Operator identity for multi-user deployments.

In a shared deployment the tool sits BEHIND an authenticating reverse proxy
(oauth2-proxy / an SSO gateway). The proxy verifies the user against your IdP
and injects the verified identity as ONE request header, which we read via
``st.context.headers``.

We trust exactly one header name (``AOS8_IDENTITY_HEADER``, default
``X-Forwarded-Email`` — the header oauth2-proxy injects AND strips from inbound
client requests in ``--upstream`` mode via ``--pass-user-headers``). We do NOT
fall through a list of candidate headers: ``X-Auth-Request-*`` are auth_request
*response* headers that oauth2-proxy does not set or sanitize on the upstream
request, so trusting them would be client-spoofable.

Two load-bearing requirements for this to be safe (see docker-compose.yml):
  1. The proxy must SET and INBOUND-STRIP the trusted header, and
  2. the app must ONLY be reachable through the proxy (put it on an internal
     Docker network / NetworkPolicy so no other container can hit :8501
     directly with a forged header).

Modes (env ``AOS8_AUTH_MODE``):
  - ``local``  (default) — single-user / laptop. Identity is a fixed local
    user; no proxy, no header required. This is the original behaviour.
  - ``proxy``  — multi-user farm. A proxy identity header is REQUIRED; with no
    header the session is unauthenticated and the app refuses to run
    (enforced in app.py). This is what enables per-user credential isolation.

Set ``AOS8_AUTH_MODE=proxy`` in the Docker-farm deployment.
"""
import hashlib
import os

import streamlit as st

# The single header we trust as the verified identity. Default is the header
# oauth2-proxy injects and inbound-strips in --upstream mode. Override only if
# your proxy sets a different sanitized header.
_DEFAULT_IDENTITY_HEADER = "X-Forwarded-Email"

LOCAL_USER = "local@localhost"


def identity_header() -> str:
    return os.environ.get("AOS8_IDENTITY_HEADER", _DEFAULT_IDENTITY_HEADER).strip()


def auth_mode() -> str:
    """'local' (single-user, default) or 'proxy' (multi-user behind SSO)."""
    return os.environ.get("AOS8_AUTH_MODE", "local").strip().lower()


def is_multiuser() -> bool:
    return auth_mode() == "proxy"


def _header_identity() -> str | None:
    """The verified identity from the single trusted proxy header, or None."""
    try:
        headers = st.context.headers  # Streamlit >= 1.37
    except Exception:
        return None
    if not headers:
        return None
    val = headers.get(identity_header())
    if val and val.strip():
        return val.strip().lower()
    return None


def current_user() -> str | None:
    """The authenticated operator's identity.

    Returns a fixed local principal in local mode. In proxy mode returns the
    proxy-asserted identity, or None when no identity header is present (the
    caller must then refuse to proceed)."""
    if not is_multiuser():
        return os.environ.get("AOS8_LOCAL_USER", LOCAL_USER).strip().lower()
    return _header_identity()


def user_slug(user: str) -> str:
    """A stable, filesystem-safe key for a user — the raw email is never used
    as a path or written to disk in the clear."""
    return hashlib.sha256(user.encode("utf-8")).hexdigest()[:32]
