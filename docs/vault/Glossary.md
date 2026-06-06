# Glossary

Terms used across the vault. Linked from most notes.

- **UIDARUBA** — the AOS 8 REST API session token returned by `/v1/api/login`
  (in `_global_result`). Sent on every request as a query param + cookie. See
  [[Source - Mobility Controller]].
- **config_path** — AOS 8 REST scoping param. Mobility **Conductor**: `/md`
  (default); standalone **controller**: `/mm/mynode`.
- **ssid-profile vs virtual-ap** — on the [[Source - Mobility Controller|MC]], `wlan virtual-ap` is the
  binding (vlan, forward-mode, refs); `wlan ssid-profile` holds the broadcast
  `essid`, `opmode`, passphrase. On [[Source - Instant IAP|Instant]] the ssid-profile **is** the WLAN.
- **ESSID vs profile name** — ESSID = the broadcast network name; profile name =
  the config object's name. `SSID.display_name` = essid, falling back to the
  profile name. **Central keys WLANs by ESSID** — drives [[Preflight Checks|duplicate ESSID]] checks.
- **opmode** — AOS 8 encryption/auth mode token (e.g. `wpa2-psk-aes`,
  `opensystem`). Mapped to an `AuthType` by `_opmode_to_auth`; unparseable →
  defaults to WPA2-Enterprise and flagged ([[Preflight Checks|auth detection]]).
- **AuthType** — tool enum: OPEN, WPA2_PSK, WPA3_SAE, WPA2_ENTERPRISE,
  WPA3_ENTERPRISE, MAC. Mapped to opmode strings per destination (`OPMODE` for
  [[Destination - New Central|New]], `OPMODE_CLASSIC` for [[Destination - Classic Central|Classic]]).
- **forward-mode** — TUNNEL (to gateway), BRIDGE (local at AP), SPLIT. Decides
  [[Destination - New Central|overlay vs underlay]] and interacts with [[Gateway Strategy]].
- **overlay / underlay** — overlay = client traffic tunnels (GRE) from the AP to
  a [[#^gwcluster|gateway cluster]] (tunnel/split SSIDs, gateways kept); underlay =
  client traffic bridges locally at the AP (bridge SSIDs / gateways retired).
- **scope** — a New Central config target: global, a site, or a **device group**.
- **scope-map** — binds a library **profile** (VLAN/WLAN/role/policy/auth-server)
  to a scope for a given persona. Idempotent (duplicates = success). See
  [[Destination - New Central]].
- **persona** — the device function a scope-map applies to: `CAMPUS_AP`,
  `MOBILITY_GW`, `SERVICE_PERSONA` (the last marks the global scope).
- **virtual-ap** — see ssid-profile vs virtual-ap above. The AOS 8 binding object.
- **gateway cluster** — AOS 10 gateways grouped for redundancy. ^gwcluster
  Overlay WLANs bind to it via `gw-cluster-list` (`cluster`, `cluster-scope-id`,
  `cluster-type=CLUSTER_ID`, `tunnel-type=GRE`). On New Central it lives in a
  `<name>-gws` device group; on Classic it auto-forms on join. [[Gateway Strategy]].
- **GLP** — HPE GreenLake Platform. Devices are **claimed** (serial + MAC) and
  **subscribed** there before Central adopts them. See [[GreenLake Onboarding]].
- **GreenLake SSO** — `sso.common.cloud.hpe.com/as/token.oauth2`,
  client-credentials grant, used by [[Destination - New Central|New Central]] and [[GreenLake Onboarding|GLP]] clients.
- **swarm** — an [[Source - Instant IAP|Instant]] cluster led by a virtual controller (VC). `show
  swarm state` shows APs leaving during conversion.
- **zone** — Instant ssid-profile attribute mapping an SSID to a subset of APs →
  becomes a [[Destination - New Central|device group]]. Zoneless SSIDs broadcast everywhere.
- **NAD** — Network Access Device: the RADIUS client an auth server trusts.
  Changes identity at migration — see [[RADIUS and NAD Changes]].
- **dynamic-radius-proxy** — Instant feature where the VC IP fronts RADIUS for
  all APs; disappears after conversion (each AP becomes its own NAD).
- **Access Tracker** — ClearPass auth log; used post-cutover to confirm the new
  NAS (GW/AP mgmt IP) — Step 6 checklist.
- **ap convert** — AOS 8 MC CLI command set that converts terminated APs to AOS
  10. `add` / `pre-validate` / `cancel` / `active ... activate`. See
  [[Source - Mobility Controller]].
- **convert-aos-ap cap** — per-AP rollback command (on the AP console) that pulls
  the AOS 8 image back from a live MC. [[Gateway Strategy|rollback target]].
- **Activate** — Aruba's ZTP cloud. Converted APs/GWs fetch their AOS 10 image
  and registration from it (`ap convert active ... activate`).
- **refresh token (Classic)** — single-use, **rotates** on every refresh; the
  new one must be captured. [[Destination - Classic Central]].
- **full_wlan** — Classic Central WLAN API; the whole flat WLAN object is
  JSON-stringified under a `"value"` key, and is **allowlisted per tenant**.
- **release train** — AOS 8 minor line (8.10, 8.11, 8.12). `ap convert` needs
  8.10 ≥ .0.12 or 8.12 ≥ .0.1; 8.11 doesn't qualify. [[Preflight Checks|firmware]].

## Related
[[Home]] · [[Migration Paths]] · [[Tool Internals]]
