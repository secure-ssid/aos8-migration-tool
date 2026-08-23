"""
Preflight compatibility and safety checks.
Returns a list of CheckResult items with pass/warn/fail status.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional
import re

from .models import AuthType, CustomerConfig, CentralConfig, ForwardMode
from .aos8_client import is_model_compatible

# ap convert is supported per release train — a build must be on one of these
# trains AND at or above the train's minimum (e.g. 8.11.x does NOT qualify).
SUPPORTED_TRAINS = {
    (8, 10): (8, 10, 0, 12),
    (8, 12): (8, 12, 0, 1),
}


class Status(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str
    detail: Optional[str] = None

    @property
    def icon(self) -> str:
        return {"pass": "✅", "warn": "⚠️", "fail": "❌"}[self.status]


def run_all(customer: CustomerConfig, central: CentralConfig) -> list[CheckResult]:
    results = []
    results += _check_ap_models(customer)
    results += _check_firmware(customer)
    results += _check_dhcp(customer)
    results += _check_vlan_tunnel_conflict(customer, central)
    results += _check_radius_nad(customer, central)
    results += _check_gateway_retirement(customer, central)
    results += _check_eap_offload(customer)
    results += _check_internal_auth(customer)
    results += _check_cluster(customer)
    results += _check_static_ips(customer)
    results += _check_ssid_mapping(customer)
    results += _check_serials(customer)
    results += _check_ssid_auth(customer, central)
    results += _check_captive_portal(customer, central)
    results += _check_named_vlans(customer)
    results += _check_split_tunnel(customer, central)
    results += _check_duplicate_essids(customer)
    results += _check_essid_limits(customer)
    return results


def _check_ap_models(customer: CustomerConfig) -> list[CheckResult]:
    incompatible, unknown = [], []
    for ap in customer.aps:
        if not ap.model:
            unknown.append(ap.name or ap.serial or "(unnamed AP)")
        elif not is_model_compatible(ap.model):
            incompatible.append(f"{ap.name} ({ap.model})")

    results = []
    if incompatible:
        results.append(CheckResult(
            name="AP Model Compatibility",
            status=Status.FAIL,
            message=f"{len(incompatible)} AP(s) do not support AOS 10 — hardware refresh required before migration.",
            detail="Incompatible APs:\n" + "\n".join(incompatible),
        ))
    else:
        known = len(customer.aps) - len(unknown)
        results.append(CheckResult(
            name="AP Model Compatibility",
            status=Status.PASS,
            message=f"All {known} APs with a known model support AOS 10.",
        ))
    if unknown:
        # a blank model can't be checked — surface it instead of silently
        # counting it as compatible
        results.append(CheckResult(
            name="AP Models Unknown",
            status=Status.WARN,
            message=f"{len(unknown)} AP(s) have no model in the discovery data — "
                    "compatibility could not be checked.",
            detail="APs without a model:\n" + "\n".join(unknown),
        ))
    return results


def _parse_firmware_tuple(version: str) -> Optional[tuple]:
    m = re.match(r"(\d+)\.(\d+)\.(\d+)\.(\d+)", version)
    if m:
        return tuple(int(x) for x in m.groups())
    m2 = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    if m2:
        return tuple(int(x) for x in m2.groups()) + (0,)
    return None


def _fw_ok(version: str) -> bool:
    parsed = _parse_firmware_tuple(version)
    if not parsed:
        return False
    minimum = SUPPORTED_TRAINS.get(parsed[:2])
    return minimum is not None and parsed >= minimum


def _check_firmware(customer: CustomerConfig) -> list[CheckResult]:
    fw = customer.mc_firmware
    if customer.source_type == "instant":
        parsed = _parse_firmware_tuple(fw)
        if parsed and parsed >= (8, 6, 0, 0):
            return [CheckResult(
                name="Instant Version",
                status=Status.PASS,
                message=f"Instant {fw} supports Central-driven conversion to AOS 10.",
            )]
        return [CheckResult(
            name="Instant Version",
            status=Status.WARN,
            message=f"Instant version {fw or 'unknown'} — verify the cluster runs "
                    "Instant 8.6+ (latest 8.10/8.12 recommended) before Central "
                    "pushes the AOS 10 image.",
        )]
    if fw == "unknown" or _parse_firmware_tuple(fw) is None:
        detected = "" if fw == "unknown" else f" (detected: {fw})"
        return [CheckResult(
            name="MC Firmware Version",
            status=Status.WARN,
            message=f"Could not fully detect MC firmware version{detected}. "
                    "Verify MC is running ≥ 8.10.0.12 (8.10 train) or ≥ 8.12.0.1 (8.12 train) "
                    "before running ap convert. In paste mode, include `show version` output.",
        )]
    if _fw_ok(fw):
        return [CheckResult(
            name="MC Firmware Version",
            status=Status.PASS,
            message=f"MC firmware {fw} meets minimum requirement for ap convert.",
        )]
    return [CheckResult(
        name="MC Firmware Version",
        status=Status.FAIL,
        message=f"MC firmware {fw} does not support ap convert. Upgrade to ≥ 8.10.0.12 "
                "(8.10 train) or ≥ 8.12.0.1 (8.12 train) first — interim trains like 8.11 do not qualify.",
        detail="After upgrading MC firmware, run 'write erase' + reload if migrating an MC that had prior upgrades.",
    )]


def _check_dhcp(customer: CustomerConfig) -> list[CheckResult]:
    static_aps = [ap for ap in customer.aps if ap.has_static_ip]
    if static_aps:
        names = [f"{ap.name} ({ap.ip})" for ap in static_aps]
        return [CheckResult(
            name="AP DHCP Requirement",
            status=Status.FAIL,
            message=f"{len(static_aps)} AP(s) have static IPs. AOS 10 requires DHCP for all APs.",
            detail="Static IP APs:\n" + "\n".join(names),
        )]
    # Static-IP provisioning isn't visible in `show ap database long` or the
    # objects this tool reads — keep this an explicit manual gate, not a PASS.
    return [CheckResult(
        name="AP DHCP Requirement",
        status=Status.WARN,
        message="Static-IP detection is not automated. Manually confirm no APs are "
                "provisioned with static IPs before conversion — AOS 10 conversion "
                "requires DHCP (+DNS) on the AP management VLAN.",
        detail="Check per-AP provisioning on the MC: show ap provisioning ap-name <name> "
               "(look for a static inner IP). Re-provision any static-IP APs for DHCP first.",
    )]


def _check_vlan_tunnel_conflict(customer: CustomerConfig,
                                central: CentralConfig) -> list[CheckResult]:
    if central.gateways_retired:
        # Everything becomes bridge mode — the tunnel/bridge port guidance
        # is replaced by the retirement check below.
        return []
    # Gateways kept: split-tunnel SSIDs are provisioned as FULL L2 overlay
    # (see _check_split_tunnel), so their client VLANs tunnel post-migration.
    tunnel_vlans = {
        s.vlan for s in customer.ssids
        if s.forward_mode in (ForwardMode.TUNNEL, ForwardMode.SPLIT)
    }
    bridge_vlans = {
        s.vlan for s in customer.ssids
        if s.forward_mode in (ForwardMode.BRIDGE,)
    }
    conflicts = tunnel_vlans & bridge_vlans
    if conflicts:
        return [CheckResult(
            name="Tunnel/Bridge VLAN Conflict",
            status=Status.WARN,
            message=f"VLANs {sorted(conflicts)} are used by both tunnel and bridge SSIDs.",
            detail=(
                "AP switch ports must trunk bridge data VLANs but PRUNE tunnel client VLANs. "
                "Set AP port as trunk with native = AP management VLAN, allowed = bridge data VLANs only."
            ),
        )]
    if tunnel_vlans:
        return [CheckResult(
            name="Tunnel/Bridge VLAN Check",
            status=Status.WARN,
            message=f"Tunnel (and split-tunnel) SSIDs use VLANs {sorted(tunnel_vlans)}. Ensure these VLANs do NOT appear on AP switch ports.",
            detail="AP switch ports should be access ports on the AP management VLAN only (no tunnel data VLANs).",
        )]
    return [CheckResult(
        name="Tunnel/Bridge VLAN Check",
        status=Status.PASS,
        message="No tunnel/bridge VLAN conflicts detected.",
    )]


def _check_radius_nad(customer: CustomerConfig,
                      central: CentralConfig) -> list[CheckResult]:
    if not customer.radius_servers:
        return []
    server_list = ", ".join(s.name for s in customer.radius_servers)
    if customer.source_type == "instant":
        return [CheckResult(
            name="RADIUS NAD Update",
            status=Status.WARN,
            message=f"RADIUS servers found: {server_list}. Instant typically sources "
                    "RADIUS from the VC IP (with dynamic RADIUS proxy) — after AOS 10 "
                    "conversion each AP authenticates directly. Add the AP management "
                    "subnet(s) as NAD network ranges before converting.",
            detail=f"Old NAD: VC IP {customer.mc_ip} (if dynamic-radius-proxy was enabled; "
                   "otherwise APs were already individual NADs)\n"
                   "New NADs: the AP management subnet(s) — add as network-range entries.",
        )]
    if central.gateways_retired:
        had_tunnel = any(s.forward_mode in (ForwardMode.TUNNEL, ForwardMode.SPLIT)
                         for s in customer.ssids)
        if not had_tunnel:
            # bridge-only design: the APs were ALREADY the RADIUS NADs — the
            # MC was never the client for these SSIDs, so nothing to replace
            return [CheckResult(
                name="RADIUS NAD Update",
                status=Status.PASS,
                message=f"RADIUS servers found: {server_list}. All SSIDs were already "
                        "bridge mode, so the APs are already the RADIUS clients — no "
                        "NAD changes required.",
            )]
        return [CheckResult(
            name="RADIUS NAD Update Required",
            status=Status.WARN,
            message=f"RADIUS servers found: {server_list}. With gateways retired, every AP "
                    "authenticates clients directly — APs become the RADIUS clients. Add the "
                    "AP management subnet(s) as NAD network ranges BEFORE running ap convert.",
            detail=(
                f"Old RADIUS client (tunnel SSIDs): {customer.mc_ip} (MC management IP)\n"
                "New RADIUS clients: the AP management subnet(s) — add as a network-range "
                "NAD entry in ClearPass (per-AP entries don't scale)\n"
                "Use a consistent RADIUS secret for the whole range."
            ),
        )]
    return [CheckResult(
        name="RADIUS NAD Update Required",
        status=Status.WARN,
        message=f"RADIUS servers found: {server_list}. After GW provisioning, add the GW management IP as a new RADIUS client in ClearPass/RADIUS BEFORE running ap convert.",
        detail=(
            f"Old RADIUS client: {customer.mc_ip} (MC management IP)\n"
            "New RADIUS client: GW management IP (set after GW comes online)\n"
            "For bridge-mode SSIDs: each AP management IP will also be a RADIUS client."
        ),
    )]


def _check_gateway_retirement(customer: CustomerConfig,
                              central: CentralConfig) -> list[CheckResult]:
    if not central.gateways_retired:
        return []
    former_tunnel = sorted({
        s.vlan for s in customer.ssids
        if s.forward_mode in (ForwardMode.TUNNEL, ForwardMode.SPLIT)
    })
    if not former_tunnel:
        return [CheckResult(
            name="Gateway Retirement",
            status=Status.PASS,
            message="Gateways retired — all SSIDs were already bridge mode, no switchport changes needed.",
        )]
    return [CheckResult(
        name="Gateway Retirement — Switchport Changes Required",
        status=Status.WARN,
        message=f"Tunnel SSIDs are being converted to bridge mode. Client VLANs "
                f"{former_tunnel} previously terminated on the MC — they must now be "
                "trunked to every AP switchport BEFORE conversion.",
        detail=(
            "Per AP switchport: trunk mode, native = AP management VLAN, "
            f"allowed = {', '.join(str(v) for v in former_tunnel)}\n"
            "DHCP for these VLANs must be reachable at the edge (no more MC relay).\n"
            "Roaming becomes L2 only — verify the client VLANs span the areas where "
            "clients roam, or split SSIDs per site/floor.\n"
            "Firewall policies enforced on the MC move to the AP role policies "
            "created during provisioning."
        ),
    )]


def _check_eap_offload(customer: CustomerConfig) -> list[CheckResult]:
    if customer.has_eap_offload:
        return [CheckResult(
            name="EAP-Offload / FastConnect",
            status=Status.FAIL,
            message="EAP-Offload (AAA FastConnect) is configured but NOT supported in AOS 10. Must be redesigned before migration.",
            detail="Remove AAA FastConnect config from all VAP/AAA profiles. Use standard 802.1X instead.",
        )]
    return [CheckResult(
        name="EAP-Offload / FastConnect",
        status=Status.PASS,
        message="No EAP-Offload configuration detected.",
    )]


def _check_internal_auth(customer: CustomerConfig) -> list[CheckResult]:
    if customer.has_internal_auth:
        return [CheckResult(
            name="Internal Authentication Server",
            status=Status.FAIL,
            message="MC internal auth server is in use but NOT supported in AOS 10. Must migrate to external RADIUS (ClearPass/NPS) before migration.",
        )]
    return [CheckResult(
        name="Internal Authentication Server",
        status=Status.PASS,
        message="No internal auth server detected.",
    )]


def _check_cluster(customer: CustomerConfig) -> list[CheckResult]:
    if customer.source_type == "instant":
        return []  # no controllers to sequence
    cluster = customer.cluster
    if cluster is None:
        return [CheckResult(
            name="Controller Cluster",
            status=Status.PASS,
            message="Single controller — no cluster migration sequencing required.",
        )]
    if cluster.type == "L2":
        return [CheckResult(
            name="Controller Cluster (L2)",
            status=Status.WARN,
            message=f"L2 cluster with {len(cluster.members)} members detected. Must use L2 cluster upgrade sequence: move all APs to MC1 first, then upgrade MC2, then convert APs, then upgrade MC1.",
            detail="Members: " + ", ".join(cluster.members),
        )]
    return [CheckResult(
        name="Controller Cluster (L3)",
        status=Status.WARN,
        message=f"L3 cluster detected ({len(cluster.members)} members). Each MC can be upgraded independently. Upgrade one at a time.",
        detail="Members: " + ", ".join(cluster.members),
    )]


def _check_static_ips(customer: CustomerConfig) -> list[CheckResult]:
    if not customer.aps:
        return [CheckResult(
            name="AP Inventory",
            status=Status.WARN,
            message="No APs detected in discovery. Ensure MC is active and APs are associated.",
        )]
    return [CheckResult(
        name="AP Inventory",
        status=Status.PASS,
        message=f"{len(customer.aps)} APs discovered across {len(customer.ap_groups)} AP group(s).",
    )]


def _check_ssid_mapping(customer: CustomerConfig) -> list[CheckResult]:
    if customer.source_type == "instant":
        if customer.ssid_mapping_incomplete:
            return [CheckResult(
                name="SSID → Zone Mapping",
                status=Status.WARN,
                message="Some SSIDs are zoned to a zone with no checked-in AP — they "
                        "were parked in the 'instant-default' group so they aren't "
                        "lost. Verify the zone names (typos/case) and which group "
                        "should really broadcast them.",
            )]
        return []  # zones resolved cleanly (or cluster-wide) — no vap bindings
    if customer.ssid_mapping_incomplete:
        return [CheckResult(
            name="SSID → AP-Group Mapping",
            status=Status.WARN,
            message="SSID-to-group bindings could not be fully discovered for at least one "
                    "AP group — ALL SSIDs were assigned to those groups as a fallback.",
            detail="Review the per-group SSID lists in Step 1 before provisioning. In paste "
                   "mode, make sure the full `show running-config` (including ap-group blocks "
                   "with their virtual-ap lines) was pasted.",
        )]
    return [CheckResult(
        name="SSID → AP-Group Mapping",
        status=Status.PASS,
        message="Per-group SSID bindings discovered from virtual-ap configuration.",
    )]


def _check_serials(customer: CustomerConfig) -> list[CheckResult]:
    missing = [ap.name for ap in customer.aps if not ap.serial]
    if missing:
        return [CheckResult(
            name="AP Serial Numbers",
            status=Status.WARN,
            message=f"{len(missing)} AP(s) have no serial number — they cannot be "
                    "pre-assigned to groups/sites, and Step 6 validation will not "
                    "be able to match them in Central.",
            detail="Paste `show ap database long` output (it includes the Serial # column), "
                   "or use API mode.\nAffected: " + ", ".join(missing[:20]) +
                   (" …" if len(missing) > 20 else ""),
        )]
    return [CheckResult(
        name="AP Serial Numbers",
        status=Status.PASS,
        message="All discovered APs have serial numbers.",
    )]


def _check_named_vlans(customer: CustomerConfig) -> list[CheckResult]:
    unresolved = [(s.display_name, s.vlan_raw, s.vlan)
                  for s in customer.ssids if s.vlan_raw]
    if not unresolved:
        return []
    detail = "\n".join(
        f"{n}: VLAN token '{raw}' → collapsed to VLAN {vid}"
        for n, raw, vid in unresolved)
    return [CheckResult(
        name="Named VLANs Unresolved",
        status=Status.FAIL,
        message=f"{len(unresolved)} SSID(s) reference a named VLAN or a VLAN "
                "pool/range that couldn't be resolved to a single VLAN ID — "
                "they would provision onto the wrong VLAN.",
        detail=detail + "\nLook up the named VLAN's ID on the MC "
               "(show vlan / show running-config | include vlan-name) and fix the "
               "VLAN before provisioning. For a pool/range, pick the VLAN the "
               "migrated SSID should actually use.",
    )]


def _check_split_tunnel(customer: CustomerConfig,
                        central: CentralConfig) -> list[CheckResult]:
    split = [s.display_name for s in customer.ssids
             if s.forward_mode == ForwardMode.SPLIT]
    if not split:
        return []
    if central.gateways_retired:
        return [CheckResult(
            name="Split-Tunnel SSIDs",
            status=Status.WARN,
            message=f"Split-tunnel SSIDs ({', '.join(split)}) become full BRIDGE mode "
                    "with gateways retired — all client traffic forwards locally. "
                    "Verify no flows depended on the tunneled leg.",
        )]
    return [CheckResult(
        name="Split-Tunnel SSIDs",
        status=Status.WARN,
        message=f"Split-tunnel SSIDs ({', '.join(split)}) will be provisioned as FULL "
                "L2 overlay (all client traffic tunnels to the gateway). AOS 10 mixed "
                "forwarding per-SSID differs from AOS 8 split-tunnel — review traffic "
                "paths before cutover.",
    )]


def _check_duplicate_essids(customer: CustomerConfig) -> list[CheckResult]:
    by_essid: dict[str, list] = {}
    for s in customer.ssids:
        by_essid.setdefault(s.display_name, []).append(s)
    conflicts, benign = [], []
    for essid, group in by_essid.items():
        if len(group) < 2:
            continue
        settings = {(s.vlan, s.forward_mode, s.auth_type, s.psk) for s in group}
        if len(settings) > 1:
            conflicts.append(f"{essid}: {len(group)} virtual-aps with DIFFERENT "
                             "vlan/forward-mode/auth")
        else:
            benign.append(essid)
    results = []
    if conflicts:
        results.append(CheckResult(
            name="Conflicting Duplicate ESSIDs",
            status=Status.FAIL,
            message="Central keys WLANs by ESSID — virtual-aps sharing an ESSID with "
                    "different settings cannot coexist. Only the FIRST definition would "
                    "be provisioned.",
            detail="\n".join(conflicts) + "\nRename the ESSIDs or consolidate the "
                   "virtual-aps before provisioning.",
        ))
    if benign:
        results.append(CheckResult(
            name="Duplicate ESSIDs (same settings)",
            status=Status.PASS,
            message=f"ESSIDs served by multiple identical virtual-aps ({', '.join(benign)}) "
                    "are consolidated into one Central WLAN bound to each group.",
        ))
    return results


def _check_essid_limits(customer: CustomerConfig) -> list[CheckResult]:
    too_long = [s.display_name for s in customer.ssids if len(s.display_name) > 32]
    if too_long:
        return [CheckResult(
            name="ESSID Length",
            status=Status.FAIL,
            message=f"ESSIDs over the 32-character limit: {', '.join(too_long)} — "
                    "Central will reject these. Shorten before provisioning.",
        )]
    return []


def _check_captive_portal(customer: CustomerConfig,
                          central: CentralConfig) -> list[CheckResult]:
    """Classic Central's full_wlan API has no external-captive-portal field
    this tool can populate, so a guest SSID would be provisioned wide open."""
    if central.destination != "classic":
        return []
    cp_ssids = [s.display_name for s in customer.ssids if s.captive_portal_url]
    if not cp_ssids:
        return []
    return [CheckResult(
        name="Captive-Portal SSIDs (classic destination)",
        status=Status.FAIL,
        message=f"External captive-portal SSIDs: {', '.join(cp_ssids)}. Classic "
                "Central's WLAN API cannot express an external portal — these "
                "would be created as fully OPEN guest networks.",
        detail="Migrate these SSIDs to New Central, or build the captive-portal "
               "profile by hand in Classic and bind it before enabling the SSID.",
    )]


def _check_ssid_auth(customer: CustomerConfig,
                     central: CentralConfig) -> list[CheckResult]:
    results = []
    wep = [s.display_name for s in customer.ssids if s.auth_type == AuthType.WEP]
    if wep:
        results.append(CheckResult(
            name="WEP SSIDs Unsupported",
            status=Status.FAIL,
            message=f"WEP SSIDs: {', '.join(wep)}. AOS 10 has no WEP opmode — "
                    "migrating them would silently change the network's "
                    "security (or fail at the API).",
            detail="Re-key these networks to WPA2 or WPA3 on the source "
                   "before migrating. WEP clients (legacy scanners, printers) "
                   "need a hardware or firmware refresh — there is no safe "
                   "mapping.",
        ))
    mac = [s for s in customer.ssids if s.auth_type == AuthType.MAC]
    mac_no_group = [s.display_name for s in mac if not s.auth_server_group]
    if mac_no_group:
        results.append(CheckResult(
            name="MAC-Auth SSIDs Without RADIUS",
            status=Status.FAIL,
            message=f"MAC-auth SSIDs with no discovered server group: "
                    f"{', '.join(mac_no_group)}. They would migrate as OPEN "
                    "networks with MAC authentication effectively disabled.",
            detail="Confirm the aaa-profile's mac-server-group on the MC "
                   "(show aaa profile) and that the group holds real RADIUS "
                   "servers before provisioning.",
        ))
    mac_ok = [s.display_name for s in mac if s.auth_server_group]
    if mac_ok:
        results.append(CheckResult(
            name="MAC-Auth SSIDs",
            status=Status.WARN,
            message=f"MAC-auth SSIDs: {', '.join(mac_ok)}. They migrate with "
                    "MAC authentication enabled (opmode OPEN + RADIUS "
                    "binding) — MAC auth is only as strong as the MAC address.",
            detail="Verify the server group resolves on the destination and "
                   "that every legitimate client's MAC is registered; "
                   "MAC-only auth is trivially spoofable.",
        ))
    owe = [s.display_name for s in customer.ssids if s.auth_type == AuthType.OWE]
    if owe and central.destination == "classic":
        results.append(CheckResult(
            name="Enhanced Open (OWE) SSIDs",
            status=Status.FAIL,
            message=f"OWE / Enhanced-Open SSIDs: {', '.join(owe)}. Classic AOS10 "
                    "has no Enhanced-Open opmode — these SSIDs would migrate "
                    "unencrypted.",
            detail="New Central supports these natively (opmode ENHANCED_OPEN). "
                   "Target New Central for them, or accept and document the "
                   "downgrade explicitly before provisioning.",
        ))
    unknown = [s.display_name for s in customer.ssids if not s.auth_known]
    if unknown:
        results.append(CheckResult(
            name="SSID Auth Detection",
            status=Status.WARN,
            message=f"Auth type could not be determined for: {', '.join(unknown)}. "
                    "They will be provisioned as WPA2-Enterprise — verify before cutover.",
            detail="In paste mode, ensure the wlan ssid-profile blocks (with opmode) are "
                   "included in the running-config paste.",
        ))
    psk_missing = [s.display_name for s in customer.ssids
                   if s.auth_type in (AuthType.WPA2_PSK, AuthType.WPA3_SAE) and not s.psk]
    if psk_missing:
        results.append(CheckResult(
            name="PSK Passphrases",
            status=Status.WARN,
            message=f"PSK SSIDs without a recovered passphrase: {', '.join(psk_missing)}. "
                    "Provisioning will create them, but you must set the passphrase in Central.",
        ))
    enterprise = [s.display_name for s in customer.ssids
                  if s.auth_type in (AuthType.WPA2_ENTERPRISE, AuthType.WPA3_ENTERPRISE)]
    if enterprise and not customer.radius_servers:
        results.append(CheckResult(
            name="802.1X SSIDs Without RADIUS Servers",
            status=Status.FAIL,
            message=f"Enterprise SSIDs ({', '.join(enterprise)}) but ZERO "
                    "RADIUS servers were discovered — they would provision as "
                    "dot1x networks with nothing to authenticate against.",
            detail="Confirm the RADIUS servers exist on the source "
                   "(show aaa authentication-server radius) and that "
                   "discovery can read them, before provisioning.",
        ))
    elif (enterprise or [s for s in mac if s.auth_server_group]) \
            and central.destination == "classic":
        mac_named = [s.display_name for s in mac if s.auth_server_group]
        # the gate covers MAC-only SSID sets too: the Classic client emits
        # the same dangling auth_server1 reference for MAC-auth WLANs
        kinds = []
        if enterprise:
            kinds.append(f"Enterprise SSIDs ({', '.join(enterprise)})")
        if mac_named:
            kinds.append(f"MAC-auth SSIDs ({', '.join(mac_named)})")
        results.append(CheckResult(
            name="Classic RADIUS Servers (manual step)",
            status=Status.FAIL,
            message=f"{' and '.join(kinds)} on a Classic "
                    "destination: the Classic provisioning path cannot create "
                    "RADIUS server objects (no public API) — full_wlan "
                    "references the auth server BY NAME, so the reference "
                    "dangles until the server exists.",
            detail="In each Classic group, create a RADIUS auth server named "
                   "EXACTLY like the source server group "
                   f"({', '.join(sorted({s.auth_server_group for s in customer.ssids if s.auth_type in (AuthType.WPA2_ENTERPRISE, AuthType.WPA3_ENTERPRISE, AuthType.MAC) and s.auth_server_group})) or 'see source config'}) "
                   "with the real host/secret, then proceed.",
        ))
    elif enterprise:
        results.append(CheckResult(
            name="802.1X SSIDs",
            status=Status.WARN,
            message=f"Enterprise SSIDs ({', '.join(enterprise)}): provisioning binds a "
                    "RADIUS server-group to them (New Central), but the shared secrets are "
                    "placeholders — set the real secrets in Central, and add the new GW/AP "
                    "IPs as RADIUS clients (see NAD check).",
        ))
    if not results:
        results.append(CheckResult(
            name="SSID Auth Detection",
            status=Status.PASS,
            message="Auth types resolved for all SSIDs.",
        ))
    return results
