"""
Synthetic discovery generator — exercise the full provisioning path against a
real (lab) tenant without needing a controller. Every named object derives from
a `prefix` (default "zztest") so a) created objects are unmistakably disposable
and trivially deletable, and b) two runs with different customer names produce
DISTINCT group/SSID/VLAN/auth-server names instead of colliding duplicates.

Keep the prefix "zztest"-stemmed (the Load button enforces this): the cleanup
teardown deletes anything named "zztest*", so a "zztest-<customer>" prefix stays
both unique per run AND safely scoped — it can never match real tenant objects.

The data deliberately spans the cases that have bitten us: multiple auth types
(PSK, enterprise, open), a tunnel SSID (overlay/GW path), a bridge SSID, two AP
groups, and APs with serial+MAC (so GLP claim is testable).
"""
from .models import (
    AP, APGroup, ClusterInfo, CustomerConfig, ForwardMode, AuthType,
    RadiusServer, SSID, VLAN,
)

TEST_PREFIX = "zztest"


def make_test_config(scenario: str = "mixed",
                     prefix: str = TEST_PREFIX) -> CustomerConfig:
    """scenario:
       "mixed"  — 2 groups, tunnel+bridge SSIDs, enterprise+PSK+open, cluster
       "bridge" — single group, all bridge (clean New-Central-native case)
       "iap-full" — comprehensive Instant scenario (all-bridge, no gateways)
       "instant"— Instant-style single cluster, bridge only

    Every named object is prefixed with `prefix` so distinct runs don't clobber
    each other in the tenant. Pass a per-customer prefix (e.g. "zztest-acme").
    """
    if scenario == "bridge":
        return _bridge_only(prefix)
    if scenario == "instant":
        return _instant_like(prefix)
    if scenario == "iap-full":
        return _instant_full(prefix)
    return _mixed(prefix)


def _ssid(name, essid, vlan, mode, auth, psk=None, server=None):
    return SSID(name=name, essid=essid, vlan=vlan, forward_mode=mode,
                auth_type=auth, psk=psk, auth_server_group=server)


def _mixed(p: str) -> CustomerConfig:
    ssids = [
        _ssid(f"{p}-corp-vap", f"{p}-corp", 100, ForwardMode.TUNNEL,
              AuthType.WPA2_ENTERPRISE, server=f"{p}-clearpass"),
        _ssid(f"{p}-guest-vap", f"{p}-guest", 200, ForwardMode.BRIDGE,
              AuthType.WPA2_PSK, psk="Zztest-PSK-1234"),
        _ssid(f"{p}-iot-vap", f"{p}-iot", 300, ForwardMode.BRIDGE,
              AuthType.OPEN),
    ]
    groups = [
        APGroup(name=f"{p}-campus", ssids=[f"{p}-corp-vap", f"{p}-guest-vap"],
                ap_serials=["ZZTESTAP0001", "ZZTESTAP0002"],
                ap_models=["AP-535"]),
        APGroup(name=f"{p}-warehouse", ssids=[f"{p}-guest-vap", f"{p}-iot-vap"],
                ap_serials=["ZZTESTAP0003"], ap_models=["AP-515"]),
    ]
    aps = [
        AP("ZZTESTAP0001", "AP-535", "aa:bb:cc:00:00:01", f"{p}-ap-01",
           f"{p}-campus", "10.90.1.11", "Up"),
        AP("ZZTESTAP0002", "AP-535", "aa:bb:cc:00:00:02", f"{p}-ap-02",
           f"{p}-campus", "10.90.1.12", "Up"),
        AP("ZZTESTAP0003", "AP-515", "aa:bb:cc:00:00:03", f"{p}-ap-03",
           f"{p}-warehouse", "10.90.2.11", "Up"),
    ]
    vlans = [VLAN(100, f"{p}-corp"), VLAN(200, f"{p}-guest"),
             VLAN(300, f"{p}-iot")]
    return CustomerConfig(
        mc_ip="10.90.0.5", mc_firmware="8.10.0.14", controller_vlan=1,
        source_type="controller", ap_groups=groups, ssids=ssids, aps=aps,
        vlans=vlans,
        radius_servers=[RadiusServer(f"{p}-clearpass", "10.90.0.50")],
        cluster=ClusterInfo(type="L2", members=["10.90.0.5", "10.90.0.6"]),
    )


def _bridge_only(p: str) -> CustomerConfig:
    ssids = [
        _ssid(f"{p}-corp-vap", f"{p}-corp", 100, ForwardMode.BRIDGE,
              AuthType.WPA3_SAE, psk="Zztest-SAE-1234"),
        _ssid(f"{p}-guest-vap", f"{p}-guest", 200, ForwardMode.BRIDGE,
              AuthType.OPEN),
    ]
    groups = [APGroup(name=f"{p}-aps", ssids=[f"{p}-corp-vap", f"{p}-guest-vap"],
                      ap_serials=["ZZTESTAP0001", "ZZTESTAP0002"],
                      ap_models=["AP-635"])]
    aps = [
        AP("ZZTESTAP0001", "AP-635", "aa:bb:cc:00:00:01", f"{p}-ap-01",
           f"{p}-aps", "10.90.1.11", "Up"),
        AP("ZZTESTAP0002", "AP-635", "aa:bb:cc:00:00:02", f"{p}-ap-02",
           f"{p}-aps", "10.90.1.12", "Up"),
    ]
    return CustomerConfig(
        mc_ip="10.90.0.5", mc_firmware="8.10.0.14", controller_vlan=1,
        source_type="controller", ap_groups=groups, ssids=ssids, aps=aps,
        vlans=[VLAN(100, f"{p}-corp"), VLAN(200, f"{p}-guest")],
        radius_servers=[], cluster=None,
    )


def _instant_like(p: str) -> CustomerConfig:
    ssids = [
        _ssid(f"{p}-corp", f"{p}-corp", 100, ForwardMode.BRIDGE,
              AuthType.WPA2_PSK, psk="Zztest-PSK-1234"),
    ]
    groups = [APGroup(name=f"{p}-cluster", ssids=[f"{p}-corp"],
                      ap_serials=["ZZTESTIAP001", "ZZTESTIAP002"],
                      ap_models=["AP-505"])]
    aps = [
        AP("ZZTESTIAP001", "AP-505", "aa:bb:cc:00:01:01", f"{p}-iap-01",
           f"{p}-cluster", "10.90.3.11", "Up"),
        AP("ZZTESTIAP002", "AP-505", "aa:bb:cc:00:01:02", f"{p}-iap-02",
           f"{p}-cluster", "10.90.3.12", "Up"),
    ]
    return CustomerConfig(
        mc_ip="10.90.3.5", mc_firmware="8.10.0.6", controller_vlan=1,
        source_type="instant", ap_groups=groups, ssids=ssids, aps=aps,
        vlans=[VLAN(100, f"{p}-corp")], radius_servers=[], cluster=None,
    )


def _instant_full(p: str) -> CustomerConfig:
    """Comprehensive IAP scenario — exercises the WHOLE config build on the
    Instant (all-bridge, no gateway/overlay) path: two zones → two device
    groups, all three auth types, three VLANs, a RADIUS auth-server."""
    ssids = [
        _ssid(f"{p}-corp", f"{p}-corp", 100, ForwardMode.BRIDGE,
              AuthType.WPA2_ENTERPRISE, server=f"{p}-clearpass"),
        _ssid(f"{p}-staff", f"{p}-staff", 200, ForwardMode.BRIDGE,
              AuthType.WPA2_PSK, psk="Zztest-PSK-1234"),
        _ssid(f"{p}-guest", f"{p}-guest", 300, ForwardMode.BRIDGE,
              AuthType.OPEN),
    ]
    groups = [
        APGroup(name=f"{p}-floor1",
                ssids=[f"{p}-corp", f"{p}-staff", f"{p}-guest"],
                ap_serials=["ZZTESTIAP001", "ZZTESTIAP002"], ap_models=["AP-635"]),
        APGroup(name=f"{p}-floor2",
                ssids=[f"{p}-staff", f"{p}-guest"],
                ap_serials=["ZZTESTIAP003"], ap_models=["AP-505"]),
    ]
    aps = [
        AP("ZZTESTIAP001", "AP-635", "aa:bb:cc:00:02:01", f"{p}-iap-01",
           f"{p}-floor1", "10.90.3.11", "Up"),
        AP("ZZTESTIAP002", "AP-635", "aa:bb:cc:00:02:02", f"{p}-iap-02",
           f"{p}-floor1", "10.90.3.12", "Up"),
        AP("ZZTESTIAP003", "AP-505", "aa:bb:cc:00:02:03", f"{p}-iap-03",
           f"{p}-floor2", "10.90.4.11", "Up"),
    ]
    vlans = [VLAN(100, f"{p}-corp"), VLAN(200, f"{p}-staff"),
             VLAN(300, f"{p}-guest")]
    return CustomerConfig(
        mc_ip="10.90.3.5", mc_firmware="8.10.0.6", controller_vlan=1,
        source_type="instant", ap_groups=groups, ssids=ssids, aps=aps,
        vlans=vlans,
        radius_servers=[RadiusServer(f"{p}-clearpass", "10.90.0.50")],
        cluster=None,
    )
