"""
Optional, per-user, encrypted persistence for the destination Aruba Central API
credentials, so a field engineer running many migrations doesn't re-type tenant
creds every launch. Strictly opt-in via the "Remember" toggle in Step 1.

Multi-user safety (this is the important part):
  - The file is keyed PER AUTHENTICATED USER — ``~/.aos8-migration/<user-slug>/
    credentials.json`` — so in a shared deployment one operator's saved creds
    can never auto-load into another operator's session. The slug is a SHA-256
    of the proxy-asserted identity; the raw email is never a path component.
  - Contents are ENCRYPTED AT REST with Fernet (AES-128-CBC + HMAC). In the
    multi-user farm the key comes from ``AOS8_CREDSTORE_KEY`` (a mounted
    secret); with no key set, persistence is DISABLED entirely (fail-safe — we
    never write plaintext secrets to a shared volume). On a single-user laptop
    a private key file (``~/.aos8-migration/key``, 0600) is auto-generated so
    "Remember" still works, encrypted.

Only durable connection fields are stored — NOT the short-lived Classic *access*
token (dies in ~2h; the refresh token + client id/secret re-mint it). Source-side
AOS 8 credentials (MC password, PSKs, RADIUS secrets) are never persisted.

Every public function takes the ``user`` (from lib.identity.current_user()); a
falsy user means "no identity" and all operations no-op.
"""
import binascii
import json
import os
from pathlib import Path

from .identity import is_multiuser, user_slug

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - cryptography is a hard dependency
    Fernet = None
    InvalidToken = Exception

CRED_ROOT = Path.home() / ".aos8-migration"
_KEY_ENV = "AOS8_CREDSTORE_KEY"
_LOCAL_KEY_FILE = CRED_ROOT / "key"
# Pre-multiuser single plaintext file — migrated then deleted on first local load.
_LEGACY_PLAINTEXT = CRED_ROOT / "credentials.json"

# session_state keys we persist — stable meaning regardless of dest_type
FIELDS = (
    "dest_type",             # which destination platform was selected
    "central_base",          # New Central regional API base
    "central_base_classic",  # Classic API gateway base
    "central_client_id",
    "central_secret",
    "classic_refresh_token",  # rotates; access token is intentionally NOT saved
    "classic_client_id",      # Classic-gateway OAuth client (hybrid refresh) —
    "classic_client_secret",  # distinct issuer from the GreenLake client above
    "hybrid_tenant",          # hybrid-ness is a tenant property — a remembered
                              # hybrid setup must stay armed across launches
)

# The actual credentials — at least one must be present before we write a file.
# Base URLs auto-populate from defaults, so saving on those alone would create a
# file that looks "saved" but holds no credential ("didn't save the APIs").
CREDENTIAL_FIELDS = ("central_client_id", "central_secret", "classic_refresh_token",
                     "classic_client_id", "classic_client_secret")


# One-time diagnostic latch: a malformed key otherwise disables persistence
# with no trace anywhere (the "Remember" toggle simply never renders).
_key_warned = False


def _env_fernet():
    global _key_warned
    key = os.environ.get(_KEY_ENV)
    if not (Fernet and key):
        return None
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError, binascii.Error) as e:
        if not _key_warned:
            _key_warned = True
            print(f"[credstore] {_KEY_ENV} is not a usable Fernet key ({e}) — "
                  "credential persistence is DISABLED. Generate one with: "
                  "python -c \"from cryptography.fernet import Fernet; "
                  "print(Fernet.generate_key().decode())\"", flush=True)
        return None


def _fernet(create: bool = False):
    """The cipher for the current mode, or None if persistence isn't possible.

    An explicit deployment key (AOS8_CREDSTORE_KEY) always wins. With no env
    key, multi-user mode refuses (returns None) so creds are never written
    without an operator-provisioned key. Single-user/local mode falls back to a
    private auto-generated key file so 'Remember' still works, encrypted."""
    if Fernet is None:
        return None
    env = _env_fernet()
    if env is not None:
        return env
    if os.environ.get(_KEY_ENV):
        return None  # key was set but invalid — do NOT silently fall back
    if is_multiuser():
        return None
    # local mode: a private per-machine key
    try:
        if _LOCAL_KEY_FILE.is_file():
            return Fernet(_LOCAL_KEY_FILE.read_bytes())
        if not create:
            return None
        CRED_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        k = Fernet.generate_key()
        fd = os.open(str(_LOCAL_KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(k)
        return Fernet(k)
    except OSError:
        return None


def available() -> bool:
    """True when credential persistence is possible right now (a usable
    encryption key exists or can be created). Gates the 'Remember' UI — in
    multi-user mode with no AOS8_CREDSTORE_KEY this is False and the toggle is
    hidden."""
    if Fernet is None:
        return False
    if os.environ.get(_KEY_ENV):
        return _env_fernet() is not None
    return not is_multiuser()


def key_invalid() -> bool:
    """True when AOS8_CREDSTORE_KEY is set but isn't a usable Fernet key.
    available() is False in that case, so the UI hides the 'Remember' toggle —
    this lets the view say why instead of leaving an unexplained gap."""
    return bool(os.environ.get(_KEY_ENV)) and _env_fernet() is None


def _path(user: str) -> Path:
    return CRED_ROOT / user_slug(user) / "credentials.json"


def exists(user) -> bool:
    return bool(user) and _path(user).is_file()


def load_status(user) -> tuple[dict, str]:
    """(saved fields, status) where status is 'ok' | 'absent' | 'undecryptable'.

    'undecryptable' — a file exists but this key can't read it, which in
    practice means AOS8_CREDSTORE_KEY changed. That MUST be distinguishable
    from 'absent': a caller that merges onto {} would silently overwrite every
    previously-saved field with whatever this render happens to hold."""
    if not user:
        return {}, "absent"
    f = _fernet(create=False)
    if f is None:
        # Upgrade path: a pre-multiuser plaintext file exists but no key yet —
        # mint a key (local mode only) so we can migrate it to encrypted-at-rest.
        if not is_multiuser() and _LEGACY_PLAINTEXT.is_file():
            f = _fernet(create=True)
        if f is None:
            return {}, "absent"
    _migrate_legacy(user, f)
    try:
        raw = _path(user).read_bytes()
    except (FileNotFoundError, OSError):
        return {}, "absent"
    try:
        data = json.loads(f.decrypt(raw))
    except InvalidToken:
        return {}, "undecryptable"
    except ValueError:
        # decrypted fine, contents are garbage — the key is right, so the file
        # is safe to rewrite; that is 'absent', not a key mismatch
        return {}, "absent"
    if not isinstance(data, dict):
        return {}, "absent"
    return {k: v for k, v in data.items() if k in FIELDS and v}, "ok"


def load(user) -> dict:
    """Saved creds for this user (known, non-empty fields only), or {} if
    absent/unreadable/persistence-disabled. Callers that must tell a missing
    file apart from a key mismatch use load_status()."""
    return load_status(user)[0]


def save_from_session(session, user) -> bool:
    """Write the persistable fields from a mapping (Streamlit session_state) for
    this user, encrypted.

    No-ops unless a real credential is present (base URLs alone don't count) and
    a cipher is available, and MERGES onto any existing file so a field that's
    momentarily blank this render never erases a previously-saved value.

    Returns False only when credentials the operator asked to keep were LOST —
    the volume rejected the write, or an existing file can't be decrypted (a
    changed AOS8_CREDSTORE_KEY) so merging would destroy it. A deliberate
    no-op (no identity, persistence disabled, nothing worth saving yet)
    returns True: there is nothing for the caller to warn about."""
    if not user:
        return True
    f = _fernet(create=True)
    if f is None:
        return True
    fresh = {k: session.get(k) for k in FIELDS if session.get(k)}
    # hybrid_tenant=False is meaningful (the operator disarmed the gate) — the
    # truthy filter above would otherwise leave a stale True merged in the file
    if "hybrid_tenant" in session:
        fresh["hybrid_tenant"] = bool(session.get("hybrid_tenant"))
    if not any(session.get(k) for k in CREDENTIAL_FIELDS):
        return True  # only defaults/URLs present — nothing worth saving yet
    # merge: keep already-saved fields not in this render
    data, status = load_status(user)
    if status == "undecryptable":
        return False  # refuse to overwrite creds we can't read back
    data.update(fresh)
    p = _path(user)
    tmp = p.with_name("credentials.json.tmp")
    try:
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        blob = f.encrypt(json.dumps(data).encode())
        # restrictive perms from creation, then atomic rename into place
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, p)
        os.chmod(p, 0o600)
    except OSError:
        # full or read-only volume — the caller tells the operator instead of
        # replacing Step 1 with a traceback
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def clear(user) -> None:
    if not user:
        return
    try:
        _path(user).unlink()
    except FileNotFoundError:
        pass


def _migrate_legacy(user, f) -> None:
    """One-time, local-mode only: import a pre-multiuser plaintext
    credentials.json into the encrypted per-user store, then delete it so no
    plaintext secret lingers. No-op in multi-user mode or once migrated."""
    if is_multiuser() or not _LEGACY_PLAINTEXT.is_file() or _path(user).is_file():
        return
    try:
        legacy = json.loads(_LEGACY_PLAINTEXT.read_text())
    except (ValueError, OSError):
        # transiently unreadable, or not JSON — leave it and retry next launch
        # (deleting now would lose data we never managed to read)
        return
    if isinstance(legacy, dict) and any(legacy.get(k) for k in CREDENTIAL_FIELDS):
        data = {k: v for k, v in legacy.items() if k in FIELDS and v}
        p = _path(user)
        try:
            p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            blob = f.encrypt(json.dumps(data).encode())
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
        except Exception:
            return  # encrypted write failed — KEEP the plaintext so nothing is lost
    # Encrypted copy is safely in place (or the file held no credentials worth
    # keeping) — only now remove the plaintext.
    try:
        _LEGACY_PLAINTEXT.unlink()
    except OSError:
        pass
