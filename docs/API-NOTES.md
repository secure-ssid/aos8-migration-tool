# API Notes

The exact API surfaces the tool uses, per platform, with the runtime-verify
caveats baked into the clients. Paths below are relative to each platform's base
URL. The three cloud clients (New Central, Classic, GLP) raise on unexpected
failure (errors are recorded per step, not swallowed) and auto-retry once on
401 (re-auth/refresh) and once on 429 (Retry-After; both the delay-seconds and
HTTP-date forms are tolerated). The AOS 8 client re-logins once on a 401
(covers out-of-band session invalidation mid-pull) but has no 429 handling —
its calls are short-lived reads inside one login session.

## Bases and auth

| Platform | Base URL | Auth | Source |
|---|---|---|---|
| AOS 8 MC / Conductor | `https://<mc-ip>:4343` | Form login → `UIDARUBA` session token (query param + cookie) | `lib/aos8_client.py` |
| New Central (GreenLake) | regional, e.g. `https://us4.api.central.arubanetworks.com` | OAuth `client_credentials` at `https://sso.common.cloud.hpe.com/as/token.oauth2` → bearer | `lib/central_client.py` |
| Classic Central | `https://apigw-<cluster>.central.arubanetworks.com` | UI-generated access token (~2h); refresh via `/oauth2/token` (rotating refresh token) | `lib/classic_central_client.py` |
| HPE GreenLake Platform | `https://global.api.greenlake.hpe.com` (fixed) | OAuth `client_credentials` at the same SSO host | `lib/glp_client.py` |

New Central and GLP both use the GreenLake `client_credentials` grant against
`sso.common.cloud.hpe.com`. Classic Central has **no** client-credentials grant —
it uses the UI access token, refreshed via a single-use, rotating refresh token.

---

## AOS 8 Mobility Controller — REST read surface

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/api/login` | Form-encoded username/password → `_global_result.UIDARUBA`. `status` is `0` (int or string) on success. |
| GET | `/v1/configuration/object/<name>` | Read a config object instance list. |
| GET | `/v1/configuration/showcommand?command=<cmd>` | Run a show command; JSON document (or `_data` text). |

Every request after login carries `UIDARUBA` and, on a Conductor, a
`config_path` query param.

| `config_path` | Use |
|---|---|
| `/md` (default) | Mobility Conductor (MM) — managed-device hierarchy, or a specific node. |
| `/mm/mynode` | Standalone controller. |

Config objects read: `ap_group` (with `virtual_ap` bindings), `ssid_prof`
(essid/opmode/passphrase), `virtual_ap` (vlan/forward-mode/profile refs —
some builds answer the legacy name `wlan_virtual_ap` instead; the client
tries both), `aaa_prof` (dot1x/mac server-group resolution), `vlan_id`,
`rad_server`, `server_group_prof`.

Show commands read: `show ap database long` (AP inventory incl. Serial #, Wired
MAC, Group), `show controller-ip`, `show version`,
`show lc-cluster group-membership`.

Quirks handled in the client:
- `opmode` arrives as a flag dict (`{"wpa2-psk-aes": true}`) — the client takes
  the first true flag.
- Some values are double-wrapped as `{key: {key: val}}` (`_field()` unwraps).
- VLAN tokens may be `"100"`, `"100,200"`, or a **named** VLAN — `_safe_vlan()`
  takes the first valid id; named pools set `SSID.vlan_raw` and are flagged by
  preflight as a FAIL.
- AP models are normalised (`205` → `AP-205`); country suffixes (`-US`, `-RW`,
  `-JP`, `-IL`, `-EG`) are stripped for the compatibility lookup, and AP-/IAP-
  prefixes are treated as interchangeable hardware.

The CLI-paste parser (`aos8_parser.py`) reads the same data from
`show running-config` + `show ap database long` (+ Instant: VC `show
running-config`, `show aps`). `parse_cli_table()` slices columns at the dash
separator row rather than guessing on whitespace.

---

## New Central — network-config / scope-maps / monitoring

The New Central model is **library profiles bound to scopes via scope-maps**.

Calls made during **Step 3 (config phase)**:

| Method | Path | Purpose |
|---|---|---|
| GET | `/network-config/v1/scope-maps` | Resolve the global scope id (`persona == SERVICE_PERSONA`, else most-frequent scope-id). Doubles as the config-access pre-check. |
| POST | `/network-config/v1/scope-maps` | Map a resource to a scope/persona. Duplicate = idempotent success. |
| GET/POST | `/network-config/v1alpha1/sites` | List / create site (idempotent by name). Falls back to `/network-config/v1/sites`, then `/network-monitoring/v1/sites`, on 404. |
| GET | `/network-config/v1/device-groups` | List device groups. |
| POST | `/network-config/v1/device-groups` | Create empty group (`scopeName`). |
| POST | `/network-config/v1/device-groups-create-and-add-devices` | Create group + add serials in one call. |
| POST/PUT | `/network-config/v1/layer2-vlan/{id}` | Create/replace VLAN profile (`{vlan, name, enable}`), then scope-map it. |
| POST | `/network-config/v1alpha1/auth-servers/{name}` | RADIUS auth-server library profile. |
| POST | `/network-config/v1alpha1/server-groups/{name}` | RADIUS server-group — 802.1X SSIDs bind to it via `auth-server-group`. |
| POST/PATCH | `/network-config/v1/wlan-ssids/{name}` | Upsert underlay SSID (PATCH on duplicate), then scope-map to the group. |
| POST | `/network-config/v1alpha1/captive-portal/{name}` | Shared external captive-portal profile (referenced by the SSID). |
| POST/PATCH | `/network-config/v1alpha1/firmware-compliance` | Set compliance. `scope-id`/`object-type`/`device-function` go in the **query string**; on 412/duplicate falls back to PATCH. |

Calls made during **Step 4 (devices phase)**:

| Method | Path | Purpose |
|---|---|---|
| POST | `/network-config/v1/device-groups-add-devices` | Move serials into their group (`desScopeId`, `devices`) — the conversion trigger for pre-assigned APs. |
| POST | `/network-config/v1alpha1/persona-assignment` | Assign device function (`CAMPUS_AP`). |
| POST | `/network-config/v1/site-add-devices` | Assign devices to the site (`desScopeId`, `devices`); falls back to `/network-monitoring/v1/sites/{id}/devices`, then `/central/v2/sites/associate`. |

Validation (Step 6):

| Method | Path | Purpose |
|---|---|---|
| GET | `/network-monitoring/v1/devices` | All devices; filter to AP-type for serial matching. |

**Deferred to cutover (recorded as manual follow-ups, not called in Step 3):**
the gateway cluster (`/network-config/v1alpha1/gateway-clusters/{name}`) and
the overlay path for tunnel/split SSIDs (role + `role-gpids`, allow-all policy
+ policy-group PATCH, SSID `default-role` re-apply, and the
`/network-config/v1/overlay-wlan/{name}` GRE binding). The cluster is a New
Central object formed by JOINING gateways — it can't exist before the MCs
convert, so Step 3 logs the follow-up and the runbook drives it.

SSID forward modes: bridge (and everything when gateways are retired) →
`FORWARD_MODE_BRIDGE` (underlay); tunnel/split → deferred overlay
(`FORWARD_MODE_L2`) as above. `OPMODE` maps `AuthType` → Central opmode enum
(e.g. `WPA2_PERSONAL`, `WPA3_SAE`, `WPA2_ENTERPRISE`,
`WPA3_ENTERPRISE_CCM_128`; MAC and OPEN → `OPEN`).

### Runtime-verify caveats (New Central)

| Behaviour | Why |
|---|---|
| Resolve global scope first | Proves config access before any write; if it fails, `provision()` returns immediately. |
| Firmware compliance POST → PATCH on 412/duplicate | Already set for the scope; PATCH updates the version. |
| Site id re-list after create | POST bodies don't always echo the id; a duplicate error triggers a refreshed re-list. |
| Duplicate scope-maps / objects | "already exists"/"duplicate" **in the response detail** are treated as idempotent success (the URL path is ignored so customer-named objects can't fake it). |
| Persona/site assignment in the devices phase | Both need claimed APs, so they run with the Step 4 cutover move (also in `phase="all"`). |

---

## Classic Central — v3 groups / full_wlan / sites / firmware / monitoring

| Method | Path | Purpose |
|---|---|---|
| POST | `/oauth2/token` (query string) | Refresh: `client_id`, `client_secret`, `grant_type=refresh_token`, `refresh_token` in the **query string**, empty body. Returns a **new** refresh token. |
| GET | `/configuration/v2/groups` | List group names (response is a list of single-element name lists). |
| POST | `/configuration/v3/groups` | Create AOS10 group (per-section `Architecture=AOS10`, `AllowedDevTypes`). |
| GET | `/configuration/v1/groups/properties` | Read back `Architecture` to verify the create actually applied. |
| POST | `/platform/device_inventory/v1/devices` | Pre-add serial+MAC pairs to inventory (duplicates fine). |
| POST | `/configuration/v1/devices/move` | Move serials into a group. |
| GET/POST | `/central/v2/sites` | List / create site (`site_address` **or** zeroed `geolocation` — mutually exclusive, one required). |
| POST | `/central/v2/sites/associations` | Associate devices (`device_type="IAP"`, `device_ids`). |
| POST | `/configuration/full_wlan/{group}/{name}` | Create WLAN (see wrapper quirk below). |
| POST | `/firmware/v2/upgrade/compliance_version` | Firmware compliance (v1 fallback on 404/405). `device_type="IAP"` for APs (incl. AOS 10). |
| GET | `/monitoring/v2/aps` | Validation: `{"aps":[...]}` with status `Up`/`Down`. |

`OPMODE_CLASSIC` maps `AuthType` → classic opmode (`opensystem`, `wpa2-psk-aes`,
`wpa3-sae-aes`, `wpa2-aes`, `wpa3-aes-ccm-128`).

### The full_wlan `{"value": json.dumps(...)}` wrapper quirk

The classic WLAN config API does **not** accept a normal JSON body. The complete
flat WLAN object plus an access rule must be JSON-**stringified** and placed under
a single `value` key:

```python
payload = {"value": json.dumps({"wlan": wlan, "access_rule": rule})}
self._post(f"/configuration/full_wlan/{group}/{name}", json_body=payload)
```

`wlan` is a full ~90-field flat object (`_BASE_WLAN` in the client, taken verbatim
from HPE's central-python-workflows examples); only per-SSID fields are
overridden (`name`, `essid`, `index`, `opmode`, `type`, `vlan`, `hide_ssid`,
`wpa_passphrase`, enterprise `access_type`/`auth_server1`, and `cluster_name` for
tunnel SSIDs). `access_rule` is a full flat object (`_BASE_ACCESS_RULE`) with the
SSID name filled in.

### Runtime-verify caveats (Classic)

| Behaviour | Why |
|---|---|
| **403 on a `full_wlan` path** | The classic WLAN config APIs are **allowlisted per tenant**. The client raises a clear message: ask your Aruba SE to enable them for the account. (Other 403s raise the generic error.) |
| Group-create Architecture readback | A known flaw lets the v3 create return success **without applying**. After creating, the client reads `/configuration/v1/groups/properties`; it raises only if `Architecture` is confirmed to be something other than `AOS10` (readback transport errors don't fail the step). |
| Firmware v2 → v1 fallback | On 404/405 the client retries the v1 compliance endpoint. |
| 401 → refresh → retry | On 401 the client attempts a token refresh and retries once. |
| Refresh token rotation | Each refresh returns a new refresh token; `self.refresh_token` holds the newest. Views read it back and persist it to the session. |
| RADIUS auth-servers / GW clusters | **Cannot** be created via the classic API. `provision()` appends them as MANUAL FOLLOW-UP results (create RADIUS per group; gateways auto-cluster on join — verify tunnel SSID binding). |
| Tunnel WLAN `cluster_name` | Set on the WLAN but unverified by any reference example — confirm in the Central UI. |

---

## HPE GreenLake Platform (GLP) — devices + subscriptions

Base is always `https://global.api.greenlake.hpe.com` regardless of Central
region.

| Method | Path | Purpose |
|---|---|---|
| GET | `/devices/v1/devices` | List / filter devices (`filter=serialNumber eq '<s>'`). |
| POST | `/devices/v1/devices` | Claim network devices → **202** + `Location: /devices/v1/async-operations/{id}`. |
| GET | `/devices/v1/async-operations/{id}` | Poll claim status until `completed`/`failed` (10s interval, 5 min timeout). |
| GET | `/subscriptions/v1/subscriptions` | List subscriptions; resolve a key → UUID (`filter=key eq '<k>'`). |
| GET | `/service-catalog/v1/service-manager-provisions` | Central application instances (id + region) in the workspace; `/v1beta1/` fallback. |
| PATCH | `/devices/v2beta1/devices?id=<uuid>` | **Two sequential merge-patches** (GLP rejects combining them): 1) `{"application":{"id":…},"region":…}` — REQUIRED for Central to adopt the AP; 2) `{"subscription":[{"id": <uuid>}]}`. Each is polled to a terminal state when GLP answers 202 (the returned `Location` path is honored as-is). |

### Runtime-verify caveats (GLP)

| Behaviour | Why |
|---|---|
| `macAddress` **required** to claim | The client raises before submitting any device without a MAC — re-discover with `show ap database long`. |
| Claim is async + reconciled | The tool polls the async-operation, then re-reads the workspace inventory and reconciles **submitted serials vs. actual workspace** — it never trusts the async body shape alone. Serials missing post-claim are flagged. |
| Active subscriptions only | The UI filters out `ENDED` subscriptions; AP-type subscriptions (`CENTRAL_AP`/`FOUNDATION_AP`-style) are listed first. |
| Subscription key vs UUID | Canonical UUIDs pass through; keys are OData-resolved. Unsafe characters are rejected with guidance to pass the UUID. |
| Claim body shape | The claim posts `{"network":[...], "compute":[], "storage":[]}`. |

---

## Mapping summary (AOS 8 → destination)

| AOS 8 construct | New Central | Classic Central (AOS10) |
|---|---|---|
| ap-group | Device group (scope) | v3 AOS10 UI group |
| virtual-ap tunnel/split (keep gateways) | Overlay SSID → GW cluster — **deferred to cutover** (manual follow-up + runbook) | `full_wlan` with `cluster_name` (verify in UI) |
| virtual-ap bridge (or all when retired) | Underlay SSID scope-mapped to the group | `full_wlan` (bridge) |
| VLAN | `layer2-vlan` profile scope-mapped to group | (implicit via WLAN `vlan` field) |
| RADIUS server | `auth-servers` library profile + `server-groups` binding | Manual follow-up (no classic API) |
| MC cluster | Gateway cluster formed when converted MCs join at cutover (manual follow-up) | Gateways auto-cluster on join (manual follow-up) |
