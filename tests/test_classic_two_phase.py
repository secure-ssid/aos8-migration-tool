"""Stream A / review finding #1: Classic Central provisioning must be
two-phase. Step 3 tells the operator "No APs are claimed, moved or rebooted"
but ClassicCentralClient.provision() used to move APs into groups inline.
These tests pin the phase split: config builds sites/groups/WLANs/firmware
only; devices moves APs + assigns sites only, and requires the config phase
to have run first."""
import pytest

from lib.classic_central_client import ClassicCentralClient
from lib.models import (AuthType, CentralConfig, CentralGroupConfig,
                        ForwardMode, SSID)


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch):
    # Offline client tests use placeholder hosts — the transport layer
    # refuses non-allowlisted/cleartext base URLs unless the harness opts
    # out via AOS8_DEV_MODE, exactly like a local lab (Stream C contract).
    monkeypatch.setenv("AOS8_DEV_MODE", "true")


def _config() -> CentralConfig:
    ssid = SSID(name="corp", vlan=10, forward_mode=ForwardMode.BRIDGE,
                auth_type=AuthType.WPA2_PSK, psk="passphrase1")
    grp = CentralGroupConfig(name="campus-aps", firmware_version="10.7.0.0",
                             site_name="hq", source_group="src-grp",
                             ssids=[ssid])
    return CentralConfig(customer_name="acme", base_url="http://x",
                         destination="classic", groups=[grp], sites=["hq"])


def _client() -> ClassicCentralClient:
    """Offline client: every API-touching method replaced by a recorder."""
    c = ClassicCentralClient("http://classic.invalid", "tok")
    c.calls = []
    for name in ("add_to_inventory", "move_devices", "create_wlan",
                 "set_firmware_compliance", "associate_site"):
        setattr(c, name, lambda *a, _n=name, **k: c.calls.append(_n))
    c.create_group = lambda name, *a, **k: c.calls.append("create_group") or name
    c.create_site = lambda name, *a, **k: c.calls.append("create_site") or 7
    c.list_sites = lambda refresh=False: [{"site_name": "hq", "site_id": 7}]
    c.list_group_names = lambda refresh=False: ["campus-aps"]
    return c


def test_config_phase_never_touches_aps():
    c = _client()
    results = c.provision(_config(), ap_serials={"src-grp": ["SN1"]},
                          ap_macs={"SN1": "aa:bb:cc:dd:ee:ff"}, phase="config")
    assert "move_devices" not in c.calls
    assert "associate_site" not in c.calls
    # config still builds everything the review expects of Step 3
    assert "add_to_inventory" in c.calls
    assert "create_site" in c.calls
    assert "create_group" in c.calls
    assert "create_wlan" in c.calls
    assert "set_firmware_compliance" in c.calls
    assert all(ok for _, ok, _ in results)


def test_devices_phase_moves_and_assigns_only():
    c = _client()
    results = c.provision(_config(), ap_serials={"src-grp": ["SN1"]},
                          ap_macs={"SN1": "aa:bb:cc:dd:ee:ff"}, phase="devices")
    assert "move_devices" in c.calls
    assert "associate_site" in c.calls
    # nothing is CREATED in the devices phase — config is the config phase's
    assert "create_wlan" not in c.calls
    assert "create_group" not in c.calls
    assert "create_site" not in c.calls
    assert "set_firmware_compliance" not in c.calls
    assert all(ok for _, ok, _ in results)


def test_devices_phase_fails_closed_when_config_missing():
    """Running devices first must not silently create half a tenant — the
    group/site lookups fail with an actionable 'run config first' error."""
    c = _client()
    c.list_group_names = lambda refresh=False: []   # config never ran
    c.list_sites = lambda refresh=False: []
    results = c.provision(_config(), ap_serials={"src-grp": ["SN1"]},
                          ap_macs={"SN1": "aa:bb:cc:dd:ee:ff"}, phase="devices")
    failed = [(label, err) for label, ok, err in results if not ok]
    assert failed, "devices phase without config must record failures"
    assert "move_devices" not in c.calls
    assert any("config" in err.lower() for _, err in failed)


def test_all_phase_preserves_legacy_single_shot():
    c = _client()
    results = c.provision(_config(), ap_serials={"src-grp": ["SN1"]},
                          ap_macs={"SN1": "aa:bb:cc:dd:ee:ff"}, phase="all")
    for expected in ("add_to_inventory", "create_site", "create_group",
                     "move_devices", "create_wlan", "set_firmware_compliance",
                     "associate_site"):
        assert expected in c.calls
    assert all(ok for _, ok, _ in results)


def test_default_phase_is_all_for_backwards_compatibility():
    c = _client()
    c.provision(_config(), ap_serials={"src-grp": ["SN1"]},
                ap_macs={"SN1": "aa:bb:cc:dd:ee:ff"})
    assert "move_devices" in c.calls


def test_invalid_phase_rejected():
    c = _client()
    with pytest.raises(ValueError):
        c.provision(_config(), ap_serials={}, phase="confing")
