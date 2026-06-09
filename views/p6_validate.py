"""
Step 6: Post-migration validation — confirm converted APs are online in Central.
"""
import streamlit as st

from lib.session_clients import (
    build_central_client, build_classic_client, persist_rotated_refresh_token,
)
from lib.styles import (
    OK, WARN, FAINT, MUTED, TEXT,
    page_header, section_label, badge, mono_row, info_banner,
)

CHECKLIST = [
    ("ap_online",     "All APs online in Central (validated above)"),
    ("ssid_bcast",    "All SSIDs broadcasting — test with a client device on each SSID"),
    ("radius_ok",     "RADIUS auth working (Access Tracker shows GW mgmt IP as NAS)"),
    ("roaming_ok",    "Client roaming working between APs"),
    ("no_alerts",     "No sustained critical alerts in Central"),
    ("mc_decom",      "Mobility Conductor decommissioned"),
    ("airwave_decom", "AirWave decommissioned or migrated to Central monitoring"),
    ("switchports",   "Switch ports updated (tunnel VLANs pruned from AP access ports)"),
]


def render():
    page_header(6, "Validate Migration",
                "Confirm converted APs are online in Central and services are healthy")

    customer    = st.session_state.get("customer_config")
    central_cfg = st.session_state.get("central_config")

    if not customer or not central_cfg:
        st.error("Missing configuration — complete Step 1 first.")
        if st.button("← Back to Connect"):
            st.session_state["step"] = 0
            st.rerun()
        return

    expected = {ap.serial.strip().upper() for ap in customer.aps if ap.serial}
    no_serial = [ap.name for ap in customer.aps if not ap.serial]
    # Instant sources have no MC and no ap convert — conversion is driven from
    # Central (firmware compliance set in Step 3), so the guidance must differ.
    is_instant = getattr(customer, "source_type", "controller") == "instant"

    if is_instant:
        info_banner(
            "<b>Conversion is driven from Central</b> — once the firmware compliance set "
            "in Step 3 takes effect, each AP takes <b>10–20 minutes</b> to upgrade and "
            "register. Re-run validation until the counts converge.",
            color=OK,
        )
    else:
        info_banner(
            "After running <code>ap convert</code> on the MC, each AP takes <b>10–20 minutes</b> "
            "to upgrade and register. Re-run validation until the counts converge.",
            color=OK,
        )
    if no_serial:
        info_banner(
            f"<b>{len(no_serial)} AP(s) have no serial number</b> from discovery and can't "
            f"be matched here — re-discover with <code>show ap database long</code> or API "
            f"mode for full validation coverage.",
            color=WARN,
        )

    col_back, _, col_run = st.columns([1, 3, 1])
    col_back.button("← Back", on_click=lambda: st.session_state.update({"step": 4}))

    if col_run.button("Run Validation", type="primary", use_container_width=True):
        if getattr(central_cfg, "destination", "new") == "classic":
            client = build_classic_client()
            with st.spinner("Fetching AP status from classic Central..."):
                all_aps = client.list_all_aps()
            persist_rotated_refresh_token(client)
        else:
            client = build_central_client()
            with st.spinner("Authenticating..."):
                try:
                    client.authenticate()
                except Exception as e:
                    st.error(f"Auth failed: {e}")
                    return
            with st.spinner("Fetching AP status from Central..."):
                all_aps = client.list_all_aps()
        if all_aps is None:
            st.error("Could not fetch device status from Central — check the API "
                     "client's monitoring permissions and retry.")
            return
        st.session_state["validation_results"] = all_aps
        st.rerun()

    # ── Results (persisted across reruns) ──────────────────────────────────
    all_aps = st.session_state.get("validation_results")
    if all_aps is not None:
        def serial_of(ap: dict) -> str:
            return str(ap.get("serialNumber") or ap.get("serial") or "").strip().upper()

        migrated        = [ap for ap in all_aps if serial_of(ap) in expected]
        migrated_online = [ap for ap in migrated
                           if str(ap.get("status", "")).lower() == "up"]

        st.divider()
        section_label("Migration status")

        m1, m2, m3 = st.columns(3)
        m1.metric("Expected APs",      len(expected))
        m2.metric("Online in Central", len(migrated_online))
        m3.metric("Missing / Offline", max(0, len(expected) - len(migrated_online)))

        if not expected:
            st.warning("No serialled APs in the source config to validate against.")
        elif len(migrated_online) == len(expected):
            st.success(f"🎉 All {len(expected)} APs are online in Central. Migration complete!")
            if not st.session_state.get("validation_celebrated"):
                st.session_state["validation_celebrated"] = True
                st.balloons()
        elif migrated_online:
            pct = len(migrated_online) / len(expected) * 100
            st.warning(
                f"{len(migrated_online)} / {len(expected)} APs online ({pct:.0f}%). "
                "Conversion takes 10–20 min per AP — wait and re-run validation."
            )
        elif is_instant:
            st.error("No migrated APs detected yet. Confirm the firmware compliance set "
                     "in Step 3 took effect (check the VC or AP console if nothing is "
                     "rebooting), give it ~15 minutes, then re-run validation.")
        else:
            st.error("No migrated APs detected yet. Confirm the ap convert commands ran "
                     "on the MC, give it ~15 minutes, then re-run validation.")

        missing_serials = expected - {serial_of(ap) for ap in migrated}
        if missing_serials and len(missing_serials) <= 30:
            with st.expander(f"Not seen in Central yet ({len(missing_serials)})"):
                st.markdown("".join(
                    mono_row([(s, MUTED)]) for s in sorted(missing_serials)
                ), unsafe_allow_html=True)

        if migrated:
            with st.expander(f"AP details ({len(migrated)} matched)", expanded=False):
                rows = []
                for ap in migrated:
                    status = str(ap.get("status", "unknown"))
                    up = status.lower() == "up"
                    b = badge("ONLINE", "green") if up else badge(status.upper(), "yellow")
                    rows.append(mono_row([
                        (serial_of(ap) or "?", MUTED),
                        (str(ap.get("model", "?")), TEXT),
                        (str(ap.get("deviceName") or ap.get("name") or "?"), MUTED),
                        (str(ap.get("firmwareVersion") or ap.get("swVersion") or ""), FAINT),
                    ], trailing_html=b))
                st.markdown("".join(rows), unsafe_allow_html=True)

    # ── Post-migration checklist ───────────────────────────────────────────
    st.divider()
    section_label("Post-migration checklist")
    done = 0
    for key, label in CHECKLIST:
        # Mirror into PLAIN session keys (chk_*) every render. Widget-key state
        # (check_*) is garbage-collected when the widget isn't rendered (e.g.
        # while back on the runbook), which was wiping checklist progress.
        ticked = st.checkbox(label, value=st.session_state.get(f"chk_{key}", False),
                             key=f"check_{key}")
        st.session_state[f"chk_{key}"] = ticked
        if ticked:
            done += 1
    st.markdown(
        f'<div style="margin-top:0.5rem;font-family:\'IBM Plex Mono\',monospace;'
        f'font-size:11px;color:{OK if done == len(CHECKLIST) else FAINT};letter-spacing:0.1em;">'
        f'{done}/{len(CHECKLIST)} COMPLETE'
        f'{" — ENGAGEMENT CLOSED OUT 🏁" if done == len(CHECKLIST) else ""}</div>',
        unsafe_allow_html=True,
    )
