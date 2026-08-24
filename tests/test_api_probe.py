"""Response-schema validation + firmware gate for the API probes: a probe
must never report green on a payload that does not match the documented API
shape (a 2xx with a non-JSON body flattens to {} and must not read as '0
site(s) readable'), and target-firmware checks must surface 'cannot
validate' as a warning instead of a silent pass."""
import pytest

from lib import api_probe
from lib.api_probe import check_target_firmware


# ── schema validation: malformed payloads must probe fail ────────────────

class _HealthyCentral:
    def __init__(self, *a, **k):
        self.scope = "123"
        self.sites = [{"siteName": "HQ"}]
        self.groups = [{"scopeName": "g1", "scopeId": "42"}]
        self.aps = [{"serial": "CNABC12345"}]

    def authenticate(self):
        return True

    def get_global_scope_id(self):
        return self.scope

    def list_sites(self):
        return self.sites

    def list_device_groups(self, refresh=False):
        return self.groups

    def list_all_aps(self):
        return self.aps

    def _post(self, *a, **k):
        return {"scopeId": "42"}

    def _request(self, *a, **k):
        return {}


class _MalformedCentral(_HealthyCentral):
    """Every read returns a shape that violates the documented schema."""

    def list_sites(self):
        return [{"nope": "not-a-site"}]

    def list_device_groups(self, refresh=False):
        return ["g1", "g2"]          # list of strings, not objects

    def list_all_aps(self):
        return {"devices": []}       # dict instead of list


class _FirmwareCentral(_HealthyCentral):
    """A healthy tenant that also answers the supported-version query."""

    FW = ["10.6.0.0", "10.7.0.0"]

    def _get(self, path, params=None):
        return {"data": [{"firmware_version": v, "release_status": "release",
                          "create_date": "2026-01-01"} for v in self.FW]}


class _FailingFirmwareCentral(_HealthyCentral):
    """The live supported-versions query fails (transport / API error)."""

    def _get(self, path, params=None):
        raise RuntimeError("firmware query failed")


class _GarbledFirmwareCentral(_HealthyCentral):
    """The query answers but the payload violates the documented schema."""

    def _get(self, path, params=None):
        return {"data": [{"nope": "not-a-firmware-version"}]}


@pytest.fixture()
def fake_central(monkeypatch):
    yield lambda cls: monkeypatch.setattr(api_probe, "CentralClient", cls)


def test_healthy_probe_passes(fake_central):
    fake_central(_HealthyCentral)
    results = api_probe.probe_new_central("https://us4.api.central.arubanetworks.com",
                                          "id", "secret")
    by_name = {r.name: r.status for r in results}
    assert by_name["Read — sites"] == "ok"
    assert by_name["Read — device groups"] == "ok"
    assert by_name["Read — monitored devices (validation source)"] == "ok"


def test_malformed_payload_probe_fails(fake_central):
    fake_central(_MalformedCentral)

    def status(name):
        return {r.name: r.status for r in
                api_probe.probe_new_central("https://us4.api.central.arubanetworks.com",
                                            "id", "secret")}[name]

    assert status("Read — sites") == "fail"
    assert status("Read — device groups") == "fail"
    assert status("Read — monitored devices (validation source)") == "fail"


def test_malformed_site_detail_names_schema_broken(fake_central):
    fake_central(_MalformedCentral)
    for r in api_probe.probe_new_central("https://us4.api.central.arubanetworks.com",
                                         "id", "secret"):
        if r.name == "Read — sites":
            assert "schema" in r.detail.lower()


# ── firmware: format check + gated live tenant query ─────────────────────

def test_firmware_bad_format_is_fail():
    r = check_target_firmware("10.7")
    assert r.status == "fail"
    assert "not a valid" in r.detail


def test_firmware_no_live_query_is_warn_not_silent_pass():
    r = check_target_firmware("10.7.0.0")
    assert r.status == "warn"
    assert "cannot validate" in r.detail


def test_firmware_live_query_error_is_warn():
    r = check_target_firmware("10.7.0.0", error="tenant handshake unavailable")
    assert r.status == "warn"
    assert "cannot validate" in r.detail
    assert "tenant handshake unavailable" in r.detail


def test_firmware_in_tenant_supported_set_is_ok():
    r = check_target_firmware("10.7.0.0", supported=["10.6.0.0", "10.7.0.0"])
    assert r.status == "ok"


def test_firmware_not_in_tenant_supported_set_is_fail():
    r = check_target_firmware("10.7.0.0", supported=["10.6.0.0", "10.6.1.1"])
    assert r.status == "fail"
    assert "NOT in this tenant" in r.detail


# ── wired probe path: probe_new_central appends the Target firmware row ────

def _firmware_row(results):
    return next(r for r in results if r.name == "Target firmware")


def test_probe_appends_target_firmware_row_ok(fake_central):
    fake_central(_FirmwareCentral)
    r = _firmware_row(api_probe.probe_new_central(
        "https://us4.api.central.arubanetworks.com", "id", "secret",
        target_fw="10.7.0.0"))
    assert r.status == "ok"
    assert "in the tenant's supported set" in r.detail


def test_probe_target_outside_tenant_set_fails(fake_central):
    fake_central(_FirmwareCentral)
    r = _firmware_row(api_probe.probe_new_central(
        "https://us4.api.central.arubanetworks.com", "id", "secret",
        target_fw="10.9.1.0"))
    assert r.status == "fail"
    assert "NOT in this tenant" in r.detail


def test_probe_target_query_failure_is_warn(fake_central):
    fake_central(_FailingFirmwareCentral)
    r = _firmware_row(api_probe.probe_new_central(
        "https://us4.api.central.arubanetworks.com", "id", "secret",
        target_fw="10.7.0.0"))
    assert r.status == "warn"
    assert "cannot validate" in r.detail
    assert "firmware query failed" in r.detail


def test_probe_target_malformed_live_query_is_warn(fake_central):
    fake_central(_GarbledFirmwareCentral)
    r = _firmware_row(api_probe.probe_new_central(
        "https://us4.api.central.arubanetworks.com", "id", "secret",
        target_fw="10.7.0.0"))
    assert r.status == "warn"
    assert "cannot validate" in r.detail


def test_probe_no_target_skips_firmware_row(fake_central):
    fake_central(_HealthyCentral)
    results = api_probe.probe_new_central(
        "https://us4.api.central.arubanetworks.com", "id", "secret")
    assert all(r.name != "Target firmware" for r in results)