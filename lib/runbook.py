"""
Generates a customer-specific ap convert CLI runbook.

`ap convert` syntax verified against the AOS-W 8.x CLI Reference Guide:
    ap convert add {ap-group <grp> | ap-name <name>}
    ap convert pre-validate {all-aps | specific-aps}      (8.10+)
    ap convert cancel
    ap convert active {all-aps | specific-aps}
               {activate | local-flash | server {http|https|ftp|scp|tftp} ...}
The `activate` source needs no image names — APs fetch their own AOS 10
image from Aruba Activate, so it is the primary path in this runbook.
"""
from datetime import date

from .models import CustomerConfig, CentralConfig, ForwardMode, AuthType
from .compatibility import _fw_ok


def _manual_secrets_block(customer: CustomerConfig) -> list[str]:
    """Checklist of secrets that AOS 8 stores encrypted (RADIUS keys, hashed
    PSKs) — created with placeholders and MUST be set by hand in Central."""
    from .central_client import secret_looks_unusable
    radius = [s.name for s in customer.radius_servers]
    psk = sorted({(s.essid or s.name) for s in customer.ssids
                  if s.auth_type in (AuthType.WPA2_PSK, AuthType.WPA3_SAE)
                  and secret_looks_unusable(s.psk)})
    if not radius and not psk:
        return []
    out = ["", "MANUAL — SET SECRETS IN CENTRAL", "─" * 40,
           "AOS 8 stores these encrypted, so the tool created them with",
           "placeholders. Set the real values in New Central before go-live:"]
    for r in radius:
        out.append(f"  [ ] RADIUS '{r}': set shared secret "
                   "(Config → Authentication → Servers)")
    for p in psk:
        out.append(f"  [ ] SSID '{p}': set WPA passphrase "
                   f"(Config → WLANs → {p} → Security)")
    out.append("")
    return out

# AP model → AOS 10 image family codename, used only for the manual-server
# alternative. Only confidently-known families are mapped; anything else gets
# an explicit placeholder so a wrong image is never pasted into a controller.
MODEL_FAMILIES = {
    "303": "Ursa", "304": "Ursa", "305": "Ursa",
    "504": "Scorpio", "505": "Scorpio",
    "535": "Norma", "555": "Norma",
}


def generate(customer: CustomerConfig, central: CentralConfig, customer_name: str = "") -> str:
    name = customer_name or central.customer_name or "Customer"
    today = date.today().strftime("%Y-%m-%d")
    target_fw = central.groups[0].firmware_version if central.groups else "10.x"

    if customer.source_type == "instant":
        return _generate_instant(customer, central, name, today, target_fw)
    fw_ok = _fw_ok(customer.mc_firmware)
    fw_status = "✓" if fw_ok else "!"
    fw_note = "" if fw_ok else "  ⚠️  MUST upgrade MC firmware before running ap convert!"

    lines = [
        "═" * 60,
        "  AOS 8 → AOS 10 AP Conversion Runbook",
        f"  Customer: {name}",
        f"  Generated: {today}",
        f"  Target AOS 10 release: {target_fw}",
        "═" * 60,
        "",
        "PRE-REQUISITES",
        "─" * 40,
        f"{fw_status} MC firmware: {customer.mc_firmware} "
        f"(minimum: 8.10.0.12 on 8.10 / 8.12.0.1 on 8.12){fw_note}",
        "✓ APs must reach DHCP + DNS + Internet (Activate + Central) after conversion",
    ]

    had_tunnel = any(s.forward_mode in (ForwardMode.TUNNEL, ForwardMode.SPLIT)
                     for s in customer.ssids)
    if customer.radius_servers and central.gateways_retired and had_tunnel:
        lines.append(
            f"! Update RADIUS/ClearPass: add the AP management subnet(s) as NAD "
            f"network-range clients (replacing MC IP {customer.mc_ip}) BEFORE converting APs"
        )
    elif customer.radius_servers and central.gateways_retired:
        lines.append(
            "✓ All SSIDs were already bridge mode — APs are already the RADIUS NADs, "
            "no ClearPass client changes needed"
        )
    elif customer.radius_servers:
        lines.append(
            f"! Update RADIUS/ClearPass: add GW management IP as new NAD client "
            f"(replacing MC IP {customer.mc_ip}) BEFORE converting APs"
        )
    if central.gateways_retired:
        lines.append(
            "! GATEWAYS RETIRED: all SSIDs go bridge mode — trunk the former tunnel "
            "client VLANs to every AP switchport BEFORE converting (see Preflight)"
        )
    lines += [
        "✓ Complete Steps 1–4 in the migration tool first:",
        "    Central provisioned (Step 3) + APs claimed & subscribed in GreenLake (Step 4)",
        "",
        "NOTE — auto-conversion: assigning the APs to their AOS 10 device group in",
        "Central (Step 4 'Move APs into groups') makes Central PUSH the conversion",
        "automatically — the APs reboot into AOS 10 (~10-20 min offline) WITHOUT you",
        "running 'ap convert'. The commands below are the controller-driven path for",
        "APs that don't auto-convert (e.g. unreachable from Central). Do one or the",
        "other, inside a maintenance window.",
        "",
    ]

    cluster = customer.cluster
    if cluster and len(cluster.members) >= 2 and cluster.type == "L2":
        _write_l2_cluster_steps(lines, customer, central, cluster)
    elif cluster and len(cluster.members) >= 2:
        _write_l3_cluster_steps(lines, customer, central, cluster)
    else:
        _write_single_mc_steps(lines, customer, central)

    lines += _manual_secrets_block(customer)

    lines += [
        "",
        "POST-CONVERSION",
        "─" * 40,
        "1. Verify APs appear in Central → Devices → Access Points (10–20 min per AP)",
        "2. Verify SSIDs are broadcasting (check with a test client)",
        "3. Verify RADIUS authentication works with a test user",
        "4. Return to the migration tool → Step 6: Validate",
        "5. Decommission Mobility Conductor and AirWave after validation",
        "",
        "ROLLBACK (per AP — must be done individually on AP console):",
        "  convert-aos-ap cap <mc-ip>",
        "  (AP downloads AOS 8 image from MC and reboots)",
        "",
        "═" * 60,
    ]

    return "\n".join(lines)


def _convert_block(customer: CustomerConfig, central: CentralConfig) -> list[str]:
    groups = [g.name for g in customer.ap_groups] or ["<ap-group-name>"]
    out = []
    for grp in groups:
        out.append(f"  ap convert add ap-group {grp}")
    out += [
        "",
        "  # Recommended: test with a single AP first:",
        "  # ap convert clear-all",
        "  # ap convert add ap-name <test-ap-name>",
        "",
        "  ap convert pre-validate specific-aps",
        "  show ap convert-status          # wait for 'Pre Validate Success'",
        "  ap convert cancel",
        "",
        "  # Primary path — APs fetch their AOS 10 image from Aruba Activate:",
        "  ap convert active specific-aps activate",
        "",
    ]
    alt = _manual_server_alternative(customer, central)
    if alt:
        out += alt
    out += [
        "  show ap convert-status          # monitor — takes 10–20 min per AP",
        "",
    ]
    return out


def _manual_server_alternative(customer: CustomerConfig, central: CentralConfig) -> list[str]:
    """Comment block for the manual image-server path (air-gapped Activate)."""
    models = sorted({ap.model for ap in customer.aps if ap.model})
    if not models:
        return []
    target_fw = central.groups[0].firmware_version if central.groups else "10.x.x.x"
    images, unmapped = [], []
    for model in models:
        digits = "".join(ch for ch in model if ch.isdigit())
        family = MODEL_FAMILIES.get(digits)
        if family:
            images.append(f"ArubaOS_{family}_{target_fw}")
        else:
            unmapped.append(model)
    images = sorted(set(images))
    out = ["  # Alternative (no Activate reachability) — host images on a local server:"]
    if images:
        out.append("  # ap convert active specific-aps server http <server-ip> \\")
        out.append("  #   path <path-to-images> " + ";".join(images))
    for model in unmapped:
        out.append(f"  #   {model}: verify the AOS 10 image family for this model "
                   f"— do NOT guess image names")
    out.append("")
    return out


def _write_single_mc_steps(lines: list, customer: CustomerConfig, central: CentralConfig) -> None:
    if central.gateways_retired:
        lines += [
            "MC ROLE",
            "─" * 40,
            "  Gateways are being RETIRED — the MC is only needed to drive ap convert.",
            "  Decommission it after all APs are online in Central (POST-CONVERSION).",
            "",
        ]
    else:
        lines += [
            "MC → GATEWAY CONVERSION",
            "─" * 40,
            "Run on MC console (or SSH to MC):",
            "",
            "  # If MC had prior firmware upgrades, load AOS 10 image first:",
            "  # upgrade-firmware <image> ; write erase ; reload",
            "  #",
            "  # If factory-new MC (AOS 8.6+, never upgraded): ZTP will handle it.",
            "",
        ]
    lines += [
        "AP CONVERSION",
        "─" * 40,
        "Run on the active MC CLI:",
        "",
    ]
    lines += _convert_block(customer, central)


def _write_l2_cluster_steps(lines: list, customer: CustomerConfig,
                            central: CentralConfig, cluster) -> None:
    mc1 = cluster.members[0] if cluster.members else "<mc1-ip>"
    # Every non-anchor member — L2 clusters support up to 12 members, and ALL
    # of them must be converted/decommissioned, not just MC2.
    others = cluster.members[1:] if len(cluster.members) > 1 else ["<mc2-ip>"]
    others_label = " · ".join(others)

    if central.gateways_retired:
        lines += [
            f"L2 CLUSTER SEQUENCE — GATEWAYS RETIRED ({len(cluster.members)} members)",
            "─" * 40,
            "",
            f"STEP 1 — Move all APs to MC1 ({mc1}):",
            f"  apmove all target-v4 {mc1}",
            "  show ap active                  # confirm all APs are on MC1",
            "",
            f"STEP 2 — Leave the other member(s) ({others_label}) online but idle — rollback targets",
            "  (convert-aos-ap cap needs a live AOS 8 MC). No GW conversion needed.",
            "",
            f"STEP 3 — Run ap convert on MC1 ({mc1}):",
            "",
        ]
        lines += _convert_block(customer, central)
        lines += [
            "STEP 4 — After all APs are online in Central and validated (Step 6):",
            f"  decommission MC1 ({mc1}) and the other member(s) ({others_label}).",
            "",
        ]
        return
    lines += [
        f"L2 CLUSTER UPGRADE SEQUENCE ({len(cluster.members)} members)",
        "─" * 40,
        "Follow this order exactly — converting all members at once strands APs.",
        "",
        f"STEP 1 — Move all APs to MC1 ({mc1}):",
        f"  apmove all target-v4 {mc1}",
        "  show ap active                  # confirm all APs are on MC1",
        "",
        "STEP 2 — Convert every member EXCEPT MC1 to Gateway:",
        *(f"  MC{i} ({ip})" for i, ip in enumerate(others, start=2)),
        "  (Complete GW ZTP or Static Activate — see Gateway migration in Step 5)",
        "",
        f"STEP 3 — Run ap convert on MC1 ({mc1}):",
        "",
    ]
    lines += _convert_block(customer, central)
    lines += [
        f"STEP 4 — Convert MC1 ({mc1}) to Gateway after all APs are online in Central:",
        "  (Complete GW ZTP or Static Activate — see Gateway migration in Step 5)",
        "",
    ]


def _write_l3_cluster_steps(lines: list, customer: CustomerConfig,
                            central: CentralConfig, cluster) -> None:
    members = " · ".join(cluster.members)
    if central.gateways_retired:
        tail = ("time: move its APs to a peer, run ap convert from the peer, then",
                "decommission the emptied MC. Repeat per member.")
    else:
        tail = ("time: move its APs to a peer, run ap convert from the peer, then convert",
                "the emptied MC to a Gateway. Repeat per member.")
    lines += [
        f"L3 CLUSTER UPGRADE SEQUENCE ({len(cluster.members)} members)"
        + (" — GATEWAYS RETIRED" if central.gateways_retired else ""),
        "─" * 40,
        f"Members: {members}",
        "L3 cluster members can be migrated independently — convert one MC at a",
        *tail,
        "",
        "Per-member AP conversion (run on the MC currently hosting the APs):",
        "",
    ]
    lines += _convert_block(customer, central)


def _generate_instant(customer: CustomerConfig, central: CentralConfig,
                      name: str, today: str, target_fw: str) -> str:
    """Instant (IAP) cluster → AOS 10: Central pushes the image — no
    controller CLI, no gateways, no ap convert."""
    lines = [
        "═" * 60,
        "  Instant (IAP) → AOS 10 Conversion Runbook",
        f"  Customer: {name}",
        f"  Generated: {today}",
        f"  Target AOS 10 release: {target_fw}",
        "═" * 60,
        "",
        "PRE-REQUISITES",
        "─" * 40,
        f"✓ Instant version: {customer.mc_firmware} (8.6+ required; latest 8.10/8.12 recommended)",
        "✓ Every AP needs DHCP + DNS + HTTPS reachability to Activate and Central",
        "✓ Complete Steps 1–4 in the migration tool first:",
        "    Central provisioned (Step 3) + APs claimed & subscribed in GreenLake (Step 4)",
        "",
        "NOTE — auto-conversion: Step 4 'Move APs into groups' assigns the APs to",
        "their AOS 10 device group, and Central then PUSHES the conversion — each AP",
        "reboots into AOS 10 (~10-20 min offline). That move IS the cutover; run it",
        "only inside a maintenance window.",
    ]
    if customer.radius_servers:
        lines.append(
            "! Update RADIUS/ClearPass: add the AP management subnet(s) as NAD "
            "network-range clients BEFORE converting"
        )
    lines += [
        "",
        "CONVERSION — DRIVEN FROM CENTRAL (no CLI on the cluster)",
        "─" * 40,
        "1. Remove any Activate provisioning rules / AirWave assignments that",
        "   point the swarm at another manager.",
        "2. APs are already claimed + subscribed (Step 4) and sit in their AOS 10",
        "   device group with firmware compliance set (Step 3).",
        "3. In Central: Devices → Access Points — the Instant swarm appears after",
        "   its next Activate check-in. Firmware compliance on the device group",
        f"   pushes ArubaOS {target_fw}; each AP downloads, converts and reboots.",
        "4. Convert a single canary AP first if the cluster is production-live:",
        "   temporarily move one AP's serial into the group, verify it comes up",
        "   on AOS 10 and broadcasts, then move the rest.",
        "",
        "  show swarm state            # (on the VC) monitor APs leaving the swarm",
        "",
    ]
    lines += _manual_secrets_block(customer)
    lines += [
        "POST-CONVERSION",
        "─" * 40,
        "1. Verify APs appear in Central → Devices → Access Points (10–20 min per AP)",
        "2. Verify SSIDs are broadcasting (check with a test client)",
        "3. Verify RADIUS authentication works with a test user",
        "4. Return to the migration tool → Step 6: Validate",
        "5. Decommission AirWave / old Activate rules after validation",
        "",
        "ROLLBACK (per AP — on AP console):",
        "  Boot the Instant partition / TFTP an Instant image via apboot,",
        "  then re-join the swarm. Keep one un-converted AP as the VC until",
        "  the cutover is validated.",
        "",
        "═" * 60,
    ]
    return "\n".join(lines)
