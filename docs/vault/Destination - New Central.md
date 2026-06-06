# Destination — New Central (GreenLake)

`destination="new"`. The default [[Migration Paths|destination]]. Code: `lib/central_client.py`
(`CentralClient`). Auth via GreenLake [[Glossary|GLP]] SSO; config via the network-config
profile/scope model. See [[Tool Internals]].

## Auth (GLP client-credentials)

- **Token** — `POST https://sso.common.cloud.hpe.com/as/token.oauth2`,
  `grant_type=client_credentials` with the GreenLake API client ID/secret.
- **Base URL** — regional, e.g. `https://us4.api.central.arubanetworks.com`
  (find the region in GreenLake → API client details).
- Create the client in GreenLake → Manage → API with Aruba Central
  (network-config) access. `_request` auto-reauths on 401 and backs off on 429.
- Same client can double as the [[GreenLake Onboarding|GLP]] claim client if it's a unified client.

## Scopes model

New Central config is **library profiles** bound to **[[Glossary|scopes]]** via **[[Glossary|scope-maps]]**.
A scope is global, a site, or a **device group**:

- **Global scope** — resolved from `GET /network-config/v1/scope-maps`, taking
  the entry with `persona == "SERVICE_PERSONA"` (fallback: most frequent
  scope-id). Needed before roles/policies.
- **Device groups** — `POST /network-config/v1/device-groups`
  (or `device-groups-create-and-add-devices` with serials). Returns a
  `scopeId`. One device group per AOS 8 [[Source - Mobility Controller|ap-group]] / Instant [[Source - Instant IAP|zone]].
- **Sites** — `POST /network-monitoring/v1/sites` (geographic scope with
  address). AP→site association tries the New Central path then a classic
  fallback.

## Profiles + scope-maps

A profile (VLAN, WLAN, role, policy, auth-server, firmware-compliance) is
created once, then **mapped to each scope** that should receive it via
`POST /network-config/v1/scope-maps` with `{scope-name, scope-id, persona,
resource}`. Duplicate scope-maps return an error that is treated as **idempotent
success**. [[Glossary|Personas]] used: `CAMPUS_AP`, `MOBILITY_GW`, `SERVICE_PERSONA`.

- **VLAN** — `POST /network-config/v1/layer2-vlan/{id}` (PUT on duplicate),
  then scope-mapped to the group.
- **Auth server** — `POST /network-config/v1alpha1/auth-servers/{name}`
  (RADIUS, AUTH_AND_COA). 802.1X SSIDs still need it **attached** in Central —
  surfaced by [[Preflight Checks|the 802.1X check]].
- **Firmware compliance** — `POST /network-config/v1alpha1/firmware-compliance`
  (PATCH on 412), `IMMEDIATE` upgrade+reboot, per device group + device-function.

## SSID forwarding: underlay vs overlay

`forward-mode` decides the object set (see [[Glossary|overlay/underlay]]):

- **Bridge → underlay** (`create_underlay_ssid`) — one `wlan-ssids/{essid}`
  with `FORWARD_MODE_BRIDGE`, scope-mapped to the group as `CAMPUS_AP`. That's it.
- **Tunnel/split → overlay** (`create_overlay_ssid`) — the full sequence below.

The SSID body (`_ssid_body`) keys by **[[Glossary|ESSID]]** (`display_name`), maps
[[Glossary|AuthType]] → opmode via `OPMODE` (OPEN / WPA2_PERSONAL / WPA3_SAE /
WPA2_ENTERPRISE / WPA3_ENTERPRISE_CCM_128), sets `vlan-id-range`, PSK under
`personal-security`, `dot1x` for enterprise, `wpa3-transition-mode` for
personal.

## Overlay SSID sequence (the load-bearing order)

For a tunnel/split SSID, `create_overlay_ssid` builds, in order:

1. **role** (`_ensure_role`) — `POST /network-config/v1/roles/{essid}`, then
   scope-map `roles/{name}` **and** `role-gpids/{name}` to global (CAMPUS_AP +
   MOBILITY_GW) and to the group (MOBILITY_GW).
2. **policy** (`_ensure_allow_all_policy`) — `POST
   /network-config/v1alpha1/policies/{essid}` (allow-all security policy whose
   source is `ADDRESS_ROLE` = the role), PATCH it into the `policy-groups`
   list, scope-map `policies/{name}` to global for CAMPUS_AP + MOBILITY_GW.
3. **SSID** — `POST /network-config/v1/wlan-ssids/{essid}` with
   `FORWARD_MODE_L2`, `type=EMPLOYEE`, `default-role`, `out-of-service=
   TUNNEL_DOWN`. The API **silently drops `default-role` on POST**, so it's
   re-applied with a follow-up PATCH.
4. **overlay-wlan** — `POST /network-config/v1/overlay-wlan/{essid}` binding the
   WLAN to the [[Glossary|gateway cluster]] via **`gw-cluster-list`** with
   `cluster` (name), **`cluster-scope-id`** (the GW device group's scope id),
   `cluster-type=CLUSTER_ID`, `tunnel-type=GRE`, `cluster-redundancy-type=PRIMARY`.
5. **scope-map** both `wlan-ssids/{name}` and `overlay-wlan/{name}` to the group
   as CAMPUS_AP.

Roles/policies are cached per run (`_ensured_roles`, `_ensured_policies`) so the
same ESSID across multiple groups doesn't redo them. Duplicates throughout are
swallowed (idempotent re-runs + same-ESSID-in-multiple-groups).

## Gateway cluster

When [[Gateway Strategy|keeping gateways]], the cluster lives in its **own device group**
(`<cluster>-gws`, MOBILITY_GW persona). `create_gw_cluster` →
`POST /network-config/v1alpha1/gateway-clusters/{name}` with `auto-cluster=
false`. The cluster's scope id is the **`cluster-scope-id`** the overlay-wlan
references. Cluster names: no spaces, must not start with `auto_`. The MC
hardware joins as a gateway via ZTP/Static Activate (see [[Source - Mobility Controller|gateway migration]]).

## Provision orchestration

`provision()` runs: resolve global scope → sites → auth servers → GW device
group + cluster → per group: device group, VLANs, SSIDs (overlay or underlay),
firmware compliance, CAMPUS_AP persona, site assignment. Duplicate ESSIDs within
a group are **skipped** (first definition wins) — see [[Preflight Checks|duplicate ESSID]]. Every
step's success/failure is recorded; the flow continues so the operator gets a
complete picture. Re-runs reuse existing objects.

## Validation

`list_all_aps` → `GET /network-monitoring/v1/devices`, filtered to
ACCESS_POINT/AP/IAP. Step 6 matches by serial against the discovered set.

## Related
[[Migration Paths]] · [[Destination - Classic Central]] · [[GreenLake Onboarding]] ·
[[Gateway Strategy]] · [[Preflight Checks]] · [[RADIUS and NAD Changes]] · [[Glossary]]
