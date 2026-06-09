"""
Standalone "Add devices" mode — onboard a batch of APs into device groups that
ALREADY exist in the tenant (pre-config done elsewhere or on a prior run),
without the full discovery/config wizard.

Sequence per device: claim in GreenLake (serial+MAC) → assign to the Central
application + a subscription → move into the chosen existing group → CAMPUS_AP
persona. Input: paste `show ap database long` (parsed) OR a serial,MAC,group
list / CSV. Credentials come from the saved-on-this-machine store (Step 1's
"Remember") or are entered here.
"""
import re

import streamlit as st

from lib import credstore
from lib.aos8_parser import _parse_ap_database
from lib.glp_client import GLPClient, GLPAPIError
from lib.session_clients import (build_central_client, build_classic_client,
                                 have_classic_creds)
from lib.styles import (OK, FAIL, WARN, FAINT, TEXT, MUTED, HPE_GREEN,
                        page_header, section_label, esc, mono_caption)


# ── input parsing ──────────────────────────────────────────────────────────
def _rows_from_ap_database(text: str) -> list[dict]:
    """Reuse the AOS 8 `show ap database long` parser → serial/MAC/group rows."""
    out = []
    for ap in _parse_ap_database(text or ""):
        if ap.serial:
            out.append({"serial": ap.serial.strip(),
                        "mac": (ap.mac or "").strip(),
                        "group": (ap.ap_group or "").strip()})
    return out


def _rows_from_list(text: str) -> list[dict]:
    """serial,MAC,group per line (comma / tab / 2+ spaces). Header line skipped."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.lower().startswith("serial"):
            continue
        parts = [p for p in re.split(r"[,\t]+|\s{2,}", line) if p.strip()]
        if len(parts) < 2:
            continue
        out.append({"serial": parts[0].strip(), "mac": parts[1].strip(),
                    "group": parts[2].strip() if len(parts) > 2 else ""})
    return out


def _glp_client() -> GLPClient:
    if st.session_state.get("glp_use_central_creds", True):
        cid = st.session_state.get("central_client_id", "")
        sec = st.session_state.get("central_secret", "")
    else:
        cid = st.session_state.get("glp_client_id", "")
        sec = st.session_state.get("glp_secret", "")
    return GLPClient(client_id=cid, client_secret=sec)


def _result_row(label: str, ok: bool, detail: str = ""):
    icon, col = ("✓", OK) if ok else ("✕", FAIL)
    st.markdown(
        f'<div style="font-family:\'IBM Plex Mono\',monospace;font-size:12px;padding:2px 0;">'
        f'<span style="color:{col};">{icon}</span> <span style="color:{TEXT};">{esc(label)}</span>'
        f'<span style="color:{FAINT};">{("  — " + esc(detail)) if detail else ""}</span></div>',
        unsafe_allow_html=True)


def render():
    page_header(0, "Add Devices",
                "Onboard APs into groups that already exist in the tenant — "
                "claim, assign, move, persona. No discovery/config needed.",
                accent=HPE_GREEN)

    # Auto-fill saved creds once (Step 1 normally does this; do it here too so
    # the standalone mode works without visiting Step 1).
    if not st.session_state.get("_creds_loaded"):
        st.session_state["_creds_loaded"] = True
        for k, v in credstore.load().items():
            st.session_state.setdefault(k, v)

    # ── Destination tenant + credentials ───────────────────────────────────
    section_label("Destination tenant", color=HPE_GREEN)
    c1, c2 = st.columns([3, 1])
    central_base = c1.text_input(
        "New Central API base URL",
        value=st.session_state.get("central_base", "https://us4.api.central.arubanetworks.com"))
    if central_base.strip():
        st.session_state["central_base"] = central_base.strip()

    cc1, cc2 = st.columns(2)
    cid = cc1.text_input("API client ID", value=st.session_state.get("central_client_id", ""))
    if cid.strip():
        st.session_state["central_client_id"] = cid.strip()
    have_secret = bool(st.session_state.get("central_secret"))
    sec = cc2.text_input("API client secret", type="password",
                         placeholder="•••••••• (saved)" if have_secret else "")
    if sec:
        st.session_state["central_secret"] = sec
        have_secret = True

    with st.expander("Hybrid tenant — Classic API Gateway (for group move on hybrid)"):
        hb = st.text_input("Classic API gateway base URL",
                           value=st.session_state.get("central_base_classic",
                                                      "https://apigw-uswest4.central.arubanetworks.com"))
        if hb.strip():
            st.session_state["central_base_classic"] = hb.strip()
        htok = st.text_input("Classic access token", type="password",
                             placeholder="•••••••• (saved)" if st.session_state.get("classic_access_token") else "")
        if htok:
            st.session_state["classic_access_token"] = htok.strip()

    st.divider()

    # ── Devices input (both: paste ap database OR a list) ──────────────────
    section_label("Devices to add", color=HPE_GREEN)
    src = st.radio("Input", ["Paste `show ap database long`", "Serial / MAC / group list"],
                   horizontal=True, label_visibility="collapsed")
    if "ap database" in src:
        txt = st.text_area("`show ap database long` output", height=200, key="add_apdb",
                           help="Serial #, Wired MAC and Group columns are read automatically.")
        rows = _rows_from_ap_database(txt)
    else:
        txt = st.text_area("One device per line: `serial, MAC, group`", height=200, key="add_list",
                           placeholder="CN1234ABCD, aa:bb:cc:dd:ee:ff, campus-aps\nCN5678WXYZ, aa:bb:cc:dd:ee:00, warehouse-aps")
        rows = _rows_from_list(txt)

    # de-dupe by serial, keep first
    seen, devices = set(), []
    for r in rows:
        s = r["serial"].upper()
        if s and s not in seen:
            seen.add(s)
            devices.append(r)

    claimable = [d for d in devices if d["mac"]]
    no_mac    = [d for d in devices if not d["mac"]]
    if devices:
        m1, m2, m3 = st.columns(3)
        m1.metric("Devices", len(devices))
        m2.metric("Claimable (have MAC)", len(claimable))
        m3.metric("Missing MAC", len(no_mac))
        with st.expander(f"Parsed devices ({len(devices)})", expanded=True):
            for d in devices[:100]:
                mac = d["mac"] or "— NO MAC (can't claim)"
                _result_row(f"{d['serial']}", bool(d["mac"]),
                            f"{mac}  ·  group: {d['group'] or '(unset)'}")
        if no_mac:
            st.warning(f"{len(no_mac)} device(s) have no MAC — GreenLake claim needs serial+MAC. "
                       "Use `show ap database long` (Wired MAC column) or add the MAC to the list.")

    # ── GLP credentials + subscription/app (only when there are devices) ───
    if not claimable:
        mono_caption("WAITING FOR: at least one device with a serial AND MAC")
        return

    st.divider()
    section_label("GreenLake", color=HPE_GREEN)
    st.checkbox("Use the New Central API client ID/secret for GreenLake too",
                value=True, key="glp_use_central_creds")
    if not st.session_state.get("glp_use_central_creds", True):
        g1, g2 = st.columns(2)
        g1.text_input("GLP client ID", key="glp_client_id")
        gs = g2.text_input("GLP client secret", type="password",
                           placeholder="•••••••• (saved)" if st.session_state.get("glp_secret") else "")
        if gs:
            st.session_state["glp_secret"] = gs

    # Resolve workspace facts (app instance + subscriptions) on demand.
    if st.button("Connect GreenLake (list subscriptions + Central app)"):
        try:
            glp = _glp_client(); glp.authenticate()
            st.session_state["add_subs"] = glp.list_subscriptions()
            st.session_state["add_sms"] = glp.list_service_managers()
            st.success("GreenLake reachable — pick a subscription + Central app below.")
        except Exception as e:
            st.error(f"GreenLake auth/list failed: {e}")

    subs = st.session_state.get("add_subs")
    sms = st.session_state.get("add_sms")
    sub_choice = app_choice = None
    if subs is not None:
        sub_labels = [f"{s.get('key', s.get('id'))}  ·  {s.get('subscriptionType', s.get('product',''))}"
                      for s in subs] or ["(no subscriptions found)"]
        sub_idx = st.selectbox("Subscription", range(len(sub_labels)),
                               format_func=lambda i: sub_labels[i], key="add_sub_idx")
        sub_choice = subs[sub_idx] if subs else None
        if sms:
            app_labels = [f"{m.get('name','Central')}  ·  {m.get('region','')}" for m in sms]
            app_idx = st.selectbox("Central application instance", range(len(app_labels)),
                                   format_func=lambda i: app_labels[i], key="add_app_idx")
            app_choice = sms[app_idx]
        else:
            st.info("No Central application instance found in this workspace — devices will "
                    "be claimed + subscribed but not assigned to Central. Assign the app in GLP.")

    # ── Run ────────────────────────────────────────────────────────────────
    st.divider()
    ready = bool(have_secret and central_base.strip() and st.session_state.get("central_client_id")
                 and subs is not None and sub_choice is not None)
    if not ready:
        mono_caption("WAITING FOR: credentials + GreenLake connect + a subscription selected")
    if st.button("🚀 Add devices", type="primary", disabled=not ready, use_container_width=True):
        _run_add(claimable, sub_choice, app_choice)

    for label, ok, detail in st.session_state.get("add_results", []):
        _result_row(label, ok, detail)


def _run_add(devices: list[dict], sub: dict, app: dict | None):
    """Claim → assign app+subscription → move into group → persona, with
    per-step result reporting. Mirrors the Step 4 'devices' phase but targets
    groups that already exist in the tenant."""
    results: list[tuple[str, bool, str]] = []

    def step(label, fn):
        try:
            fn(); results.append((label, True, ""))
        except Exception as e:
            results.append((label, False, str(e)[:200]))

    glp = _glp_client()
    classic = build_classic_client() if have_classic_creds() else None
    central = build_central_client()

    with st.spinner("Authenticating..."):
        try:
            glp.authenticate(); central.authenticate()
        except Exception as e:
            st.session_state["add_results"] = [("Authenticate", False, str(e)[:200])]
            st.rerun()

    serials = [d["serial"] for d in devices]
    macs = {d["serial"]: d["mac"] for d in devices}

    # 1. claim into GreenLake
    with st.spinner(f"Claiming {len(serials)} device(s) into GreenLake..."):
        try:
            task = glp.add_devices([{"serialNumber": s, "macAddress": macs[s]} for s in serials])
            glp.poll_task(task)
            results.append((f"Claim {len(serials)} device(s) in GreenLake", True, ""))
        except Exception as e:
            results.append((f"Claim {len(serials)} device(s) in GreenLake", False, str(e)[:200]))

    # 2. assign Central application + subscription (per device)
    sub_key = sub.get("key") or sub.get("id")
    for s in serials:
        if app is not None:
            step(f"Assign {s} → Central ({app.get('region','')}) + subscription",
                 lambda s=s: glp.assign_application(s, app["id"], app.get("region", ""), sub_key))
        else:
            step(f"Assign subscription → {s}",
                 lambda s=s: glp.assign_subscription(s, sub_key))

    # 3. move into the (existing) group + CAMPUS_AP persona, grouped by target
    by_group: dict[str, list[str]] = {}
    for d in devices:
        by_group.setdefault(d["group"], []).append(d["serial"])

    group_scope = {g.get("scopeName"): str(g.get("scopeId"))
                   for g in (central.list_device_groups(refresh=True) or [])}

    for gname, gserials in by_group.items():
        if not gname:
            results.append((f"Move {len(gserials)} device(s): no target group set", False,
                            "add a group column to claim into a specific group"))
            continue
        if gname not in group_scope:
            results.append((f"Move into group '{gname}'", False,
                            f"group not found in tenant — create it first (existing: "
                            f"{', '.join(list(group_scope)[:6])})"))
            continue
        if classic is not None:
            step(f"Move {len(gserials)} device(s) → '{gname}' (Classic/hybrid)",
                 lambda g=gname, s=gserials: classic.move_devices(g, s))
        else:
            step(f"Add {len(gserials)} device(s) → '{gname}'",
                 lambda sid=group_scope[gname], s=gserials: central.add_devices_to_group(sid, s))
        step(f"CAMPUS_AP persona → {len(gserials)} device(s) in '{gname}'",
             lambda s=gserials: central.assign_persona(s))

    st.session_state["add_results"] = results
    st.rerun()
