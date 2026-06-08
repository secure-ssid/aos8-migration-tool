"""
Synthetic discovery generator — exercise the full provisioning path against a
real (lab) tenant without needing a controller. Everything is named with a
`zztest-` prefix so created objects are unmistakably disposable and trivial to
find/delete in Central afterward.

The data deliberately spans the cases that have bitten us: multiple auth types
(PSK, enterprise, open), a tunnel SSID (exercises the overlay/GW path), a
bridge SSID, two AP groups, and APs with serial+MAC (so GLP claim is testable).
"""
from .models import (
    AP, APGroup, ClusterInfo, CustomerConfig, ForwardMode, AuthType,
    RadiusServer, SSID, VLAN,
)

TEST_PREFIX = "zztest"


def make_test_config(scenario: str = "mixed") -> CustomerConfig:
    """scenario:
       "mixed"  — 2 groups, tunnel+bridge SSIDs, enterprise+PSK+open, cluster
       "bridge" — single group, all bridge (clean New-Central-native case)
       "instant"— Instant-style single cluster, bridge only
    """
    if scenario == "bridge":
        return _bridge_only()
    if scenario == "instant":
        return _instant_like()
    if scenario == "iap-full":
        return _instant_full()
    return _mixed()


def _ssid(name, essid, vlan, mode, auth, psk=None, server=None):
    return SSID(name=name, essid=essid, vlan=vlan, forward_mode=mode,
                auth_type=auth, psk=psk, auth_server_group=server)


def _mixed() -> CustomerConfig:
    ssids = [
        _ssid("corp-vap", f"{TEST_PREFIX}-corp", 100, ForwardMode.TUNNEL,
              AuthType.WPA2_ENTERPRISE, server="zztest-clearpass"),
        _ssid("guest-vap", f"{TEST_PREFIX}-guest", 200, ForwardMode.BRIDGE,
              AuthType.WPA2_PSK, psk="Zztest-PSK-1234"),
        _ssid("iot-vap", f"{TEST_PREFIX}-iot", 300, ForwardMode.BRIDGE,
              AuthType.OPEN),
    ]
    groups = [
        APGroup(name=f"{TEST_PREFIX}-campus", ssids=["corp-vap", "guest-vap"],
                ap_serials=["ZZTESTAP0001", "ZZTESTAP0002"],
                ap_models=["AP-535"]),
        APGroup(name=f"{TEST_PREFIX}-warehouse", ssids=["guest-vap", "iot-vap"],
                ap_serials=["ZZTESTAP0003"], ap_models=["AP-515"]),
    ]
    aps = [
        AP("ZZTESTAP0001", "AP-535", "aa:bb:cc:00:00:01", "zztest-ap-01",
           f"{TEST_PREFIX}-campus", "10.90.1.11", "Up"),
        AP("ZZTESTAP0002", "AP-535", "aa:bb:cc:00:00:02", "zztest-ap-02",
           f"{TEST_PREFIX}-campus", "10.90.1.12", "Up"),
        AP("ZZTESTAP0003", "AP-515", "aa:bb:cc:00:00:03", "zztest-ap-03",
           f"{TEST_PREFIX}-warehouse", "10.90.2.11", "Up"),
    ]
    vlans = [VLAN(100, f"{TEST_PREFIX}-corp"), VLAN(200, f"{TEST_PREFIX}-guest"),
             VLAN(300, f"{TEST_PREFIX}-iot")]
    return CustomerConfig(
        mc_ip="10.90.0.5", mc_firmware="8.10.0.14", controller_vlan=1,
        source_type="controller", ap_groups=groups, ssids=ssids, aps=aps,
        vlans=vlans,
        radius_servers=[RadiusServer("zztest-clearpass", "10.90.0.50")],
        cluster=ClusterInfo(type="L2", members=["10.90.0.5", "10.90.0.6"]),
    )


def _bridge_only() -> CustomerConfig:
    ssids = [
        _ssid("corp-vap", f"{TEST_PREFIX}-corp", 100, ForwardMode.BRIDGE,
              AuthType.WPA3_SAE, psk="Zztest-SAE-1234"),
        _ssid("guest-vap", f"{TEST_PREFIX}-guest", 200, ForwardMode.BRIDGE,
              AuthType.OPEN),
    ]
    groups = [APGroup(name=f"{TEST_PREFIX}-aps", ssids=["corp-vap", "guest-vap"],
                      ap_serials=["ZZTESTAP0001", "ZZTESTAP0002"],
                      ap_models=["AP-635"])]
    aps = [
        AP("ZZTESTAP0001", "AP-635", "aa:bb:cc:00:00:01", "zztest-ap-01",
           f"{TEST_PREFIX}-aps", "10.90.1.11", "Up"),
        AP("ZZTESTAP0002", "AP-635", "aa:bb:cc:00:00:02", "zztest-ap-02",
           f"{TEST_PREFIX}-aps", "10.90.1.12", "Up"),
    ]
    return CustomerConfig(
        mc_ip="10.90.0.5", mc_firmware="8.10.0.14", controller_vlan=1,
        source_type="controller", ap_groups=groups, ssids=ssids, aps=aps,
        vlans=[VLAN(100, f"{TEST_PREFIX}-corp"), VLAN(200, f"{TEST_PREFIX}-guest")],
        radius_servers=[], cluster=None,
    )


def _instant_like() -> CustomerConfig:
    ssids = [
        _ssid("zztest-corp", f"{TEST_PREFIX}-corp", 100, ForwardMode.BRIDGE,
              AuthType.WPA2_PSK, psk="Zztest-PSK-1234"),
    ]
    groups = [APGroup(name="instant-cluster", ssids=["zztest-corp"],
                      ap_serials=["ZZTESTIAP001", "ZZTESTIAP002"],
                      ap_models=["AP-505"])]
    aps = [
        AP("ZZTESTIAP001", "AP-505", "aa:bb:cc:00:01:01", "zztest-iap-01",
           "instant-cluster", "10.90.3.11", "Up"),
        AP("ZZTESTIAP002", "AP-505", "aa:bb:cc:00:01:02", "zztest-iap-02",
           "instant-cluster", "10.90.3.12", "Up"),
    ]
    return CustomerConfig(
        mc_ip="10.90.3.5", mc_firmware="8.10.0.6", controller_vlan=1,
        source_type="instant", ap_groups=groups, ssids=ssids, aps=aps,
        vlans=[VLAN(100, f"{TEST_PREFIX}-corp")], radius_servers=[], cluster=None,
    )


def _instant_full() -> CustomerConfig:
    """Comprehensive IAP scenario — exercises the WHOLE config build on the
    Instant (all-bridge, no gateway/overlay) path: two zones → two device
    groups, all three auth types, three VLANs, a RADIUS auth-server."""
    ssids = [
        _ssid("zztest-corp", f"{TEST_PREFIX}-corp", 100, ForwardMode.BRIDGE,
              AuthType.WPA2_ENTERPRISE, server="zztest-clearpass"),
        _ssid("zztest-staff", f"{TEST_PREFIX}-staff", 200, ForwardMode.BRIDGE,
              AuthType.WPA2_PSK, psk="Zztest-PSK-1234"),
        _ssid("zztest-guest", f"{TEST_PREFIX}-guest", 300, ForwardMode.BRIDGE,
              AuthType.OPEN),
    ]
    groups = [
        APGroup(name=f"{TEST_PREFIX}-floor1",
                ssids=["zztest-corp", "zztest-staff", "zztest-guest"],
                ap_serials=["ZZTESTIAP001", "ZZTESTIAP002"], ap_models=["AP-635"]),
        APGroup(name=f"{TEST_PREFIX}-floor2",
                ssids=["zztest-staff", "zztest-guest"],
                ap_serials=["ZZTESTIAP003"], ap_models=["AP-505"]),
    ]
    aps = [
        AP("ZZTESTIAP001", "AP-635", "aa:bb:cc:00:02:01", "zztest-iap-01",
           f"{TEST_PREFIX}-floor1", "10.90.3.11", "Up"),
        AP("ZZTESTIAP002", "AP-635", "aa:bb:cc:00:02:02", "zztest-iap-02",
           f"{TEST_PREFIX}-floor1", "10.90.3.12", "Up"),
        AP("ZZTESTIAP003", "AP-505", "aa:bb:cc:00:02:03", "zztest-iap-03",
           f"{TEST_PREFIX}-floor2", "10.90.4.11", "Up"),
    ]
    vlans = [VLAN(100, f"{TEST_PREFIX}-corp"), VLAN(200, f"{TEST_PREFIX}-staff"),
             VLAN(300, f"{TEST_PREFIX}-guest")]
    return CustomerConfig(
        mc_ip="10.90.3.5", mc_firmware="8.10.0.6", controller_vlan=1,
        source_type="instant", ap_groups=groups, ssids=ssids, aps=aps,
        vlans=vlans,
        radius_servers=[RadiusServer("zztest-clearpass", "10.90.0.50")],
        cluster=None,
    )
