"""
Contextual help content per wizard step — rendered by app.py as a
"Step help" expander under the pipeline. Markdown; keep it operator-focused.
"""
import streamlit as st


def _connect_help() -> str:
    base = """
**What this step does** — pulls the AOS 8 design (SSIDs, groups/zones, APs with
serial + wired MAC, VLANs, RADIUS, cluster topology) and points the tool at the
destination Central tenant.

**Source tips**
- **Mobility Controller**: API mode logs in at `https://<mc>:4343/v1/api/login`.
  On a Mobility Conductor use `config_path=/md` (default); on a standalone
  controller set `/mm/mynode` (Advanced options). If port 4343 is firewalled,
  switch to paste mode — `show running-config` and `show ap database long`
  carry most of the data (the latter is the only command with **Serial # and
  wired MAC**, both needed for GreenLake claiming).
- **Instant (IAP)**: paste from the **virtual controller** CLI. The
  `wlan ssid-profile` blocks are parsed as WLANs directly; `zone` keywords map
  to device groups.

**Destination credentials**
- **New Central**: GreenLake → *Manage → API* → create client credentials with
  Aruba Central access. Base URL is your **regional** endpoint
  (e.g. `us4.api.central.arubanetworks.com`).
- **Classic Central**: API Gateway → *System Apps & Tokens* → Generate Token
  (~2 h lifetime). Add the refresh token + client id/secret to let the tool
  auto-refresh — note the refresh token **rotates on every use**.

**Gateway strategy** (MC sources with tunnel SSIDs): *keep* turns the MCs into
AOS 10 gateways and keeps tunnel SSIDs as overlay; *retire* converts everything
to bridge mode and decommissions the MCs — preflight will list the switchport
and RADIUS changes that requires.
"""
    return base


HELP = {
    0: ("Connect & Discover", _connect_help()),
    1: ("Preflight", """
**What this step does** — read-only safety gate before anything is written.

- **Blockers (red)** stop provisioning: incompatible AP models (no AOS 10
  image exists for them), unsupported firmware trains (ap convert needs
  ≥ 8.10.0.12 on 8.10 or ≥ 8.12.0.1 on 8.12 — 8.11 does not qualify),
  unresolved **named VLANs**, conflicting duplicate ESSIDs, ESSIDs over
  32 chars.
- **Warnings (yellow)** need review, not necessarily action: RADIUS NAD
  changes, cluster sequencing, split-tunnel conversion, PSKs that couldn't be
  recovered, missing serials.
- The **override checkbox** lets you proceed with blockers when you're
  deliberately phasing (e.g. provisioning Central now, refreshing AP-2xx
  hardware later). The blockers don't go away — plan for them.
"""),
    2: ("Build Config", """
**What this step does** — writes the target design into the Central tenant.
Steps are idempotent: existing sites/groups/WLANs with matching names are
reused, so re-running after a partial failure is safe.

- **New Central**: site → auth-server profiles (+ server-group for 802.1X)
  → device groups (one per AP group/zone) → VLANs → SSIDs → firmware
  compliance. Tunnel/split SSIDs are **deferred**: the gateway cluster only
  exists after the MCs convert at cutover, so overlay binding is a recorded
  follow-up on the runbook. Persona + site assignment happen in Step 4
  (they need claimed APs).
- **Classic Central**: inventory pre-add (serial+MAC) → AOS 10 UI groups →
  device move → WLANs (`full_wlan`) → firmware compliance → site association.
  *Caveats*: the WLAN APIs are **allowlisted per tenant** (403 → ask your
  Aruba SE); RADIUS auth-servers and named gateway clusters can't be created
  via the classic API — they're listed as manual follow-ups in the results.
- Every failure shows per step with the raw API error — nothing is silently
  skipped.
"""),
    3: ("Onboard APs (GreenLake)", """
**What this step does** — makes the APs known to GreenLake so Central adopts
them the moment they convert — claim (serial + wired MAC) and assign a
subscription — then runs the **cutover**: moving the APs into their AOS 10
device groups.

- Claiming is async — the tool polls the operation, then **verifies against
  the actual workspace inventory** (the API's own result body isn't trusted).
- Only APs with both serial *and* MAC can be claimed automatically — others
  are listed; add them manually in GreenLake or re-discover with
  `show ap database long`.
- Subscriptions: active ones only, AP tiers listed first. Assignment targets
  only devices actually in the workspace.
- Already claimed via CSV or the GreenLake UI? Skip the claim section — that
  part is optional. The group move below it is **not**.
- **Move APs into device groups** is the conversion trigger (New Central):
  Central pushes the AOS 10 conversion, **each AP reboots and is offline
  ~10–20 min**, then adopts into New Central — no separate `ap convert`
  needed. It also assigns the CAMPUS_AP persona and the site. Only run it
  inside the maintenance/cutover window.
"""),
    4: ("Runbook", """
**What this step does** — generates the conversion procedure for *this*
customer's topology. Download it; it's the artifact you work from during the
maintenance window.

- **MC sources**: `ap convert` commands per AP group, with pre-validate first
  and the Activate-sourced image path (no image names to get wrong). L2
  clusters get strict sequencing (converting both members at once strands
  APs); L3 members migrate independently.
- **Gateways kept**: MCs convert to gateways via ZTP or static-activate after
  the APs move.
- **Gateways retired / Instant**: no controller conversion at all — keep one
  AOS 8 box (or un-converted AP) alive as the rollback target until
  validation passes.
- Rollback per AP: `convert-aos-ap cap <mc-ip>` (needs a live AOS 8 MC).
"""),
    5: ("Validate", """
**What this step does** — compares discovered AP serials against what Central
actually sees online. Conversion takes 10–20 min per AP; re-run until the
counts converge.

- "Not seen in Central yet" lists exact serials still missing — check
  `show ap convert-status` (MC) or the AP console for those.
- The post-migration checklist is the engagement close-out: SSIDs broadcasting,
  RADIUS auth via the new NADs, roaming, alert review, AirWave/MC
  decommission, switchport cleanup.
"""),
}


def render_help(step: int) -> None:
    title, body = HELP.get(step, ("", ""))
    if not body:
        return
    with st.expander(f"Step help — {title}", expanded=False):
        st.markdown(body)
