"""Stream A / review finding #8: blank site data must not silently become real
placeholder sites in a production tenant ("1 Lab Street, San Jose" in New
Central; latitude/longitude 0.0,0.0 in Classic). Production mode requires
complete site data; placeholders exist only behind an explicit lab-mode
switch (CentralConfig.lab_mode)."""
import pytest

from lib.central_client import CentralAPIError, CentralClient
from lib.classic_central_client import (ClassicCentralAPIError,
                                        ClassicCentralClient)
from lib.models import CentralConfig, site_data_error


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch):
    # Offline client tests use placeholder hosts — the transport layer
    # refuses non-allowlisted/cleartext base URLs unless the harness opts
    # out via AOS8_DEV_MODE, exactly like a local lab (Stream C contract).
    monkeypatch.setenv("AOS8_DEV_MODE", "true")


def _cfg(**kw) -> CentralConfig:
    base = dict(customer_name="acme", base_url="http://x", sites=["hq"])
    base.update(kw)
    return CentralConfig(**base)


# ─────────────────── the model-level validator ───────────────────

def test_lab_mode_defaults_off():
    assert _cfg().lab_mode is False


def test_production_requires_address_city_country():
    err = site_data_error(_cfg())
    assert err and "lab" in err.lower()  # names the way out
    assert site_data_error(_cfg(site_address="400 TradeCenter")) is not None
    assert site_data_error(_cfg(site_address="400 TradeCenter",
                                site_city="San Jose")) is not None
    assert site_data_error(_cfg(site_address="400 TradeCenter",
                                site_city="San Jose",
                                site_country="US")) is None


def test_lab_mode_waives_site_data():
    assert site_data_error(_cfg(lab_mode=True)) is None


def test_no_sites_means_nothing_to_validate():
    assert site_data_error(_cfg(sites=[])) is None


# ─────────────────── Classic client refuses the 0.0,0.0 placeholder ───────────────────

def test_classic_site_refuses_placeholder_geolocation_in_production():
    c = ClassicCentralClient("http://classic.invalid", "tok")
    c._sites_cache = []
    with pytest.raises(ClassicCentralAPIError) as exc:
        c.create_site("hq")
    assert "lab" in str(exc.value).lower()


def test_classic_site_placeholder_only_in_lab_mode():
    c = ClassicCentralClient("http://classic.invalid", "tok")
    c._sites_cache = []
    bodies = []
    c._post = lambda path, json_body=None, params=None: (
        bodies.append(json_body), {"site_id": 7})[1]
    sid = c.create_site("hq", lab_mode=True)
    assert sid == 7
    assert bodies[0]["geolocation"] == {"latitude": "0.0", "longitude": "0.0"}


def test_classic_site_with_real_address_needs_no_lab_mode():
    c = ClassicCentralClient("http://classic.invalid", "tok")
    c._sites_cache = []
    bodies = []
    c._post = lambda path, json_body=None, params=None: (
        bodies.append(json_body), {"site_id": 7})[1]
    c.create_site("hq", address="400 TradeCenter", city="San Jose",
                  country="US")
    assert "site_address" in bodies[0]
    assert "geolocation" not in bodies[0]


# ─────────────────── New Central client refuses "1 Lab Street" ───────────────────

def test_new_central_site_refuses_placeholder_address_in_production():
    c = CentralClient("http://central.invalid", "cid", "secret")
    c._sites_cache = []
    with pytest.raises(CentralAPIError) as exc:
        c.create_site("hq")
    assert "Lab Street" not in str(exc.value)  # refuses, doesn't name the crutch
    assert "lab" in str(exc.value).lower()


def test_new_central_site_lab_mode_still_uses_placeholder():
    c = CentralClient("http://central.invalid", "cid", "secret")
    c._sites_cache = []
    posted = []
    # first list (pre-create) sees nothing; the post-create id re-list does
    seen = {"posted": False}
    def _list(refresh=False):
        return [{"scopeName": "hq", "scopeId": 42}] if seen["posted"] else []
    def _post(path, json=None, params=None):
        seen["posted"] = True
        return posted.append(json) or {}
    c._post = _post
    c.list_sites = _list
    sid = c.create_site("hq", lab_mode=True)
    assert sid == "42"
    assert posted[0]["address"] == "1 Lab Street"
