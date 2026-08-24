# Preflight Checks

[[Migration Paths|Step 2]]. Every compatibility/safety gate in `lib/compatibility.py`
(`run_all`). Each returns a `CheckResult` with status **PASS / WARN / FAIL**.
FAIL = blocker: **critical** FAILs must be fixed at the source (no override);
non-critical FAILs must be fixed or individually acknowledged with a written
reason (an audit record) to continue. WARN = review.
Run against the discovered `CustomerConfig` + translated `CentralConfig`, so
results depend on the chosen [[Gateway Strategy]] and [[Migration Paths|source/destination]]. See [[Tool Internals]].

## AP model compatibility — `_check_ap_models`
**FAIL** if any AP model is in the AOS 10 incompatible set (`is_model_compatible`
/ `INCOMPATIBLE_MODELS` in `lib/aos8_client.py`). Incompatible: all Wi-Fi 4 and
older (9x/10x/11x/13x/17x, RAP-*), the entire 200 series, and every 300-series
model except 303/303H/303P, AP-318, 344/345 and 374/375/377 — see
`INCOMPATIBLE_MODELS` in `lib/aos8_client.py` for the exact set (IAP- and
AP- prefixes treated as the same hardware; `-US/-RW/-JP/-IL/-EG` country
suffixes stripped; bare `205` normalized to `AP-205`). **Remediate:** hardware
refresh before migration. Blank model → not blocked, but a separate "AP Models
Unknown" WARN lists the APs whose compatibility could not be checked.

## Firmware train — `_check_firmware`
`ap convert` is supported only on specific **release trains**, at/above a
minimum (`SUPPORTED_TRAINS`):
- **8.10 train** → minimum **8.10.0.12**
- **8.12 train** → minimum **8.12.0.1**
- Interim trains like **8.11 do NOT qualify**, even at a high build.

- MC firmware on a supported train ≥ minimum → **PASS**.
- Below minimum / wrong train → **FAIL** (upgrade; the detail notes `write erase`
  + reload if the MC had prior upgrades). See [[Source - Mobility Controller]].
- `unknown` / unparseable → **WARN** (paste `show version`).
- **Instant** source → different check entirely: Instant **8.6+** → PASS, else
  WARN (latest 8.10/8.12 recommended). See [[Source - Instant IAP]].

## AP DHCP requirement — `_check_dhcp`
AOS 10 requires DHCP (+DNS) on the AP mgmt VLAN. Static-IP provisioning isn't
visible in the objects the tool reads, so:
- APs flagged `has_static_ip` → **FAIL**.
- Otherwise → **WARN** (not PASS): manual gate — check `show ap provisioning
  ap-name <name>` for a static inner IP and re-provision for DHCP.

## Tunnel/Bridge VLAN conflict — `_check_vlan_tunnel_conflict`
Only when [[Gateway Strategy|gateways are kept]] (skipped when retired — replaced by the
retirement check). AP switchports must trunk bridge data VLANs but **prune**
tunnel client VLANs (tunnel VLANs terminate on the gateway, not the AP).
- VLAN used by both tunnel and bridge SSIDs → **WARN** (trunk: native = AP mgmt
  VLAN, allowed = bridge data VLANs only).
- Tunnel VLANs present → **WARN** (keep them off AP switchports).
- Neither → **PASS**.

## RADIUS NAD — `_check_radius_nad`
Only if RADIUS servers were discovered. The NAD (RADIUS client) identity changes
per path — full detail in [[RADIUS and NAD Changes]]:
- **Instant** → **WARN**: VC IP (if dynamic-radius-proxy) → add AP management
  subnet(s) as NAD ranges.
- **MC, gateways retired** → **WARN**: every AP becomes the RADIUS client → add
  AP mgmt subnet(s) as a network-range NAD (per-AP doesn't scale).
- **MC, gateways retired, bridge-only** (no former tunnel SSIDs) → **PASS** —
  the APs were already the RADIUS clients, no NAD changes required.
- **MC, gateways kept** → **WARN**: add the **GW management IP** as a new RADIUS
  client (bridge-mode SSIDs: each AP mgmt IP too).
Always do this in ClearPass/RADIUS **BEFORE** `ap convert`.

## Gateway retirement switchport changes — `_check_gateway_retirement`
Only when [[Gateway Strategy|gateways are retired]].
- No former tunnel/split SSIDs (already all bridge) → **PASS** (no switchport
  changes).
- Tunnel SSIDs being converted to bridge → **WARN**: their client VLANs used to
  terminate on the MC and must now be **trunked to every AP switchport before
  conversion** (native = AP mgmt VLAN). DHCP for those VLANs must be reachable at
  the edge (no more MC relay); roaming becomes **L2 only**; MC firewall policy
  moves to the AP **role** policies created during provisioning.

## EAP-Offload / FastConnect — `_check_eap_offload`
AAA FastConnect (EAP termination on the controller) is **NOT** supported in AOS
10. Configured (`aaa-fastconnect` in running-config) → **FAIL**: redesign to
standard 802.1X first.

## Internal auth server — `_check_internal_auth`
MC internal auth server is **NOT** supported in AOS 10. In use → **FAIL**:
migrate to external RADIUS (ClearPass/NPS) first.

## Controller cluster — `_check_cluster`
Skipped for [[Source - Instant IAP|Instant]] (no controllers).
- Single MC → **PASS** (no sequencing).
- **L2 cluster** → **WARN**: must follow the L2 sequence (move all APs to MC1,
  upgrade MC2, convert APs, upgrade MC1) — converting both at once strands APs.
- **L3 cluster** → **WARN**: members upgradeable independently, one at a time.
Details + runbook in [[Source - Mobility Controller]].

## AP inventory — `_check_static_ips`
No APs discovered → **WARN** (MC active? APs associated?). Otherwise **PASS**
with AP/group counts.

## SSID → AP-group mapping — `_check_ssid_mapping`
Instant: replaced by an SSID → Zone Mapping check — WARN when an SSID is zoned
to a zone with no checked-in AP (it is parked in the instant-default group;
verify zone names/case); silent when zones resolve cleanly. If `ssid_mapping_
incomplete` (virtual-ap bindings couldn't be discovered for a group, so **all**
SSIDs were assigned as a fallback) → **WARN**: in paste mode include the full
running-config with ap-group blocks (their `virtual-ap` lines). Else **PASS**.

## AP serial coverage — `_check_serials`
APs with no serial → **WARN**: they can't be pre-assigned to groups/sites,
can't be [[GreenLake Onboarding|claimed in GreenLake]], and won't be matched at Step 6 validation.
**Remediate:** use API mode or paste `show ap database long` (has the Serial #
column); `show ap active` has none. Else **PASS**.

## SSID auth detection — `_check_ssid_auth`
Multiple results from [[Glossary|opmode]] → AuthType mapping:
- **Opensystem SSID, aaa-profile unreadable** (`auth_known=False` on the REST
  path) → **FAIL — critical, no override**: MAC auth can neither be confirmed
  nor ruled out, so migrating as plain OPEN could publish a network that was
  MAC-authenticated. Restore the aaa-profile read and re-discover, or switch
  to paste mode (parses `mac-server-group` from the running-config directly).
- **Auth unknown** (non-opensystem; opmode couldn't be parsed) → **WARN**:
  provisioned as WPA2-Enterprise — verify. Paste mode: include the
  `wlan ssid-profile` blocks (with `opmode`).
- **PSK without recovered passphrase** → **WARN**: created, but set the
  passphrase in Central.
- **Enterprise SSIDs** → **WARN**: attach the RADIUS auth server in Central and
  add the new GW/AP IPs as RADIUS clients ([[RADIUS and NAD Changes|NAD]]).
- All resolved → **PASS**.

## Captive-portal SSIDs — `_check_captive_portal`
Classic destination only (skipped for [[Destination - New Central|New Central]]).
An SSID with a resolved external captive portal (`captive_portal_url` — MC: the
virtual-ap names an aaa-profile, whose `initial-role` user-role carries
`captive-portal <profile>`, resolved to the `aaa authentication captive-portal`
profile when its `login-page` is an off-box URL; Instant: `captive-portal
external [profile "X"]` / `captive-portal-profile "X"` on the ssid-profile) →
**FAIL — critical, no override**: Classic Central's `full_wlan` API has no
external-captive-portal field this tool can populate, so the guest SSID would
be created as a fully OPEN network. **Remediate:** migrate these SSIDs to
New Central, or build the captive-portal profile by hand in Classic and bind
it before enabling the SSID.

## Unsupported source fields — `_check_unsupported_fields`
Paste mode only: `wlan virtual-ap` / `ssid-profile` lines the migration cannot
represent land in `CustomerConfig.unsupported_fields` → **FAIL** (overridable):
the migrated WLANs would silently take Central defaults for those settings.
Reproduce each setting in Central after provisioning, or acknowledge the row
as a deliberate drop.

## Named VLANs unresolved — `_check_named_vlans`
An SSID referencing a **named VLAN pool** that couldn't resolve to a numeric id
(`vlan_raw` set) → **FAIL**: it would provision onto VLAN 1. **Remediate:** look
up the named VLAN's id (`show vlan` / `show running-config | include vlan-name`)
and fix it before provisioning.

## Split-tunnel SSIDs — `_check_split_tunnel`
Any `ForwardMode.SPLIT` SSID → **WARN** (always):
- gateways kept → provisioned as **full L2 overlay** (all client traffic tunnels
  to the GW) — AOS 10 per-SSID forwarding differs from AOS 8 split-tunnel.
- [[Gateway Strategy|gateways retired]] → becomes **full bridge** (all local) — verify nothing
  relied on the tunneled leg.

## Duplicate ESSIDs — `_check_duplicate_essids`
Central keys WLANs by **[[Glossary|ESSID]]** ([[Destination - New Central|New]] and [[Destination - Classic Central|Classic]]). Multiple
virtual-aps sharing an essid:
- **Different** vlan/forward-mode/auth/psk → **FAIL** (conflicting): only the
  first definition provisions — rename or consolidate first.
- **Identical** settings → **PASS** (consolidated into one WLAN bound per group).

## ESSID length — `_check_essid_limits`
ESSID > **32 characters** → **FAIL** (Central rejects it). Shorten first.
Within limit → no result emitted.

## Acknowledgement
Blockers (FAIL) gate the Provision button. **Critical** FAILs (e.g. SSID Auth
Unprovable, WEP, 802.1X/MAC-auth without RADIUS) cannot be overridden — resolve
them at the source and Re-run. Each non-critical FAIL can be acknowledged
individually with a written reason; every acknowledgement is an audited
`preflight-blocker-ack` record. Re-run, VLAN remap, or rediscovery clears the
cached results and their acknowledgements together.

## Related
[[Migration Paths]] · [[Gateway Strategy]] · [[Source - Mobility Controller]] ·
[[Source - Instant IAP]] · [[RADIUS and NAD Changes]] · [[Destination - New Central]] ·
[[Destination - Classic Central]] · [[GreenLake Onboarding]] · [[Glossary]]
