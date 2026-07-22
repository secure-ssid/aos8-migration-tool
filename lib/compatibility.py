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
    results += _check_ssid_auth(customer)
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
                    "pre-assigned to groups/sites, and Step 5 validation will not "
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
    named = [(s.display_name, s.vlan_raw) for s in customer.ssids if s.vlan_raw]
    if not named:
        return []
    detail = "\n".join(f"{n}: VLAN token '{raw}' → defaulted to VLAN 1" for n, raw in named)
    return [CheckResult(
        name="Named VLANs Unresolved",
        status=Status.FAIL,
        message=f"{len(named)} SSID(s) reference a named VLAN pool that couldn't be "
                "resolved to a VLAN ID — they would provision onto VLAN 1.",
        detail=detail + "\nLook up the named VLAN's ID on the MC "
               "(show vlan / show running-config | include vlan-name) and fix the "
               "VLAN before provisioning.",
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


def _check_ssid_auth(customer: CustomerConfig) -> list[CheckResult]:
    results = []
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
    if enterprise:
        results.append(CheckResult(
            name="802.1X SSIDs",
            status=Status.WARN,
            message=f"Enterprise SSIDs ({', '.join(enterprise)}) will need their RADIUS "
                    "auth server attached in Central after provisioning, and the new GW/AP "
                    "IPs added as RADIUS clients (see NAD check).",
        ))
    if not results:
        results.append(CheckResult(
            name="SSID Auth Detection",
            status=Status.PASS,
            message="Auth types resolved for all SSIDs.",
        ))
    return results
