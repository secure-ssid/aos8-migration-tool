"""
Optional on-disk persistence for the destination Aruba Central API credentials,
so a field engineer running many migrations/tests doesn't re-type tenant creds
every launch. Strictly opt-in via the "Remember on this machine" toggle in
Step 1; unchecking it deletes the file.

Plaintext JSON at ~/.aos8-migration/credentials.json (dir 0700 / file 0600),
outside the repo so it can never be committed. Only the durable connection
fields are stored — NOT the short-lived Classic *access* token, which dies in
~2h; the refresh token + client id/secret re-mint it. Source-side AOS 8
credentials (MC password, PSKs, RADIUS secrets) are never persisted.
"""
import json
import os
from pathlib import Path

CRED_DIR = Path.home() / ".aos8-migration"
CRED_PATH = CRED_DIR / "credentials.json"

# session_state keys we persist — stable meaning regardless of dest_type
FIELDS = (
    "dest_type",             # which destination platform was selected
    "central_base",          # New Central regional API base
    "central_base_classic",  # Classic API gateway base
    "central_client_id",
    "central_secret",
    "classic_refresh_token",  # rotates; access token is intentionally NOT saved
)


def exists() -> bool:
    return CRED_PATH.is_file()


def load() -> dict:
    """Saved creds (known, non-empty fields only), or {} if absent/unreadable."""
    try:
        data = json.loads(CRED_PATH.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in FIELDS and v}


def save_from_session(session) -> None:
    """Write the persistable fields from a mapping (Streamlit session_state)."""
    data = {k: session.get(k) for k in FIELDS if session.get(k)}
    if not data:
        return
    CRED_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = CRED_PATH.with_name("credentials.json.tmp")
    # restrictive perms from creation, then atomic rename into place
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CRED_PATH)
    os.chmod(CRED_PATH, 0o600)


def clear() -> None:
    try:
        CRED_PATH.unlink()
    except FileNotFoundError:
        pass
