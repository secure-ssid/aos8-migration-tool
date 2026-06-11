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

The app has its **own self-service login** (`AOS8_AUTH_MODE=accounts`) — no
OAuth, no IdP. Engineers register with an **@hpe.com** email; a 6-digit code is
emailed to confirm the address is really theirs, then they set a password and
sign in. The signed-in email becomes the identity that scopes the per-user
encrypted credential store and the audit log.

```bash
cp .env.example .env        # set SMTP (verification emails) + AOS8_CREDSTORE_KEY
docker compose up --build
```

How it works and what to know:

- **Verified registration.** Open to `@hpe.com` only (set
  `AOS8_ALLOWED_EMAIL_DOMAIN` to change). The emailed code proves ownership, so
  someone can't register a colleague's address. Passwords are stored
  scrypt-hashed with a per-user salt; codes are short-lived and hashed.
- **HTTPS via Caddy (recommended).** Passwords/codes traverse the connection.
  The compose file binds the app to `127.0.0.1:8501`; put **Caddy** in front to
  terminate HTTPS and reverse-proxy to it — `deploy/Caddyfile` is a ready
  example (Caddy upgrades the websockets Streamlit needs automatically). Never
  serve plain `:8501` to users.
- **Verification email — two ways, no corporate relay needed:**
  - `AOS8_SMTP_MODE=direct` — the app looks up the recipient domain's MX and
    delivers itself (no relay). Simplest, but a gateway like **Proofpoint
    (hpe.com)** usually rejects mail from an unsanctioned IP, so this is
    reliable only when the app's egress IP is an authorized sender for
    `AOS8_SMTP_FROM` (e.g. running inside the org network).
  - `AOS8_SMTP_MODE=relay` (default) + `AOS8_SMTP_*` — hand off to any SMTP
    server: a free transactional provider (SendGrid/Mailgun/Brevo/Resend — gives
    you a verified sender + good deliverability) or your own mailbox via an app
    password. **This is the dependable path.**
  - With neither set, codes are written to the **container log only** (dev).
- **Per-user credential isolation.** Saved creds are keyed and encrypted per
  signed-in user; one engineer's tenant secrets never load into another's
  session. With no `AOS8_CREDSTORE_KEY`, persistence is disabled entirely
  (session-only) — a fail-safe.
- **Persistence.** The `aos8_state` volume holds `users.json` + the encrypted
  cred files. Without it, accounts and saved creds reset on redeploy. Keep
  `AOS8_CREDSTORE_KEY` stable across deploys.
- **Audit trail.** Sensitive actions (provision, cutover, claim, cleanup) are
  emitted as JSON audit lines to stdout, tagged with the signed-in user.
- **Scaling.** Streamlit sessions are websocket-bound to one replica. If you
  scale `app`, pin each user to one replica (cookie/IP affinity) at the LB and
  share the volume so all replicas see the same accounts.

> Login lasts for the browser session — a full page refresh signs the user out
> and they log back in (no cookie/JWT persistence yet). Ask if you want
> stay-signed-in across refreshes.

A header-injecting reverse proxy is also supported as an alternative
(`AOS8_AUTH_MODE=proxy` + `AOS8_IDENTITY_HEADER`), but the built-in `accounts`
mode above is the recommended path.

### Environment variables

| Var | Default | Purpose |
|---|---|---|
| `AOS8_AUTH_MODE` | `local` | `accounts` = built-in self-service login (multi-user); `proxy` = trust a reverse-proxy identity header; `local` = single user |
| `AOS8_ALLOWED_EMAIL_DOMAIN` | `hpe.com` | Email domain allowed to register in `accounts` mode |
| `AOS8_USERS_FILE` | `~/.aos8-migration/users.json` | Path to the user registry (put on a persistent volume) |
| `AOS8_SMTP_MODE` | `relay` | `direct` = MX-lookup delivery (no relay); `relay` = send via `AOS8_SMTP_HOST` |
| `AOS8_SMTP_FROM` | `no-reply@hpe.com` | From address on verification emails |
| `AOS8_SMTP_HOST` / `_PORT` / `_USER` / `_PASS` | _(unset)_ / `587` / — / — | `relay` mode SMTP server. No host (and not `direct`) ⇒ codes logged to console (dev only) |
| `AOS8_CREDSTORE_KEY` | _(unset)_ | Fernet key enabling per-user encrypted "Remember". Unset in a multi-user mode = persistence off |
| `AOS8_IDENTITY_HEADER` | `X-Forwarded-Email` | (`proxy` mode only) the single trusted identity header; the proxy must set **and** inbound-strip it |
| `AOS8_LOCAL_USER` | `local@localhost` | Principal used to scope the credstore in `local` mode |
