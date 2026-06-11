"""
Operator identity for multi-user deployments — the single seam every other
module reads "who is this user" from, so the identity SOURCE is swappable.

Modes (env ``AOS8_AUTH_MODE``):
  - ``local``    (default) — single-user / laptop. Identity is a fixed local
    principal; no login. The original behaviour.
  - ``password`` — one SHARED gate password (``AOS8_APP_PASSWORD``). Simplest
    multi-user option: no registration, no email. There is no per-person
    identity, so saved creds are a single shared store (and only if a key is
    set) and the audit log is attributed to a generic team principal.
  - ``accounts`` — the app's OWN self-service login (no OAuth/IdP). Users
    register with a verified email (optionally restricted to one domain via
    ``AOS8_ALLOWED_EMAIL_DOMAIN``); the signed-in email is the identity, read
    from ``st.session_state['_auth_user']``. Per-person isolation, needs email.
  - ``proxy``    — behind a header-injecting reverse proxy; identity comes from
    ONE trusted header (``AOS8_IDENTITY_HEADER``). The proxy must SET and
    INBOUND-STRIP that header and be the sole ingress.

``password``/``accounts`` render an in-app login gate. ``accounts``/``proxy``
have per-person identities (credstore goes per-user). ``password`` has a single
shared identity (one shared store).
"""
import hashlib
import hmac
import os

import streamlit as st

# The single header we trust as the verified identity. Default is the header
# oauth2-proxy injects and inbound-strips in --upstream mode. Override only if
# your proxy sets a different sanitized header.
_DEFAULT_IDENTITY_HEADER = "X-Forwarded-Email"

LOCAL_USER = "local@localhost"


def identity_header() -> str:
    return os.environ.get("AOS8_IDENTITY_HEADER", _DEFAULT_IDENTITY_HEADER).strip()


SHARED_USER = "team"


def auth_mode() -> str:
    """'local' (default), 'password' (shared gate), 'accounts' (login), 'proxy'."""
    return os.environ.get("AOS8_AUTH_MODE", "local").strip().lower()


def requires_login() -> bool:
    """Modes where the app renders its own in-app login gate."""
    return auth_mode() in ("password", "accounts")


def check_app_password(password: str) -> bool:
    """Constant-time check against the single shared password
    (``AOS8_APP_PASSWORD``). Fail-closed: with no password configured, nobody
    gets in."""
    expected = os.environ.get("AOS8_APP_PASSWORD", "")
    if not expected:
        return False
    return hmac.compare_digest(password or "", expected)


def is_multiuser() -> bool:
    """True for any shared (non-local) deployment. Gates the credstore
    fail-safe + per-user behaviour identically for 'accounts' and 'proxy'."""
    return auth_mode() != "local"


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
    """The authenticated operator's identity, or None if not signed in.

    local mode: a fixed local principal. accounts mode: the email the in-app
    login established (session_state['_auth_user']). proxy mode: the trusted
    proxy header. A None return in a multi-user mode means the caller must
    refuse to proceed / show the login gate."""
    mode = auth_mode()
    if mode == "local":
        return os.environ.get("AOS8_LOCAL_USER", LOCAL_USER).strip().lower()
    if mode == "password":
        return SHARED_USER if st.session_state.get("_authenticated") else None
    if mode == "accounts":
        return st.session_state.get("_auth_user")
    return _header_identity()  # proxy


def user_slug(user: str) -> str:
    """A stable, filesystem-safe key for a user — the raw email is never used
    as a path or written to disk in the clear."""
    return hashlib.sha256(user.encode("utf-8")).hexdigest()[:32]
