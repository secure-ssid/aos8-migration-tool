from lib.models import (
    AP, APGroup, AuthType, ClusterInfo, CustomerConfig, ForwardMode,
    RadiusServer, SSID, VLAN,
)
from lib.runbook import generate
from lib.translator import translate


def _customer(cluster=None):
    ssids = [
        SSID(name="corp-vap", essid="Corp", vlan=100,
             forward_mode=ForwardMode.TUNNEL,
             auth_type=AuthType.WPA2_ENTERPRISE, auth_server_group="cp-sg"),
        SSID(name="guest-vap", essid="Guest", vlan=200,
             forward_mode=ForwardMode.BRIDGE,
             auth_type=AuthType.WPA2_PSK, psk="SecretPass123"),
    ]
    return CustomerConfig(
        mc_ip="10.0.0.5", mc_firmware="8.10.0.14", controller_vlan=1,
        source_type="controller",
        ap_groups=[APGroup(name="campus", ssids=["corp-vap", "guest-vap"],
                           ap_serials=["CN1"], ap_models=["AP-535"])],
        ssids=ssids,
        aps=[AP("CN1", "AP-535", "aa:bb:cc:00:00:01", "ap-01", "campus",
                "10.1.1.11", "Up")],
        vlans=[VLAN(100, "corp"), VLAN(200, "guest")],
        radius_servers=[RadiusServer("cp-1", "10.0.0.50")],
        cluster=cluster,
    )


def _runbook(cluster=None):
    customer = _customer(cluster)
    central = translate(customer, "Acme", "https://x")
    return generate(customer, central, "Acme")


def test_no_unrendered_placeholders():
    text = _runbook()
    assert "{SSID}" not in text
    assert "{p}" not in text


def test_convert_block_present_with_groups():
    text = _runbook()
    assert "ap convert add ap-group campus" in text
    assert "ap convert" in text


def test_l2_cluster_sequence():
    text = _runbook(ClusterInfo(type="L2", members=["10.0.0.5", "10.0.0.6"]))
    assert "L2 CLUSTER" in text.upper()
    assert "10.0.0.5" in text
