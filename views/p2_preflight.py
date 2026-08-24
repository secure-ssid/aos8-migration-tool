"""
Step 2: Preflight compatibility checks.
"""
import streamlit as st

from lib import audit, compatibility
from lib.models import VLAN
from lib.translator import translate
from lib.styles import (page_header, section_label, check_card, mono_caption,
                        FAINT, MUTED, TEXT, WARN, esc)


# ─────────────────── Provision gate (finding #5) ───────────────────
# Pure functions so the gate contract is unit-tested directly
# (tests/test_preflight_gate.py), mirroring the Step 4 cutover gate.

def provision_blockers(results, acks) -> list[str]:
    """Why provisioning must stay locked. `acks` maps a non-critical
    blocker's check name to the operator's free-text justification (blank is
    not a justification). Critical blockers have no override at all. Empty
    list = provisioning may proceed."""
    blockers = []
    for r in results:
        if r.status != compatibility.Status.FAIL:
            continue
        if getattr(r, "critical", False):
            blockers.append(f"Critical blocker (cannot be overridden): {r.name}")
        elif not (acks.get(r.name) or "").strip():
            blockers.append(f"Blocker not acknowledged: {r.name}")
    return blockers


def acknowledge_blocker(result, reason, *, user=None, customer=None,
                        record=audit.record) -> str:
    """Record the operator's written acknowledgement of one non-critical
    blocker and return the stored reason. Raises ValueError on a blank
    reason; propagates AuditWriteError — an acknowledgement without a
    durable audit record must not take effect."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a written reason is required")
    record("preflight-blocker-ack",
           user=user, customer=customer,
           check=result.name, message=(result.message or "")[:200],
           reason=reason)
    return reason


def _render_override_gate(fails, acks) -> None:
    """Per-risk acknowledgements for the non-critical blockers: each one
    needs its own written reason, and every acknowledgement writes an audit
    record with the operator's identity. The global 'override everything'
    checkbox this replaces waived every blocker at once and left no trail.
    Critical blockers never reach this UI — they cannot be overridden."""
    overridable = [r for r in fails if not getattr(r, "critical", False)]
    if not overridable:
        return
    with st.container(border=True):
        st.markdown(
            f'<div style="font-weight:600;color:{WARN};margin-bottom:0.4rem;">'
            f'🔒 Blocker acknowledgements — {len(overridable)} item(s) need an '
            f'individual written reason before provisioning</div>',
            unsafe_allow_html=True)
        for r in overridable:
            reason = (acks.get(r.name) or "").strip()
            if reason:
                st.markdown(
                    f'<div style="font-size:12.5px;color:{MUTED};margin:3px 0;">'
                    f'✅ <s>{esc(r.name)}</s> — acknowledged: '
                    f'<i>{esc(reason)}</i></div>', unsafe_allow_html=True)
                continue
            st.markdown(
                f'<div style="font-size:12.5px;color:{TEXT};margin:5px 0 2px;">'
                f'❌ <b>{esc(r.name)}</b></div>', unsafe_allow_html=True)
            reason_in = st.text_input(
                "Why is it safe to provision despite this blocker?",
                key=f"p2_ack_reason_{abs(hash(r.name))}",
                placeholder="Required — e.g. 'WLAN will be rebuilt by hand in "
                            "Central before cutover'")
            if st.button("Acknowledge this blocker",
                         key=f"p2_ack_btn_{abs(hash(r.name))}"):
                if not reason_in.strip():
                    st.error("A written reason is required — the acknowledgement "
                             "is an audit record, not a checkbox.")
                else:
                    try:
                        acks[r.name] = acknowledge_blocker(
                            r, reason_in,
                            user=st.session_state.get("_user"),
                            customer=st.session_state.get("customer_name"))
                    except audit.AuditWriteError as e:
                        # stashed because the rerun below would wipe a
                        # same-render warning — and the ack does NOT take
                        # effect when its audit record is missing
                        st.session_state["p2_ack_audit_error"] = str(e)
                    # no st.rerun(): the button click already re-runs the
                    # script, and a mid-render rerun would unmount the
                    # widgets below and garbage-collect their keyed state


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
        # the number inputs default to 1 — an untouched token silently mapping
        # everything onto VLAN 1 is exactly the mistake this editor exists to
        # prevent, so call those out explicitly
        ones = [t for t, v in mapping.items() if int(v) == 1]
        if ones:
            st.warning("Mapped to **VLAN 1** (the input default): "
                       + ", ".join(f"`{t}`" for t in ones)
                       + " — confirm that is really the intended VLAN.")
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
            # keep the base URL the config was built with — session's
            # central_base holds the NEW-Central URL even in classic sessions
            central_base_url=central.base_url
                or st.session_state.get("central_base", ""),
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
        # a cutover banner from the pre-remap config must not survive either
        st.session_state.pop("onboard_results", None)
        st.session_state.pop("onboard_results_fp", None)
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
            # results + acknowledgements live in ONE session entry, so every
            # place that clears the cached results (Re-run, the VLAN remap
            # above, reset_downstream_state) clears stale acks with them —
            # an ack from a previous run must never waive a fresh blocker
            st.session_state["preflight_results"] = {
                "results": compatibility.run_all(customer, central),
                "acks": {},
            }
    _cached = st.session_state["preflight_results"]
    results = _cached["results"]
    acks = _cached["acks"]

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
            if getattr(r, "critical", False):
                mono_caption("NOT OVERRIDABLE — RESOLVE AT THE SOURCE AND RE-RUN")
        _render_override_gate(fails, acks)
        _ack_err = st.session_state.pop("p2_ack_audit_error", None)
        if _ack_err:
            st.warning("Acknowledgement did NOT take effect — its audit "
                       f"record could not be written: {_ack_err}")

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
        # computed AFTER the acknowledgement UI above: ack buttons mutate
        # `acks` earlier in this same run, and the gate must see that
        blockers = provision_blockers(results, acks)
        if col_next.button("Provision →", type="primary", use_container_width=True,
                           disabled=bool(blockers)):
            st.session_state["step"] = 2
            st.rerun()
        if blockers:
            with col_mid:
                mono_caption(
                    f"{len(blockers)} BLOCKER(S) HOLDING — CRITICAL ONES MUST BE "
                    "FIXED, THE REST ACKNOWLEDGED INDIVIDUALLY")
    else:
        if col_next.button("Provision →", type="primary", use_container_width=True):
            st.session_state["step"] = 2
            st.rerun()
