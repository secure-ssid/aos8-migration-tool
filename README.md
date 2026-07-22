# AOS 8 → Aruba Central Migration Console

**A guided web wizard that migrates Aruba AOS 8 wireless networks (Mobility
Controllers or Instant APs) to AOS 10 on Aruba Central — discovery, safety
checks, Central provisioning, GreenLake onboarding, conversion runbook, and
validation, in six steps.**

Migrating by hand means reading an old controller config, rebuilding it
object-by-object in Central, registering every AP in HPE GreenLake, and
running conversion commands in exactly the right order. This tool automates
the tedious parts and generates the commands for the rest — and it shows you
everything it's about to do before it does it.

<img src="docs/screenshots/02-connect-discovered.png" alt="Step 1 — a discovered AOS 8 deployment: AP groups, SSIDs, APs with compatibility badges" width="900">

## New here? Two links

- 🚀 **[Getting Started](docs/GETTING-STARTED.md)** — zero to a full simulated
  migration in 5 minutes, using the built-in test customer. No controller, no
  tenant, no risk.
- 📖 **[Migration Guide](docs/MIGRATION-GUIDE.md)** — the full operator
  walkthrough for real migrations, with a screenshot and numbered click-path
  for every step.

## The six steps

| # | Step | What happens | Writes anything? |
|---|---|---|---|
| 1 | **Connect & Discover** | Pulls the AOS 8 config over the REST API, or parses pasted CLI output. Shows everything it found. | No |
| 2 | **Preflight Checks** | Pass/warn/fail report: AP hardware compatibility, firmware minimums, auth coverage, VLAN conflicts, cluster sequencing. | No |
| 3 | **Build Config** | Creates sites, device groups, VLANs, SSIDs, RADIUS profiles and firmware compliance in Central — after showing you the full manifest. Every API call is logged with its result. | Central tenant |
| 4 | **Onboard APs** | Claims APs into HPE GreenLake (serial + MAC), assigns the Central application + subscription, and at cutover moves them into their groups. | GreenLake + Central |
| 5 | **Runbook** | Generates the customer-specific `ap convert` CLI script — single controller, L2 or L3 cluster ordering, gateway strategy included. | No |
| 6 | **Validate** | Confirms every converted AP is back online in Central, by serial. Closeout checklist. | No |

<details>
<summary><b>See it in action</b> (click to expand more screenshots)</summary>

**Preflight tells you what will and won't migrate — before anything is written:**

<img src="docs/screenshots/04-preflight.png" alt="Step 2 — preflight checks" width="900">

**Provisioning shows a live per-step log — nothing fails silently:**

<img src="docs/screenshots/06-provision-results.png" alt="Step 3 — provisioning results" width="900">

**The generated conversion runbook:**

<img src="docs/screenshots/09-runbook.png" alt="Step 5 — ap convert runbook" width="900">

**Validation confirms the migration worked:**

<img src="docs/screenshots/10-validate.png" alt="Step 6 — validation" width="900">

</details>

## Quick start

```bash
git clone https://github.com/secure-ssid/aos8-migration-tool.git
cd aos8-migration-tool
pip install -r requirements.txt
streamlit run app.py          # opens http://localhost:8501
```

Then open the **🧪 Load test customer** expander in Step 1 and click through
the whole wizard with zero infrastructure — the
[Getting Started guide](docs/GETTING-STARTED.md) walks you through it.

For a **real migration** you'll need three things (details and where to get
them: [Migration Guide → Credentials setup](docs/MIGRATION-GUIDE.md#credentials-setup)):

1. **AOS 8 access** — controller admin login (REST API, port 4343), or just
   paste the output of a few `show` commands the wizard lists for you.
2. **Central API credentials** — New Central (GreenLake client id/secret) or
   Classic Central (API Gateway token).
3. **GreenLake workspace access** — usually the same GreenLake client.

Steps 1–2 are read-only; nothing is written anywhere until you press
**🚀 Provision** in Step 3, and your AOS 8 network keeps running untouched
until you execute the runbook in Step 5.

## What's supported

| Choice | Options |
|---|---|
| **Source** | Mobility Controller / Conductor (MM/MD) · Instant cluster (IAP) |
| **Destination** | New Central (HPE GreenLake) · Classic Central |
| **Gateway strategy** (MC + tunnel SSIDs only) | Keep the MCs as AOS 10 gateways (SSIDs stay overlay) · Retire them (everything becomes bridge) |

All four source × destination combinations work; the wizard adapts each step
to your path. Instant sources are Central-driven (no controller commands at
all). See [Migration paths](docs/MIGRATION-GUIDE.md#the-four-migration-paths).

## Beyond the wizard

The sidebar **Mode** switch has two more tools:

- **Add devices only** — claim → subscribe → move APs into groups that
  already exist in the tenant, skipping discovery/config entirely.
- **Help & Docs** — in-app reference: how each page works, every API call as
  curl, a downloadable Postman collection, and how to create the API keys.

## Documentation

| Doc | What's in it |
|---|---|
| [Getting Started](docs/GETTING-STARTED.md) | 5-minute hands-on demo, no hardware needed |
| [Migration Guide](docs/MIGRATION-GUIDE.md) | Full step-by-step operator walkthrough with screenshots |
| [Deployment Guide](docs/DEPLOYMENT.md) | Docker, team logins (shared password / per-person accounts), HTTPS, env vars |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | The errors you're most likely to hit, decoded |
| [API Notes](docs/API-NOTES.md) | Every API call the tool makes, per platform, with quirks |
| [Architecture](docs/ARCHITECTURE.md) | How the code is put together |
| [docs/vault](docs/vault/Home.md) | Deep-dive engineering notes (Obsidian vault) |

## Deployment in one paragraph

Single user: `streamlit run app.py` (or the Dockerfile) — no login, secrets
stay in the session. Teams: `docker compose up` gives a shared-password gate
by default, or switch to per-person verified-email accounts; front it with
Caddy for HTTPS (`deploy/Caddyfile` is ready to use). Full detail, including
the security notes that matter, in the
[Deployment Guide](docs/DEPLOYMENT.md).

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q        # 40 tests, no hardware or tenant needed
python -m pyflakes app.py lib/*.py views/*.py
```

CI runs lint + tests + a Docker build on every push
(`.github/workflows/ci.yml`). The test suite includes mocked-HTTP
reproductions of real field bugs — see `tests/test_clients_http.py`.
