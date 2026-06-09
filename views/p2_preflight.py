"""
Step 2: Preflight compatibility checks.
"""
import streamlit as st

from lib import compatibility
from lib.models import VLAN
from lib.translator import translate
from lib.styles import (page_header, section_label, check_card, mono_caption,
                        FAINT, TEXT, esc)


def _named_vlan_editor(customer, central) -> None:
    """SSIDs whose AOS 8 VLAN is a NAMED pool with no numeric id default to
    VLAN 1. Let the operator map each named token to a real VLAN id, then
    re-translate so the SSID lands on the right VLAN."""
    named: dict[str, list[str]] = {}
    for s in customer.ssids:
        if getattr(s, "vlan_raw", None):
            named.setdefault(s.vlan_raw, []).append(s.display_name)
    if not named:
        return

    section_label("Named VLAN mapping — fix before provisioning")
    st.markdown(
        f'<div style="font-size:12px;color:{FAINT};margin-bottom:0.5rem;">'
        f'{len(named)} SSID(s) reference a <b>named</b> VLAN with no numeric ID, so '
        f'they defaulted to VLAN 1. Enter the real VLAN ID so each SSID lands on the '
        f'correct VLAN.</div>', unsafe_allow_html=True)

    mapping: dict[str, int] = {}
    for token, ssid_names in named.items():
        c1, c2 = st.columns([2, 1])
        c1.markdown(
            f'<div style="padding-top:6px;color:{TEXT};font-size:13px;">'
            f'<code>{esc(token)}</code> '
            f'<span style="color:{FAINT};">→ {esc(", ".join(ssid_names))}</span></div>',
            unsafe_allow_html=True)
        mapping[token] = c2.number_input(
            f"VLAN for {token}", min_value=1, max_value=4094, value=1, step=1,
            key=f"vlanmap_{token}", label_visibility="collapsed")

    if st.button("Apply VLAN mapping", type="primary"):
        for s in customer.ssids:
            tok = getattr(s, "vlan_raw", None)
            if tok in mapping:
                s.vlan = int(mapping[tok])
                if not any(v.id == s.vlan for v in customer.vlans):
                    customer.vlans.append(VLAN(s.vlan, tok))
                s.vlan_raw = None
        # re-translate, preserving fields translate() doesn't set (plus the
        # cluster name, which the operator may have renamed in Step 3)
        gw_mode = "retire" if getattr(central, "gateways_retired", False) else "keep"
        new_central = translate(
            customer,
            customer_name=st.session_state.get("customer_name", central.customer_name),
            central_base_url=st.session_state.get("central_base", central.base_url),
            aos10_firmware=st.session_state.get("aos10_fw", "10.7.0.0"),
            site_name=(central.sites[0] if central.sites else ""),
            gateway_mode=gw_mode)
        for f in ("destination", "site_address", "site_city", "site_state",
                  "site_country", "site_zipcode", "site_timezone", "gw_serial",
                  "gw_cluster_name"):
            setattr(new_central, f, getattr(central, f, getattr(new_central, f)))
        st.session_state["customer_config"] = customer
        st.session_state["central_config"] = new_central
        st.session_state.pop("preflight_results", None)
        # the old config may already be provisioned — force Step 3 to re-run
        # so the corrected VLANs actually reach Central
        st.session_state.pop("provision_done", None)
        st.session_state.pop("provision_results", None)
        st.success("VLAN mapping applied — re-running preflight.")
        st.rerun()


def render():
    page_header(2, "Preflight Checks",
                "Compatibility and safety verification before anything is written to Central")

    customer = st.session_state.get("customer_config")
    central  = st.session_state.get("central_config")

    if not customer or not central:
        st.error("Missing configuration — complete Step 1 first.")
        if st.button("← Back to Connect"):
            st.session_state["step"] = 0
            st.rerun()
        return

    if "preflight_results" not in st.session_state:
        with st.spinner("Running checks..."):
            st.session_state["preflight_results"] = compatibility.run_all(customer, central)
    results = st.session_state["preflight_results"]

    fails  = [r for r in results if r.status == compatibility.Status.FAIL]
    warns  = [r for r in results if r.status == compatibility.Status.WARN]
    passes = [r for r in results if r.status == compatibility.Status.PASS]

    # ── Score card ─────────────────────────────────────────────────────────
    m1, m2, m3 = st.columns(3)
    m1.metric("Passed",   len(passes))
    m2.metric("Warnings", len(warns))
    m3.metric("Blockers", len(fails))

    if fails:
        st.error(f"**{len(fails)} blocker(s)** must be resolved before provisioning.")
    elif warns:
        st.warning(f"**{len(warns)} warning(s)** — review each one before continuing.")
    else:
        st.success("All checks passed. Ready to provision.")

    st.divider()

    # ── Check results ──────────────────────────────────────────────────────
    if fails:
        section_label("Blockers — must fix")
        for r in fails:
            check_card("⛔", r.name, r.message, r.detail or "", variant="red")

    if warns:
        section_label("Warnings — review before cutover")
        for r in warns:
            check_card("⚠️", r.name, r.message, r.detail or "", variant="yellow")

    if passes:
        with st.expander(f"✓  {len(passes)} checks passed", expanded=False):
            for r in passes:
                check_card("✓", r.name, r.message, variant="green")

    # ── Named VLAN mapping (fix non-numeric VLANs before provisioning) ──────
    st.divider()
    _named_vlan_editor(customer, central)

    st.divider()

    # ── Navigation ─────────────────────────────────────────────────────────
    col_back, col_mid, col_rerun, col_next = st.columns(
        [1, 2.4, 0.8, 1], vertical_alignment="center")
    col_back.button("← Back", on_click=lambda: st.session_state.update({"step": 0}))

    if col_rerun.button("Re-run", use_container_width=True):
        st.session_state.pop("preflight_results", None)
        st.rerun()

    if fails:
        override = col_mid.checkbox(
            "Override blockers — I understand the risk and will resolve them before cutover",
            key="preflight_override",
        )
        if col_next.button("Provision →", type="primary", use_container_width=True,
                           disabled=not override):
            st.session_state["step"] = 2
            st.rerun()
        if not override:
            with col_mid:
                mono_caption("RESOLVE BLOCKERS OR OVERRIDE TO CONTINUE")
    else:
        if col_next.button("Provision →", type="primary", use_container_width=True):
            st.session_state["step"] = 2
            st.rerun()
