"""Account-store behavior: hashing, verification codes, lockout, throttling.
Uses a temp users file — never touches ~/.aos8-migration."""
from datetime import timedelta
import os

import pytest

from lib import accounts


@pytest.fixture(autouse=True)
def temp_users_file(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "USERS_FILE", tmp_path / "users.json")


def _register(email="user@example.com", password="longenough123"):
    ok, msg, code = accounts.register(email, password)
    assert ok, msg
    return code


def test_register_verify_signin_roundtrip():
    code = _register()
    ok, _ = accounts.verify_code("user@example.com", code)
    assert ok
    assert accounts.verify_password("user@example.com", "longenough123") == "ok"
    assert accounts.verify_password("user@example.com", "wrongpassword") == "bad"
    assert accounts.verify_password("nobody@example.com", "x") == "bad"


def test_short_password_rejected():
    ok, msg, _ = accounts.register("u@example.com", "short")
    assert not ok


def test_code_attempt_budget():
    _register()
    for _ in range(accounts.MAX_CODE_ATTEMPTS):
        ok, _ = accounts.verify_code("user@example.com", "000000")
        assert not ok
    ok, msg = accounts.verify_code("user@example.com", "000000")
    assert not ok and "Too many" in msg


def test_resend_is_throttled():
    _register()
    ok, msg, code = accounts.resend_code("user@example.com")
    # immediately after registration the min-interval applies
    assert not ok and "wait" in msg.lower()


def test_resend_allowed_after_interval(monkeypatch):
    _register()
    real_now = accounts._now()
    monkeypatch.setattr(
        accounts, "_now",
        lambda: real_now + timedelta(seconds=accounts.RESEND_MIN_INTERVAL_S + 5))
    ok, msg, code = accounts.resend_code("user@example.com")
    assert ok and code


def test_login_lockout_after_consecutive_failures(monkeypatch):
    code = _register()
    accounts.verify_code("user@example.com", code)
    for _ in range(accounts.MAX_LOGIN_FAILS):
        assert accounts.verify_password("user@example.com", "wrongpass!!") == "bad"
    # locked now — even the RIGHT password is refused during the window
    assert accounts.verify_password("user@example.com", "longenough123") == "locked"
    # after the window the right password unlocks and clears the state
    real_now = accounts._now()
    monkeypatch.setattr(
        accounts, "_now",
        lambda: real_now + timedelta(minutes=accounts.LOGIN_LOCK_MINUTES, seconds=5))
    assert accounts.verify_password("user@example.com", "longenough123") == "ok"
    assert accounts.verify_password("user@example.com", "wrongpass!!") == "bad"


# ── Read-failure must not clobber: a corrupt users.json is surfaced, never
# treated as an empty registry that a later write overwrites (review A1). ──

def test_register_raises_on_corrupt_store_and_does_not_overwrite(tmp_path,
                                                                 monkeypatch):
    store = tmp_path / "users.json"
    store.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(accounts, "USERS_FILE", store)
    with pytest.raises(accounts.AccountsStorageError):
        accounts.register("user@example.com", "longenough123")
    # the on-disk registry must be untouched — a fresh registration must
    # never overwrite an unreadable (possibly recoverable) account database
    assert store.read_text(encoding="utf-8") == "{not json"


def test_verify_password_raises_on_corrupt_store(tmp_path, monkeypatch):
    store = tmp_path / "users.json"
    store.write_text("[]", encoding="utf-8")   # wrong top-level type
    monkeypatch.setattr(accounts, "USERS_FILE", store)
    with pytest.raises(accounts.AccountsStorageError):
        accounts.verify_password("user@example.com", "longenough123")


def test_register_raises_on_unreadable_store(tmp_path, monkeypatch):
    store = tmp_path / "users.json"
    store.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(accounts, "USERS_FILE", store)
    monkeypatch.setattr(accounts, "_load",
                        lambda: (_ for _ in ()).throw(
                            accounts.AccountsStorageError("read failed")))
    with pytest.raises(accounts.AccountsStorageError):
        accounts.register("user@example.com", "longenough123")


# ── Write-failure must surface too: an unwritable registry raises
# AccountsStorageError, never a raw OSError at the UI boundary (review A2). ──

def test_register_raises_on_unwritable_store(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "USERS_FILE", tmp_path / "users.json")

    def _deny(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(os, "replace", _deny)
    with pytest.raises(accounts.AccountsStorageError):
        accounts.register("user@example.com", "longenough123")
