# Migration Paths

Two orthogonal choices set at Step 1 ([[Tool Internals|views/p1_connect.py]]): the **source**
platform and the **destination** Central. A third choice — [[Gateway Strategy]] —
only appears when tunnel/split SSIDs exist.

## The 2×2 matrix (source × destination)

|                        | [[Destination - New Central]] (GreenLake) | [[Destination - Classic Central]] (apigw) |
|------------------------|-------------------------------------------|-------------------------------------------|
| **[[Source - Mobility Controller|Mobility Controller/Conductor]]** | `ap convert` + scope-map provisioning; overlay SSIDs → GW cluster | `ap convert` + v3 AOS10 groups + `full_wlan` |
| **[[Source - Instant IAP|Instant cluster (IAP)]]** | Central-driven image push; zones → device groups | Central-driven push; zones → AOS10 groups |

`source_type` is `"controller"` or `"instant"` on `CustomerConfig`.
`destination` is `"new"` or `"classic"` on `CentralConfig`. See
[[Tool Internals|lib/models.py]].

## Source axis

- **Mobility Controller / Conductor** — APs terminate on MCs. Conversion is a
  CLI operation on the MC: [[Source - Mobility Controller|ap convert]]. The MCs can stay as AOS 10
  gateways or be retired ([[Gateway Strategy]]).
- **Instant (IAP)** — a swarm with a virtual controller, no separate
  controllers. Conversion is [[Source - Instant IAP|driven entirely from Central]] (firmware
  compliance pushes the AOS 10 image). No gateways exist before or after, so
  the gateway choice never applies.

## Destination axis

- **New Central** — HPE GreenLake tenant. Config model is library **profiles**
  bound to **scopes** via [[Glossary|scope-maps]]; see [[Destination - New Central]]. Auth is
  GreenLake [[Glossary|GLP]] client-credentials. Requires [[GreenLake Onboarding]].
- **Classic Central** — legacy apigw tenant. Config model is **v3 UI groups**
  with `Architecture=AOS10` and `full_wlan` payloads; see
  [[Destination - Classic Central]]. Auth is a ~2h API-Gateway token with a
  rotating [[Glossary|refresh token]].

## Gateway keep vs retire (only when tunnel/split SSIDs exist)

The third choice, detailed in [[Gateway Strategy]]:

- **Keep** (default) — tunnel/split SSIDs stay overlay; MC hardware becomes the
  AOS 10 [[Glossary|gateway cluster]]. Mirrors the AOS 8 design. On New Central this builds
  [[Destination - New Central|overlay-wlan]] objects bound to the cluster.
- **Retire** — every tunnel/split SSID is rewritten to **bridge** mode, no GW
  cluster is created, MCs are decommissioned after conversion. Triggers
  switchport + [[RADIUS and NAD Changes|NAD]] changes (see [[Preflight Checks]]).

Bridge-only AOS 8 deployments never see this choice — there is nothing to
tunnel. Instant is always bridge, so it never sees it either.

## When to use which

- **MC → New Central, keep GWs** — lift-and-shift of a tunneled campus that
  still wants centralized forwarding/firewalling. Lowest behavioral change.
- **MC → New Central, retire GWs** — customer wants to exit gateway hardware
  and forward at the edge. Biggest network change (switchports, DHCP at edge,
  L2-only roaming) — see [[Gateway Strategy]].
- **MC → Classic Central** — tenant is still on classic, or AOS10 groups are
  the agreed target. Watch the [[Destination - Classic Central|allowlist + readback]] caveats.
- **Instant → New Central** — the clean path: claim + subscribe in
  [[GreenLake Onboarding|GreenLake]], pre-assign to a device group, Central pushes the image.
- **Instant → Classic Central** — same idea on a classic tenant.

Start at [[Home]] · pick the source ([[Source - Mobility Controller]] / [[Source - Instant IAP]])
and destination ([[Destination - New Central]] / [[Destination - Classic Central]]).
