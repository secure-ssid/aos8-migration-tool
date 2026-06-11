"""
Step 4: Onboard devices to HPE GreenLake — claim APs into the workspace
(serial + wired MAC) and assign subscriptions, so they're recognized the
moment `ap convert` brings them up in Central.
"""
import streamlit as st

from lib import audit
from lib.glp_client import GLPClient
from lib.session_clients import (
    build_central_client, build_classic_client, use_classic_for_moves,
    persist_rotated_refresh_token,
)
from lib.testdata import TEST_PREFIX
from lib.styles import (
    OK, WARN, MUTED, TEXT, FAINT, HPE_GREEN,
    page_header, section_label, badge, esc, mono_row, mono_caption, info_banner,
    provision_step_line,
)


def _review_checklist(central_cfg, customer) -> bool:
    """Pre-onboarding gate — the operator reviews the staged config in New
    Central before any AP is claimed or moved. Returns True once confirmed."""
    n_groups = len(central_cfg.groups)
    n_ssids = len({s.display_name for g in central_cfg.groups for s in g.ssids})
    n_vlans = len({v.id for g in central_cfg.groups for v in g.vlans})
    fw = central_cfg.groups[0].firmware_version if central_cfg.groups else "—"
    has_radius = bool(central_cfg.radius_servers)

    with st.container(border=True):
        st.markdown(
            f'<div style="font-weight:600;color:{HPE_GREEN};margin-bottom:0.4rem;">'
            f'✅ Before onboarding — verify the staged config in New Central</div>'
            f'<div style="font-size:12px;color:{FAINT};margin-bottom:0.6rem;">'
            f'Step 3 built the config but <b>has not touched any AP</b>. Confirm it '
            f'looks right in the New Central UI, then onboard the devices below. '
            f'Moving APs into groups is what converts them over.</div>',
            unsafe_allow_html=True)
        for item in (
            f"Device groups created: <b>{n_groups}</b> "
            f"({', '.join(g.name for g in central_cfg.groups) or '—'})",
            f"SSIDs present with correct VLANs: <b>{n_ssids}</b> SSID(s), "
            f"<b>{n_vlans}</b> VLAN(s)",
            (f"RADIUS server-group bound to enterprise SSIDs "
             f"(remember to set the real shared secret)" if has_radius
             else "No RADIUS — PSK/open SSIDs only"),
            f"Firmware compliance set: <b>{esc(fw)}</b>",
            "APs in GreenLake workspace with a subscription (claim below)",
        ):
            st.markdown(f'<div style="font-size:13px;color:{TEXT};margin:2px 0;">'
                        f'☐ {item}</div>', unsafe_allow_html=True)
        return st.checkbox(
            "I've reviewed the configuration in New Central — proceed to onboard the APs",
            key="onboard_reviewed")


def _client() -> GLPClient:
    if st.session_state.get("glp_use_central_creds", True):
        cid = st.session_state.get("central_client_id", "")
        sec = st.session_state.get("central_secret", "")
    else:
        cid = st.session_state.get("glp_client_id", "")
        sec = st.session_state.get("glp_secret", "")
    return GLPClient(client_id=cid, client_secret=sec)


def render():
    page_header(4, "Onboard APs",
                "Claim the APs into the GLP workspace and assign subscriptions before conversion",
                accent=HPE_GREEN)

    customer = st.session_state.get("customer_config")
    if not customer:
        st.error("Missing configuration — complete Step 1 first.")
        if st.button("← Back to Connect"):
            st.session_state["step"] = 0
            st.rerun()
        return

    if not st.session_state.get("provision_done"):
        info_banner("Configuration isn't built yet (Step 3). Build it first so you can "
                    "review it here before onboarding APs: build config → review → "
                    "claim → move APs → convert.",
                    color=WARN)

    central_cfg = st.session_state.get("central_config")
    # Pre-onboarding review checklist (only meaningful for the New Central path
    # once config is built)
    reviewed = True
    if central_cfg and getattr(central_cfg, "destination", "new") == "new" \
            and st.session_state.get("provision_done"):
        reviewed = _review_checklist(central_cfg, customer)
        st.divider()

    if central_cfg and getattr(central_cfg, "destination", "new") == "classic":
        info_banner(
            "<b>Classic destination:</b> Step 3 already pre-added the serial+MAC pairs "
            "to the classic device inventory. This GreenLake step applies to "
            "GLP-onboarded classic accounts (most current ones) — if this account "
            "predates GreenLake onboarding, just continue to the runbook.",
        )

    claimable = [ap for ap in customer.aps if ap.serial and ap.mac]
    no_mac    = [ap for ap in customer.aps if ap.serial and not ap.mac]
    no_serial = [ap for ap in customer.aps if not ap.serial]

    info_banner(
        "Converted APs are only adopted by Central if they exist in the GreenLake "
        "workspace <b>with a subscription</b>. Claiming needs <b>serial + wired MAC</b> "
        "(both come from <code>show ap database long</code>).",
    )

    m1, m2, m3 = st.columns(3)
    m1.metric("Claimable APs", len(claimable))
    m2.metric("Missing MAC",   len(no_mac))
    m3.metric("Missing serial", len(no_serial))

    if no_mac or no_serial:
        names = ", ".join(ap.name for ap in (no_mac + no_serial)[:15])
        info_banner(
            f"<b>{len(no_mac) + len(no_serial)} AP(s) can't be claimed automatically</b> "
            f"({esc(names)}{' …' if len(no_mac) + len(no_serial) > 15 else ''}) — re-discover "
            "with <code>show ap database long</code> or add the wired MAC below.",
            color=WARN,
        )

    # ── Add wired MACs in-app so the APs become claimable without re-running
    #    discovery (the wired MAC isn't in the short `show ap database`) ──────
    if no_mac:
        import re as _re
        _MAC = _re.compile(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$")
        with st.expander(f"➕ Add wired MACs for {len(no_mac)} AP(s) — needed to claim",
                         expanded=True):
            st.markdown(
                f'<div style="font-size:12px;color:{FAINT};margin-bottom:0.4rem;">'
                f'GreenLake needs each AP\'s <b>wired (Ethernet) MAC</b> — the '
                f'<code>show ap database long</code> "Wired MAC" column / the device '
                f'label. Pre-filled from the AP name where it looks like a MAC; '
                f'<b>verify it\'s the wired MAC</b> (not a radio/BSSID MAC) before '
                f'claiming.</div>', unsafe_allow_html=True)
            macs: dict[str, str] = {}
            for ap in no_mac:
                c1, c2 = st.columns([1, 1])
                c1.markdown(
                    f'<div style="padding-top:6px;font-family:\'IBM Plex Mono\',monospace;'
                    f'font-size:12px;color:{TEXT};">{esc(ap.serial)} '
                    f'<span style="color:{FAINT};">({esc(ap.name)})</span></div>',
                    unsafe_allow_html=True)
                default = ap.name if _MAC.match((ap.name or "").strip()) else ""
                macs[ap.serial] = c2.text_input(
                    f"Wired MAC for {ap.serial}", value=default,
                    key=f"macedit_{ap.serial}", label_visibility="collapsed",
                    placeholder="aa:bb:cc:dd:ee:ff")
            if st.button("Apply wired MACs", type="primary"):
                applied, bad = 0, []
                for ap in customer.aps:
                    v = (macs.get(ap.serial) or "").strip()
                    if not v:
                        continue
                    if _MAC.match(v):
                        ap.mac = v
                        applied += 1
                    else:
                        bad.append(f"{ap.serial}: '{v}'")
                st.session_state["customer_config"] = customer
                if applied:
                    # stash-then-rerun (like glp_claim_result) so the messages
                    # survive the rerun that refreshes the claimable metrics
                    st.session_state["macedit_result"] = {"applied": applied,
                                                          "bad": bad}
                    st.rerun()
                elif bad:
                    # nothing changed — a rerun would wipe the error and leave
                    # the operator with zero feedback; render it in place
                    st.error("Invalid MAC format (need aa:bb:cc:dd:ee:ff): "
                             + "; ".join(bad))

    macedit = st.session_state.pop("macedit_result", None)
    if macedit:
        if macedit["bad"]:
            st.error("Invalid MAC format (need aa:bb:cc:dd:ee:ff): "
                     + "; ".join(macedit["bad"]))
        st.success(f"Applied {macedit['applied']} wired MAC(s) — these APs are "
                   "now claimable.")

    # ── Credentials ────────────────────────────────────────────────────────
    section_label("GLP API credentials", color=HPE_GREEN)
    # classic apigw clients are NOT GreenLake unified clients — don't default
    # to reusing them for the GLP client_credentials grant
    is_classic = bool(central_cfg and
                      getattr(central_cfg, "destination", "new") == "classic")
    st.session_state.setdefault("glp_use_central_creds", not is_classic)
    use_same = st.checkbox(
        "Use the same API client as Central (works if it's a GreenLake unified client)",
        key="glp_use_central_creds",
    )
    if not use_same:
        c1, c2 = st.columns(2)
        c1.text_input("GLP client ID", key="glp_client_id")
        glp_secret_in = c2.text_input(
            "GLP client secret", type="password",
            placeholder="•••••••• (saved this session)" if st.session_state.get("glp_secret") else "",
        )
        if glp_secret_in:
            st.session_state["glp_secret"] = glp_secret_in

    st.divider()

    # ── Workspace check / claim ────────────────────────────────────────────
    section_label("Claim devices", color=HPE_GREEN)
    col_a, col_b = st.columns([1, 1])

    if col_a.button("Check workspace", use_container_width=True,
                    help="Lists the workspace inventory and marks APs already claimed"):
        try:
            client = _client()
            with st.spinner("Authenticating with GreenLake..."):
                client.authenticate()
            with st.spinner("Reading workspace inventory..."):
                existing = set()
                assigned_app = None  # app id+region from a device already assigned
                offset = 0
                while True:
                    page = client.list_devices(limit=100, offset=offset)
                    for d in page:
                        sn = str(d.get("serialNumber", "")).upper()
                        if sn:
                            existing.add(sn)
                        if assigned_app is None:
                            app = d.get("application")
                            aid = None
                            if isinstance(app, dict):
                                aid = app.get("id") or app.get("applicationId")
                            elif isinstance(app, str) and app:
                                aid = app
                            aid = aid or d.get("applicationId")
                            if aid:
                                region = (d.get("region")
                                          or (app.get("region") if isinstance(app, dict) else "")
                                          or "")
                                nm = (app.get("name") if isinstance(app, dict) else "") \
                                    or f"Central (from {d.get('deviceType','assigned device')})"
                                assigned_app = {"id": aid, "name": nm, "region": region,
                                                "verified": True}
                    if len(page) < 100:
                        break
                    offset += 100
                subs = client.list_subscriptions()
                try:
                    sms = client.list_service_managers()
                except Exception:
                    sms = []
            # an id read off an already-assigned device is GROUND TRUTH (GreenLake
            # accepted it) — prefer it over the service-catalog provision id
            if assigned_app:
                sms = [assigned_app] + [s for s in sms
                                        if s.get("id") != assigned_app["id"]]
            st.session_state["glp_existing"] = sorted(existing)
            st.session_state["glp_subscriptions"] = subs
            st.session_state["glp_service_managers"] = sms
            st.rerun()
        except Exception as e:
            st.error(f"GreenLake error: {e}")

    existing = set(st.session_state.get("glp_existing", []))
    to_claim = [ap for ap in claimable if ap.serial.upper() not in existing]

    if st.session_state.get("glp_existing") is not None:
        already = len(claimable) - len(to_claim)
        st.markdown(
            f'<div style="font-family:\'IBM Plex Mono\',monospace;font-size:12px;color:{MUTED};'
            f'margin:0.4rem 0;">WORKSPACE: {len(existing)} devices · '
            f'<span style="color:{OK};">{already} of these APs already claimed</span> · '
            f'<span style="color:{WARN if to_claim else OK};">{len(to_claim)} to claim</span></div>',
            unsafe_allow_html=True,
        )

    if col_b.button(f"Claim {len(to_claim) if st.session_state.get('glp_existing') is not None else len(claimable)} APs into GreenLake",
                    type="primary", use_container_width=True,
                    disabled=not claimable):
        targets = to_claim if st.session_state.get("glp_existing") is not None else claimable
        if not targets:
            st.info("Nothing to claim — all discovered APs are already in the workspace.")
        else:
            try:
                client = _client()
                with st.spinner("Authenticating with GreenLake..."):
                    client.authenticate()
                payload = [{"serialNumber": ap.serial, "macAddress": ap.mac} for ap in targets]
                submitted = {d["serialNumber"].upper() for d in payload}
                with st.status(f"Claiming {len(payload)} devices (async — can take a few "
                               "minutes)...", expanded=True) as status_box:
                    task_id = client.add_devices(payload)
                    client.poll_task(task_id, on_poll=lambda attempt, s: status_box.update(
                        label=f"Claiming {len(payload)} devices — poll {attempt}, "
                              f"status: {s}"))
                    # Never trust the async-op body shape alone: reconcile the
                    # submitted serials against the actual workspace inventory.
                    status_box.update(label="Claim finished — verifying against "
                                            "workspace inventory...")
                    in_workspace = client.workspace_serials()
                ok = sorted(submitted & in_workspace)
                failed = sorted(submitted - in_workspace)
                st.session_state["glp_existing"] = sorted(in_workspace)
                st.session_state["glp_claim_result"] = {"ok": ok, "failed": failed}
                audit.record(
                    "claim",
                    user=st.session_state.get("_user"),
                    customer=st.session_state.get("customer_name"),
                    claimed=len(ok),
                    failed=len(failed),
                )
                st.rerun()
            except Exception as e:
                # Soft-fail only when the SUBMITTED serials are synthetic test
                # data (zztest prefix) — sniffing the error text downgraded
                # real claim failures (glp_client's failed-serial message
                # always mentions zztest/fake/HPE's records).
                if all(ap.serial.upper().startswith(TEST_PREFIX.upper())
                       for ap in targets):
                    st.info(f"Expected with test data — {e}")
                else:
                    st.error(f"Claim failed: {e}")

    claim_result = st.session_state.get("glp_claim_result")
    if claim_result:
        ok_list, failed = claim_result["ok"], claim_result["failed"]
        if failed:
            st.warning(f"Verified against the workspace — claimed: {len(ok_list)}, "
                       f"NOT in workspace: {len(failed)}. These must be resolved "
                       "before their APs are converted:")
            st.code("\n".join(str(s) for s in failed), language="text")
        else:
            st.success(f"Verified: all {len(ok_list)} device(s) are in the workspace.")

    # Only CLAIMING is skippable (CSV / GreenLake UI) — for New Central the
    # group-move cutover further down this page is still mandatory.
    if central_cfg and getattr(central_cfg, "destination", "new") == "new":
        mono_caption("ALREADY CLAIMED VIA CSV/GREENLAKE UI? CLAIMING IS OPTIONAL — "
                     "THE 'MOVE APS INTO DEVICE GROUPS' CUTOVER BELOW IS STILL "
                     "REQUIRED")
    else:
        mono_caption("ALREADY CLAIMED VIA CSV/GREENLAKE UI? JUST CONTINUE — "
                     "THIS STEP IS OPTIONAL FOR CLASSIC")

    st.divider()

    # ── Assign to Central (application + region) + subscription ─────────────
    section_label("Assign to Central + subscription", color=HPE_GREEN)
    st.markdown(
        f'<div style="font-size:12px;color:{FAINT};margin-bottom:0.4rem;">'
        f'A claimed AP only appears in New Central once it\'s assigned to the '
        f'Central <b>application + region</b> — GreenLake won\'t consume the '
        f'subscription without it (that\'s the <code>--</code> Application column). '
        f'This assigns the app, region, and subscription in one operation.</div>',
        unsafe_allow_html=True)
    subs = st.session_state.get("glp_subscriptions")
    sms = st.session_state.get("glp_service_managers")
    if subs is None:
        mono_caption('RUN "CHECK WORKSPACE" FIRST TO LOAD APPLICATIONS + SUBSCRIPTIONS')
    else:
        def _label(s: dict) -> str:
            key = s.get("key", s.get("id", "?"))
            tier = (s.get("tier") or s.get("subscriptionTier")
                    or s.get("subscriptionType") or "")
            qty = s.get("availableQuantity", s.get("quantity", ""))
            end = str(s.get("endTime", ""))[:10]
            return f"{key} · {tier} · avail {qty} · ends {end}"

        def _is_ap(s: dict) -> bool:
            return "AP" in str(s.get("subscriptionType", "")) or \
                   "AP" in str(s.get("tier", ""))
        active = [s for s in subs
                  if str(s.get("subscriptionStatus", "STARTED")).upper() != "ENDED"]
        active.sort(key=lambda s: (not _is_ap(s), str(s.get("tier", ""))))

        # application instance (Central) selector
        app_id, region = "", ""
        if not sms:
            st.warning("No Central application instance found in the workspace "
                       "(GET service-manager-provisions returned none). Assign the "
                       "APs to the Central application + region in the GreenLake UI, "
                       "or re-run Check workspace.")
        else:
            # options are the objects themselves — positional indexes go stale
            # when a re-run of Check workspace reorders the list
            app_pick = st.selectbox(
                "Central application instance (region)",
                options=sms,
                format_func=lambda m: (
                    f"{'✓ ' if m.get('verified') else ''}{m['name']} · "
                    f"{m['region'] or 'region?'}"
                    + (" — verified from an assigned device" if m.get('verified') else "")),
                help="Pick the ✓ option (read off a device GreenLake already "
                     "assigned — guaranteed-valid application id)")
            app_id, region = app_pick["id"], app_pick["region"]

        if not active:
            st.warning("No active subscriptions found — add subscription keys in "
                       "GreenLake before APs can be managed by Central.")
        else:
            subs = active
            sub_pick = st.selectbox("Subscription to apply to all claimed APs",
                                    options=subs, format_func=_label,
                                    help="Active subscriptions only; AP subscriptions listed first")
            claim_ok = {str(s).upper()
                        for s in (st.session_state.get("glp_claim_result") or {}).get("ok", [])}
            in_workspace = claim_ok | existing
            if in_workspace:
                assign_serials = [ap.serial for ap in claimable
                                  if ap.serial.upper() in in_workspace]
            else:
                assign_serials = [ap.serial for ap in claimable]
            ready = bool(assign_serials and app_id and region)
            if st.button(f"Assign {len(assign_serials)} APs to Central + subscription",
                         type="primary", disabled=not ready):
                key_or_id = sub_pick.get("id") or sub_pick.get("key")
                try:
                    client = _client()
                    with st.spinner("Authenticating..."):
                        client.authenticate()
                    results = []
                    prog = st.progress(0.0)
                    serials = assign_serials
                    for i, serial in enumerate(serials):
                        try:
                            client.assign_application(serial, app_id, region,
                                                      subscription_key_or_id=key_or_id)
                            results.append((serial, True, ""))
                        except Exception as e:
                            results.append((serial, False, str(e)))
                        prog.progress((i + 1) / len(serials))
                    st.session_state["glp_sub_results"] = results
                    st.rerun()
                except Exception as e:
                    st.error(f"Assignment failed: {e}")
            if assign_serials and not (app_id and region):
                mono_caption("SELECT A CENTRAL APPLICATION INSTANCE ABOVE TO ENABLE ASSIGNMENT")

    sub_results = st.session_state.get("glp_sub_results")
    if sub_results:
        ok_n = sum(1 for _, ok, _ in sub_results if ok)
        fail_rows = [(s, e) for s, ok, e in sub_results if not ok]
        if fail_rows:
            st.warning(f"Subscriptions: {ok_n} assigned, {len(fail_rows)} failed")
            for serial, err in fail_rows[:10]:
                st.code(f"{serial}: {err}", language="text")
        else:
            st.success(f"Subscription assigned to all {ok_n} APs.")

    # ── Device list ────────────────────────────────────────────────────────
    with st.expander(f"Device claim list ({len(claimable)})", expanded=False):
        rows = []
        for ap in claimable[:100]:
            claimed = ap.serial.upper() in existing
            b = badge("IN WORKSPACE", "green") if claimed else badge("TO CLAIM", "yellow")
            rows.append(mono_row([(ap.serial, MUTED), (ap.mac, FAINT),
                                  (ap.model, TEXT), (ap.name, FAINT)],
                                 trailing_html=b))
        st.markdown("".join(rows), unsafe_allow_html=True)

    # ── Move APs into device groups (the devices phase) ────────────────────
    if central_cfg and getattr(central_cfg, "destination", "new") == "new" \
            and st.session_state.get("provision_done"):
        st.divider()
        section_label("Move APs into device groups + assign", color=HPE_GREEN)
        info_banner(
            "⚠️ <b>This is the cutover — it CONVERTS your APs now.</b> Moving a live "
            "AOS 8 AP into its AOS 10 group makes Central push the AOS 10 conversion: "
            "<b>each AP reboots and goes offline ~10–20 min</b>, then comes up on AOS 10 "
            "in New Central. You do <b>not</b> need to run <code>ap convert</code> "
            "separately. Only run this inside your maintenance/cutover window.",
            color=WARN)
        st.markdown(
            f'<div style="font-size:12px;color:{FAINT};margin-bottom:0.5rem;">'
            f'Moves the claimed APs into their device groups (Classic on hybrid), '
            f'assigns the CAMPUS_AP persona, and adds them to the site. Requires the '
            f'APs to be claimed + subscribed above first.</div>',
            unsafe_allow_html=True)
        if use_classic_for_moves():
            st.caption("Hybrid mode armed — the group moves below route through "
                       "the Classic API Gateway "
                       f"`{st.session_state.get('central_base_classic','')}`.")
        if not reviewed:
            info_banner("Tick the review checklist at the top first.", color=WARN)
        ap_serials = {grp.name: [s for s in grp.ap_serials if s]
                      for grp in customer.ap_groups}
        _n_aps = sum(len(v) for v in ap_serials.values())
        cutover_ok = st.checkbox(
            f"I'm in my cutover window — convert these {_n_aps} AP(s) now "
            "(they will reboot into AOS 10 and drop offline)",
            key="cutover_confirm")
        if st.button("Move APs into groups + assign persona/site",
                     type="primary", disabled=not (reviewed and cutover_ok)):
            box = st.empty()
            lines: list[tuple[str, bool]] = []

            def on_step(label: str, ok: bool):
                lines.append((label, ok))
                with box.container():
                    for lbl, success in lines:
                        provision_step_line(lbl, success)
            try:
                client = build_central_client()
                with st.spinner("Authenticating with New Central..."):
                    client.authenticate()
                classic = build_classic_client() if use_classic_for_moves() else None
                with st.spinner("Moving APs and assigning persona/site..."):
                    results = client.provision(central_cfg, ap_serials=ap_serials,
                                               on_step=on_step, classic_client=classic,
                                               phase="devices")
                if classic is not None:
                    persist_rotated_refresh_token(classic)
                audit.record(
                    "cutover",
                    user=st.session_state.get("_user"),
                    tenant=st.session_state.get("central_base"),
                    customer=st.session_state.get("customer_name"),
                    hybrid=bool(classic),
                    steps=len(results),
                    failed=sum(1 for r in results if not r[1]),
                )
                st.session_state["onboard_results"] = results
                st.rerun()
            except Exception as e:
                st.error(f"Device onboarding failed: {e}")

        onb = st.session_state.get("onboard_results")
        if onb:
            fail = [r for r in onb if not r[1]]
            if fail:
                st.warning(f"{len(fail)} step(s) failed — APs must be in the GreenLake "
                           "workspace (claimed + subscribed) first; fix and re-run.")
                for label, _, err in fail:
                    provision_step_line(label, False)
                    if err:
                        st.code(err, language="text")
            else:
                st.success("APs moved into their groups and assigned — migration complete.")
            with st.expander("Onboarding step log", expanded=False):
                for label, success, _ in onb:
                    provision_step_line(label, success)

    st.divider()
    col_back, _, col_next = st.columns([1, 3, 1])
    col_back.button("← Back", on_click=lambda: st.session_state.update({"step": 2}))
    if col_next.button("Runbook →", type="primary", use_container_width=True):
        st.session_state["step"] = 4
        st.rerun()
