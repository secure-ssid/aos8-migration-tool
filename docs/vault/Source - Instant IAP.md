# Source — Instant (IAP) Cluster

The `instant` [[Migration Paths|source path]]. A [[Glossary|swarm]] of IAPs led by a virtual controller
(VC) — no separate controllers, no gateways. Conversion is **driven from
Central**: there is NO `ap convert`, no controller CLI. Code:
`parse_instant_config` in `lib/aos8_parser.py`, `_generate_instant` in
`lib/runbook.py`. See [[Tool Internals]].

## Discovery — VC paste only

Instant has no port-4343 REST pull in this tool; Step 1 uses paste from the VC:

```
show running-config   # SSIDs, auth servers, zones (from the virtual controller)
show aps              # cluster inventory (Serial/Zone columns captured when present)
show version          # Instant build (8.6+ required for Central-driven conversion)
```

The VC IP is captured only as a [[RADIUS and NAD Changes|RADIUS NAD]] reference.

## ssid-profiles ARE the WLANs

Instant has **no virtual-ap / ap-group layer**. Each `wlan ssid-profile` block
*is* the WLAN — `essid`, `opmode`, `vlan`, `wpa-passphrase`, optional
`auth-server` and `zone` all inline. The parser reads these directly into
`SSID` objects. A `disable` line marks the SSID hidden (`broadcast=False`).
Contrast with the [[Source - Mobility Controller|MC path]] where virtual-ap and ssid-profile are separate.

All Instant SSIDs are **`ForwardMode.BRIDGE`** — they forward locally. There is
no gateway in the design before or after migration, so [[Gateway Strategy]] never
applies and the gateway choice never appears in Step 1.

## Zones → device groups

Optional `zone` on an ssid-profile maps an SSID to a subset of APs (the AP's
`Zone` column in `show aps`). The parser turns zones into [[Destination - New Central|device groups]]:

- **With zones** — one group per zone. Each group gets the SSIDs whose zone
  matches **plus** any zoneless SSID (zoneless = broadcast everywhere). APs with
  no zone fall into a catch-all `instant-default` group with the zoneless SSIDs.
- **No zones** — one synthetic `instant-cluster` group holds every SSID and AP.

VLANs are synthesized from the SSIDs' VLAN ids (no separate VLAN config block).
RADIUS comes from `wlan auth-server` blocks (`ip`, `port`, `acctport`).

## Central-driven conversion

`_generate_instant` produces an informational runbook, not CLI:

1. Remove any Activate provisioning rules / AirWave assignments pointing the
   swarm at another manager.
2. APs are already claimed + subscribed ([[GreenLake Onboarding|Step 4]]) and sit in their AOS 10
   device group with [[Destination - New Central|firmware compliance]] set ([[Destination - New Central|Step 3]]).
3. In Central → Devices → Access Points, the swarm appears after its next
   Activate check-in. Firmware compliance on the device group pushes the AOS 10
   image; each AP downloads, converts, reboots (**10–20 min per AP**).
4. Monitor with `show swarm state` on the VC (APs leaving the swarm).

## Canary AP advice

If the cluster is production-live: temporarily move **one** AP's serial into the
AOS 10 device group, verify it comes up on AOS 10 and broadcasts, then move the
rest. This is the Instant equivalent of the [[Source - Mobility Controller|MC `ap convert add ap-name`]] canary.

## Rollback

Per AP, on the AP console: boot the Instant partition / TFTP an Instant image
via `apboot`, then re-join the swarm. **Keep one un-converted AP as the VC**
until the cutover is validated — losing the VC mid-cutover loses the swarm's
identity.

## Preflight differences

Instant skips the [[Preflight Checks|cluster, SSID→AP-group mapping]] checks (no controllers, no
vap bindings). Firmware check verifies **Instant 8.6+** (not the
8.10.0.12/8.12.0.1 trains the MC path requires). [[RADIUS and NAD Changes|NAD]] guidance: Instant
typically sources RADIUS from the VC IP via dynamic-radius-proxy — after
conversion each AP authenticates directly, so add the **AP management
subnet(s)** as NAD ranges.

## Related
[[Migration Paths]] · [[Preflight Checks]] · [[GreenLake Onboarding]] ·
[[Destination - New Central]] · [[RADIUS and NAD Changes]] · [[Glossary]]
