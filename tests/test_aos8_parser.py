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
