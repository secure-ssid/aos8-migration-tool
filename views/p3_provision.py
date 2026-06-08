"""
Step 3: Provision New Central — site, device groups, VLANs, SSIDs, GW cluster,
auth servers, firmware compliance.
"""
import streamlit as st

from lib.session_clients import (
    build_central_client, build_classic_client, persist_rotated_refresh_token,
    have_classic_creds,
)
from lib.styles import (
    BORDER, FAINT, MUTED, TEXT,
    page_header, section_label, provision_step_line, badge, esc, info_banner,
)


def render():
    central_cfg_hdr = st.session_state.get("central_config")
    dest = getattr(central_cfg_hdr, "destination", "new") if central_cfg_hdr else "new"
    page_header(3, "Provision Central",
                "Create the site, AOS 10 groups, WLANs and firmware compliance in classic Central"
                if dest == "classic" else
                "Create the site, device groups, VLANs, SSIDs and gateway cluster in New Central")

    customer    = st.session_state.get("customer_config")
    central_cfg = st.session_state.get("central_config")

    if not customer or not central_cfg:
        st.error("Missing configuration — complete Step 1 first.")
        if st.button("← Back to Connect"):
            st.session_state["step"] = 0
            st.rerun()
        return

    # ── Manifest: what will be created ─────────────────────────────────────
    with st.expander("Manifest — what will be created",
                     expanded=not st.session_state.get("provision_done")):
        section_label("Site")
        addr = ", ".join(p for p in (central_cfg.site_address, central_cfg.site_city,
                                     central_cfg.site_country) if p)
        st.markdown(
            " ".join(
                f'{badge(s, "blue")} '
                f'<span style="color:{FAINT};font-size:11.5px;">{esc(addr) if addr else "no address set"}</span>'
                for s in central_cfg.sites
            ),
            unsafe_allow_html=True,
        )

        if central_cfg.gw_cluster_name and central_cfg.destination == "classic":
            st.markdown('<div style="height:0.6rem;"></div>', unsafe_allow_html=True)
            section_label("Gateways (classic)")
            st.markdown(
                badge("⚡ AOS10 groups allow Gateways — cluster auto-forms on join", "orange"),
                unsafe_allow_html=True,
            )
        elif central_cfg.gw_cluster_name:
            st.markdown('<div style="height:0.6rem;"></div>', unsafe_allow_html=True)
            section_label("Gateway cluster")
            st.markdown(
                badge(f"⚡ {central_cfg.gw_cluster_name}", "orange") + " " +
                badge(f"{central_cfg.gw_cluster_name}-gws (device group)", "gray"),
                unsafe_allow_html=True,
            )
        elif central_cfg.gateways_retired:
            st.markdown('<div style="height:0.6rem;"></div>', unsafe_allow_html=True)
            section_label("Gateway strategy")
            st.markdown(
                badge("GATEWAYS RETIRED — ALL SSIDS BRIDGE MODE", "yellow"),
                unsafe_allow_html=True,
            )

        st.markdown('<div style="height:0.6rem;"></div>', unsafe_allow_html=True)
        section_label("Device groups")
        for grp in central_cfg.groups:
            modes = []
            if grp.has_tunnel_ssid: modes.append(badge("OVERLAY", "blue"))
            if grp.has_bridge_ssid: modes.append(badge("UNDERLAY", "green"))
            ssid_names = ", ".join(s.display_name for s in grp.ssids[:6])
            if len(grp.ssids) > 6:
                ssid_names += f" +{len(grp.ssids) - 6}"
            st.markdown(
                f'<div style="padding:7px 0;border-bottom:1px solid {BORDER};">'
                f'<span style="font-weight:600;font-size:13.5px;color:{TEXT};">{esc(grp.name)}</span>'
                f' &nbsp;{"".join(modes)}&nbsp; '
                f'<span style="color:{FAINT};font-size:11.5px;font-family:\'IBM Plex Mono\',monospace;">'
                f'{len(grp.ssids)} SSIDs · {len(grp.vlans)} VLANs · fw {esc(grp.firmware_version)}</span>'
                f'<div style="color:{MUTED};font-size:12px;margin-top:3px;">{esc(ssid_names)}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        if central_cfg.radius_servers:
            st.markdown('<div style="height:0.6rem;"></div>', unsafe_allow_html=True)
            section_label("Auth servers (library profiles)")
            for s in central_cfg.radius_servers:
                st.markdown(
                    f'<span style="font-size:13px;color:{MUTED};font-family:\'IBM Plex Mono\',monospace;">'
                    f'<b style="color:{TEXT};">{esc(s.name)}</b> → {esc(s.address)}:{s.auth_port}</span><br>',
                    unsafe_allow_html=True,
                )

    # ── Already provisioned ────────────────────────────────────────────────
    if st.session_state.get("provision_done"):
        _show_results(st.session_state.get("provision_results", []))
        st.divider()
        col_back, col_mid, col_next = st.columns([1, 3, 1])
        col_back.button("← Back", on_click=lambda: st.session_state.update({"step": 1}))
        if col_mid.button("Reset & re-run provisioning"):
            st.session_state.pop("provision_done", None)
            st.session_state.pop("provision_results", None)
            st.rerun()
        if col_next.button("GreenLake →", type="primary", use_container_width=True):
            st.session_state["step"] = 3
            st.rerun()
        return

    # ── Provision ──────────────────────────────────────────────────────────
    info_banner(
        "Provisioning <b>writes to the customer tenant</b>. Steps are idempotent — "
        "existing sites/groups/profiles with matching names are reused, and every "
        "API failure is reported per step (nothing is silently skipped).",
    )

    if central_cfg.gw_cluster_name:
        new_name = st.text_input(
            "Gateway cluster name",
            value=central_cfg.gw_cluster_name,
            help="Auto-generated from the customer name — change it if it could "
                 "collide with an existing cluster in the tenant. No spaces; "
                 "must not start with 'auto_'.",
        )
        sanitized = new_name.strip().replace(" ", "-")
        if sanitized.startswith("auto_"):
            st.warning("Cluster names must not start with 'auto_' — adjust before provisioning.")
        elif sanitized:
            central_cfg.gw_cluster_name = sanitized

    # Hybrid credential status — group create/move route through Classic on a
    # hybrid tenant; make it obvious whether that's wired before provisioning.
    if central_cfg.destination == "new":
        if have_classic_creds():
            st.success(f"Hybrid mode armed — groups/moves will use Classic API "
                       f"`{st.session_state.get('central_base_classic','')}`. "
                       "SSIDs/VLANs stay on New Central.")
        else:
            st.warning("No Classic API Gateway token registered. On a HYBRID tenant "
                       "device-group create/move will be blocked — add the base URL + "
                       "token in Step 1 → 'Hybrid cluster?' (the status line there must "
                       "read ✓ token registered).")

    st.divider()
    col_back, _, col_run = st.columns([1, 3, 1])
    col_back.button("← Back", on_click=lambda: st.session_state.update({"step": 1}))

    if col_run.button("🚀 Provision", type="primary", use_container_width=True):
        ap_serials = {
            grp.name: [s for s in grp.ap_serials if s]
            for grp in customer.ap_groups
        }

        progress_box = st.empty()
        status_lines: list[tuple[str, bool]] = []

        def on_step(label: str, ok: bool):
            status_lines.append((label, ok))
            with progress_box.container():
                for lbl, success in status_lines:
                    provision_step_line(lbl, success)

        if central_cfg.destination == "classic":
            client = build_classic_client()
            with st.spinner("Checking classic Central access..."):
                try:
                    client.list_group_names()  # also seeds the group cache
                except Exception as e:
                    st.error(f"Classic Central access check failed: {e}")
                    st.info("The access token may be expired (~2h lifetime) — "
                            "generate a fresh one in API Gateway → System Apps & Tokens.")
                    return
            st.success("Classic Central reachable")
            ap_macs = {ap.serial: ap.mac for ap in customer.aps if ap.serial and ap.mac}
            with st.spinner("Provisioning classic Central..."):
                results = client.provision(central_cfg, ap_serials=ap_serials,
                                           ap_macs=ap_macs, on_step=on_step)
            if persist_rotated_refresh_token(client):
                st.info("The refresh token rotated during this run — the new one is "
                        "saved in this session. Update wherever you store it.")
        else:
            client = build_central_client()
            with st.spinner("Authenticating with New Central (GreenLake SSO)..."):
                try:
                    client.authenticate()
                except Exception as e:
                    st.error(f"Authentication failed: {e}")
                    st.info("Check the API client ID/secret and that the client has "
                            "Aruba Central (network-config) access in GreenLake.")
                    return
            st.success("Authenticated with New Central")
            # hybrid clusters need the Classic API for device-group create/move
            classic_client = None
            if have_classic_creds():
                classic_client = build_classic_client()
                st.caption("Hybrid mode: device groups + moves will route through "
                           "the Classic API Gateway.")
            elif st.session_state.get("classic_access_token"):
                st.warning("A Classic access token is set but the Classic API Gateway "
                           "base URL is empty — set it in Step 1 → 'Hybrid cluster?' "
                           "before provisioning, or device-group create will fail.")
            with st.spinner("Provisioning..."):
                results = client.provision(central_cfg, ap_serials=ap_serials,
                                           on_step=on_step, classic_client=classic_client)
            if classic_client is not None and persist_rotated_refresh_token(classic_client):
                st.info("The Classic refresh token rotated during this run — the new "
                        "one is saved in this session.")

        st.session_state["provision_results"] = results
        st.session_state["provision_done"]    = True
        st.rerun()


def _show_results(results: list[tuple[str, bool, str]]):
    ok   = [r for r in results if r[1]]
    fail = [r for r in results if not r[1]]

    m1, m2 = st.columns(2)
    m1.metric("Steps completed", len(ok))
    m2.metric("Steps failed",    len(fail))

    if fail:
        st.error(f"{len(fail)} step(s) failed — review each error, fix the cause, "
                 "then use **Reset & re-run provisioning** (completed objects are reused).")
        for label, _, err in fail:
            provision_step_line(label, False)
            if err:
                st.code(err, language="text")
    else:
        st.success("All provisioning steps completed successfully.")

    with st.expander("Full step log", expanded=False):
        for label, success, _ in results:
            provision_step_line(label, success)
