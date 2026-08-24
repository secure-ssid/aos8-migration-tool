"""Transport hardening (review finding 10): normalize_base must stop
accepting arbitrary hosts and explicit http:// as a flow-through. A
mistyped base URL must never ship a Central/GLP bearer token to an
unintended or cleartext endpoint."""
import pytest

from lib.http_base import normalize_base


# ── allowlisted HPE/Aruba hosts, HTTPS ───────────────────────────────────

def test_allowlisted_central_region_host_passes():
    assert normalize_base("https://us4.api.central.arubanetworks.com") == \
        "https://us4.api.central.arubanetworks.com"


def test_allowlisted_classic_gateway_host_passes():
    assert normalize_base("https://apigw-uswest4.central.arubanetworks.com") == \
        "https://apigw-uswest4.central.arubanetworks.com"


def test_allowlisted_greenlake_host_passes():
    assert normalize_base("https://global.api.greenlake.hpe.com") == \
        "https://global.api.greenlake.hpe.com"


def test_allowlisted_sso_host_passes():
    assert normalize_base("https://sso.common.cloud.hpe.com") == \
        "https://sso.common.cloud.hpe.com"


def test_schemeless_allowlisted_host_defaults_to_https():
    assert normalize_base("us4.api.central.arubanetworks.com") == \
        "https://us4.api.central.arubanetworks.com"


def test_path_query_fragment_stripped_after_allowlist():
    assert normalize_base("https://us4.api.central.arubanetworks.com/api/v1?x=1") == \
        "https://us4.api.central.arubanetworks.com"


# ── rejection: arbitrary hosts / cleartext http ──────────────────────────

def test_non_allowlisted_host_rejected():
    with pytest.raises(ValueError):
        normalize_base("https://evil.example.com")


def test_http_cleartext_rejected_outside_dev_mode():
    with pytest.raises(ValueError):
        normalize_base("http://us4.api.central.arubanetworks.com")


def test_http_cleartext_arbitrary_host_rejected():
    with pytest.raises(ValueError):
        normalize_base("http://central.arubanetworks.com.evil.example.com")


def test_lookalike_suffix_host_rejected():
    # "arubanetworks.com.evil.io" must not pass a naive suffix match
    with pytest.raises(ValueError):
        normalize_base("https://arubanetworks.com.evil.io")


def test_arbitrary_https_host_rejected_even_with_credentials():
    with pytest.raises(ValueError):
        normalize_base("https://attacker.example.com")


# ── loopback carve-out: test harness + local mocks bind plain HTTP ───────

def test_loopback_http_allowed_for_test_harness():
    assert normalize_base("http://127.0.0.1:8501") == "http://127.0.0.1:8501"


def test_localhost_http_allowed():
    assert normalize_base("http://localhost:8765") == "http://localhost:8765"


def test_schemeless_loopback_defaults_to_https():
    assert normalize_base("127.0.0.1:8000") == "https://127.0.0.1:8000"


# ── explicit dev/test mode opt-out ───────────────────────────────────────

def test_dev_mode_allows_cleartext_http(monkeypatch):
    monkeypatch.setenv("AOS8_DEV_MODE", "true")
    assert normalize_base("http://internal.lab:8080") == "http://internal.lab:8080"


def test_dev_mode_allows_non_allowlisted_host(monkeypatch):
    monkeypatch.setenv("AOS8_DEV_MODE", "true")
    assert normalize_base("https://arbitrary.example.com") == \
        "https://arbitrary.example.com"


# ── degenerate input stays "" (callers treat unset as disabled) ──────────

def test_empty_url_returns_empty():
    assert normalize_base("") == ""
    assert normalize_base("   ") == ""
