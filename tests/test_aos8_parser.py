"""Parser regression tests on canned CLI output — no hardware needed."""
from lib.aos8_parser import (
    _aaa_server_groups, _clean_group, _clean_zone, parse_customer_config,
    parse_instant_config,
)
from lib.models import ForwardMode

RUNNING_CONFIG = '''
version 8.10

wlan ssid-profile "corp-ssid"
   essid "Corp"
   opmode wpa2-aes
!
wlan ssid-profile "guest-ssid"
   essid "Guest"
   opmode wpa2-psk-aes
   wpa-passphrase SecretPass123
!
wlan ssid-profile "pool-ssid"
   essid "Pool"
   opmode opensystem
!
aaa profile "corp-aaa"
   authentication-dot1x "default"
   dot1x-default-role "authenticated"
   dot1x-server-group "clearpass-sg"
!
aaa profile "guest-aaa"
   initial-role "guest-logon"
!
wlan virtual-ap "corp-vap"
   aaa-profile "corp-aaa"
   ssid-profile "corp-ssid"
   vlan 100
   forward-mode tunnel
!
wlan virtual-ap "guest-vap"
   aaa-profile "guest-aaa"
   ssid-profile "guest-ssid"
   vlan 200
   forward-mode bridge
!
wlan virtual-ap "pool-vap"
   ssid-profile "pool-ssid"
   vlan guest2020
   forward-mode bridge
!
ap-group "campus"
   virtual-ap "corp-vap"
   virtual-ap "guest-vap"
!
ap-group "warehouse"
   virtual-ap "guest-vap"
   virtual-ap "pool-vap"
!
aaa authentication-server radius "clearpass-1"
   host "10.0.0.50"
!
'''

AP_DATABASE = '''
AP Database
-----------
Name      Group      AP Type  IP Address   Status         Flags  Serial #    Wired MAC Address
----      -----      -------  ----------   ------         -----  --------    -----------------
ap-01     campus     535      10.1.1.11    Up 10d:2h:3m          CN12345678  aa:bb:cc:00:00:01
ap-02     warehouse  515      10.1.2.11    Up 3d:1h:10m          CN22345678  aa:bb:cc:00:00:02
ap-03     -          303      10.1.3.11    Down                  CN32345678  aa:bb:cc:00:00:03
'''


def _parse():
    return parse_customer_config(
        {"running_config": RUNNING_CONFIG, "ap_database": AP_DATABASE},
        mc_ip="10.0.0.1")


def test_ssids_parsed_with_bindings():
    cfg = _parse()
    by_name = {s.name: s for s in cfg.ssids}
    assert set(by_name) == {"corp-vap", "guest-vap", "pool-vap"}
    assert by_name["corp-vap"].forward_mode is ForwardMode.TUNNEL
    assert by_name["guest-vap"].psk == "SecretPass123"
    groups = {g.name: g.ssids for g in cfg.ap_groups}
    assert groups["campus"] == ["corp-vap", "guest-vap"]
    assert groups["warehouse"] == ["guest-vap", "pool-vap"]


def test_aaa_profile_resolves_to_server_group_not_profile_name():
    cfg = _parse()
    corp = next(s for s in cfg.ssids if s.name == "corp-vap")
    # the RADIUS binding must be the dot1x-server-group INSIDE the
    # aaa-profile, never the aaa-profile's own name
    assert corp.auth_server_group == "clearpass-sg"
    # a profile with no server group falls back to the profile name
    guest = next(s for s in cfg.ssids if s.name == "guest-vap")
    assert guest.auth_server_group == "guest-aaa"


def test_aaa_server_groups_helper():
    assert _aaa_server_groups(RUNNING_CONFIG) == {"corp-aaa": "clearpass-sg"}


def test_named_vlan_with_digits_is_flagged_not_parsed():
    cfg = _parse()
    pool = next(s for s in cfg.ssids if s.name == "pool-vap")
    # 'guest2020' must NOT become VLAN 2020
    assert pool.vlan != 2020
    assert pool.vlan_raw == "guest2020"


def test_ap_database_parsing():
    cfg = _parse()
    by_serial = {a.serial: a for a in cfg.aps}
    assert by_serial["CN12345678"].ap_group == "campus"
    assert by_serial["CN12345678"].mac == "aa:bb:cc:00:00:01"
    # placeholder group column maps to the literal default group (MC land)
    assert by_serial["CN32345678"].ap_group == "default"


def test_clean_group_vs_clean_zone():
    assert _clean_group("-") == "default"     # MC: default group is real
    assert _clean_zone("-") == ""             # Instant: no zone stays empty
    assert _clean_zone("Zone7") == "Zone7"


INSTANT_CONFIG = '''
version 8.10.0.6

wlan ssid-profile corp
 enable
 index 0
 type employee
 essid corp
 opmode wpa2-psk-aes
 wpa-passphrase SecretPass123
 vlan 100
!
'''

INSTANT_APS = '''
AP List
-------
Name      IP Address  Mode    Spectrum  Clients  Type  Mesh Role  Zone  Serial #    MAC Address
----      ----------  ----    --------  -------  ----  ---------  ----  --------    -----------
iap-01    10.2.1.11   access  disabled  4        505              -     CN99911111  aa:bb:cc:11:00:01
iap-02    10.2.1.12   access  disabled  2        505              -     CN99911112  aa:bb:cc:11:00:02
'''


def test_instant_zoneless_aps_group_into_synthetic_cluster():
    cfg = parse_instant_config(
        {"running_config": INSTANT_CONFIG, "show_aps": INSTANT_APS},
        vc_ip="10.2.1.9")
    assert cfg.source_type == "instant"
    # zoneless APs must not invent a zone named 'default'
    names = {g.name for g in cfg.ap_groups}
    assert names == {"instant-cluster"}
    assert all(a.ap_group == "instant-cluster" for a in cfg.aps)


def test_internal_auth_not_triggered_by_summary_table():
    # every AOS 8 box lists the built-in "Internal" server — its presence in
    # `show aaa authentication-server all` output must NOT flag internal auth
    cfg = parse_customer_config(
        {"running_config": RUNNING_CONFIG,
         "aaa_auth_server": "Auth Server Table\nName      Type    IP\n"
                            "Internal  Local   10.0.0.1\n"
                            "cp-1      Radius  10.0.0.50\n"},
        mc_ip="10.0.0.1")
    assert cfg.has_internal_auth is False
    # ...but a server-group actually referencing it does
    cfg2 = parse_customer_config(
        {"running_config": RUNNING_CONFIG +
         '\naaa server-group "corp-sg"\n   auth-server Internal\n!\n'},
        mc_ip="10.0.0.1")
    assert cfg2.has_internal_auth is True


def test_eap_offload_detected_via_dot1x_termination():
    cfg = parse_customer_config(
        {"running_config": RUNNING_CONFIG +
         '\naaa authentication dot1x "corp-dot1x"\n   termination enable\n!\n'},
        mc_ip="10.0.0.1")
    assert cfg.has_eap_offload is True


def test_quoted_passphrase_with_spaces():
    cfg = parse_customer_config(
        {"running_config": RUNNING_CONFIG.replace(
            "wpa-passphrase SecretPass123",
            'wpa-passphrase "pass with spaces"')},
        mc_ip="10.0.0.1")
    guest = next(s for s in cfg.ssids if s.name == "guest-vap")
    assert guest.psk == "pass with spaces"


def test_ap_group_list_title_is_not_a_group():
    from lib.aos8_parser import _parse_ap_groups
    groups = _parse_ap_groups("AP group List\n-------------\nName\n----\ncampus\n")
    assert all(g.name.lower() != "list" for g in groups)


def test_instant_disabled_wlan_stays_disabled_not_hidden():
    cfg = parse_instant_config(
        {"running_config": INSTANT_CONFIG + '''
wlan ssid-profile off-net
 essid off-net
 opmode wpa2-psk-aes
 wpa-passphrase Something123
 vlan 100
 disable
!
''', "show_aps": INSTANT_APS}, vc_ip="10.2.1.9")
    off = next(s for s in cfg.ssids if s.name == "off-net")
    assert off.enabled is False          # administratively OFF...
    assert off.broadcast is True         # ...NOT a hidden-but-active SSID


def test_instant_quoted_psk_with_spaces():
    cfg = parse_instant_config(
        {"running_config": INSTANT_CONFIG.replace(
            "wpa-passphrase SecretPass123",
            'wpa-passphrase "instant pass with spaces"')},
        vc_ip="10.2.1.9")
    corp = next(s for s in cfg.ssids if s.name == "corp")
    assert corp.psk == "instant pass with spaces"


def test_api_group_cell_placeholder_normalized():
    from lib.aos8_client import _normalize_group_cell
    assert _normalize_group_cell("-") == "default"
    assert _normalize_group_cell("") == "default"
    assert _normalize_group_cell("campus") == "campus"


MAC_RUNNING = '''
wlan ssid-profile "iot-ssid"
   essid "IoT"
   opmode opensystem
!
aaa profile "iot-aaa"
   mac-server-group "cppm-sg"
!
wlan virtual-ap "iot-vap"
   aaa-profile "iot-aaa"
   ssid-profile "iot-ssid"
   vlan 40
   forward-mode bridge
!
ap-group "campus"
   virtual-ap "iot-vap"
!
'''


def test_mac_auth_ssid_detected_from_mac_server_group():
    """H2 (paste path): opensystem + mac-server-group in the bound aaa-profile
    is MAC auth — migrating it as OPEN publishes an open network."""
    from lib.models import AuthType
    cfg = parse_customer_config({"running_config": MAC_RUNNING},
                                mc_ip="10.0.0.1")
    iot = next(s for s in cfg.ssids if s.name == "iot-vap")
    assert iot.auth_type is AuthType.MAC
    assert iot.auth_known
    assert iot.auth_server_group == "cppm-sg"


def test_vlan_pool_tokens_set_vlan_raw():
    """M3 (paste path): VLAN pools/ranges must be flagged for operator mapping,
    not silently collapsed to the first id."""
    cfg = parse_customer_config(
        {"running_config": RUNNING_CONFIG.replace("vlan guest2020",
                                                  "vlan 100,200")},
        mc_ip="10.0.0.1")
    pool = next(s for s in cfg.ssids if s.name == "pool-vap")
    assert pool.vlan == 100
    assert pool.vlan_raw == "100,200"

    cfg2 = parse_customer_config(
        {"running_config": RUNNING_CONFIG.replace("vlan guest2020",
                                                  "vlan 100-105")},
        mc_ip="10.0.0.1")
    pool2 = next(s for s in cfg2.ssids if s.name == "pool-vap")
    assert pool2.vlan == 100
    assert pool2.vlan_raw == "100-105"


PARITY_RUNNING = '''
wlan ssid-profile "iot-ssid"
   essid "IoT"
   opmode opensystem
   hide
!
aaa profile "iot-aaa"
   mac-server-group "cppm-sg"
!
aaa server-group "cppm-sg"
   auth-server cppm-primary
   auth-server cppm-backup
!
wlan virtual-ap "iot-vap"
   aaa-profile "iot-aaa"
   ssid-profile "iot-ssid"
   vlan 40
   forward-mode bridge
   disable
!
ap-group "campus"
   virtual-ap "iot-vap"
!
'''


def test_mc_paste_disabled_vap_not_migrated_active():
    """#9 parity: a `disable`d virtual-AP is administratively OFF — the MC
    paste path silently migrated it as active (Instant path already did not)."""
    cfg = parse_customer_config({"running_config": PARITY_RUNNING},
                                mc_ip="10.0.0.1")
    iot = next(s for s in cfg.ssids if s.name == "iot-vap")
    assert iot.enabled is False


def test_mc_paste_hidden_ssid_not_broadcast():
    """#9 parity: `hide` in the ssid-profile must not silently default to
    broadcast=True."""
    cfg = parse_customer_config({"running_config": PARITY_RUNNING},
                                mc_ip="10.0.0.1")
    iot = next(s for s in cfg.ssids if s.name == "iot-vap")
    assert iot.broadcast is False


def test_mc_paste_server_groups_preserve_member_order():
    """#9 parity: aaa server-group membership is reconstructed from paste,
    in order — order IS failover order."""
    cfg = parse_customer_config({"running_config": PARITY_RUNNING},
                                mc_ip="10.0.0.1")
    sg = next(g for g in cfg.server_groups if g.name == "cppm-sg")
    assert sg.servers == ["cppm-primary", "cppm-backup"]


def test_unsupported_source_directives_fail_preflight():
    """#9: a field the migration cannot represent (802.11r here) FAILs
    preflight as non-overridable — never silently dropped onto defaults."""
    from lib import compatibility
    from lib.models import CentralConfig
    cfg = parse_customer_config(
        {"running_config": PARITY_RUNNING.replace(
            "   hide\n", "   dot11r enable\n")},
        mc_ip="10.0.0.1")
    iot = next(s for s in cfg.ssids if s.name == "iot-vap")
    assert any("dot11r" in f for f in iot.unsupported_fields)
    central = CentralConfig(customer_name="acme", base_url="https://x",
                            destination="new")
    results = compatibility.run_all(cfg, central)
    hit = [r for r in results if r.name == "Unsupported Source Fields"]
    assert len(hit) == 1
    assert hit[0].status == compatibility.Status.FAIL
    assert hit[0].critical
    assert "iot-vap" in hit[0].message or "IoT" in hit[0].message
