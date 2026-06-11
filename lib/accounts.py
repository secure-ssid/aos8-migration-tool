"""
Self-service local user accounts for the multi-user deployment — registration
restricted to a verified company email domain (default @hpe.com), no OAuth/IdP.

Flow: register (domain-gated) -> a 6-digit code is emailed to prove the address
is really theirs -> enter the code -> the account is activated -> sign in.

Storage: a single JSON file (AOS8_USERS_FILE, default
~/.aos8-migration/users.json, perms 0600) holding, per email: a scrypt password
hash + salt, a verified flag, and (while pending) a short-lived hashed
verification code. Passwords and codes are never stored in the clear. On the
Docker farm this file MUST live on a persistent volume or accounts vanish on
restart.

No new dependencies — scrypt/sha256/secrets are all stdlib.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- policy knobs -----------------------------------------------------------
ALLOWED_DOMAIN = os.environ.get("AOS8_ALLOWED_EMAIL_DOMAIN", "hpe.com").strip().lower()
MIN_PASSWORD_LEN = 10
CODE_TTL_MINUTES = 15
MAX_CODE_ATTEMPTS = 5

_SCRYPT = dict(n=2 ** 14, r=8, p=1)   # ~16 MB work factor; safe under default maxmem
_KEYLEN = 64

USERS_FILE = Path(os.environ.get(
    "AOS8_USERS_FILE", str(Path.home() / ".aos8-migration" / "users.json")))

_EMAIL_RE = re.compile(rf"^[A-Za-z0-9._%+\-]+@{re.escape(ALLOWED_DOMAIN)}$", re.I)


def allowed_email(email: str) -> bool:
    return bool(_EMAIL_RE.match((email or "").strip()))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def _load() -> dict:
    try:
        data = json.loads(USERS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save(users: dict) -> None:
    USERS_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = USERS_FILE.with_name(USERS_FILE.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(users, f)
    os.replace(tmp, USERS_FILE)
    os.chmod(USERS_FILE, 0o600)


def _pw_hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, dklen=_KEYLEN, **_SCRYPT).hex()


def _code_hash(code: str, salt: bytes) -> str:
    return hashlib.sha256(salt + code.encode()).hexdigest()


def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def exists(email: str) -> bool:
    return _norm(email) in _load()


def is_verified(email: str) -> bool:
    rec = _load().get(_norm(email))
    return bool(rec and rec.get("verified"))


def register(email: str, password: str):
    """Create (or re-arm) a PENDING account and return (ok, message, code).

    `code` is the plaintext verification code to email; it is None on failure.
    An existing *verified* account cannot be re-registered."""
    email = _norm(email)
    if not allowed_email(email):
        return False, f"Use your @{ALLOWED_DOMAIN} email address.", None
    if len(password or "") < MIN_PASSWORD_LEN:
        return False, f"Password must be at least {MIN_PASSWORD_LEN} characters.", None
    users = _load()
    rec = users.get(email)
    if rec and rec.get("verified"):
        return False, "An account with that email already exists — sign in instead.", None
    salt = os.urandom(16)
    code = _new_code()
    users[email] = {
        "salt": salt.hex(),
        "hash": _pw_hash(password, salt),
        "created": _now().isoformat(),
        "verified": False,
        "code": {
            "hash": _code_hash(code, salt),
            "expires": (_now() + timedelta(minutes=CODE_TTL_MINUTES)).isoformat(),
            "attempts": 0,
        },
    }
    _save(users)
    return True, "Account created — check your email for a verification code.", code


def resend_code(email: str):
    """Issue a fresh code for a pending account. Returns (ok, message, code)."""
    email = _norm(email)
    users = _load()
    rec = users.get(email)
    if not rec:
        return False, "No pending registration for that email.", None
    if rec.get("verified"):
        return False, "That account is already verified — just sign in.", None
    salt = bytes.fromhex(rec["salt"])
    code = _new_code()
    rec["code"] = {
        "hash": _code_hash(code, salt),
        "expires": (_now() + timedelta(minutes=CODE_TTL_MINUTES)).isoformat(),
        "attempts": 0,
    }
    _save(users)
    return True, "A new code is on its way.", code


def verify_code(email: str, code: str):
    """Activate a pending account if the code matches. Returns (ok, message)."""
    email = _norm(email)
    users = _load()
    rec = users.get(email)
    if not rec or not rec.get("code"):
        return False, "Nothing to verify — register first."
    c = rec["code"]
    try:
        expired = _now() > datetime.fromisoformat(c["expires"])
    except (ValueError, KeyError):
        expired = True
    if expired:
        return False, "That code expired. Request a new one."
    if c.get("attempts", 0) >= MAX_CODE_ATTEMPTS:
        return False, "Too many attempts. Request a new code."
    salt = bytes.fromhex(rec["salt"])
    if hmac.compare_digest(_code_hash((code or "").strip(), salt), c["hash"]):
        rec["verified"] = True
        rec.pop("code", None)
        _save(users)
        return True, "Email verified — you're signed in."
    c["attempts"] = c.get("attempts", 0) + 1
    _save(users)
    return False, "Incorrect code."


def verify_password(email: str, password: str) -> str:
    """Return 'ok', 'unverified', or 'bad'. Constant-time; dummy-hashes unknown
    emails so response timing doesn't reveal whether an account exists."""
    email = _norm(email)
    rec = _load().get(email)
    if not rec:
        _pw_hash(password or "", b"0" * 16)   # equalize timing
        return "bad"
    salt = bytes.fromhex(rec["salt"])
    if not hmac.compare_digest(_pw_hash(password or "", salt), rec["hash"]):
        return "bad"
    return "ok" if rec.get("verified") else "unverified"
