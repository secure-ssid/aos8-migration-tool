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

# Every mode the app knows how to gate. Anything else is a CONFIGURATION ERROR
# and must fail closed — a typo like 'passwrod' previously fell through every
# gate and served the console unauthenticated.
VALID_MODES = ("local", "password", "accounts", "proxy")

# Minimum length for the shared gate password. It protects a tool that can
# rewrite live customer tenants, so it gets a passphrase, not a word.
MIN_APP_PASSWORD_LEN = 16

# Placeholders shipped in .env.example / docs. Treated as "not configured" so a
# `cp .env.example .env` deploy can never serve a publicly known password.
_PLACEHOLDER_PASSWORDS = frozenset({
    "change-me-to-a-strong-shared-password",
    "change-me",
    "changeme",
    "password",
})


def auth_mode() -> str:
    """'local' (default), 'password' (shared gate), 'accounts' (login), 'proxy'.

    Returned verbatim (lowercased) even when invalid — callers use
    ``auth_mode_error()`` to fail closed, so the bad value stays visible in the
    operator-facing error instead of being silently coerced to a default."""
    return os.environ.get("AOS8_AUTH_MODE", "local").strip().lower()


def auth_mode_error() -> str | None:
    """Why the deployment cannot be served, or None when it's usable.

    Fail-closed configuration gate covering BOTH the mode name and, for
    ``password`` mode, the strength of the configured shared password."""
    mode = auth_mode()
    if mode not in VALID_MODES:
        return (f"AOS8_AUTH_MODE is set to '{mode}', which is not a valid mode. "
                f"Use one of: {', '.join(VALID_MODES)}.")
    if mode == "password":
        return app_password_error()
    return None


def app_password_error() -> str | None:
    """Why the shared password is unusable, or None when it's acceptable."""
    expected = os.environ.get("AOS8_APP_PASSWORD", "")
    if not expected:
        return ("AOS8_AUTH_MODE=password requires AOS8_APP_PASSWORD to be set. "
                "Nobody can sign in until it is.")
    if expected.strip().lower() in _PLACEHOLDER_PASSWORDS:
        return ("AOS8_APP_PASSWORD is still the example placeholder. Set a real "
                "shared passphrase before exposing this console.")
    if len(expected) < MIN_APP_PASSWORD_LEN:
        return (f"AOS8_APP_PASSWORD is shorter than {MIN_APP_PASSWORD_LEN} "
                f"characters. Use a longer shared passphrase.")
    return None


def requires_login() -> bool:
    """Modes where the app renders its own in-app login gate."""
    return auth_mode() in ("password", "accounts")


def check_app_password(password: str) -> bool:
    """Constant-time check against the single shared password
    (``AOS8_APP_PASSWORD``). Fail-closed: with no password configured — or a
    placeholder/too-weak one — nobody gets in."""
    if app_password_error():
        return False
    expected = os.environ.get("AOS8_APP_PASSWORD", "")
    # compare_digest on str raises TypeError for non-ASCII input, which would
    # surface as a crash on a pasted unicode password — compare bytes instead.
    return hmac.compare_digest((password or "").encode("utf-8"),
                               expected.encode("utf-8"))


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
    if mode == "proxy":
        return _header_identity()
    # Unknown mode — no identity. app.py refuses to serve these deployments;
    # returning None here keeps every other caller fail-closed too.
    return None


def user_slug(user: str) -> str:
    """A stable, filesystem-safe key for a user — the raw email is never used
    as a path or written to disk in the clear."""
    return hashlib.sha256(user.encode("utf-8")).hexdigest()[:32]
