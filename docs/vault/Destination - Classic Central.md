# Destination — Classic Central (apigw)

`destination="classic"`. The legacy [[Migration Paths|destination]] on
`apigw-*.central.arubanetworks.com`. Code: `lib/classic_central_client.py`
(`ClassicCentralClient`). Config model is **v3 UI groups** + `full_wlan`, not
the [[Destination - New Central|New Central scope model]]. See [[Tool Internals]].

## Auth — token + rotating refresh token

- **Access token** — generated in the API Gateway UI (System Apps & Tokens),
  valid **~2h**. There is **no client_credentials grant** on classic.
- **Refresh** — `POST /oauth2/token` with `client_id`, `client_secret`,
  `grant_type=refresh_token`, `refresh_token` **in the QUERY STRING** (empty
  body). On 401 the client auto-refreshes once.
- **The refresh token ROTATES** — it is single-use; the response returns a NEW
  one that must be captured. The tool reads `client.refresh_token` back after a
  run and, if it changed, re-saves it in session and warns the operator to
  update wherever they store it (Step 3 / Step 6). Needs client ID+secret too.

## v3 AOS10 groups (+ readback flaw)

`create_group` → `POST /configuration/v3/groups` with
`group_properties.Architecture = "AOS10"`, `AllowedDevTypes` =
`["AccessPoints"]` (+ `["Gateways"]` when [[Gateway Strategy|keeping
gateways]]). **No `GwNetworkRole` is ever sent** — Central 3.x rejects a
WLAN/AOS10 group carrying the branch-gateway role; wireless gateways are
MOBILITY_GW and cluster at runtime, not via a group property.
Existence is checked via `GET /configuration/v2/groups`
(which returns a list of single-element name lists; `unprovisioned` filtered out).

> **Readback flaw** — invalid property combos return **200 without applying**.
> After create, the tool reads `GET /configuration/v1/groups/properties` and, if
> `Architecture` confirms a value that is **not** `AOS10`, raises an error
> telling the operator to delete the group and check the tenant supports AOS10.
> The readback itself is best-effort — transport errors don't fail the step;
> only a *confirmed* wrong architecture does.

## WLANs — `full_wlan` value-wrapper payload

`create_wlan` → `POST /configuration/full_wlan/{group}/{name}`. The body is the
**entire flat WLAN object JSON-stringified under a `"value"` key**:

```python
{"value": json.dumps({"wlan": {...}, "access_rule": {...}})}
```

`_BASE_WLAN` is a verbatim ~90-field template (from HPE's
central-python-workflows); per-SSID fields override `name`, `essid`, `index`,
`opmode` (`OPMODE_CLASSIC`: opensystem / wpa2-psk-aes / wpa3-sae-aes / wpa2-aes
/ wpa3-aes-ccm-128), `vlan`, `hide_ssid`, passphrase, and for enterprise
`access_type=network_based` + `auth_server1`. Tunnel/split SSIDs set
`cluster_name` (verify the binding in the UI — no verbatim reference exists).
WLANs are keyed by **[[Glossary|ESSID]]**; duplicate ESSIDs in a group are skipped (see
[[Preflight Checks|duplicate ESSID]]).

> **Allowlist caveat** — the classic WLAN config APIs are **allowlisted per
> tenant**. A `403` on a `full_wlan` path means the tenant needs the API enabled
> by an Aruba SE. The client raises a specific message for this.

## Sites, inventory, firmware

- **Inventory pre-add** — `POST /platform/device_inventory/v1/devices` with
  `{serial, mac}` pairs (so this path can run even without
  [[GreenLake Onboarding]]). Duplicates are fine.
- **Move to group** — `POST /configuration/v1/devices/move`.
- **Sites** — `POST /central/v2/sites`; associate via
  `POST /central/v2/sites/associations` with `device_type="IAP"`. (Address and
  geolocation are mutually exclusive — defaults to zeroed geolocation when no
  address.)
- **Firmware compliance** — `POST /firmware/v2/upgrade/compliance_version`
  (v1 fallback). `device_type` for APs is **`"IAP"`** even on AOS 10.
- **Monitoring** — `GET /monitoring/v2/aps` → `{"aps":[...]}`, status Up/Down
  (used by Step 6 validation).

## Manual follow-ups (classic can't automate)

`provision()` appends MANUAL FOLLOW-UP entries to the result log:

- **RADIUS auth-servers** — must be created in each group (Group → Devices →
  Config → Security); enterprise WLANs reference them by name. See
  [[RADIUS and NAD Changes]].
- **Gateway cluster auto-form** — when [[Gateway Strategy|keeping gateways]], gateways
  **auto-cluster when moved into the group**. Verify tunnel SSIDs bind to the
  cluster in the group WLAN config. (Contrast New Central, where the
  [[Destination - New Central|overlay-wlan + gateway-cluster]] objects are created explicitly.)

## GreenLake note

Most current classic accounts are GLP-onboarded, so [[GreenLake Onboarding]] still
applies. If the account predates GreenLake onboarding, the Step 3 inventory
pre-add covers it and you can skip Step 4.

## Related
[[Migration Paths]] · [[Destination - New Central]] · [[GreenLake Onboarding]] ·
[[Gateway Strategy]] · [[Preflight Checks]] · [[Glossary]]
