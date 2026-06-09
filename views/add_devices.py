"""
Standalone "Add devices" mode — claim a batch of APs into the GreenLake
workspace, assign a subscription (+ the Central application instance), and
OPTIONALLY move them into device groups that already exist in the tenant.

Credential model (this is the whole point of the mode's two scopes):
  - Claim + subscribe + application assignment are pure GreenLake API
    operations (GLP client credentials) — no Central API involved.
  - New Central credentials are only needed for the optional group move +
    persona step.
  - The Classic API Gateway is only used for that move, and only when the
    operator explicitly marks the tenant hybrid.

Input: paste `show ap database long` (parsed) OR a serial,MAC,group list /
CSV. Credentials come from the saved-on-this-machine store (Step 1's
"Remember") or are entered here.
"""
import hashlib
import json
import re

import streamlit as st

from lib import credstore
from lib.aos8_parser import _parse_ap_database
from lib.glp_client import GLPClient
from lib.session_clients import (build_central_client, build_classic_client,
                                 persist_rotated_refresh_token,
                                 use_classic_for_moves)
from lib.styles import (OK, FAIL, WARN, FAINT, TEXT, HPE_GREEN,
                        page_header, section_label, esc, mono_caption)

_SCOPE_GLP = "Claim + subscribe (GreenLake only)"
_SCOPE_MOVE = "Claim + subscribe + move into existing groups"

# wired-MAC formats operators actually paste: aa:bb:cc:dd:ee:ff, aa-bb-…,
# Cisco-style aabb.ccdd.eeff
_MAC_COLON = re.compile(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$")
_MAC_DOTTED = re.compile(r"^[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}$")
_SERIAL_LIKE = re.compile(r"^[A-Za-z0-9_-]{4,}$")


def _norm_mac(raw: str) -> str:
    """Normalize a pasted MAC to aa:bb:cc:dd:ee:ff, or '' if it isn't one."""
    s = (raw or "").strip()
    if _MAC_DOTTED.match(s):
        s = s.replace(".", "")
        return ":".join(s[i:i + 2] for i in range(0, 12, 2)).lower()
    if _MAC_COLON.match(s):
        return s.replace("-", ":").lower()
    return ""


# ── input parsing ──────────────────────────────────────────────────────────
def _rows_from_ap_database(text: str) -> tuple[list[dict], int]:
    """Reuse the AOS 8 `show ap database long` parser → serial/MAC/group rows.

    Also counts APs the parser kept but whose serial was blanked (fixed-width
    column overflow) — the wizard's preflight flags those, this mode must
    surface them itself."""
    out, dropped = [], 0
    for ap in _parse_ap_database(text or ""):
        if ap.serial:
            out.append({"serial": ap.serial.strip(),
                        "mac": (ap.mac or "").strip(),
                        "group": (ap.ap_group or "").strip()})
        else:
            dropped += 1
    return out, dropped


def _rows_from_list(text: str) -> tuple[list[dict], int]:
    """serial,MAC,group per line (comma / tab / spaces). Header line skipped.

    Forgiving on purpose: single-space-delimited lines work, a non-MAC second
    column is treated as `serial, group` (missing MAC), and unparsable lines
    are counted so the UI can say how many were skipped."""
    out, skipped = [], 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.lower().startswith("serial"):
            continue
        parts = [p.strip() for p in re.split(r"[,\t]+|\s{2,}", line) if p.strip()]
        if len(parts) < 2:
            parts = line.split()  # single-space-delimited fallback
        serial = parts[0] if parts else ""
        if not _SERIAL_LIKE.match(serial):
            skipped += 1
            continue
        mac = _norm_mac(parts[1]) if len(parts) > 1 else ""
        if len(parts) == 2 and not mac:
            # second column isn't a MAC → `serial, group` shorthand; with 3+
            # columns the shape is unambiguous, so a non-MAC second column is
            # a bad MAC (missing-MAC row), NOT the group
            group = parts[1]
        else:
            group = parts[2] if len(parts) > 2 else ""
        # uppercase like the ap-database parser — GLP device lookups are
        # exact-case, so a lowercase pasted serial would pass the workspace
        # reconcile yet fail the per-device assign
        out.append({"serial": serial.upper(), "mac": mac, "group": group})
    return out, skipped


def _dedupe(rows: list[dict]) -> list[dict]:
    """De-dupe by serial (case-insensitive), keeping order — but a later
    duplicate that HAS a MAC corrects an earlier MAC-less row."""
    seen: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        s = r["serial"].upper()
        if not s:
            continue
        if s not in seen:
            seen[s] = r
            order.append(s)
        elif not seen[s]["mac"] and r["mac"]:
            # merge the MAC in — replacing the row would drop a group the
            # earlier line carried
            seen[s]["mac"] = r["mac"]
            if not seen[s]["group"] and r["group"]:
                seen[s]["group"] = r["group"]
    return [seen[s] for s in order]


# ── credentials ────────────────────────────────────────────────────────────
def _glp_client() -> GLPClient:
    if st.session_state.get("glp_use_central_creds", True):
        cid = st.session_state.get("central_client_id", "")
        sec = st.session_state.get("central_secret", "")
    else:
        cid = st.session_state.get("glp_client_id", "")
        sec = st.session_state.get("glp_secret", "")
    return GLPClient(client_id=cid, client_secret=sec)


def _glp_creds_ok() -> bool:
    if st.session_state.get("glp_use_central_creds", True):
        return bool(st.session_state.get("central_client_id")
                    and st.session_state.get("central_secret"))
    return bool(st.session_state.get("glp_client_id")
                and st.session_state.get("glp_secret"))


def _glp_fp() -> str:
    """Fingerprint of the effective GLP credentials — add_subs/add_sms cached
    from one tenant must never satisfy the gate after switching to another."""
    if st.session_state.get("glp_use_central_creds", True):
        raw = "central|" + st.session_state.get("central_client_id", "") + \
              "|" + st.session_state.get("central_secret", "")
    else:
        raw = "glp|" + st.session_state.get("glp_client_id", "") + \
              "|" + st.session_state.get("glp_secret", "")
    return hashlib.sha1(raw.encode()).hexdigest()


def _inputs_fp(devices: list[dict], scope: str, sub, app) -> str:
    """Fingerprint of everything a run consumes, so stale results are labeled."""
    blob = json.dumps([[d["serial"], d["mac"], d["group"]] for d in devices]
                      + [scope,
                         (sub or {}).get("id") or (sub or {}).get("key") or "",
                         (app or {}).get("id") or ""])
    return hashlib.sha1(blob.encode()).hexdigest()


def _result_row(label: str, ok: bool, detail: str = ""):
    icon, col = ("✓", OK) if ok else ("✕", FAIL)
    st.markdown(
        f'<div style="font-family:\'IBM Plex Mono\',monospace;font-size:12px;padding:2px 0;">'
        f'<span style="color:{col};">{icon}</span> <span style="color:{TEXT};">{esc(label)}</span>'
        f'<span style="color:{FAINT};">{("  — " + esc(detail)) if detail else ""}</span></div>',
        unsafe_allow_html=True)


def _sub_label(s: dict) -> str:
    key = s.get("key", s.get("id", "?"))
    tier = (s.get("tier") or s.get("subscriptionTier")
            or s.get("subscriptionType") or "")
    qty = s.get("availableQuantity", s.get("quantity", ""))
    end = str(s.get("endTime", ""))[:10]
    return f"{key} · {tier} · avail {qty} · ends {end}"


def _is_ap_sub(s: dict) -> bool:
    return "AP" in str(s.get("subscriptionType", "")) or \
           "AP" in str(s.get("tier", ""))


def render():
    page_header(None, "Add Devices",
                "Claim APs into GreenLake + assign a subscription; optionally "
                "move them into groups that already exist in the tenant.",
                accent=HPE_GREEN)

    # Auto-fill saved creds once (Step 1 normally does this; do it here too so
    # the standalone mode works without visiting Step 1). Mirrors p1 exactly —
    # including arming the Remember toggle when a real credential was saved,
    # so visiting this mode first can't make Step 1 delete the saved file.
    if not st.session_state.get("_creds_loaded"):
        st.session_state["_creds_loaded"] = True
        saved = credstore.load()
        for k, v in saved.items():
            st.session_state.setdefault(k, v)
        if any(saved.get(k) for k in credstore.CREDENTIAL_FIELDS):
            st.session_state.setdefault("remember_creds", True)

    # ── What to do ──────────────────────────────────────────────────────────
    section_label("What to do", color=HPE_GREEN)
    scope = st.radio(
        "Scope", [_SCOPE_GLP, _SCOPE_MOVE], key="add_scope", horizontal=True,
        label_visibility="collapsed",
        help="Claiming + subscribing (and assigning the Central application) "
             "are pure GreenLake API operations — no Central credentials "
             "needed. Moving devices into device groups additionally needs "
             "the New Central API (Classic API Gateway only if the tenant is "
             "hybrid).")
    do_move = scope == _SCOPE_MOVE

    st.divider()

    # ── Devices input (both: paste ap database OR a list) ──────────────────
    section_label("Devices to add", color=HPE_GREEN)
    src = st.radio("Input", ["Paste `show ap database long`", "Serial / MAC / group list"],
                   horizontal=True, label_visibility="collapsed", key="add_input_src")
    if "ap database" in src:
        txt = st.text_area("`show ap database long` output", height=200, key="add_apdb",
                           help="Serial #, Wired MAC and Group columns are read automatically.")
        rows, bad_lines = _rows_from_ap_database(txt)
        bad_what = "AP row(s) had no usable Serial # (often fixed-width column overflow)"
    else:
        txt = st.text_area("One device per line: `serial, MAC, group`", height=200, key="add_list",
                           placeholder="CN1234ABCD, aa:bb:cc:dd:ee:ff, campus-aps\nCN5678WXYZ, aa:bb:cc:dd:ee:00, warehouse-aps")
        rows, bad_lines = _rows_from_list(txt)
        bad_what = "line(s) couldn't be parsed"

    devices = _dedupe(rows)
    claimable = [d for d in devices if d["mac"]]
    no_mac    = [d for d in devices if not d["mac"]]
    no_group  = [d for d in claimable if not d["group"]]

    if devices or bad_lines:
        m1, m2, m3 = st.columns(3)
        m1.metric("Devices", len(devices))
        m2.metric("Claimable (have MAC)", len(claimable))
        m3.metric("Missing MAC", len(no_mac))
        if devices:
            with st.expander(f"Parsed devices ({len(devices)})", expanded=True):
                for d in devices[:100]:
                    mac = d["mac"] or "— NO MAC (can't claim)"
                    _result_row(f"{d['serial']}", bool(d["mac"]),
                                f"{mac}  ·  group: {d['group'] or '(unset)'}")
        if bad_lines:
            st.warning(f"{bad_lines} {bad_what} — these were skipped and will "
                       "NOT be claimed. Check the pasted text.")
        if no_mac:
            st.warning(f"{len(no_mac)} device(s) have no MAC — GreenLake claim needs serial+MAC. "
                       "Use `show ap database long` (Wired MAC column) or add the MAC to the list.")
        if do_move and no_group:
            st.warning(f"{len(no_group)} claimable device(s) have no target group — "
                       "they'll be claimed + subscribed, but the move will be "
                       "skipped for them.")

    if not claimable:
        mono_caption("WAITING FOR: at least one device with a serial AND MAC")
        return

    st.divider()

    # ── GreenLake credentials + subscription/app (always required) ─────────
    section_label("GreenLake", color=HPE_GREEN)
    st.session_state.setdefault(
        "glp_use_central_creds",
        bool(st.session_state.get("central_client_id")
             and st.session_state.get("central_secret")))
    use_same = st.checkbox(
        "Use the same API client for GreenLake and Central (GreenLake unified client)",
        key="glp_use_central_creds",
        help="GreenLake unified API clients authenticate to both the GLP API "
             "and the New Central API. Uncheck to use a dedicated GLP client.")
    g1, g2 = st.columns(2)
    if use_same:
        cid = g1.text_input("API client ID",
                            value=st.session_state.get("central_client_id", ""))
        if cid.strip():
            st.session_state["central_client_id"] = cid.strip()
        sec = g2.text_input("API client secret", type="password",
                            placeholder="•••••••• (saved)"
                            if st.session_state.get("central_secret") else "")
        if sec:
            st.session_state["central_secret"] = sec.strip()
    else:
        g1.text_input("GLP client ID", key="glp_client_id")
        gs = g2.text_input("GLP client secret", type="password",
                           placeholder="•••••••• (saved)"
                           if st.session_state.get("glp_secret") else "")
        if gs:
            st.session_state["glp_secret"] = gs.strip()

    # Cached workspace facts belong to the creds that fetched them — drop them
    # the moment the effective GLP credentials change (tenant switch).
    if st.session_state.get("add_subs") is not None and \
            st.session_state.get("add_subs_fp") != _glp_fp():
        for k in ("add_subs", "add_sms", "add_subs_fp"):
            st.session_state.pop(k, None)
        st.info("GreenLake credentials changed — connect again to reload "
                "subscriptions + the Central application.")

    if st.button("Connect GreenLake (list subscriptions + Central app)",
                 disabled=not _glp_creds_ok()):
        try:
            glp = _glp_client()
            with st.spinner("Authenticating with GreenLake..."):
                glp.authenticate()
            with st.spinner("Reading subscriptions + application instances..."):
                st.session_state["add_subs"] = glp.list_subscriptions()
                st.session_state["add_sms"] = glp.list_service_managers()
            st.session_state["add_subs_fp"] = _glp_fp()
            st.success("GreenLake reachable — pick a subscription below.")
        except Exception as e:
            st.error(f"GreenLake auth/list failed: {e}")
    if not _glp_creds_ok():
        mono_caption("WAITING FOR: GREENLAKE API CLIENT ID + SECRET")

    subs = st.session_state.get("add_subs")
    sms = st.session_state.get("add_sms")
    sub_choice = app_choice = None
    if subs is not None:
        # active only, AP subscriptions first — same rules as Step 4
        active = [s for s in subs
                  if str(s.get("subscriptionStatus", "STARTED")).upper() != "ENDED"]
        active.sort(key=lambda s: (not _is_ap_sub(s), str(s.get("tier", ""))))
        if not active:
            st.warning("No active subscriptions in this workspace — add "
                       "subscription keys in GreenLake before APs can be "
                       "managed by Central.")
        else:
            sub_choice = st.selectbox("Subscription to assign", active,
                                      format_func=_sub_label,
                                      help="Active subscriptions only; AP "
                                           "subscriptions listed first")
        if sms:
            app_choice = st.selectbox("Central application instance", sms,
                                      format_func=lambda m:
                                      f"{m.get('name', 'Central')}  ·  {m.get('region', '')}")
        else:
            st.info("No Central application instance found in this workspace — devices will "
                    "be claimed + subscribed but not assigned to Central. Assign the app in GLP.")

    # ── New Central (only for the group move) ───────────────────────────────
    central_ready = True
    if do_move:
        st.divider()
        section_label("New Central — group move + persona", color=HPE_GREEN)
        c1, _ = st.columns([3, 1])
        central_base = c1.text_input(
            "Central API base URL (regional)",
            value=st.session_state.get("central_base",
                                       "https://us4.api.central.arubanetworks.com"))
        if central_base.strip():
            st.session_state["central_base"] = central_base.strip()
        if use_same:
            mono_caption("USES THE UNIFIED API CLIENT ENTERED ABOVE")
        else:
            cc1, cc2 = st.columns(2)
            ccid = cc1.text_input("Central API client ID",
                                  value=st.session_state.get("central_client_id", ""))
            if ccid.strip():
                st.session_state["central_client_id"] = ccid.strip()
            csec = cc2.text_input("Central API client secret", type="password",
                                  placeholder="•••••••• (saved)"
                                  if st.session_state.get("central_secret") else "")
            if csec:
                st.session_state["central_secret"] = csec.strip()

        with st.expander("Hybrid tenant? Classic API Gateway for the group move "
                         "(optional)"):
            hb = st.text_input("Classic API gateway base URL",
                               value=st.session_state.get("central_base_classic",
                                                          "https://apigw-uswest4.central.arubanetworks.com"))
            if hb.strip():
                st.session_state["central_base_classic"] = hb.strip()
            htok = st.text_input("Classic access token", type="password",
                                 placeholder="•••••••• (saved)" if st.session_state.get("classic_access_token") else "")
            if htok:
                # a NEWLY entered token arms the hybrid gate once (comparing
                # first keeps text sitting in the box from re-arming every
                # rerun; must happen before the checkbox is instantiated)
                if htok.strip() != st.session_state.get("classic_access_token"):
                    st.session_state["hybrid_tenant"] = True
                st.session_state["classic_access_token"] = htok.strip()
            hybrid = st.checkbox(
                "This tenant is hybrid — route the group move via the Classic API",
                key="hybrid_tenant",
                help="Only needed when New Central group moves fail with "
                     "API_ACCESS_RESTRICTED_IN_HYBRID_CLUSTER. Claiming and "
                     "subscriptions never use the Classic API. Auto-checked "
                     "when you enter a token.")
            if hybrid and not use_classic_for_moves():
                mono_caption("WAITING FOR: A CLASSIC ACCESS TOKEN (OR REFRESH "
                             "TOKEN + CLIENT ID/SECRET FROM STEP 1)", color=WARN)

        central_ready = bool(st.session_state.get("central_base")
                             and st.session_state.get("central_client_id")
                             and st.session_state.get("central_secret"))

    # ── Run ────────────────────────────────────────────────────────────────
    st.divider()
    missing = []
    if not _glp_creds_ok():
        missing.append("GreenLake API credentials")
    elif subs is None:
        missing.append("Connect GreenLake")
    elif sub_choice is None:
        missing.append("an active subscription")
    if do_move and not central_ready:
        missing.append("New Central base URL + API client (for the group move)")
    if do_move and st.session_state.get("hybrid_tenant") \
            and not use_classic_for_moves():
        # don't let the run silently fall back to New Central moves on a
        # tenant the operator marked hybrid — they'd all fail
        missing.append("a usable Classic token (tenant is marked hybrid)")
    ready = not missing
    if not ready:
        mono_caption("WAITING FOR: " + " · ".join(missing))

    fp = _inputs_fp(claimable, scope, sub_choice, app_choice)
    label = "🚀 Add devices" + (" + move into groups" if do_move else " (GreenLake only)")
    if st.button(label, type="primary", disabled=not ready, use_container_width=True):
        _run_add(claimable, sub_choice, app_choice, do_move, fp)

    results = st.session_state.get("add_results")
    if results:
        r1, r2 = st.columns([4, 1])
        with r1:
            section_label("Last run", color=HPE_GREEN)
        if r2.button("Clear results", use_container_width=True):
            st.session_state.pop("add_results", None)
            st.session_state.pop("add_results_fp", None)
            st.rerun()
        if st.session_state.get("add_results_fp") != fp:
            mono_caption("RESULTS FROM A PREVIOUS RUN — THE INPUTS ABOVE HAVE "
                         "CHANGED SINCE", color=WARN)
        for rlabel, ok, detail in results:
            _result_row(rlabel, ok, detail)


def _run_add(devices: list[dict], sub: dict, app: dict | None,
             do_move: bool, inputs_fp: str):
    """Claim → assign app+subscription → (optionally) move into group +
    persona, with per-step result reporting. Results are ALWAYS persisted to
    session state, even on unexpected errors — once GreenLake has been
    mutated, the record of what happened must survive."""
    results: list[tuple[str, bool, str]] = []
    try:
        _run_add_body(devices, sub, app, do_move, results)
    except Exception as e:
        results.append(("Unexpected error — run stopped", False, str(e)[:300]))
    st.session_state["add_results"] = results
    st.session_state["add_results_fp"] = inputs_fp
    st.rerun()


def _run_add_body(devices: list[dict], sub: dict, app: dict | None,
                  do_move: bool, results: list):
    def step(label, fn):
        try:
            fn(); results.append((label, True, ""))
        except Exception as e:
            results.append((label, False, str(e)[:200]))

    # ── authenticate everything up front: nothing is mutated until every
    #    client this run needs has working credentials ───────────────────────
    glp = _glp_client()
    with st.spinner("Authenticating with GreenLake..."):
        try:
            glp.authenticate()
        except Exception as e:
            results.append(("Authenticate GreenLake", False, str(e)[:200]))
            return
    central = classic = None
    if do_move:
        central = build_central_client()
        with st.spinner("Authenticating with New Central..."):
            try:
                central.authenticate()
            except Exception as e:
                results.append(("Authenticate New Central", False,
                                str(e)[:200] + " — aborted before claiming"))
                return
        classic = build_classic_client() if use_classic_for_moves() else None

    serials = [d["serial"] for d in devices]
    macs = {d["serial"]: d["mac"] for d in devices}

    # ── 1. claim into GreenLake (skip serials already in the workspace) ─────
    try:
        with st.spinner("Reading workspace inventory..."):
            existing = glp.workspace_serials()
    except Exception:
        existing = set()
    to_claim = [s for s in serials if s.upper() not in existing]
    if existing and len(to_claim) < len(serials):
        results.append((f"{len(serials) - len(to_claim)} device(s) already in "
                        "the workspace — claim skipped for them", True, ""))
    if to_claim:
        try:
            with st.status(f"Claiming {len(to_claim)} device(s) into GreenLake "
                           "(async — can take a few minutes)...") as box:
                task = glp.add_devices(
                    [{"serialNumber": s, "macAddress": macs[s]} for s in to_claim])
                glp.poll_task(task, on_poll=lambda a, s_: box.update(
                    label=f"Claiming {len(to_claim)} device(s) — poll {a}, status: {s_}"))
        except Exception as e:
            results.append((f"Claim {len(to_claim)} device(s) in GreenLake",
                            False, str(e)[:200]))

    # Never trust the async-op body alone — reconcile against the workspace,
    # and only assign/move serials that are actually there (mirrors Step 4).
    verify_ok = True
    try:
        with st.spinner("Verifying claims against the workspace inventory..."):
            in_ws = glp.workspace_serials()
    except Exception as e:
        verify_ok = False
        results.append(("Verify workspace inventory", False,
                        str(e)[:200] + " — proceeding with the submitted serials"))
        in_ws = existing | {s.upper() for s in to_claim}
    ok_serials = [s for s in serials if s.upper() in in_ws]
    failed = sorted({s.upper() for s in serials} - in_ws)
    if to_claim and verify_ok and not failed:
        results.append((f"Claim verified — all {len(serials)} device(s) in the "
                        "workspace", True, ""))
    if failed:
        results.append((f"{len(failed)} device(s) NOT in the workspace after the "
                        "claim", False,
                        ", ".join(failed[:8]) + ("…" if len(failed) > 8 else "")
                        + " — subscription/move skipped for these"))
    if not ok_serials:
        return

    # ── 2. assign Central application + subscription (per device, GLP API) ──
    sub_key = sub.get("id") or sub.get("key")
    prog = st.progress(0.0, text="Assigning Central application + subscription...")
    for i, s in enumerate(ok_serials):
        if app is not None:
            step(f"Assign {s} → Central ({app.get('region', '')}) + subscription",
                 lambda s=s: glp.assign_application(s, app["id"],
                                                    app.get("region", ""), sub_key))
        else:
            step(f"Assign subscription → {s}",
                 lambda s=s: glp.assign_subscription(s, sub_key))
        prog.progress((i + 1) / len(ok_serials),
                      text=f"Assigning... {i + 1}/{len(ok_serials)}")

    if not do_move:
        return

    # ── 3. move into the (existing) group + CAMPUS_AP persona ───────────────
    ok_set = {s.upper() for s in ok_serials}
    by_group: dict[str, list[str]] = {}
    for d in devices:
        if d["serial"].upper() in ok_set:
            by_group.setdefault(d["group"], []).append(d["serial"])

    # resolve existing groups via whichever API will do the move — and ONLY
    # that one: on the classic path a New Central config read may be the very
    # thing the hybrid tenant restricts, and it isn't needed there
    group_scope: dict[str, str] = {}
    if classic is not None:
        try:
            known = set(classic.list_group_names(refresh=True))
        except Exception as e:
            results.append(("List Classic groups", False,
                            str(e)[:200] + " — group move skipped"))
            _persist_classic(classic)
            return
    else:
        try:
            group_scope = {g.get("scopeName"): str(g.get("scopeId"))
                           for g in (central.list_device_groups(refresh=True) or [])}
        except Exception as e:
            results.append(("List device groups in New Central", False,
                            str(e)[:200] + " — group move skipped"))
            return
        known = set(group_scope)

    with st.spinner("Moving devices into groups + assigning persona..."):
        for gname, gserials in by_group.items():
            if not gname:
                results.append((f"Move {len(gserials)} device(s): no target group set",
                                False, "add a group column to claim into a specific group"))
                continue
            if gname not in known:
                results.append((f"Move into group '{gname}'", False,
                                f"group not found in tenant — create it first (existing: "
                                f"{', '.join(sorted(known)[:6])})"))
                continue
            if classic is not None:
                step(f"Move {len(gserials)} device(s) → '{gname}' (Classic/hybrid)",
                     lambda g=gname, s=gserials: classic.move_devices(g, s))
            else:
                step(f"Add {len(gserials)} device(s) → '{gname}'",
                     lambda sid=group_scope[gname], s=gserials:
                     central.add_devices_to_group(sid, s))
            step(f"CAMPUS_AP persona → {len(gserials)} device(s) in '{gname}'",
                 lambda s=gserials: central.assign_persona(s))
    _persist_classic(classic)


def _persist_classic(classic) -> None:
    """The classic refresh token is single-use and rotates on any mid-run
    401-refresh — persist it on EVERY exit path that touched the classic
    client, not just the happy one, or later steps inherit a dead token."""
    if classic is None:
        return
    persist_rotated_refresh_token(classic)
    if st.session_state.get("remember_creds"):
        credstore.save_from_session(st.session_state)
