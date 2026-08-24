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

> ⚠️ **The New Central configuration APIs are an ALPHA interface.** HPE's
> developer portal banners `/network-config` as "subject to change at any
> moment". Preflight surfaces this as a warning; re-run it immediately before
> cutover, because a tenant-side schema change can break a rehearsed
> migration. Provisioning negotiates both the shape published in HPE's
> reference collection (`POST /network-config/v1alpha1/<resource>` with a
> wrapper array, e.g. `{"wlan-ssid":[{…}]}`) and the older singular form
> (`POST /network-config/v1/<resource>/{name}`), pinning whichever the tenant
> accepts for the rest of the run.
>
> The read-only API probe in Step 1 reports which shape your tenant exposes
> ("Config API shape — v1alpha1 vs v1") before you write anything. Run it
> first if provisioning is failing with 404s.

## Migration destinations

Step 1 lets you pick where the migration lands. Both are full destinations —
Classic is not merely a hybrid helper:

| Destination | What it creates | Notes |
|---|---|---|
| **New Central** (default) | Device groups/scopes, scope-mapped SSIDs, VLANs, roles/policies, overlay-wlan → GW cluster | Uses the alpha `/network-config` APIs above |
| **Classic Central** | `v3` groups with `Architecture=AOS10`, sites, `full_wlan` SSIDs, inventory pre-add, device moves, firmware compliance | Uses the API Gateway (`apigw-*`); tokens last ~2h and the refresh token rotates |

A **hybrid** tenant is a third case rather than a destination: New Central is
the target, but device-group create/move is routed through the Classic API
Gateway. Enable it in Step 1 ("Hybrid cluster?") — it only engages when the
tenant is explicitly marked hybrid *and* usable Classic credentials exist.

### Security properties preserved in translation

OWE / Enhanced Open maps natively in both destinations (`ENHANCED_OPEN` on New
Central, `enhanced-open` on Classic) with OWE transition mode disabled, so an
encrypted open SSID stays encrypted. MAC authentication has no equivalent and
*does* land as a plain OPEN WLAN — preflight raises a blocker that the operator
must consciously override.

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

Two built-in login options — no OAuth, no IdP:

**Simplest — one shared password (`AOS8_AUTH_MODE=password`, the default).** Set
`AOS8_APP_PASSWORD` and everyone uses that one password to get in. No
registration, no email. There's no per-person identity, so saved creds are a
single shared store and audit lines are attributed to a generic `team`.

```bash
cp .env.example .env        # set AOS8_APP_PASSWORD
docker compose up --build
```

**Per-person — self-service login (`AOS8_AUTH_MODE=accounts`).**
Users register with a verified email; a 6-digit code is emailed to confirm the
address, then they set a password. The signed-in email scopes the per-user
encrypted credential store and the audit log. Needs email (below).

How accounts mode works and what to know:

- **Verified registration.** Open to any valid email by default. Set
  `AOS8_ALLOWED_EMAIL_DOMAIN=example.com` to restrict to one domain. The
  emailed code proves ownership so someone can't register a colleague's address.
  Passwords are stored scrypt-hashed with a per-user salt; codes are
  short-lived and hashed.
- **HTTPS via Caddy (recommended).** Passwords/codes traverse the connection.
  The compose file binds the app to `127.0.0.1:8501`; put **Caddy** in front
  to terminate HTTPS and reverse-proxy to it — `deploy/Caddyfile` is a ready
  example (Caddy handles the websockets Streamlit needs automatically). Never
  serve plain `:8501` to users.
- **Verification email.** The **From can be any account** (Gmail, throwaway,
  transactional provider — anything that can SMTP-send).
  - **Gmail (easiest + reliable):** `AOS8_SMTP_MODE=relay`,
    `AOS8_SMTP_HOST=smtp.gmail.com`, port `587`, user/from = your Gmail address,
    pass = a Google **App Password** (Security → App passwords).
  - **Transactional provider** (SendGrid/Resend/Brevo free tier) — same shape,
    a verified sender domain.
  - **`AOS8_SMTP_MODE=direct`** — no account at all; the app does the MX lookup
    and delivers itself. May be spam-filtered from an unauthenticated IP; set
    `AOS8_SMTP_FROM` to a domain you control.
  - With nothing set, registration cannot complete. Verification codes are
    **not** written to logs; set `AOS8_ALLOW_CONSOLE_CODES=true` for local
    development only.
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
mode above is the recommended path. **The proxy must strip the identity header
on inbound requests** and only re-add it after authenticating — otherwise a
client can set it themselves and impersonate any user. `deploy/Caddyfile` shows
the required `request_header -X-Forwarded-Email` lines.

### Running the tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -q          # unit + security regression suite
pyflakes app.py lib views # lint (also enforced in CI)
```

CI (`.github/workflows/ci.yml`) byte-compiles every module, lints, and runs the
test suite on Python 3.12 and 3.13 for each push and pull request.

### Environment variables

| Var | Default | Purpose |
|---|---|---|
| `AOS8_AUTH_MODE` | `local` | `password` = one shared gate password; `accounts` = per-person login; `proxy` = reverse-proxy header; `local` = single user. An unrecognised value is **refused at startup** (fail-closed) |
| `AOS8_APP_PASSWORD` | _(unset)_ | The shared password for `password` mode. Must be ≥16 chars and not the example placeholder — the app refuses to start otherwise |
| `AOS8_ALLOWED_EMAIL_DOMAIN` | _(unset — any email)_ | Restrict registration to one domain in `accounts` mode (e.g. `example.com`) |
| `AOS8_ALLOW_CONSOLE_CODES` | _(unset = off)_ | **Dev only.** Print verification codes to the server log when email delivery fails. Off by default so live codes never reach logs |
| `AOS8_CA_BUNDLE` | _(unset)_ | CA bundle used to verify the AOS 8 controller certificate (verification is on by default) |
| `AOS8_INSECURE_TLS` | _(unset = off)_ | Lab escape hatch — disables AOS 8 certificate verification entirely |
| `AOS8_USERS_FILE` | `~/.aos8-migration/users.json` | Path to the user registry (put on a persistent volume) |
| `AOS8_SMTP_MODE` | `relay` | `direct` = MX-lookup delivery (no relay); `relay` = send via `AOS8_SMTP_HOST` |
| `AOS8_SMTP_FROM` | _(sending host)_ | From address on verification emails — set to your sender (e.g. a gmail address); do **not** use @hpe.com |
| `AOS8_SMTP_HOST` / `_PORT` / `_USER` / `_PASS` | _(unset)_ / `587` / — / — | `relay` mode SMTP server. No host (and not `direct`) ⇒ codes logged to console (dev only) |
| `AOS8_CREDSTORE_KEY` | _(unset)_ | Fernet key enabling per-user encrypted "Remember". Unset in a multi-user mode = persistence off |
| `AOS8_IDENTITY_HEADER` | `X-Forwarded-Email` | (`proxy` mode only) the single trusted identity header; the proxy must set **and** inbound-strip it |
| `AOS8_LOCAL_USER` | `local@localhost` | Principal used to scope the credstore in `local` mode |
