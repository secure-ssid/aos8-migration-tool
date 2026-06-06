# RADIUS and NAD Changes

When wireless moves off AOS 8, the **NAD** ([[Glossary|Network Access Device]] — the RADIUS
client the auth server trusts) changes. Add the new NAD(s) in ClearPass/RADIUS
**BEFORE** running [[Source - Mobility Controller|ap convert]] or the [[Source - Instant IAP|Instant push]], or 802.1X
auth breaks at cutover. Logic: `_check_radius_nad` in `lib/compatibility.py`
(see [[Preflight Checks]]) and the runbook prereqs in `lib/runbook.py`.

## Who the NAD becomes, per path

| Source / strategy | Old NAD (AOS 8) | New NAD (AOS 10) |
|---|---|---|
| **[[Source - Mobility Controller|MC]], [[Gateway Strategy|gateways kept]]** | MC management IP | **GW management IP** (set once the GW comes online). Bridge-mode SSIDs also: each **AP management IP**. |
| **MC, [[Gateway Strategy|gateways retired]]** | MC management IP | The **AP management subnet(s)** as a network-range NAD — every AP authenticates clients directly. |
| **[[Source - Instant IAP|Instant]]** | VC IP (if dynamic-radius-proxy was on; otherwise APs were already individual NADs) | The **AP management subnet(s)** as network-ranges — each AP authenticates directly after conversion. |

## Why

- **Tunnel + GW kept** — clients still tunnel to the gateway, so the GW is the
  one device talking RADIUS for tunneled SSIDs. Bridge SSIDs forward (and
  authenticate) at the AP, so those AP IPs are NADs too.
- **GW retired / Instant** — there is no central terminator; **each AP** is a
  RADIUS client. Use a **network-range** NAD covering the AP management
  subnet(s) — per-AP entries don't scale — with **one consistent shared secret**
  for the range.

## Practical steps

1. Identify the AP management subnet(s) and the GW management IP (if keeping).
2. In ClearPass: add the network device / network-range with the right secret
   **before** conversion.
3. Enterprise SSIDs also need the [[Glossary|auth-server profile]] **attached** in Central
   — automatic on [[Destination - New Central|New Central]] as a library profile (still attach to the
   SSID); a **manual follow-up** on [[Destination - Classic Central|Classic]] (create per group).
4. Validate: [[Glossary|Access Tracker]] should show the new GW/AP mgmt IP as the NAS
   (Step 6 checklist).

## Related
[[Gateway Strategy]] · [[Preflight Checks]] · [[Source - Mobility Controller]] ·
[[Source - Instant IAP]] · [[Destination - New Central]] · [[Destination - Classic Central]] · [[Glossary]]
