# GreenLake Onboarding (GLP)

[[Migration Paths|Step 4]]. Claim APs into the HPE GreenLake Platform ([[Glossary|GLP]]) workspace and
assign subscriptions. **Converted APs are only adopted by Central if they exist
in the workspace WITH a subscription.** Code: `lib/glp_client.py` (`GLPClient`),
`views/p4_greenlake.py`. See [[Tool Internals]].

## Why it's required

After [[Source - Mobility Controller|ap convert]] (or the [[Source - Instant IAP|Instant push]]) an AP reaches Activate and tries
to register in Central. If its serial isn't in the GreenLake workspace with an
active subscription, Central won't adopt it. So this step runs **before**
conversion (recommended order: provision → claim → convert).

## Auth + base

- **Token** — `POST https://sso.common.cloud.hpe.com/as/token.oauth2`,
  `client_credentials`. Can reuse the [[Destination - New Central|New Central]] API client if it's a unified
  GreenLake client (Step 4 checkbox), else a separate GLP client.
- **Base** — `https://global.api.greenlake.hpe.com`.

## Claim (serial + MAC, async)

`add_devices` → `POST /devices/v1/devices` with
`{"network":[{serialNumber, macAddress}], "compute":[], "storage":[]}`.

- **`macAddress` is REQUIRED** for network devices — comes from the Wired MAC
  column of `show ap database long`. The client refuses to claim a device
  without a MAC. APs discovered via `show ap active` (no serial/MAC) **can't be
  auto-claimed** — see [[Preflight Checks|serial coverage]].
- Returns **202 Accepted** with a `Location:
  /devices/v1/async-operations/{id}`. `poll_task` polls that op every 10s (5 min
  timeout) until `completed`/`success`/`succeeded` (or raises on
  failed/error/timeout). The UI streams poll progress.

## Workspace reconciliation

The async-op body shape isn't trusted alone. After the claim, the tool calls
`workspace_serials()` (`GET /devices/v1/devices`, paginated, uppercased) and
diffs the **submitted serials against the actual workspace inventory** →
reports claimed vs NOT-in-workspace. "Check workspace" can also be run first to
mark APs already claimed (via CSV/UI) so only the delta is claimed.

## Subscription assignment

1. List subs — `GET /subscriptions/v1/subscriptions`. The UI shows active subs
   only (status ≠ ENDED), AP-type tiers first.
2. `assign_subscription(serial, key_or_id)`:
   - resolve the subscription: canonical UUIDs pass through; a **key** is
     resolved via OData filter `key eq '<key>'`.
   - resolve the device's GLP id from its serial (cached; must already be in the
     workspace — claim first).
   - `PATCH /devices/v2beta1/devices?id=<device-uuid>` with
     `{"subscription":[{"id": <sub-uuid>}]}` and
     `Content-Type: application/merge-patch+json`.
   - Targets only APs actually in the workspace (claim results ∪ snapshot).

## Optional / classic note

The step is optional in the UI ("already claimed via CSV/GreenLake UI? just
continue"). For [[Destination - Classic Central|classic destinations]], Step 3 already pre-added the
serial+MAC pairs to the classic inventory; GreenLake claiming still applies to
GLP-onboarded classic accounts (most current ones).

## Related
[[Migration Paths]] · [[Source - Mobility Controller]] · [[Source - Instant IAP]] ·
[[Destination - New Central]] · [[Destination - Classic Central]] · [[Preflight Checks]] · [[Glossary]]
