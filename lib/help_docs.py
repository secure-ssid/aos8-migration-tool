"""
Help & Docs page (the "smart help"): how each wizard page works, the scripts
behind it, how to reproduce it by hand with curl, and a downloadable Postman
collection — plus the two API-key creation how-tos. Content is grounded in the
shared API catalog (lib.api_catalog) so the curl + Postman never drift from the
real client code.
"""
import json

import streamlit as st

from lib import postman
from lib.api_catalog import GROUPS, VARIABLES
from lib.styles import page_header, section_label


# ── per-page walkthrough (what it does + the functions behind it) ─────────────
STEP_DOCS = [
    {"title": "Step 1 — Connect & Discover",
     "what": "Pulls the AOS 8 design (SSIDs, groups/zones, APs with serial + wired "
             "MAC, VLANs, RADIUS, cluster topology) either by live REST login to the "
             "Mobility Controller (:4343) or by pasting CLI output, and registers the "
             "destination Central credentials. 'Continue' translates the discovery "
             "into the CentralConfig every later step uses.",
     "funcs": ["AOS8Client.connect / pull_config — login + read config objects / showcommands",
               "aos8_parser.parse_customer_config / parse_instant_config — paste-mode parsers",
               "translator.translate — CustomerConfig → CentralConfig",
               "api_probe.probe_new_central / probe_glp / probe_classic — read-only connectivity test"],
     "groups": ["AOS 8 — source pull", "GreenLake — auth", "New Central — provision + validate", "Classic Central"]},
    {"title": "Step 2 — Preflight",
     "what": "Read-only safety gate: AP-model + firmware-train compatibility, SSID "
             "mapping/auth coverage, serial coverage, duplicate/oversized ESSIDs, "
             "named-VLAN resolution, cluster sequencing. No API calls — it runs "
             "entirely against the config pulled in Step 1.",
     "funcs": ["compatibility.run_all — 17 in-memory checks → PASS/WARN/FAIL",
               "aos8_client.is_model_compatible — pure model-string check"],
     "groups": []},
    {"title": "Step 3 — Build Config",
     "what": "Writes the target design into the tenant (idempotent): site, "
             "auth-servers, device groups, VLANs, underlay/overlay SSIDs, "
             "scope-maps, firmware compliance. No APs are touched here.",
     "funcs": ["CentralClient.provision(phase='config') — New Central orchestrator",
               "ClassicCentralClient.provision — Classic API-gateway path (full_wlan, etc.)",
               "session_clients.use_classic_for_moves — hybrid routing decision"],
     "groups": ["GreenLake — auth", "New Central — provision + validate", "Classic Central"]},
    {"title": "Step 4 — Onboard APs (GreenLake)",
     "what": "Claims APs into the GreenLake workspace (serial + wired MAC), assigns "
             "the Central app instance + region and a subscription, then runs the "
             "cutover — moving APs into their AOS 10 groups (the conversion trigger; "
             "APs reboot ~10-20 min).",
     "funcs": ["GLPClient.add_devices / poll_task — async claim + reconcile vs inventory",
               "GLPClient.assign_application — two sequential PATCHes (app+region, then subscription)",
               "CentralClient.provision(phase='devices') — group move + persona + site"],
     "groups": ["GreenLake — auth", "GreenLake — devices & subscriptions (GLP)", "New Central — provision + validate"]},
    {"title": "Step 5 — Runbook",
     "what": "Generates the customer-specific ap-convert procedure (single MC / L2 / "
             "L3 cluster sequencing, or the Central-driven Instant path) and the "
             "gateway-migration guide. Read-only, no API calls.",
     "funcs": ["runbook.generate — builds the copy-paste runbook text"],
     "groups": []},
    {"title": "Step 6 — Validate",
     "what": "Reconciles discovered AP serials against what Central sees online; "
             "re-run until counts converge. Plus the post-migration checklist.",
     "funcs": ["CentralClient.list_all_aps — paginate /network-monitoring/v1/devices",
               "ClassicCentralClient.list_all_aps — paginate /monitoring/v2/aps"],
     "groups": ["New Central — provision + validate", "Classic Central"]},
    {"title": "Add devices only (standalone mode)",
     "what": "Claim + subscribe a batch of APs and optionally move them into groups "
             "that already exist in the tenant — skips discovery/config. Input is "
             "pasted 'show ap database long' or a serial,MAC,group list.",
     "funcs": ["add_devices._run_add_body — claim → verify → assign → move + persona",
               "GLPClient.* (claim/subscribe), CentralClient.add_devices_to_group / assign_persona"],
     "groups": ["GreenLake — auth", "GreenLake — devices & subscriptions (GLP)", "New Central — provision + validate", "Classic Central"]},
]


def _curl(r: dict) -> str:
    """Render a copy-paste curl for one catalog request (placeholders kept)."""
    flags = "-sk" if ":4343" in r["url"] else "-sS"   # -k for AOS 8 self-signed TLS
    parts = [f"curl {flags} -X {r['method']} '{r['url']}'"]
    for k, v in (r.get("headers") or {}).items():
        parts.append(f"-H '{k}: {v}'")
    b = r.get("body")
    if b:
        if b["mode"] == "urlencoded":
            for k, v in b["data"].items():
                parts.append(f"--data-urlencode '{k}={v}'")
        else:
            data = b["data"] if isinstance(b["data"], str) else json.dumps(b["data"])
            parts.append("-H 'Content-Type: application/json'")
            parts.append(f"-d '{data}'")
    return " \\\n  ".join(parts)


def _api_keys() -> None:
    section_label("New Central — HPE GreenLake API client")
    st.markdown(
        "1. Sign in to **HPE GreenLake** (`common.cloud.hpe.com`).\n"
        "2. **Manage → API** (workspace/identity menu) → **Create credentials**.\n"
        "3. Give the client access to the **Aruba Central** service + your region, "
        "and ensure **GreenLake** (devices/subscriptions) scope too — the *same* "
        "client id/secret serves both New Central and GLP.\n"
        "4. Copy the **Client ID** and **Client Secret** (the secret is shown once). "
        "Note your **regional API base** (e.g. `us4.api.central.arubanetworks.com`) "
        "from the API client details.\n"
        "5. Exchange them for a Bearer token (~2 h) at the SSO endpoint:")
    st.code(_curl(_find("SSO token (client_credentials)")), language="bash")

    section_label("Classic Central — API Gateway token")
    st.markdown(
        "1. In **Classic Central** → **Global Settings / Account Home → API Gateway**.\n"
        "2. **System Apps & Tokens → + Add / Generate Token** → pick your user → "
        "**Generate**.\n"
        "3. Copy the **Access Token** (valid ~2 h). For auto-refresh past 2 h, also "
        "grab the **Refresh Token** + the app's **Client ID/Secret**.\n"
        "4. Base URL is your cluster's gateway host "
        "(e.g. `apigw-uswest4.central.arubanetworks.com`).\n"
        "5. The refresh token **rotates on every use** — always save the new one:")
    st.code(_curl(_find("Refresh token (rotates!)")), language="bash")


def _find(name: str) -> dict:
    for g in GROUPS:
        for r in g["requests"]:
            if r["name"] == name:
                return r
    return {"name": name, "method": "GET", "url": "", "headers": {}, "body": None}


def _walkthrough() -> None:
    st.caption("What each page does and the functions behind it. The API calls each "
               "page makes are in the curl + Postman tabs.")
    for d in STEP_DOCS:
        with st.expander(d["title"]):
            st.markdown(f"**What it does** — {d['what']}")
            st.markdown("**Scripts behind it**")
            for f in d["funcs"]:
                st.markdown(f"- `{f}`")
            if d["groups"]:
                st.caption("Relevant API groups (curl/Postman tabs): " + ", ".join(d["groups"]))
            else:
                st.caption("No API calls — runs in-process against the pulled config.")


def _curl_docs() -> None:
    st.caption("Every real call the tool makes, as copy-paste curl. Substitute the "
               "`{{placeholders}}` (or import the Postman collection, next tab).")
    for g in GROUPS:
        with st.expander(g["name"]):
            st.markdown(f"_{g['blurb']}_")
            for r in g["requests"]:
                st.markdown(f"**{r['method']} — {r['name']}**"
                            + (f"  \n{r['desc']}" if r.get("desc") else ""))
                st.code(_curl(r), language="bash")


def _postman() -> None:
    st.markdown(
        "Import this collection into Postman, set the **collection variables** "
        "(or a Postman environment) below, and send. Get a GreenLake token first "
        "(the **GreenLake — auth** request) and paste it into `central_token` / "
        "`glp_token`.")
    st.download_button(
        "Download Postman collection (.json)",
        data=postman.collection_json(),
        file_name="aos8-central-migration.postman_collection.json",
        mime="application/json",
        type="primary",
    )
    section_label("Collection variables")
    st.dataframe(
        {"variable": [k for k, _v, _d in VARIABLES],
         "default": [v for _k, v, _d in VARIABLES],
         "what": [d for _k, _v, d in VARIABLES]},
        width="stretch", hide_index=True,
    )


def render() -> None:
    page_header(None, "Help & Docs",
                "How each page works, the scripts behind it, and how to run it by hand")
    st.divider()
    t_keys, t_walk, t_curl, t_postman = st.tabs(
        ["API Keys", "What Each Page Does", "Run it by Hand (curl)", "Postman"])
    with t_keys:
        _api_keys()
    with t_walk:
        _walkthrough()
    with t_curl:
        _curl_docs()
    with t_postman:
        _postman()
