"""
Step 4: Onboard devices to HPE GreenLake — claim APs into the workspace
(serial + wired MAC) and assign subscriptions, so they're recognized the
moment `ap convert` brings them up in Central.
"""
import streamlit as st

from lib.glp_client import GLPClient
from lib.session_clients import (
    build_central_client, build_classic_client, have_classic_creds,
    persist_rotated_refresh_token,
)
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
    page_header(4, "GreenLake Onboarding",
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
            "with <code>show ap database long</code> or add them manually in GreenLake.",
            color=WARN,
        )

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
                offset = 0
                while True:
                    page = client.list_devices(limit=100, offset=offset)
                    existing |= {str(d.get("serialNumber", "")).upper() for d in page}
                    if len(page) < 100:
                        break
                    offset += 100
                subs = client.list_subscriptions()
            st.session_state["glp_existing"] = sorted(existing)
            st.session_state["glp_subscriptions"] = subs
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
                st.rerun()
            except Exception as e:
                msg = str(e)
                if "zztest" in msg.lower() or "fake" in msg.lower() or \
                        "HPE's records" in msg:
                    st.info(f"Expected with test data — {msg}")
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

    st.divider()

    # ── Subscriptions ──────────────────────────────────────────────────────
    section_label("Assign subscriptions", color=HPE_GREEN)
    subs = st.session_state.get("glp_subscriptions")
    if subs is None:
        mono_caption('RUN "CHECK WORKSPACE" FIRST TO LOAD AVAILABLE SUBSCRIPTIONS')
    else:
        def _label(s: dict) -> str:
            key = s.get("key", s.get("id", "?"))
            tier = (s.get("tier") or s.get("subscriptionTier")
                    or s.get("subscriptionType") or "")
            qty = s.get("availableQuantity", s.get("quantity", ""))
            end = str(s.get("endTime", ""))[:10]
            return f"{key} · {tier} · avail {qty} · ends {end}"

        # active subs only, AP-type first (CENTRAL_AP / FOUNDATION_AP tiers)
        def _is_ap(s: dict) -> bool:
            return "AP" in str(s.get("subscriptionType", "")) or \
                   "AP" in str(s.get("tier", ""))
        active = [s for s in subs
                  if str(s.get("subscriptionStatus", "STARTED")).upper() != "ENDED"]
        active.sort(key=lambda s: (not _is_ap(s), str(s.get("tier", ""))))

        if not active:
            st.warning("No active subscriptions found in the workspace — add subscription "
                       "keys in GreenLake before APs can be managed by Central.")
        else:
            subs = active
            choice = st.selectbox("Subscription to apply to all claimed APs",
                                  options=range(len(subs)),
                                  format_func=lambda i: _label(subs[i]),
                                  help="Active subscriptions only; AP subscriptions listed first")
            # target only APs actually in the workspace: claim results first,
            # then the workspace snapshot, falling back to all claimable
            claim_ok = {str(s).upper()
                        for s in (st.session_state.get("glp_claim_result") or {}).get("ok", [])}
            in_workspace = claim_ok | existing
            if in_workspace:
                assign_serials = [ap.serial for ap in claimable
                                  if ap.serial.upper() in in_workspace]
            else:
                assign_serials = [ap.serial for ap in claimable]
            if st.button(f"Assign subscription to {len(assign_serials)} claimed APs",
                         type="primary", disabled=not assign_serials):
                sub = subs[choice]
                key_or_id = sub.get("id") or sub.get("key")
                try:
                    client = _client()
                    with st.spinner("Authenticating..."):
                        client.authenticate()
                    results = []
                    prog = st.progress(0.0)
                    serials = assign_serials
                    for i, serial in enumerate(serials):
                        try:
                            client.assign_subscription(serial, key_or_id)
                            results.append((serial, True, ""))
                        except Exception as e:
                            results.append((serial, False, str(e)))
                        prog.progress((i + 1) / len(serials))
                    st.session_state["glp_sub_results"] = results
                    st.rerun()
                except Exception as e:
                    st.error(f"Subscription assignment failed: {e}")

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
        st.markdown(
            f'<div style="font-size:12px;color:{FAINT};margin-bottom:0.5rem;">'
            f'Moves the <b>claimed</b> APs into their New Central device groups '
            f'(Classic on hybrid), assigns the CAMPUS_AP persona, and adds them to '
            f'the site. Run this <b>after</b> the APs are claimed + subscribed above. '
            f'This is the step that converts the APs over.</div>',
            unsafe_allow_html=True)
        if not reviewed:
            info_banner("Tick the review checklist at the top first.", color=WARN)
        ap_serials = {grp.name: [s for s in grp.ap_serials if s]
                      for grp in customer.ap_groups}
        if st.button("Move APs into groups + assign persona/site",
                     type="primary", disabled=not reviewed):
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
                classic = build_classic_client() if have_classic_creds() else None
                with st.spinner("Moving APs and assigning persona/site..."):
                    results = client.provision(central_cfg, ap_serials=ap_serials,
                                               on_step=on_step, classic_client=classic,
                                               phase="devices")
                if classic is not None:
                    persist_rotated_refresh_token(classic)
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
    col_back, col_mid, col_next = st.columns([1, 3, 1])
    col_back.button("← Back", on_click=lambda: st.session_state.update({"step": 2}))
    with col_mid:
        mono_caption("ALREADY CLAIMED VIA CSV/GREENLAKE UI? JUST CONTINUE — "
                     "THIS STEP IS OPTIONAL")
    if col_next.button("Runbook →", type="primary", use_container_width=True):
        st.session_state["step"] = 4
        st.rerun()
