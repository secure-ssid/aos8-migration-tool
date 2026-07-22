# Gateway Strategy

The keep-vs-retire choice (Step 1, only shown when tunnel/split SSIDs exist).
Set via `gateway_mode` in `lib/translator.py` → `CentralConfig.gateways_retired`
+ `gw_cluster_name`. Applies only to the [[Source - Mobility Controller|Mobility Controller path]] —
[[Source - Instant IAP|Instant]] has no gateways and bridge-only MC deployments have nothing to
tunnel. See [[Tool Internals]].

## Keep gateways (default)

Tunnel/split SSIDs stay **overlay**; the MC hardware is converted to an AOS 10
**[[Glossary|gateway cluster]]** (`<slug>-cluster`). Mirrors the AOS 8 design — lowest
behavioral change.

- [[Destination - New Central|New Central]]: overlay-wlan objects bound to the cluster via
  `gw-cluster-list` + `cluster-scope-id` — **deferred to cutover**: the
  cluster forms when the converted MCs join in Central (Step 3 records the
  follow-up; the runbook drives the binding).
- [[Destination - Classic Central|Classic]]: AOS10 group allows Gateways; the cluster **auto-forms** on
  join (manual follow-up to verify the SSID binding).
- MC → gateway via ZTP or Static Activate (see [[Source - Mobility Controller|gateway migration]]).

## Retire gateways

Every tunnel/split SSID is rewritten to **bridge** mode (`translator.py` does
`replace(forward_mode=BRIDGE)`); no GW cluster is created; MCs are
decommissioned after conversion. (Discovery data is left untouched so
[[Preflight Checks]] still reasons about the original tunneled design.)

## Trade-offs

| Dimension | Keep | Retire |
|---|---|---|
| **Switchports** | Tunnel client VLANs **pruned** from AP ports (terminate on GW); trunk only bridge data VLANs | Former tunnel VLANs **trunked to every AP switchport** before conversion (native = AP mgmt VLAN) — see [[Preflight Checks|switchport check]] |
| **[[RADIUS and NAD Changes|RADIUS NAD]]** | GW management IP becomes the RADIUS client (bridge SSIDs: AP IPs too) | Every AP authenticates directly → add AP mgmt subnet(s) as NAD network-ranges |
| **Roaming** | Centralized — clients keep their tunnel through the GW | **L2 only** — verify client VLANs span the roam area, or split SSIDs per site/floor |
| **DHCP** | Reachable via the GW (relay as before) | Must be reachable **at the edge** (no more MC relay) |
| **Firewall policy** | Enforced on the GW as before | Moves to the AP **role** policies created during provisioning |
| **Rollback target** | A live AOS 8 MC exists until GW conversion | Keep one MC live (idle) as the [[Source - Mobility Controller|`convert-aos-ap cap`]] target |
| **Hardware** | MCs retained as gateways | MCs decommissioned |

## Rollback target nuance

[[Source - Mobility Controller|`convert-aos-ap cap <mc-ip>`]] needs a **live AOS 8 MC**. This is why the L2
cluster sequence keeps MC2 online but idle even when retiring — so a per-AP
rollback is still possible until validation. After Step 6, decommission both.

## Which to choose
See the decision table in [[Migration Paths]]. Keep for lift-and-shift of a
tunneled campus; retire when the customer wants to exit gateway hardware and
forward at the edge (accept the switchport/DHCP/roaming changes).

## Related
[[Migration Paths]] · [[Source - Mobility Controller]] · [[Preflight Checks]] ·
[[RADIUS and NAD Changes]] · [[Destination - New Central]] · [[Destination - Classic Central]] · [[Glossary]]
