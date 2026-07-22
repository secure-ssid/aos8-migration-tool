"""
Self-service local user accounts for the multi-user deployment — registration
with verified email, no OAuth/IdP. Optionally gate to a specific domain via
AOS8_ALLOWED_EMAIL_DOMAIN; unset (the default) allows any valid email address.

Flow: register (optionally domain-gated) -> a 6-digit code is emailed to prove
the address is really theirs -> enter the code -> the account is activated -> sign in.

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
# Set AOS8_ALLOWED_EMAIL_DOMAIN to restrict registration to one domain (e.g.
# "example.com"). Leave unset to allow any valid email address.
ALLOWED_DOMAIN = os.environ.get("AOS8_ALLOWED_EMAIL_DOMAIN", "").strip().lower()
MIN_PASSWORD_LEN = 10
CODE_TTL_MINUTES = 15
MAX_CODE_ATTEMPTS = 5
# Login throttling: after MAX_LOGIN_FAILS consecutive bad passwords the
# account locks for LOGIN_LOCK_MINUTES (persisted in users.json, so a page
# refresh / new session doesn't reset it). A successful login clears it.
MAX_LOGIN_FAILS = 5
LOGIN_LOCK_MINUTES = 5
# A fresh verification code resets the per-code attempt budget, so unthrottled
# resends would defeat MAX_CODE_ATTEMPTS — enforce a minimum interval.
RESEND_MIN_INTERVAL_S = 60

_SCRYPT = dict(n=2 ** 14, r=8, p=1)   # ~16 MB work factor; safe under default maxmem
_KEYLEN = 64

USERS_FILE = Path(os.environ.get(
    "AOS8_USERS_FILE", str(Path.home() / ".aos8-migration" / "users.json")))

# Generic valid-email pattern; domain suffix appended only when a domain is set.
_ANY_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$", re.I)
_DOMAIN_EMAIL_RE = (
    re.compile(rf"^[A-Za-z0-9._%+\-]+@{re.escape(ALLOWED_DOMAIN)}$", re.I)
    if ALLOWED_DOMAIN else None
)


def allowed_email(email: str) -> bool:
    e = (email or "").strip()
    if _DOMAIN_EMAIL_RE:
        return bool(_DOMAIN_EMAIL_RE.match(e))
    return bool(_ANY_EMAIL_RE.match(e))


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
        domain_hint = f"@{ALLOWED_DOMAIN} " if ALLOWED_DOMAIN else ""
        return False, f"Enter a valid {domain_hint}email address.", None
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
            "issued": _now().isoformat(),
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
    # throttle: each new code resets the attempt budget, so unlimited resends
    # would turn 5 attempts/code into unlimited guesses
    prev = rec.get("code") or {}
    try:
        issued = datetime.fromisoformat(prev["issued"])
        wait = RESEND_MIN_INTERVAL_S - (_now() - issued).total_seconds()
    except (KeyError, ValueError):
        wait = 0
    if wait > 0:
        return False, f"A code was just sent — wait {int(wait) + 1}s before requesting another.", None
    salt = bytes.fromhex(rec["salt"])
    code = _new_code()
    rec["code"] = {
        "hash": _code_hash(code, salt),
        "expires": (_now() + timedelta(minutes=CODE_TTL_MINUTES)).isoformat(),
        "issued": _now().isoformat(),
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
    """Return 'ok', 'unverified', 'bad', or 'locked'. Constant-time;
    dummy-hashes unknown emails so response timing doesn't reveal whether an
    account exists. Consecutive failures lock the account for
    LOGIN_LOCK_MINUTES (persisted, so a fresh session doesn't reset it)."""
    email = _norm(email)
    users = _load()
    rec = users.get(email)
    if not rec:
        _pw_hash(password or "", b"0" * 16)   # equalize timing
        return "bad"
    try:
        locked = _now() < datetime.fromisoformat(rec["lock_until"])
    except (KeyError, ValueError):
        locked = False
    if locked:
        _pw_hash(password or "", b"0" * 16)   # equalize timing while locked
        return "locked"
    salt = bytes.fromhex(rec["salt"])
    if not hmac.compare_digest(_pw_hash(password or "", salt), rec["hash"]):
        rec["fails"] = rec.get("fails", 0) + 1
        if rec["fails"] >= MAX_LOGIN_FAILS:
            rec["fails"] = 0
            rec["lock_until"] = (
                _now() + timedelta(minutes=LOGIN_LOCK_MINUTES)).isoformat()
        _save(users)
        return "bad"
    if rec.get("fails") or rec.get("lock_until"):
        rec.pop("fails", None)
        rec.pop("lock_until", None)
        _save(users)
    return "ok" if rec.get("verified") else "unverified"
