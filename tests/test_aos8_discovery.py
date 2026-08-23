"""Regression tests for the discovery re-probe path (pull_config).

The object-API pull re-probes other config nodes when the configured node has
no WLAN config. Everything derived from `show running-config` (external
captive-portal chain, EAP-offload / internal-auth flags) belongs to the node
it was fetched from — switching nodes without re-fetching migrates SSIDs with
the WRONG node's flags (worst case: an external-captive-portal guest SSID
migrating as a fully open network while both blocking preflight checks pass).
"""
from lib.aos8_client import AOS8Client
from lib.models import APGroup, SSID, AuthType, ForwardMode


_RUNNING_OLD = """\
version 8.10.0.0
"""
_RUNNING_NEW = """\
version 8.10.0.0
aaa authentication dot1x "dot1x-prof"
    termination enable
aaa authentication captive-portal "guest-cp"
    login-page "https://portal.example.com/guest"
user-role "guest-role"
    captive-portal "guest-cp"
aaa profile "guest-aaa"
    initial-role "guest-role"
"""


def _bare_client(config_path="/mm"):
    c = object.__new__(AOS8Client)
    c.ip = "10.0.0.9"
    c.timeout = 5
    c.config_path = config_path
    return c


def _ssid(name="corp"):
    return SSID(name=name, essid="Corp", vlan=10,
                forward_mode=ForwardMode.BRIDGE,
                auth_type=AuthType.WPA2_PSK, psk="secret")


def test_reprobe_refetches_running_config_at_the_new_node(monkeypatch):
    c = _bare_client()
    show_calls, pull_calls = [], []

    def fake_show(command, config_path=None, timeout=None):
        show_calls.append(config_path if config_path is not None else c.config_path)
        return _RUNNING_NEW if c.config_path == "/mm/mynode" else _RUNNING_OLD

    def fake_pull(captive_portals=None):
        pull_calls.append(dict(captive_portals or {}))
        if len(pull_calls) == 1:
            return [], {}, [], [], [], []          # no SSIDs → triggers re-probe
        return [APGroup(name="g1")], {"g1": ["vap1"]}, [_ssid()], [], [], []

    monkeypatch.setattr(c, "get_mc_firmware", lambda: "8.10.0.0")
    monkeypatch.setattr(c, "get_controller_ip", lambda: ("10.0.0.9", 10))
    monkeypatch.setattr(c, "_show_text", fake_show)
    monkeypatch.setattr(c, "_pull_objects", fake_pull)
    monkeypatch.setattr(c, "find_config_node", lambda: "/mm/mynode")
    monkeypatch.setattr(c, "get_active_aps", lambda: [])
    monkeypatch.setattr(c, "get_cluster_info", lambda: None)

    cfg = c.pull_config()

    assert c.config_path == "/mm/mynode"
    # running-config was fetched again AFTER the node switch
    assert len(show_calls) >= 2
    assert show_calls[-1] == "/mm/mynode"
    # the second object pull ran with the NEW node's captive-portal chain
    assert pull_calls[0] == {}
    assert pull_calls[1].get("guest-aaa", {}).get("url") == \
        "https://portal.example.com/guest"
    # and the blocking preflight flags come from the new node, not the old
    assert cfg.has_eap_offload is True


def test_reprobe_failed_running_config_records_error_not_stale_flags(monkeypatch):
    """A failed re-fetch must degrade like the initial fetch: empty parse +
    recorded error — never the OLD node's flags on the NEW node's SSIDs."""
    c = _bare_client()
    pull_calls = []

    def fake_show(command, config_path=None, timeout=None):
        if c.config_path == "/mm/mynode":
            raise ValueError("showcommand refused at this node")
        return _RUNNING_NEW                          # old node HAD flags

    def fake_pull(captive_portals=None):
        pull_calls.append(dict(captive_portals or {}))
        if len(pull_calls) == 1:
            return [], {}, [], [], [], []
        return [APGroup(name="g1")], {"g1": ["vap1"]}, [_ssid()], [], [], []

    monkeypatch.setattr(c, "get_mc_firmware", lambda: "8.10.0.0")
    monkeypatch.setattr(c, "get_controller_ip", lambda: ("10.0.0.9", 10))
    monkeypatch.setattr(c, "_show_text", fake_show)
    monkeypatch.setattr(c, "_pull_objects", fake_pull)
    monkeypatch.setattr(c, "find_config_node", lambda: "/mm/mynode")
    monkeypatch.setattr(c, "get_active_aps", lambda: [])
    monkeypatch.setattr(c, "get_cluster_info", lambda: None)

    cfg = c.pull_config()

    assert c.running_config_error                      # failure is visible
    assert pull_calls[1] == {}                         # no stale portal chain
    assert cfg.has_eap_offload is False                # no stale flags


def test_transition_opmode_dict_picks_strongest_flag(monkeypatch):
    """Multi-flag opmode dicts (WPA2/WPA3 transition) must not reduce by JSON
    key order — the strongest flag wins, deterministically."""
    c = _bare_client()
    monkeypatch.setattr(c, "_get_object", lambda name: [
        {"profile-name": "transition",
         "opmode": {"wpa2-psk-aes": True, "wpa3-sae-aes": True},
         "essid": "Corp"},
        {"profile-name": "plain",
         "opmode": {"wpa2-psk-aes": True},
         "essid": "Plain"},
        {"profile-name": "legacy",
         "opmode": "opensystem",
         "essid": "Legacy"},
    ])
    profiles = c.get_ssid_profiles()
    assert profiles["transition"]["opmode"] == "wpa3-sae-aes"
    assert profiles["plain"]["opmode"] == "wpa2-psk-aes"
    assert profiles["legacy"]["opmode"] == "opensystem"


def test_transition_opmode_selection_is_order_independent(monkeypatch):
    c = _bare_client()
    monkeypatch.setattr(c, "_get_object", lambda name: [
        {"profile-name": "fwd",
         "opmode": {"wpa2-psk-aes": True, "wpa3-sae-aes": True},
         "essid": "A"},
        {"profile-name": "rev",
         "opmode": {"wpa3-sae-aes": True, "wpa2-psk-aes": True},
         "essid": "B"},
    ])
    profiles = c.get_ssid_profiles()
    assert profiles["fwd"]["opmode"] == profiles["rev"]["opmode"] == "wpa3-sae-aes"
