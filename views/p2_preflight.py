"""
Step 2: Preflight compatibility checks.
"""
import streamlit as st

from lib import compatibility
from lib.styles import page_header, section_label, check_card, mono_caption


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
