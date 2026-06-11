# AOS 8 → Aruba Central Migration Console

Web-based wizard for migrating customers from AOS 8 to AOS 10 on
**New Central** (HPE GreenLake). Supported source platforms:

- **Mobility Controller / Conductor** — `ap convert` path, with the choice to
  keep the MCs as AOS 10 gateways (overlay SSIDs) or retire them (all bridge)
- **Instant cluster (IAP)** — Central-driven conversion: claim + subscribe in
  GreenLake, pre-assign to an AOS 10 device group, Central pushes the image.
  No controller commands, no gateways. Zones map to device groups.

## Quick Start

```bash
pip install -r requirements.txt
streamlit run app.py
```

Open http://localhost:8501 in your browser.

## Wizard Steps

| Step | What it does |
|---|---|
| 1. Connect & Discover | Pulls AOS 8 config via REST API (or CLI paste fallback) — SSIDs with per-group bindings, auth types/PSKs, AP inventory with serials, VLANs, RADIUS, cluster topology |
| 2. Preflight Checks | AP model compatibility, firmware train check (8.10 ≥ .0.12 / 8.12 ≥ .0.1), SSID mapping/auth coverage, serial coverage, cluster sequencing warnings |
| 3. Provision Central | Creates the site, device groups (one per AP group), VLANs, overlay/underlay SSIDs, gateway cluster, auth-server profiles and firmware compliance in **New Central** — every API failure is reported per step |
| 4. GreenLake Onboarding | Claims the APs into the GLP workspace (serial + wired MAC, async claim with polling) and assigns subscriptions — required for Central to adopt converted APs |
| 5. AP Convert Runbook | Customer-specific `ap convert` CLI runbook (single MC, L2 or L3 cluster sequencing) |
| 6. Validate | Confirms converted APs are online in Central by serial; post-migration checklist |

## AOS 8 API Access

The tool logs in at `https://<mc-ip>:4343/v1/api/login` and reads configuration
via `/v1/configuration/object/...` and `showcommand` with the UIDARUBA session
token. On a Mobility Conductor use `config_path=/md` (default); on a standalone
controller set it to `/mm/mynode` (Advanced options in Step 1).

If port 4343 is firewalled or the API is disabled, use **Paste CLI output**
mode in Step 1. Recommended commands to paste:

```
show running-config
show ap database long        # includes Group, Serial #, Wired MAC
show version
show lc-cluster group-membership
show controller-ip
show aaa authentication-server all
```

## New Central API Credentials

Create API client credentials in HPE GreenLake (Manage → API) with access to
the Aruba Central service. The tool authenticates against
`sso.common.cloud.hpe.com` (client-credentials grant) and calls your
**regional** New Central base URL, e.g.
`https://us4.api.central.arubanetworks.com`.

Provisioning maps AOS 8 constructs onto the New Central model:

| AOS 8 | New Central |
|---|---|
| ap-group | Device group (scope) |
| virtual-ap (tunnel/split) | Overlay SSID + role/policy + overlay-wlan → GW cluster |
| virtual-ap (bridge) | Underlay SSID scope-mapped to the device group |
| VLAN | layer2-vlan profile scope-mapped to the group |
| RADIUS server | auth-server library profile |
| MC cluster | Gateway cluster (in its own `-gws` device group) |

## Deployment

### Single user (laptop / one engagement)

```bash
# Docker
docker build -t aos8-migration .
docker run -p 8501:8501 aos8-migration

# Or just run locally:
streamlit run app.py
```

In this default (`AOS8_AUTH_MODE=local`) mode there is no app login. Live
credentials stay in the Streamlit session only. The optional **Remember**
toggle persists *destination* API creds (client id/secret + Classic refresh
token, never source-side secrets) to `~/.aos8-migration/<user>/credentials.json`,
**encrypted at rest** with a private auto-generated key. Uncheck to delete.

### Multi-user (Docker farm, concurrent engineers)

Run behind the bundled **oauth2-proxy** so every session maps to a verified
SSO identity. The app trusts **one** header — `X-Forwarded-Email` — which
oauth2-proxy injects *and strips from inbound client requests* in `--upstream`
mode. That stripping plus the network boundary is what makes the identity
non-spoofable, so the app container must be reachable **only** through the
proxy. The compose file enforces this: the app uses `expose:` (never `ports:`)
and sits on a dedicated `appnet` bridge shared with nothing but the proxy.
**This network boundary is load-bearing for identity integrity — don't attach
other containers to `appnet`, and never publish the app's `8501`.**

```bash
cp .env.example .env        # fill in OIDC issuer/client + COOKIE_SECRET
# optional: set AOS8_CREDSTORE_KEY to enable per-user encrypted "Remember"
docker compose up --build
```

Key properties in this mode (`AOS8_AUTH_MODE=proxy`):

- **Per-user credential isolation.** Saved creds are keyed and encrypted per
  authenticated user; one engineer's tenant secrets never load into another's
  session. With no `AOS8_CREDSTORE_KEY` set, credential persistence is disabled
  entirely (session-only) — a fail-safe so nothing is written to a shared
  volume without an operator-provisioned key.
- **Sticky sessions required if you scale out.** Streamlit sessions are
  websocket-bound to one replica. The single-replica compose file needs no
  stickiness; if you run multiple `app` replicas, pin each user to one replica
  (cookie/IP affinity) at your load balancer or in-flight migrations are lost
  on reconnect.
- **Audit trail.** Sensitive actions (provision, cutover, claim, cleanup) are
  emitted as JSON audit lines to stdout, tagged with the signed-in user — wire
  your container logs into your log pipeline.
- **Saved creds are ephemeral by default.** No volume is mounted at
  `/home/appuser/.aos8-migration`, so "Remember" resets on every container
  restart/redeploy. To persist it, mount a named volume there (0700) and keep
  `AOS8_CREDSTORE_KEY` stable across deploys (a new key makes old files
  undecryptable, which just falls back to empty — no crash).

### Environment variables

| Var | Default | Purpose |
|---|---|---|
| `AOS8_AUTH_MODE` | `local` | `proxy` = require an SSO identity header (multi-user); `local` = single user |
| `AOS8_IDENTITY_HEADER` | `X-Forwarded-Email` | The single request header trusted as the verified identity in proxy mode. Must be one the proxy sets **and** inbound-strips |
| `AOS8_CREDSTORE_KEY` | _(unset)_ | Fernet key enabling per-user encrypted "Remember". Unset in proxy mode = persistence off |
| `AOS8_LOCAL_USER` | `local@localhost` | Principal used to scope the credstore in local mode |
