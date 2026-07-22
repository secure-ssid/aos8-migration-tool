"""
Step 5: Generate and display the ap convert CLI runbook.
"""
import streamlit as st

from lib import runbook
from lib.styles import page_header, section_label, badge, esc, info_banner, WARN


def render():
    customer_hdr = st.session_state.get("customer_config")
    is_instant = bool(customer_hdr and
                      getattr(customer_hdr, "source_type", "controller") == "instant")
    page_header(5, "Conversion Runbook" if is_instant else "AP Convert Runbook",
                "Central-driven conversion — no controller CLI" if is_instant else
                "Customer-specific CLI commands to run on the Mobility Controller")

    customer      = st.session_state.get("customer_config")
    central_cfg   = st.session_state.get("central_config")
    customer_name = st.session_state.get("customer_name", "Customer")

    if not customer or not central_cfg:
        st.error("Missing configuration — complete Step 1 first.")
        if st.button("← Back to Connect"):
            st.session_state["step"] = 0
            st.rerun()
        return

    if not st.session_state.get("provision_done"):
        st.warning("Complete **Step 3 — Build Config** before running ap convert commands. "
                   "Converted APs look for their config in Central — if it isn't there yet, "
                   "they come up with nothing to broadcast.")
        col_back, _ = st.columns([1, 5])
        col_back.button("← Back to Provision",
                        on_click=lambda: st.session_state.update({"step": 2}))
        return

    _failed = [r for r in st.session_state.get("provision_results", [])
               if not r[1]]
    if _failed:
        st.warning(f"⚠️ Step 3 finished with **{len(_failed)} failed step(s)** — "
                   "converted APs may come up without parts of their config. "
                   "Fix and re-run provisioning before converting.")

    rb = runbook.generate(customer, central_cfg, customer_name)

    if is_instant:
        info_banner(
            "<b>Conversion is driven from Central</b> — there is nothing to run on the "
            "cluster. Follow the runbook order; the firmware compliance set in Step 3 "
            "does the conversion."
        )
    else:
        info_banner(
            "<b>These commands run on the Mobility Controller CLI</b> — SSH in or use the "
            "console. Copy and paste each block in order. Central is provisioned (Step 3 ✓)."
        )

    cluster = customer.cluster
    if cluster and len(cluster.members) >= 2:
        members = " · ".join(cluster.members)
        info_banner(
            f"<b>⚠️ {esc(cluster.type)} cluster detected</b> ({esc(members)}) — follow the "
            f"{esc(cluster.type)} upgrade sequence in the runbook exactly or APs will be "
            "stranded mid-migration.",
            color=WARN,
        )

    st.code(rb, language="text")

    col1, _ = st.columns([1.4, 3.6])
    col1.download_button(
        "⬇ Download runbook (.txt)",
        data=rb,
        file_name=f"{customer_name.lower().replace(' ', '_')}_ap_convert_runbook.txt",
        mime="text/plain",
        use_container_width=True,
    )

    st.divider()

    # ── GW migration guide ─────────────────────────────────────────────────
    section_label("Gateway migration")
    if central_cfg.gw_cluster_name:
        st.markdown(
            f'Tunnel-mode SSIDs need the gateway cluster '
            f'{badge("⚡ " + central_cfg.gw_cluster_name, "orange")} — the MC hardware '
            f'becomes the Gateway once AOS 10 firmware is applied. After it registers, '
            f'add it to the cluster in Central → Devices → Gateways.',
            unsafe_allow_html=True,
        )
        tab1, tab2 = st.tabs(["ZTP (preferred)", "Static Activate"])
        with tab1:
            st.code(
                "1. Remove MC from its Activate folder (if it has existing provisioning rules)\n"
                "2. If MC had prior upgrades: load AOS 10 image, then: write erase → reload\n"
                "3. Plug any GW port (NOT GE 0/0/1) into a DHCP + Internet-connected port\n"
                "4. GW auto-contacts Activate → upgrades to AOS 10 → registers in Central\n"
                "5. In Central: Devices → Gateways → assign to cluster",
                language="text",
            )
        with tab2:
            st.code(
                "On GW console, type: static-activate\n"
                "Enter IP address:   <gw-mgmt-ip>\n"
                "Enter subnet mask:  <mask>\n"
                "Enter gateway:      <default-gw>\n"
                "GW contacts Activate and proceeds automatically.",
                language="text",
            )
    elif central_cfg.gateways_retired:
        st.info("Gateways retired by design — every SSID was provisioned as bridge/underlay. "
                "No GW ZTP needed; decommission the MCs after Step 6 validation. "
                "Make sure the switchport changes from preflight are done BEFORE converting.")
    else:
        st.info("No gateway cluster required — all SSIDs are bridge mode. "
                "APs connect directly to Central.")

    st.divider()
    col_back, _, col_next = st.columns([1, 3, 1])
    col_back.button("← Back", on_click=lambda: st.session_state.update({"step": 3}))
    if col_next.button("Validate →", type="primary", use_container_width=True):
        st.session_state["step"] = 5
        st.rerun()
