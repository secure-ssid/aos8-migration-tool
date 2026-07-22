# Getting Started

**New here? This page takes you from nothing to a full simulated migration in
about 5 minutes — no controller, no Central tenant, no risk.** The tool ships
with a built-in test customer, so you can click through every screen and see
exactly what a real migration looks like before you ever touch production
credentials.

## What is this tool?

Aruba wireless networks running **AOS 8** (controllers or Instant APs) are
managed on-premises. HPE's current platform is **AOS 10 + Aruba Central**
(cloud-managed). Migrating between them by hand means reading the old config,
rebuilding it in Central, registering every AP in HPE GreenLake, and running
conversion commands — dozens of error-prone manual steps.

This tool is a **guided 6-step wizard** that does the heavy lifting:

1. **Connect** — reads your existing AOS 8 configuration automatically
2. **Preflight** — tells you what will and won't migrate, *before* changing anything
3. **Build Config** — recreates your networks in Aruba Central
4. **Onboard APs** — registers your access points in HPE GreenLake
5. **Runbook** — generates the exact CLI commands for conversion day
6. **Validate** — confirms every AP came back online

Nothing is written anywhere until Step 3, and every write is logged
step-by-step with its result.

## Try it in 5 minutes (no hardware needed)

### 1. Install and launch

You need Python 3.11+. Then:

```bash
git clone https://github.com/secure-ssid/aos8-migration-tool.git
cd aos8-migration-tool
pip install -r requirements.txt
streamlit run app.py
```

Your browser opens http://localhost:8501 and shows Step 1:

![Step 1 — the Connect & Discover screen](screenshots/01-connect-empty.png)

### 2. Load the built-in test customer

Instead of connecting to a real controller, open the
**🧪 Load test customer** expander, keep the default scenario, and click
**Load test customer**. The wizard fills with a realistic fake deployment
(2 AP groups, 3 SSIDs, 3 APs, RADIUS, an L2 controller cluster) — every
object is named `zztest-…` so it's obviously disposable:

![Step 1 — discovery summary after loading the test customer](screenshots/02-connect-discovered.png)

> This is exactly what you'd see after pulling a real controller's config —
> AP groups with their SSIDs (color-coded by forwarding mode), the AP
> inventory with AOS 10 compatibility badges, VLANs, and RADIUS servers.

### 3. Walk the wizard

Scroll down to **Destination — Aruba Central**. For the demo you can type
anything plausible into the credential fields (auth isn't attempted until
Step 3), then click **Continue →**.

- **Step 2 (Preflight)** runs instantly and shows the pass/warn/fail report —
  read a couple of warnings to get a feel for what it checks:

  ![Step 2 — preflight checks](screenshots/04-preflight.png)

- **Step 3 (Build Config)** shows the **manifest** — the complete list of
  what *would* be created in Central. With fake credentials the provisioning
  run will simply fail its auth pre-check and write nothing; the manifest
  itself is the point here:

  ![Step 3 — the manifest](screenshots/05-provision-manifest.png)

- **Step 5 (Runbook)** and the other steps can be browsed from the stepper
  even before provisioning — each explains what it needs.

### 4. Explore the other two modes

The sidebar's **Mode** switch has two more entries:

- **Add devices only** — for when the config already exists in Central and
  you just need to claim/subscribe/move APs.
- **Help & Docs** — every API call the tool makes, as curl commands and a
  downloadable Postman collection, plus instructions for creating the API
  keys you'll need for a real run.

## Ready for a real migration?

Gather these three things, then follow the
[**Migration Guide**](MIGRATION-GUIDE.md) — it has a screenshot and a
numbered click-path for every step:

| You need | Where to get it | Used in |
|---|---|---|
| AOS 8 access | Controller admin login (API on port 4343), **or** just paste CLI output — the wizard lists the exact `show` commands | Step 1 |
| Central API credentials | New Central: GreenLake → Manage → API → create client credentials. Classic: API Gateway → System Apps & Tokens | Steps 1, 3, 6 |
| GreenLake workspace access | Usually the same GreenLake client; needs device + subscription permissions | Step 4 |

**Golden rule:** Steps 1–2 are read-only. You can connect to a production
controller and run preflight as many times as you like — nothing is changed
until you click **🚀 Provision** in Step 3, and even that only writes *new*
objects to Central (your AOS 8 network keeps running untouched until you run
the conversion runbook in Step 5).

## Where to go next

- [Migration Guide](MIGRATION-GUIDE.md) — the full operator walkthrough, step by step with screenshots
- [Deployment Guide](DEPLOYMENT.md) — Docker, multi-user logins, HTTPS
- [Troubleshooting](TROUBLESHOOTING.md) — the errors you're most likely to hit and what they mean
- [API Notes](API-NOTES.md) — every API call the tool makes, per platform
- In-app **Help & Docs** mode — the same reference material, always in reach
