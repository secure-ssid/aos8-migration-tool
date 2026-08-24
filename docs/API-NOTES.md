# API Notes

The exact API surfaces the tool uses, per platform, with the runtime-verify
caveats baked into the clients. Paths below are relative to each platform's base
URL. The three cloud clients (New Central, Classic, GLP) raise on unexpected
failure (errors are recorded per step, not swallowed) and auto-retry once on
401 (re-auth/refresh) and once on 429 (Retry-After; both the delay-seconds and
HTTP-date forms are tolerated). A **2xx with a non-JSON body raises** in both
Central clients — every API response here is JSON, so an unparseable success
body is a protocol violation, never flattened into a fake success. The Step 1
probe adds **response-schema validation**: a collection endpoint answering
anything but a list of objects (or objects missing their name key) FAILs the
probe row instead of reading as "0 resources readable". Destination base URLs
are gated by `lib/http_base.py`: only HPE/Aruba hosts
(`arubanetworks.com`, `cloud.hpe.com`, `greenlake.hpe.com`) are accepted,
`http://` is refused except on loopback, and anything else needs
`AOS8_DEV_MODE` (local dev/test only) — a mistyped base URL can never ship a
bearer token to an unintended or cleartext endpoint. The AOS 8 client
re-logins once on a 401 (covers out-of-band session invalidation mid-pull)
but has no 429 handling — its calls are short-lived reads inside one login
session. Controller TLS verification is ON by default. A factory self-signed
cert is handled by trust-on-first-use: `connect()` raises `AOS8TLSError`
carrying the certificate's SHA-256 fingerprint, Step 1 shows it for
confirmation, and trusting it pins that exact certificate for the run
(`assert_fingerprint`, so a substituted cert is still refused). Alternatives:
`AOS8_CERT_FINGERPRINT` to pin headlessly, `AOS8_CA_BUNDLE` for your own CA,
`AOS8_DEV_MODE` to disable verification entirely in a local lab.

## Bases and auth

| Platform | Base URL | Auth | Source |
|---|---|---|---|
| AOS 8 Mobility Controller (MC) / Mobility Conductor | `https://<mc-ip>:4343` | Form login → `UIDARUBA` session token (query param + cookie) | `lib/aos8_client.py` |
| New Central (GreenLake) | regional, e.g. `https://us4.api.central.arubanetworks.com` | OAuth `client_credentials` at `https://sso.common.cloud.hpe.com/as/token.oauth2` → bearer | `lib/central_client.py` |
| Classic Central | `https://apigw-<cluster>.central.arubanetworks.com` | UI-generated access token (~2h); refresh via `/oauth2/token` (rotating refresh token) | `lib/classic_central_client.py` |
| HPE GreenLake Platform | `https://global.api.greenlake.hpe.com` (fixed) | OAuth `client_credentials` at the same SSO host | `lib/glp_client.py` |

New Central and GLP both use the GreenLake `client_credentials` grant against
`sso.common.cloud.hpe.com`. Classic Central has **no** client-credentials grant —
it uses the UI access token, refreshed via a single-use, rotating refresh token.

**Ownership manifest (`lib/manifest.py`).** Objects provisioning creates are
registered in a per-customer+tenant manifest under
`~/.aos8-migration/manifests/` (plaintext JSON — names, ids, payload hashes;
owner-only permissions; written atomically on every mutation so a crash
mid-provision can't lose the record). **Coverage is currently partial:**
registration and gating are wired for **SSIDs (both clients) and Classic
device groups** only. For those kinds, idempotency is no longer name-only:
reusing or PATCHing a same-named tenant object is allowed only when the
manifest owns it (created by this tool, or explicitly adopted in Step 3 — an
audited, per-object decision). A same-named SSID/group with no manifest entry
raises a **collision refusal** instead of being silently reused or patched.
Every other resource kind — sites, VLANs, auth servers, New Central device
groups, server groups, policies, roles, captive portals, gateway clusters — is
still
**name-based reuse** this wave: a same-named object is silently reused as
before. An unreadable manifest fails closed — it must not read as "we own
nothing". Cleanup is the mirror image: with a manifest it deletes **only**
manifest-owned objects, so in practice only owned SSIDs and Classic groups
are torn down; anything else the run created (and adopted or foreign objects)
is kept and listed as "not manifest-owned". With no manifest on disk, both
sides keep the legacy behavior (name-only reuse, prefix-only `zztest-*`
teardown).

---

## AOS 8 Mobility Controller — REST read surface

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/api/login` | Form-encoded username/password → `_global_result.UIDARUBA`. `status` is `0` (int or string) on success. |
| GET | `/v1/configuration/object/<name>` | Read a config object instance list. |
| GET | `/v1/configuration/showcommand?command=<cmd>` | Run a show command; JSON document (or `_data` text). |

Every request after login carries `UIDARUBA`. A `config_path` is REQUIRED on
`/v1/configuration/object/<name>` (`/md` on a Conductor, `/mm/mynode` on a
standalone controller). HPE documents only `command` + `UIDARUBA` for
`showcommand`, so the client makes `config_path` caller-controlled there: the
show-command fallback pull retries across every candidate node **and** with no
`config_path` at all, because re-running it at the same dead node returns the
same empty parse. When the config-node re-probe replaces the node mid-pull, the
client **re-fetches `show running-config` at the detected node** — everything
derived from it (external captive-portal chain, EAP-offload, internal-auth
flags) belongs to the original node, so a stale copy would let blocking
preflight checks pass on the wrong data.

AOS 8 caps concurrent sessions at **64** across CLI + WebUI + API (default idle
timeout 900 s), which is why the client releases its session before every
re-login and on every exit path — a leaked session per 401 eventually locks the
account out of the API.

A bad `config_path`, an unknown object and an expired session all come back as
**HTTP 200** with a `_global_result.status != 0` payload. The client raises on
that instead of returning an empty list: an empty list is indistinguishable
from "this controller has no WLANs", which is exactly how a wrong node used to
read as a clean-but-empty pull.

`node_hierarchy` (used to enumerate config nodes) appears in **no** HPE
published AOS 8 reference — HPE enumerates the hierarchy with
`showcommand?command=show switches`. The client therefore records why the node
scan failed (`node_scan_error`) rather than treating an unreadable hierarchy as
an empty one; `debug_pull.py` dumps both.

| `config_path` | Use |
|---|---|
| `/md` (default) | Mobility Conductor (MM) — managed-device hierarchy, or a specific node. |
| `/mm/mynode` | Standalone controller. |

Config objects read: `ap_group` (with `virtual_ap` bindings), `ssid_prof`
(essid/opmode/passphrase/hide-ssid), `virtual_ap` (vlan/forward-mode/profile refs —
some builds answer the legacy name `wlan_virtual_ap` instead; the client
tries both), `aaa_prof` (dot1x/mac server-group resolution), `vlan_id`,
`rad_server`, `server_group_prof`.

Show commands read: `show ap database long` (AP inventory incl. Serial #, Wired
MAC, Group), `show controller-ip`, `show version`,
`show lc-cluster group-membership`.

Quirks handled in the client:
- `opmode` arrives as a flag dict (`{"wpa2-psk-aes": true}`) — a transition-mode
  profile carries TWO true flags, so the client reduces by security strength
  (strongest wins: WPA3/SAE > OWE > PSK/802.1X > open) with the token itself
  as alphabetical tie-break. Deterministic, and unknown/future tokens rank
  below known ones so they never silently mask a known flag.
- Some values are double-wrapped as `{key: {key: val}}` (`_field()` unwraps).
- VLAN tokens may be `"100"`, a comma list (`"100,200"`), a range
  (`"100-105"`), or a **named** VLAN — `_safe_vlan()` takes the first valid
  id; named VLANs **and** multi-id pools/ranges set `SSID.vlan_raw` and are
  flagged by preflight as a FAIL showing the actual collapse target.
- `opensystem` + a `mac-server-group` on the bound aaa-profile is a
  **MAC-auth** network (legacy printer/IoT SSIDs are exactly this) —
  `auth_type` becomes `MAC`, never `OPEN`, so it cannot migrate wide open.
  On the REST discovery path this detection depends on the best-effort
  `aaa_prof` object read: if that fetch fails, the opensystem SSIDs keep
  `auth_type=OPEN` but with `auth_known=False`, and preflight raises a
  **critical FAIL** ("SSID Auth Unprovable") that cannot be overridden —
  they are never migrated as plain OPEN. The paste path parses
  `mac-server-group` from the running-config directly and is unaffected.
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
| GET/POST | `/network-config/v1alpha1/sites` | List / create site (idempotent by name). Both fall back to `/network-config/v1/sites`, then `/network-monitoring/v1/sites`, on a **404 only** — a 403/500 raises rather than caching an empty site list. (The monitoring-sites route appears in no published OpenAPI spec — checked 2026-08 against 31 vendored specs — and is kept only as a last resort for tenants that still serve it.) |
| GET | `/network-config/v1/device-groups` | List device groups. |
| POST | `/network-config/v1/device-groups` | Create empty group (`scopeName`). |
| POST | `/network-config/v1/device-groups-create-and-add-devices` | Create group + add serials in one call (implemented in the client but not used by the wizard — Step 3 creates groups empty; APs are moved in Step 4 via `device-groups-add-devices`). |
| POST/PUT | `/network-config/v1/layer2-vlan/{id}` | Create/replace VLAN profile (`{vlan, name, enable}`), then scope-map it. |
| POST | `/network-config/v1alpha1/auth-servers/{name}` | RADIUS auth-server library profile. |
| POST | `/network-config/v1alpha1/server-groups/{name}` | RADIUS server-group — 802.1X SSIDs bind to it via `auth-server-group`. |
| POST/PATCH | `/network-config/v1/wlan-ssids/{name}` | Upsert underlay SSID (PATCH on duplicate), then scope-map to the group. |
| POST | `/network-config/v1alpha1/captive-portal/{name}` | Shared external captive-portal profile (referenced by the SSID). |
| POST/PATCH | `/network-config/v1alpha1/firmware-compliance` | Set compliance. `scope-id`/`object-type`/`device-function` go in the **query string**; on 412/duplicate falls back to PATCH. HPE's local-profile write convention is underscored (`object_type=LOCAL`, `scope_id`, `persona`) and its docs warn that a MISSING `object_type` silently stores the profile at Library level instead of erroring — verify against a live tenant that the profile lands at the target scope before assuming kebab-case is accepted here. |

Calls made during **Step 4 (devices phase)**:

| Method | Path | Purpose |
|---|---|---|
| POST | `/network-config/v1/device-groups-add-devices` | Move serials into their group (`desScopeId`, `devices`) — the conversion trigger for pre-assigned APs. |
| POST | `/network-config/v1alpha1/persona-assignment/{device-function}` | Assign device function (`CAMPUS_AP`). The OpenAPI spec defines only this path-parameter form, so it is tried first; on a routing 404 the client falls back to the bare `/persona-assignment` collection (the form field-verified on earlier tenants). Body key is `device-id` (a LIST of serials). |
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
`WPA3_ENTERPRISE_CCM_128`; OPEN → `OPEN`). MAC-auth SSIDs (detected from the
aaa-profile's `mac-server-group`) map to `OPEN` opmode plus first-class
`mac-authentication: true` and an `auth-server-group` binding on the SSID
body — without those they would migrate wide open. WEP has no AOS 10
equivalent: preflight FAILs it and `_ssid_body` raises as a last line of
defence. WPA3-SAE bodies do NOT set `wpa3-transition-mode-enable` (that flag
is a WPA2-PSK compatibility feature — enabling it on a WPA3-only SSID would
silently add a WPA2 fallback). OWE / Enhanced Open maps to
the first-class `ENHANCED_OPEN` opmode — it is **encrypted**, so it must never
be folded into `OPEN`. Classic has its own equivalent, `enhanced-open`, used
verbatim in HPE's Classic Central reference
(`Classic-Central/wlan_config/configurations/enhanced_captive.yaml` and
`ap_config/configurations/open-captive-portal.txt`), so both destinations
preserve the encryption and neither blocks an OWE SSID. `opmode_transition_
disable` stays set, so an OWE SSID never also advertises a legacy open BSS.

**Config API version fallback.** HPE's published v26.04 reference puts scope
management (`sites`, `device-groups`, `global`, `site-collections`) on `/v1`
and feature configuration (`wlan-ssids`, `layer2-vlan`, `overlay-wlan`,
`scope-maps`, …) on `/v1alpha1`, while this repo's paths were runtime-verified
against a live tenant serving `/v1`. Both can be true — the surface is Select
Availability. So `scope-maps`, `layer2-vlan/{id}`, `wlan-ssids/{name}` and
`overlay-wlan/{name}` go through `_config_request()`, which tries a version,
falls through on a **404 only**, and caches whichever answered for the rest of
the run. Keep the fallback even if a tenant answers `v1` for all four: it costs
one 404 on the first call per run and removes the whole class of breakage when
HPE promotes or retires a route.

**Known divergence — `scope-map` body.** HPE's schema and
`pycentral/scopes/scope_maps.py` send `{"scope-name": str(scope_id), "persona",
"resource"}` with **no** `scope-id` member; this client also sends a numeric
`scope-id`. It is accepted by the tenants this tool has run against. If a
tenant 400s on the extra key, drop it. HPE now documents
`POST /network-config/v1alpha1/config-assignments` as the successor — that is a
separate migration with its own delete-path semantics, not a drop-in swap.

### Runtime-verify caveats (New Central)

| Behaviour | Why |
|---|---|
| Resolve global scope first | Proves config access before any write; if it fails, `provision()` returns immediately. |
| Firmware compliance POST → PATCH on 412/duplicate | Already set for the scope; PATCH updates the version. |
| Site id re-list after create | POST bodies don't always echo the id; a duplicate error triggers a refreshed re-list. |
| Duplicate scope-maps / objects | "already exists"/"duplicate" **in the response detail** are treated as idempotent success (the URL path is ignored so customer-named objects can't fake it). For SSIDs the duplicate-PATCH path is additionally manifest-gated (below). |
| Same-name SSID reuse / PATCH | With a manifest attached (Step 3 always attaches one), a duplicate SSID is PATCHed only when the manifest owns it; otherwise the step fails with a collision refusal and Step 3 offers explicit adoption. Without a manifest, legacy name-only idempotency. |
| 2xx with a non-JSON body | Raises `CentralAPIError` — every API response is JSON, so an unparseable success body is a protocol violation, not `{"_raw": ...}`. |
| Persona/site assignment in the devices phase | Both need claimed APs, so they run with the Step 4 cutover move (also in `phase="all"`). |

---

## Classic Central — v3 groups / full_wlan / sites / firmware / monitoring

| Method | Path | Purpose |
|---|---|---|
| POST | `/oauth2/token` (query string) | Refresh: `client_id`, `client_secret`, `grant_type=refresh_token`, `refresh_token` in the **query string**, empty body. Returns a **new** refresh token. |
| GET | `/configuration/v2/groups` | List group names (response is a list of single-element name lists). |
| POST | `/configuration/v3/groups` | Create AOS10 group. `Architecture` is a **single scalar** inside `group_properties` (alongside `AllowedDevTypes`), not a per-section value. |
| GET | `/configuration/v1/groups/properties` | Read back `Architecture` to verify the create actually applied. |
| POST | `/platform/device_inventory/v1/devices` | Pre-add serial+MAC pairs to inventory (duplicates fine). |
| POST | `/configuration/v1/devices/move` | Move serials into a group. **Hard cap of 50 serials per request** (HPE returns 400 "More than 50 devices cannot be moved to a group"), so the client chunks. |
| GET/POST | `/central/v2/sites` | List / create site (`site_address` **or** zeroed `geolocation` — mutually exclusive, one required). |
| POST | `/central/v2/sites/associations` | Associate devices (`device_type="IAP"`, `device_ids`). |
| POST | `/configuration/full_wlan/{group}/{name}` | Create WLAN (see wrapper quirk below). |
| POST | `/firmware/v2/upgrade/compliance_version` | Firmware compliance. Fallback chain on 404/405: `/firmware/v1/upgrade/compliance_version`, then `/firmware/v1/set-firmware-compliance` (`device_type` + `firmware_version` + `group`, no scheduling fields — the form verified in HPE's own AOS 10 migration pipeline; some tenants serve only this one). `device_type="IAP"` for APs (incl. AOS 10). |
| GET | `/monitoring/v2/aps` | Validation: `{"aps":[...]}` with status `Up`/`Down`. |

`OPMODE_CLASSIC` maps `AuthType` → classic opmode (`opensystem`, `wpa2-psk-aes`,
`wpa3-sae-aes`, `wpa2-aes`, `wpa3-aes-ccm-128`).

`provision()` runs in **two phases** (`phase="config|devices|all"`), mirroring
the New Central client:

- **`phase="config"`** (Step 3) builds configuration ONLY: inventory pre-add,
  sites, groups, WLANs, firmware compliance. Nothing touches the APs, so Step
  3's "no APs are claimed, moved or rebooted" banner is actually true. The
  manual follow-ups (RADIUS servers, gateway-cluster binding) are config-phase
  output so the Step 4 cutover gate can track them as outstanding work.
- **`phase="devices"`** (the Step 4 classic cutover) moves APs into their
  groups and associates them with the site. It is fail-closed: a group the
  config phase didn't create aborts that group's move with a "run the config
  phase first" error instead of building half a tenant, and a failed site
  read is surfaced as an error rather than reading as "no sites" (which would
  silently skip every site assignment mid-cutover).
- **`phase="all"`** (the default) keeps the legacy single-pass behavior.

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
`wpa_passphrase`, enterprise `access_type`/`auth_server1`,
`mac_authentication` + `access_type`/`auth_server1` for MAC-auth SSIDs, and
`cluster_name` for tunnel SSIDs). Three `_BASE_WLAN` defaults are deliberately
NOT shipped verbatim: `high_throughput_disable`,
`very_high_throughput_disable` and `high_efficiency_disable` are forced to
`False` — the YAML disables 802.11n/ac/ax, which would cap every migrated
client at legacy ~54 Mbps rates (HPE's own migration pipeline omits the keys
entirely). Two hard rules: a WEP SSID raises instead of provisioning (no AOS
10 opmode exists — preflight FAILs it first), and enterprise `auth_server1`
references a single RADIUS server OBJECT by name — the Classic API cannot
create server objects, so preflight FAILs enterprise/MAC-auth SSIDs on a
Classic destination until the operator creates a server with that exact name
in the group by hand. `access_rule` is a full flat object (`_BASE_ACCESS_RULE`) with the
SSID name filled in.

### Runtime-verify caveats (Classic)

| Behaviour | Why |
|---|---|
| **403 on a `full_wlan` path** | The classic WLAN config APIs are **allowlisted per tenant**. The client raises a clear message: ask your Aruba SE to enable them for the account. (Other 403s raise the generic error.) |
| Group-create Architecture readback | A known flaw lets the v3 create return success **without applying**. After creating, the client reads `/configuration/v1/groups/properties`; it raises only if `Architecture` is confirmed to be something other than `AOS10` (readback transport errors don't fail the step). |
| Firmware v2 → v1 fallback | On 404/405 the client retries the v1 compliance endpoint. |
| 401 → refresh → retry | On 401 the client attempts a token refresh and retries once. |
| 2xx with a non-JSON body | Raises `ClassicCentralAPIError` — flattening to `{}` made `list_group_names` return `[]` and `create_group` re-POST. |
| Pre-existing group / duplicate WLAN | With a manifest attached, reuse of a same-named group and the duplicate-WLAN swallow are allowed only for manifest-owned (or explicitly adopted) objects — a foreign same-name object fails the step with a collision refusal. |
| Refresh token rotation | Each refresh returns a new refresh token; `self.refresh_token` holds the newest. Views read it back and persist it to the session. |
| RADIUS auth-servers / GW clusters | **Cannot** be created via the classic API. Preflight **FAILs** enterprise/MAC-auth SSIDs on a Classic destination until the operator hand-creates a RADIUS server named exactly like the source server group in each group (`full_wlan` references it by name), and `provision()` still appends the MANUAL FOLLOW-UP (gateways auto-cluster on join — verify tunnel SSID binding). |
| Tunnel WLAN `cluster_name` | Set on the WLAN but unverified by any reference example — confirm in the Central UI. |

---

## HPE GreenLake Platform (GLP) — devices + subscriptions

Base is always `https://global.api.greenlake.hpe.com` regardless of Central
region.

| Method | Path | Purpose |
|---|---|---|
| GET | `/devices/v1/devices` | List / filter devices (`filter=serialNumber eq '<s>'`). |
| POST | `/devices/v1/devices` | Claim network devices → **202** + `Location: /devices/v1/async-operations/{id}`. |
| GET | `/devices/v1/async-operations/{id}` | Poll the operation (10s interval, 5 min timeout). HPE's `AsyncOperationResource.status` enum is `INITIALIZED, RUNNING, FAILED, SUCCEEDED, TIMEDOUT, PAUSED`, but HPE's own prose says `TIMEOUT` and the New Central guide says `TIMED_OUT` — the client normalises the value to letters only and matches on that, never on a literal spelling. `PAUSED` is non-terminal and is surfaced through the poll callback. A body with no recognisable status raises rather than polling to the deadline. |
| GET | `/subscriptions/v1/subscriptions` | List subscriptions; resolve a key → UUID (`filter=key eq '<k>'`). |
| GET | `/service-catalog/v1/service-manager-provisions` | Central application instances (id + region) in the workspace. The `/v1beta1/` route was retired (EOL 2025-06-30) and is no longer tried; a 403 here raises instead of reporting "no Central instances". |
| PATCH | `/devices/v2beta1/devices?id=<uuid>` | **Two sequential merge-patches** (GLP rejects combining them): 1) `{"application":{"id":…},"region":…}` — REQUIRED for Central to adopt the AP; 2) `{"subscription":[{"id": <uuid>}]}`. Each is polled to a terminal state when GLP answers 202 (the returned `Location` path is honored as-is). |

A `SUCCEEDED` batch can still carry a per-device breakdown: there is no
`PARTIAL_SUCCESS` status, so `poll_task` returns the result unchanged and the
views record a failed step naming `failed_serials(result)`. Only a batch where
**every** submitted device was rejected raises.

GreenLake token lifetimes differ by scope and neither has a refresh token:
**Central**-scoped clients get `expires_in: 7199` (2 h), **GLP**-scoped clients
get `expires_in: 899` (15 min). Since one claim can block for 5 minutes and
Step 4 loops per AP, both clients track `expires_in` and re-authenticate 60 s
before expiry instead of waiting for a mid-run 401.

### Runtime-verify caveats (GLP)

| Behaviour | Why |
|---|---|
| `macAddress` **required** to claim | The client raises before submitting any device without a MAC — re-discover with `show ap database long`. |
| Claim is async + reconciled | The tool polls the async-operation, then re-reads the workspace inventory and reconciles **submitted serials vs. actual workspace** — it never trusts the async body shape alone. Serials missing post-claim are flagged. The reconciliation read is fail-closed: if it fails, the run **aborts** (no devices are assigned or moved) — a failed verification must never expand the mutation set beyond positively confirmed serials. |
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
