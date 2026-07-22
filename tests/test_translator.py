from lib.models import (
    AP, APGroup, AuthType, CustomerConfig, ForwardMode, SSID, VLAN,
)
from lib.translator import translate


def _ssid(name, vlan, mode, auth=AuthType.OPEN, **kw):
    return SSID(name=name, essid=name, vlan=vlan, forward_mode=mode,
                auth_type=auth, **kw)


def _cfg(groups, ssids, vlans=None):
    return CustomerConfig(
        mc_ip="10.0.0.1", mc_firmware="8.10.0.14", controller_vlan=1,
        source_type="controller", ap_groups=groups, ssids=ssids,
        aps=[], vlans=vlans or [])


def test_generic_groups_merge_into_one_central_group():
    cfg = _cfg(
        [APGroup(name="default", ssids=["s1"]),
         APGroup(name="NoAuthApGroup", ssids=["s2"])],
        [_ssid("s1", 10, ForwardMode.BRIDGE), _ssid("s2", 20, ForwardMode.BRIDGE)],
        [VLAN(10, "v10"), VLAN(20, "v20")])
    central = translate(cfg, "Acme Corp", "https://x")
    assert [g.name for g in central.groups] == ["acme-corp-aps"]
    g = central.groups[0]
    assert g.source_group == "default"
    assert g.extra_source_groups == ["NoAuthApGroup"]
    assert {s.name for s in g.ssids} == {"s1", "s2"}
    assert {v.id for v in g.vlans} == {10, 20}


def test_named_group_kept_and_not_merged():
    cfg = _cfg(
        [APGroup(name="campus", ssids=["s1"])],
        [_ssid("s1", 10, ForwardMode.BRIDGE)], [VLAN(10, "v10")])
    central = translate(cfg, "Acme", "https://x")
    assert [g.name for g in central.groups] == ["campus"]
    assert central.groups[0].extra_source_groups == []


def test_retire_mode_converts_tunnel_to_bridge_no_cluster():
    cfg = _cfg([APGroup(name="campus", ssids=["s1"])],
               [_ssid("s1", 10, ForwardMode.TUNNEL)], [VLAN(10, "v10")])
    central = translate(cfg, "Acme", "https://x", gateway_mode="retire")
    assert central.gateways_retired is True
    assert central.gw_cluster_name is None
    assert central.groups[0].ssids[0].forward_mode is ForwardMode.BRIDGE


def test_keep_mode_creates_cluster_name_for_tunnel():
    cfg = _cfg([APGroup(name="campus", ssids=["s1"])],
               [_ssid("s1", 10, ForwardMode.TUNNEL)], [VLAN(10, "v10")])
    central = translate(cfg, "Acme", "https://x", gateway_mode="keep")
    assert central.gw_cluster_name == "acme-cluster"


def test_group_with_no_bindings_gets_all_ssids():
    cfg = _cfg([APGroup(name="campus", ssids=[])],
               [_ssid("s1", 10, ForwardMode.BRIDGE),
                _ssid("s2", 20, ForwardMode.BRIDGE)])
    central = translate(cfg, "Acme", "https://x")
    assert {s.name for s in central.groups[0].ssids} == {"s1", "s2"}


def test_dangling_bindings_do_not_flood_all_ssids():
    cfg = _cfg([APGroup(name="campus", ssids=["gone-vap"])],
               [_ssid("s1", 10, ForwardMode.BRIDGE),
                _ssid("s2", 20, ForwardMode.BRIDGE)])
    central = translate(cfg, "Acme", "https://x")
    # bindings existed but resolved to nothing — the group must stay empty
    # (and the customer config flagged) rather than broadcast everything
    assert central.groups[0].ssids == []
    assert cfg.ssid_mapping_incomplete is True


def test_server_groups_carried_into_central_config():
    from lib.models import ServerGroup
    cfg = _cfg([APGroup(name="campus", ssids=["s1"])],
               [_ssid("s1", 10, ForwardMode.BRIDGE)])
    cfg.server_groups = [ServerGroup(name="sg-a", servers=["r1"])]
    central = translate(cfg, "Acme", "https://x")
    assert [g.name for g in central.server_groups] == ["sg-a"]
