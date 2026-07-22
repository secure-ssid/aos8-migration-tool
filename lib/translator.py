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

    slug = re.sub(r"[^a-z0-9-]+", "-", customer_name.lower()).strip("-") or "migrated"

    # AOS 8 groups like "default"/"NoAuthApGroup" are generic — migrate their
    # APs into a NEW, customer-specific Central device group rather than reusing
    # a bare "default" group in the tenant. The AOS 8 name is preserved as
    # source_group for the `ap convert` runbook and the serial lookup.
    _GENERIC = {"default", "default-aps", "noauthapgroup", ""}

    def central_group_name(aos8_name: str) -> str:
        return f"{slug}-aps" if aos8_name.strip().lower() in _GENERIC else aos8_name

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
            if not ap_group.ssids:
                # no VAP bindings were discovered for this group at all —
                # mirror discovery's assign-everything fallback, and make sure
                # preflight surfaces it (the API-pull path reaches here
                # without the parser having set the flag)
                group_ssids = cc.ssids
                customer_config.ssid_mapping_incomplete = True
            else:
                # bindings exist but none resolved to a discovered SSID
                # (dangling refs) — flooding every SSID in would silently
                # broadcast the wrong networks; keep the group empty and
                # surface it through the preflight mapping warning instead
                customer_config.ssid_mapping_incomplete = True

        used_vlan_ids = {s.vlan for s in group_ssids}
        group_vlans = [v for v in cc.vlans if v.id in used_vlan_ids]

        central_name = central_group_name(ap_group.name)
        existing = next((g for g in central.groups if g.name == central_name), None)
        if existing is not None:
            # Two generic source groups map to the same Central group — merge
            # instead of provisioning the same group twice. Serials from every
            # folded source group are picked up via extra_source_groups.
            existing.extra_source_groups.append(ap_group.name)
            known = {s.name for s in existing.ssids}
            existing.ssids.extend(s for s in group_ssids if s.name not in known)
            known_vlans = {v.id for v in existing.vlans}
            existing.vlans.extend(v for v in group_vlans if v.id not in known_vlans)
            existing.has_tunnel_ssid = existing.has_tunnel_ssid or has_tunnel
            existing.has_bridge_ssid = existing.has_bridge_ssid or has_bridge
            groups[ap_group.name] = existing
            continue
        cgc = CentralGroupConfig(
            name=central_name,
            source_group=ap_group.name,
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
            name=f"{slug}-aps",
            source_group="",
            firmware_version=aos10_firmware,
            site_name=site_name,
            ssids=cc.ssids,
            vlans=all_vlans,
            has_tunnel_ssid=any(s.forward_mode in (ForwardMode.TUNNEL, ForwardMode.SPLIT) for s in cc.ssids),
            has_bridge_ssid=any(s.forward_mode in (ForwardMode.BRIDGE, ForwardMode.SPLIT) for s in cc.ssids),
        )
        central.groups.append(cgc)

    return central
