"""
Translates an AOS 8 CustomerConfig into a CentralConfig.

gateway_mode:
  "keep"   — tunnel/split SSIDs stay overlay; MC hardware becomes the GW
             cluster (default, mirrors the AOS 8 design).
  "retire" — customer is migrating away from gateways: every SSID becomes
             bridge/underlay, no GW cluster is created, MCs are
             decommissioned after conversion.
"""
import re
from dataclasses import replace

from .models import (
    CentralConfig, CentralGroupConfig, CustomerConfig, ForwardMode,
)


def translate(customer_config: CustomerConfig, customer_name: str, central_base_url: str,
              aos10_firmware: str = "10.7.0.0", site_name: str = "",
              gateway_mode: str = "keep") -> CentralConfig:
    cc = customer_config
    retire = gateway_mode == "retire"

    if not site_name:
        site_name = customer_name.replace(" ", "-").lower() + "-site"

    central = CentralConfig(
        customer_name=customer_name,
        base_url=central_base_url.rstrip("/"),
        sites=[site_name],
        radius_servers=cc.radius_servers,
        gateways_retired=retire,
    )

    if retire:
        # Convert every tunneled SSID to bridge mode; discovery data stays
        # untouched (preflight still reasons about the original design).
        cc = replace(cc, ssids=[
            replace(s, forward_mode=ForwardMode.BRIDGE) for s in cc.ssids
        ])
    elif any(s.forward_mode in (ForwardMode.TUNNEL, ForwardMode.SPLIT) for s in cc.ssids):
        # GW cluster names must not contain spaces or start with "auto_"
        slug = re.sub(r"[^a-z0-9-]+", "-", customer_name.lower()).strip("-") or "migrated"
        central.gw_cluster_name = f"{slug}-cluster"

    groups = {}
    for ap_group in cc.ap_groups:
        has_tunnel = False
        has_bridge = False
        group_ssids = []
        for ssid_name in ap_group.ssids:
            ssid = cc.ssid_by_name(ssid_name)
            if ssid:
                group_ssids.append(ssid)
                if ssid.forward_mode in (ForwardMode.TUNNEL, ForwardMode.SPLIT):
                    has_tunnel = True
                if ssid.forward_mode in (ForwardMode.BRIDGE, ForwardMode.SPLIT):
                    has_bridge = True

        if not group_ssids:
            group_ssids = cc.ssids

        used_vlan_ids = {s.vlan for s in group_ssids}
        group_vlans = [v for v in cc.vlans if v.id in used_vlan_ids]

        cgc = CentralGroupConfig(
            name=ap_group.name,
            firmware_version=aos10_firmware,
            site_name=site_name,
            ssids=group_ssids,
            vlans=group_vlans,
            has_tunnel_ssid=has_tunnel,
            has_bridge_ssid=has_bridge,
        )
        groups[ap_group.name] = cgc
        central.groups.append(cgc)

    if not central.groups and cc.ssids:
        all_vlans = cc.vlans
        cgc = CentralGroupConfig(
            name=f"{customer_name.lower().replace(' ', '-')}-aps",
            firmware_version=aos10_firmware,
            site_name=site_name,
            ssids=cc.ssids,
            vlans=all_vlans,
            has_tunnel_ssid=any(s.forward_mode in (ForwardMode.TUNNEL, ForwardMode.SPLIT) for s in cc.ssids),
            has_bridge_ssid=any(s.forward_mode in (ForwardMode.BRIDGE, ForwardMode.SPLIT) for s in cc.ssids),
        )
        central.groups.append(cgc)

    return central
