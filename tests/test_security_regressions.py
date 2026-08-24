"""Regression tests for the security/safety fixes.

Every test here pins behaviour that was previously wrong and is cheap to break
again — a fail-open auth gate, a silently accepted placeholder password, a
session that survived sign-out, a destructive cleanup with no scope, and SSIDs
that lost their protection in translation.
"""

import pytest

from lib import cleanup, compatibility, identity
from lib.models import (
    APGroup, AuthType, CentralConfig, CustomerConfig, ForwardMode, SSID,
)


# ── auth mode must fail closed ──────────────────────────────────────────────
@pytest.mark.parametrize("mode", ["local", "password", "accounts", "proxy"])
def test_known_modes_are_valid(mode, monkeypatch):
    monkeypatch.setenv("AOS8_AUTH_MODE", mode)
    # password mode additionally needs a usable password to be servable
    monkeypatch.setenv("AOS8_APP_PASSWORD", "a-long-enough-shared-passphrase")
    assert identity.auth_mode_error() is None


@pytest.mark.parametrize("mode", ["prox", "passwrod", "", "PASSWORD ", "none"])
def test_unknown_mode_is_refused(mode, monkeypatch):
    """A typo used to fall through every gate and serve the app unauthenticated."""
    monkeypatch.setenv("AOS8_AUTH_MODE", mode)
    if mode.strip().lower() in identity.VALID_MODES:
        pytest.skip("normalizes to a valid mode")
    err = identity.auth_mode_error()
    assert err and "not a valid mode" in err


def test_unknown_mode_yields_no_identity(monkeypatch):
    monkeypatch.setenv("AOS8_AUTH_MODE", "bogus")
    assert identity.current_user() is None


# ── shared password strength ────────────────────────────────────────────────
def test_placeholder_password_is_refused(monkeypatch):
    """`cp .env.example .env` must not yield a working, publicly known password."""
    monkeypatch.setenv("AOS8_AUTH_MODE", "password")
    monkeypatch.setenv("AOS8_APP_PASSWORD",
                       "change-me-to-a-strong-shared-password")
    assert identity.app_password_error() is not None
    assert identity.check_app_password(
        "change-me-to-a-strong-shared-password") is False


def test_short_password_is_refused(monkeypatch):
    monkeypatch.setenv("AOS8_AUTH_MODE", "password")
    monkeypatch.setenv("AOS8_APP_PASSWORD", "short")
    assert identity.app_password_error() is not None
    assert identity.check_app_password("short") is False


def test_unset_password_is_refused(monkeypatch):
    monkeypatch.setenv("AOS8_AUTH_MODE", "password")
    monkeypatch.delenv("AOS8_APP_PASSWORD", raising=False)
    assert identity.app_password_error() is not None
    assert identity.check_app_password("") is False


def test_strong_password_accepted_and_wrong_one_rejected(monkeypatch):
    monkeypatch.setenv("AOS8_AUTH_MODE", "password")
    monkeypatch.setenv("AOS8_APP_PASSWORD", "correct-horse-battery-staple")
    assert identity.app_password_error() is None
    assert identity.check_app_password("correct-horse-battery-staple") is True
    assert identity.check_app_password("wrong") is False


def test_non_ascii_password_does_not_crash(monkeypatch):
    """compare_digest on str raises TypeError for non-ASCII — must compare bytes."""
    monkeypatch.setenv("AOS8_AUTH_MODE", "password")
    monkeypatch.setenv("AOS8_APP_PASSWORD", "pässwort-that-is-long-enough")
    assert identity.check_app_password("pässwort-that-is-long-enough") is True
    assert identity.check_app_password("nope") is False


# ── cleanup must be scoped ──────────────────────────────────────────────────
@pytest.mark.parametrize("prefix", ["", "   ", None])
def test_cleanup_refuses_empty_prefix(prefix):
    """An empty prefix matched every object in the tenant."""
    with pytest.raises(ValueError):
        cleanup.cleanup(prefix)


def test_matches_never_matches_on_empty_prefix():
    assert cleanup._matches("production-wifi", "") is False
    assert cleanup._matches("zztest-lab", "zztest") is True
    assert cleanup._matches("production-wifi", "zztest") is False


# ── auth downgrades must block, not silently pass ───────────────────────────
def _customer(ssids):
    return CustomerConfig(
        mc_ip="10.0.0.1", mc_firmware="8.10.0.12", controller_vlan=1,
        ap_groups=[APGroup(name="campus", ssids=[s.name for s in ssids])],
        ssids=ssids,
    )


def _ssid(name, auth, **kw):
    return SSID(name=name, vlan=10, forward_mode=ForwardMode.BRIDGE,
                auth_type=auth, **kw)


def test_owe_maps_natively_and_does_not_block():
    """OWE is natively supported (opmode ENHANCED_OPEN / enhanced-open), so it
    must NOT be treated as a downgrade — blocking it here sent operators off to
    do pointless manual work, and mapping it to OPEN silently stripped
    encryption. Source: HPE 'Open SSID (OWE)' reference workflow."""
    res = compatibility._check_auth_downgrades(
        _customer([_ssid("guest", AuthType.OWE)]))
    assert res[0].status is compatibility.Status.PASS


def test_owe_preserves_encryption_in_both_destinations():
    from lib.central_client import OPMODE
    from lib.classic_central_client import OPMODE_CLASSIC
    assert OPMODE[AuthType.OWE] == "ENHANCED_OPEN"
    assert OPMODE_CLASSIC[AuthType.OWE] == "enhanced-open"
    # the whole point: OWE must not collapse onto the plain-open value
    assert OPMODE[AuthType.OWE] != OPMODE[AuthType.OPEN]
    assert OPMODE_CLASSIC[AuthType.OWE] != OPMODE_CLASSIC[AuthType.OPEN]


def test_mac_auth_ssid_blocks_migration():
    res = compatibility._check_auth_downgrades(
        _customer([_ssid("scanners", AuthType.MAC)]))
    assert res[0].status is compatibility.Status.FAIL
    assert "scanners" in res[0].detail


def test_normal_ssids_do_not_trigger_downgrade_blocker():
    res = compatibility._check_auth_downgrades(_customer([
        _ssid("corp", AuthType.WPA2_ENTERPRISE),
        _ssid("psk", AuthType.WPA2_PSK),
        _ssid("open-by-design", AuthType.OPEN),
    ]))
    assert res[0].status is compatibility.Status.PASS


def test_owe_is_not_the_same_enum_as_open():
    """OWE collapsing into OPEN is what made the downgrade invisible."""
    assert AuthType.OWE is not AuthType.OPEN


def test_owe_opmode_parses_as_owe():
    from lib.aos8_client import _opmode_to_auth
    for opmode in ("enhanced-open", "owe", "wpa3-owe"):
        auth, known = _opmode_to_auth(opmode)
        assert auth is AuthType.OWE, opmode
        assert known is True


def test_destination_opmode_maps_cover_every_auth_type():
    """A missing entry would be a KeyError mid-provisioning."""
    from lib.central_client import OPMODE
    from lib.classic_central_client import OPMODE_CLASSIC
    for auth in AuthType:
        assert auth in OPMODE, f"central OPMODE missing {auth}"
        assert auth in OPMODE_CLASSIC, f"classic OPMODE missing {auth}"


# ── captive portal must not land as a bare OPEN WLAN on Classic ─────────────
def test_classic_captive_portal_blocks():
    customer = _customer([_ssid("guest", AuthType.OPEN,
                                captive_portal_url="https://portal.example/login")])
    central = CentralConfig(customer_name="Acme", base_url="https://x",
                            destination="classic")
    res = compatibility._check_captive_portal(customer, central)
    assert res[0].status is compatibility.Status.FAIL


def test_new_central_captive_portal_warns_only():
    customer = _customer([_ssid("guest", AuthType.OPEN,
                                captive_portal_url="https://portal.example/login")])
    central = CentralConfig(customer_name="Acme", base_url="https://x",
                            destination="new")
    res = compatibility._check_captive_portal(customer, central)
    assert res[0].status is compatibility.Status.WARN


def test_no_captive_portal_produces_no_check():
    customer = _customer([_ssid("corp", AuthType.WPA2_ENTERPRISE)])
    central = CentralConfig(customer_name="Acme", base_url="https://x")
    assert compatibility._check_captive_portal(customer, central) == []


# ── AOS 8 TLS verification defaults to ON ───────────────────────────────────
# NOTE: no importlib.reload here. default_tls_verify() reads os.environ at call
# time, and reloading the module rebinds its classes — which silently breaks
# isinstance/pytest.raises checks in every other test module for the rest of
# the session.
def test_tls_verification_on_by_default(monkeypatch):
    monkeypatch.delenv("AOS8_CA_BUNDLE", raising=False)
    monkeypatch.delenv("AOS8_INSECURE_TLS", raising=False)
    monkeypatch.delenv("AOS8_CERT_FINGERPRINT", raising=False)
    import lib.aos8_client as ac
    assert ac.default_tls_verify() is True
    assert ac.AOS8Client("10.0.0.1", "admin", "pw").session.verify is True


def test_ca_bundle_is_used_when_set(monkeypatch):
    monkeypatch.setenv("AOS8_CA_BUNDLE", "/etc/ssl/controller-ca.pem")
    import lib.aos8_client as ac
    assert ac.default_tls_verify() == "/etc/ssl/controller-ca.pem"


def test_insecure_tls_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("AOS8_CA_BUNDLE", raising=False)
    monkeypatch.setenv("AOS8_INSECURE_TLS", "true")
    import lib.aos8_client as ac
    assert ac.default_tls_verify() is False


def test_caller_can_override_verification_per_connection(monkeypatch):
    monkeypatch.delenv("AOS8_CA_BUNDLE", raising=False)
    monkeypatch.delenv("AOS8_INSECURE_TLS", raising=False)
    import lib.aos8_client as ac
    assert ac.AOS8Client("10.0.0.1", "a", "b", verify=False).session.verify is False


# ── verification codes must not be logged by default ────────────────────────
def test_console_codes_off_by_default(monkeypatch):
    monkeypatch.delenv("AOS8_ALLOW_CONSOLE_CODES", raising=False)
    from lib import auth_ui
    assert auth_ui.console_codes_allowed() is False


def test_console_codes_require_explicit_opt_in(monkeypatch):
    from lib import auth_ui
    monkeypatch.setenv("AOS8_ALLOW_CONSOLE_CODES", "true")
    assert auth_ui.console_codes_allowed() is True
    monkeypatch.setenv("AOS8_ALLOW_CONSOLE_CODES", "false")
    assert auth_ui.console_codes_allowed() is False


def test_failed_delivery_does_not_print_the_code(monkeypatch, capsys):
    """The code stays valid after a send failure — it must never hit the log."""
    from lib import auth_ui
    monkeypatch.delenv("AOS8_ALLOW_CONSOLE_CODES", raising=False)
    monkeypatch.setattr(auth_ui.mailer, "send",
                        lambda *a, **k: (False, "smtp down"))
    auth_ui._deliver_code("someone@example.com", "424242")
    out = capsys.readouterr().out
    assert "424242" not in out
    assert "FAILED" in out


def test_dev_opt_in_does_print_the_code(monkeypatch, capsys):
    from lib import auth_ui
    monkeypatch.setenv("AOS8_ALLOW_CONSOLE_CODES", "true")
    monkeypatch.setattr(auth_ui.mailer, "send",
                        lambda *a, **k: (False, "smtp down"))
    auth_ui._deliver_code("someone@example.com", "424242")
    assert "424242" in capsys.readouterr().out


# ── sign-out must not leave tenant secrets behind ───────────────────────────
def test_logout_clears_the_entire_session(monkeypatch):
    """Popping only the auth flags left API secrets readable by the next user."""
    from lib import auth_ui

    session = {
        "_authenticated": True,
        "_auth_user": "a@example.com",
        "central_secret": "super-secret",
        "classic_access_token": "tok",
        "customer_config": object(),
        "provision_results": [("x", True, "")],
    }
    monkeypatch.setattr(auth_ui.st, "session_state", session, raising=False)
    monkeypatch.setattr(auth_ui.st, "rerun", lambda *a, **k: None)

    auth_ui.logout()

    assert session == {}
