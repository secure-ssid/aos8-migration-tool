"""
Step 1: Connect to AOS 8 MC (via API or CLI paste) + New Central destination.
"""
import json
import re
from dataclasses import asdict

import streamlit as st

from lib.aos8_client import AOS8Client, AOS8APIError, is_model_compatible
from lib.aos8_parser import parse_customer_config, parse_instant_config
from lib.translator import translate
from lib.styles import (
    OK, FAIL, WARN, MUTED, TEXT, FAINT,
    page_header, section_label, badge, ssid_tag, esc, mono_row, mono_caption,
    telemetry_chip,
)

PASTE_COMMANDS = [
    ("running_config", "show running-config",
     "Full config — SSIDs, VLANs, ap-group bindings, RADIUS"),
    ("ap_database", "show ap database long",
     "AP inventory with Group, Serial # and Wired MAC columns"),
    ("version", "show version",
     "Exact MC firmware build (needed for the ap convert check)"),
    ("lc_cluster", "show lc-cluster group-membership",
     "Cluster membership — leave empty for a single MC"),
    ("controller_ip", "show controller-ip",
     "Controller IP + VLAN (RADIUS NAD reference)"),
    ("aaa_auth_server", "show aaa authentication-server all",
     "RADIUS server summary (optional if running-config pasted)"),
    ("ap_active", "show ap active",
     "Fallback AP list — only if `show ap database long` unavailable"),
]

PASTE_COMMANDS_INSTANT = [
    ("running_config", "show running-config",
     "From the virtual controller — SSIDs, auth servers, zones"),
    ("show_aps", "show aps",
     "Cluster AP inventory (Serial/Zone columns captured when present)"),
    ("version", "show version",
     "Instant build (8.6+ required for Central-driven conversion)"),
]

_FW_RE = re.compile(r"^\d+\.\d+(\.\d+){1,2}$")


def _store_discovery(cfg) -> None:
    st.session_state["customer_config"] = cfg
    st.session_state["_reset_downstream"]()


def render():
    page_header(1, "Connect & Discover",
                "Pull the AOS 8 configuration, then point at the destination Central tenant")

    # ── Customer ───────────────────────────────────────────────────────────
    section_label("Customer")
    col1, col2 = st.columns(2)
    customer_name = col1.text_input(
        "Customer name",
        value=st.session_state.get("customer_name", ""),
        placeholder="Acme Corp",
    )
    site_name = col2.text_input(
        "Site name",
        value=st.session_state.get("site_name", ""),
        placeholder="auto-generated from customer name",
    )
    with st.expander("Site address (used for the Central site — optional)"):
        a1, a2 = st.columns([2, 1])
        site_address = a1.text_input("Street address", value=st.session_state.get("site_address", ""))
        site_city    = a2.text_input("City",  value=st.session_state.get("site_city", ""))
        a3, a4, a5 = st.columns(3)
        site_state   = a3.text_input("State",    value=st.session_state.get("site_state", ""))
        site_country = a4.text_input("Country",  value=st.session_state.get("site_country", "US"))
        site_zip     = a5.text_input("ZIP code", value=st.session_state.get("site_zipcode", ""))

    st.divider()

    # ── AOS 8 source ───────────────────────────────────────────────────────
    section_label("Source — AOS 8 platform")
    source_type = st.radio(
        "Source platform",
        ["Mobility Controller (MM / MD)", "Instant cluster (IAP virtual controller)"],
        horizontal=True,
        index=0 if st.session_state.get("source_type", "controller") == "controller" else 1,
        label_visibility="collapsed",
    )
    source_type = "instant" if "Instant" in source_type else "controller"
    prev_source = st.session_state.get("source_type")
    st.session_state["source_type"] = source_type
    if prev_source is not None and prev_source != source_type:
        # switching platforms invalidates the previous discovery — never show
        # (or let the user continue on) data from the other source type
        st.session_state.pop("customer_config", None)
        st.session_state["_reset_downstream"]()

    customer_cfg = st.session_state.get("customer_config")
    if customer_cfg and getattr(customer_cfg, "source_type", "controller") != source_type:
        customer_cfg = None  # belt-and-suspenders: never render a mismatched config

    if source_type == "instant":
        vc_ip = st.text_input(
            "Virtual controller IP (RADIUS NAD reference only)",
            value=st.session_state.get("mc_ip", ""),
            placeholder="10.1.1.9",
        )
        st.markdown(
            f'<div style="font-size:11.5px;color:{FAINT};margin:-0.3rem 0 0.7rem;">'
            f'Run each command on the virtual controller CLI and paste the output. '
            f'Conversion is driven from Central — no controller commands needed later.</div>',
            unsafe_allow_html=True,
        )
        for key, cmd, hint in PASTE_COMMANDS_INSTANT:
            st.text_area(f"`{cmd}` — {hint}", height=110, key=f"ipaste_{key}")

        if st.button("Parse Instant Output", type="primary"):
            pasted = {key: st.session_state.get(f"ipaste_{key}", "")
                      for key, _, _ in PASTE_COMMANDS_INSTANT}
            if not pasted.get("running_config"):
                st.error("Nothing to parse — paste at least the VC `show running-config`.")
            else:
                try:
                    customer_cfg = parse_instant_config(pasted, vc_ip=vc_ip)
                    st.session_state.update({"mc_ip": vc_ip, "mc_mode": "paste"})
                    _store_discovery(customer_cfg)
                    st.success(f"Instant cluster parsed — {len(customer_cfg.ssids)} SSIDs, "
                               f"{len(customer_cfg.aps)} APs")
                except Exception as e:
                    st.error(f"Parse error: {e}")
        mode = "paste"
    else:
        mode = st.radio(
            "Connection mode",
            ["API — direct pull (recommended)", "Paste CLI output"],
            horizontal=True,
            index=0 if st.session_state.get("mc_mode", "api") == "api" else 1,
            label_visibility="collapsed",
        )

    if source_type == "instant":
        pass  # handled above
    elif "API" in mode:
        c1, c2, c3 = st.columns(3)
        mc_ip   = c1.text_input("MC IP address", value=st.session_state.get("mc_ip", ""),
                                placeholder="10.1.1.5")
        mc_user = c2.text_input("Username", value=st.session_state.get("mc_user", "admin"))
        mc_pass = c3.text_input("Password", type="password",
                                help="Used for this connection only — never stored")
        with st.expander("Advanced — API options"):
            config_path = st.text_input(
                "config_path",
                value=st.session_state.get("mc_config_path", "/md"),
                help="Mobility Conductor: /md (or a specific node). Standalone controller: /mm/mynode",
            )
        st.markdown(
            f'<div style="font-size:11.5px;color:{FAINT};margin:-0.3rem 0 0.7rem;">'
            f'REST API on port 4343 · self-signed cert accepted · UIDARUBA session token</div>',
            unsafe_allow_html=True,
        )

        if st.button("Connect & Pull Config", type="primary",
                     disabled=not (mc_ip and mc_user and mc_pass)):
            with st.spinner(f"Connecting to {mc_ip} ..."):
                try:
                    client = AOS8Client(mc_ip, mc_user, mc_pass,
                                        config_path=config_path.strip() or "/md")
                    client.connect()
                    customer_cfg = client.pull_config()
                    st.session_state.update({"mc_ip": mc_ip, "mc_user": mc_user,
                                             "mc_config_path": config_path, "mc_mode": "api"})
                    _store_discovery(customer_cfg)
                    st.success(f"Connected to {mc_ip} — configuration pulled via API")
                except AOS8APIError as e:
                    st.error(f"AOS 8 API error: {e}")
                    st.info("If port 4343 is firewalled or the API is disabled, "
                            "switch to **Paste CLI output** mode.")
                except Exception as e:
                    st.error(f"Connection error: {e}")
                    st.info("Verify port 4343 is reachable from this machine, "
                            "then retry — or use paste mode.")
    else:
        mc_ip = st.text_input(
            "MC IP address (RADIUS NAD reference only)",
            value=st.session_state.get("mc_ip", ""),
            placeholder="10.1.1.5",
        )
        st.markdown(
            f'<div style="font-size:11.5px;color:{FAINT};margin:-0.3rem 0 0.7rem;">'
            f'Run each command on the MC CLI and paste its output. '
            f'<b>running-config</b> and <b>ap database long</b> carry most of the data.</div>',
            unsafe_allow_html=True,
        )
        for key, cmd, hint in PASTE_COMMANDS:
            st.text_area(f"`{cmd}` — {hint}", height=110, key=f"paste_{key}")

        if st.button("Parse Pasted Output", type="primary"):
            pasted = {key: st.session_state.get(f"paste_{key}", "") for key, _, _ in PASTE_COMMANDS}
            if not any(pasted.values()):
                st.error("Nothing to parse — paste at least `show running-config`.")
            else:
                try:
                    customer_cfg = parse_customer_config(pasted, mc_ip=mc_ip)
                    st.session_state.update({"mc_ip": mc_ip, "mc_mode": "paste"})
                    _store_discovery(customer_cfg)
                    st.success("CLI output parsed")
                except Exception as e:
                    st.error(f"Parse error: {e}")

    # ── Discovery summary ──────────────────────────────────────────────────
    customer_cfg = st.session_state.get("customer_config")
    if customer_cfg:
        st.divider()
        section_label("Discovery")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("AP Groups",      len(customer_cfg.ap_groups))
        m2.metric("SSIDs",          len(customer_cfg.ssids))
        m3.metric("APs",            len(customer_cfg.aps))
        m4.metric("RADIUS Servers", len(customer_cfg.radius_servers))

        chips = telemetry_chip("firmware", customer_cfg.mc_firmware,
                               FAIL if customer_cfg.mc_firmware == "unknown" else OK)
        chips += telemetry_chip("controller", customer_cfg.mc_ip or "—")
        if customer_cfg.cluster:
            chips += telemetry_chip(
                "cluster",
                f"{customer_cfg.cluster.type} × {len(customer_cfg.cluster.members)}",
                WARN,
            )
        st.markdown(f'<div style="margin:0.6rem 0 0.2rem;">{chips}</div>', unsafe_allow_html=True)

        with st.expander("AP Groups & SSIDs", expanded=True):
            mode_by_name = {s.name: s.forward_mode.value for s in customer_cfg.ssids}
            display_by_name = {s.name: s.display_name for s in customer_cfg.ssids}
            for grp in customer_cfg.ap_groups:
                ssid_html = " ".join(
                    ssid_tag(display_by_name.get(s, s), mode_by_name.get(s, "bridge"))
                    for s in grp.ssids[:10]
                )
                extra = (f' <span style="color:{FAINT};font-size:11px;">+{len(grp.ssids)-10} more</span>'
                         if len(grp.ssids) > 10 else "")
                st.markdown(
                    f'<div style="margin-bottom:0.7rem;">'
                    f'<span style="font-weight:600;font-size:13.5px;color:{TEXT};">{esc(grp.name)}</span> '
                    f'<span style="color:{FAINT};font-size:11.5px;font-family:\'IBM Plex Mono\',monospace;'
                    f'margin-left:6px;">{len(grp.ap_serials)} APs</span>'
                    f'<div style="margin-top:5px;">{ssid_html}{extra}</div></div>',
                    unsafe_allow_html=True,
                )

        with st.expander(f"APs ({len(customer_cfg.aps)})", expanded=False):
            rows = []
            for ap in customer_cfg.aps[:80]:
                compat = is_model_compatible(ap.model)
                b = badge("AOS10 OK", "green") if compat else badge("UNSUPPORTED", "red")
                rows.append(mono_row([
                    (ap.serial or "(no serial)", MUTED if ap.serial else FAIL),
                    (ap.model or "?", TEXT),
                    (ap.name, MUTED),
                    (ap.ap_group, FAINT),
                    (ap.ip, FAINT),
                ], trailing_html=b))
            if len(customer_cfg.aps) > 80:
                rows.append(f'<div style="color:{FAINT};font-size:11.5px;padding-top:6px;">'
                            f'… and {len(customer_cfg.aps)-80} more</div>')
            st.markdown("".join(rows), unsafe_allow_html=True)

        if customer_cfg.radius_servers:
            with st.expander("RADIUS Servers", expanded=False):
                st.markdown("".join(
                    mono_row([(s.name, TEXT), (f"{s.address}:{s.auth_port}", MUTED)])
                    for s in customer_cfg.radius_servers
                ), unsafe_allow_html=True)

        export = asdict(customer_cfg)
        for s in export.get("ssids", []):           # never write secrets to disk
            if s.get("psk"):
                s["psk"] = "***REDACTED***"
        for r in export.get("radius_servers", []):
            if r.get("secret"):
                r["secret"] = "***REDACTED***"
        st.download_button(
            "Export discovered config (JSON)",
            data=json.dumps(export, indent=2, default=str),
            file_name=f"{(customer_name or 'customer').lower().replace(' ', '_')}_aos8_discovery.json",
            mime="application/json",
            help="PSKs and RADIUS secrets are redacted in the export",
        )

    # ── Destination ────────────────────────────────────────────────────────
    st.divider()
    section_label("Destination — Aruba Central")

    dest_choice = st.radio(
        "Destination platform",
        ["New Central (HPE GreenLake)", "Classic Central"],
        horizontal=True,
        index=0 if st.session_state.get("dest_type", "new") == "new" else 1,
        label_visibility="collapsed",
    )
    dest_type = "classic" if "Classic" in dest_choice else "new"
    st.session_state["dest_type"] = dest_type

    have_secret = bool(st.session_state.get("central_secret"))
    have_token = bool(st.session_state.get("classic_access_token"))

    if dest_type == "new":
        c1, c2 = st.columns([3, 1])
        central_base = c1.text_input(
            "Central API base URL (regional)",
            value=st.session_state.get("central_base", "https://us4.api.central.arubanetworks.com"),
            help="New Central regional base, e.g. https://us4.api.central.arubanetworks.com — "
                 "find your region in GreenLake → API client details",
        )
        aos10_fw = c2.text_input("Target AOS 10", value=st.session_state.get("aos10_fw", "10.7.0.0"))

        c1b, c2b = st.columns(2)
        central_client_id = c1b.text_input(
            "API client ID",
            value=st.session_state.get("central_client_id", ""),
            help="GreenLake → Manage → API → Create client credentials (Aruba Central service)",
        )
        secret_input = c2b.text_input(
            "API client secret", type="password",
            placeholder="•••••••• (saved this session)" if have_secret else "",
            help="Kept in this browser session only — re-enter after a restart",
        )
        if secret_input:
            st.session_state["central_secret"] = secret_input
            have_secret = True
    else:
        c1, c2 = st.columns([3, 1])
        central_base = c1.text_input(
            "Classic API gateway base URL",
            value=st.session_state.get("central_base_classic",
                                       "https://apigw-uswest4.central.arubanetworks.com"),
            help="Classic Central → API Gateway → the cluster base URL "
                 "(e.g. apigw-uswest4 / apigw-eucentral3)",
        )
        aos10_fw = c2.text_input("Target AOS 10", value=st.session_state.get("aos10_fw", "10.7.0.0"))

        t1, t2 = st.columns(2)
        token_input = t1.text_input(
            "Access token", type="password",
            placeholder="•••••••• (saved this session)" if have_token else "",
            help="API Gateway → System Apps & Tokens → Generate Token (valid ~2h)",
        )
        if token_input:
            st.session_state["classic_access_token"] = token_input.strip()
            have_token = True
        refresh_input = t2.text_input(
            "Refresh token (optional)", type="password",
            placeholder="enables auto-refresh past the 2h token lifetime",
        )
        if refresh_input:
            st.session_state["classic_refresh_token"] = refresh_input.strip()

        c1b, c2b = st.columns(2)
        central_client_id = c1b.text_input(
            "API client ID (needed for refresh)",
            value=st.session_state.get("central_client_id", ""),
        )
        secret_input = c2b.text_input(
            "API client secret (needed for refresh)", type="password",
            placeholder="•••••••• (saved this session)" if have_secret else "",
        )
        if secret_input:
            st.session_state["central_secret"] = secret_input
            have_secret = True

    fw_valid = bool(_FW_RE.match(aos10_fw.strip()))
    if aos10_fw and not fw_valid:
        st.warning(f"'{aos10_fw}' doesn't look like an AOS 10 version (expected e.g. 10.7.0.0)")

    # ── Gateway strategy (only relevant when tunnel SSIDs exist) ───────────
    has_tunnel = bool(customer_cfg) and any(
        s.forward_mode.value in ("tunnel", "split") for s in customer_cfg.ssids)
    gw_strategy = st.session_state.get("gw_strategy", "keep")
    if has_tunnel:
        st.markdown('<div style="height:0.4rem;"></div>', unsafe_allow_html=True)
        section_label("Gateway strategy")
        choice = st.radio(
            "Gateway strategy",
            ["Keep gateways — tunnel SSIDs stay overlay (MCs become AOS 10 gateways)",
             "Retire gateways — convert ALL SSIDs to bridge mode (decommission MCs)"],
            horizontal=False,
            index=0 if gw_strategy == "keep" else 1,
            label_visibility="collapsed",
        )
        gw_strategy = "retire" if "Retire" in choice else "keep"
        if gw_strategy == "retire":
            st.markdown(
                f'<div style="font-size:12px;color:{FAINT};margin:-0.2rem 0 0.6rem;">'
                f'Former tunnel client VLANs must be trunked to AP switchports and APs '
                f'become the RADIUS NAD clients — preflight will detail the changes.</div>',
                unsafe_allow_html=True,
            )

    # ── Continue ───────────────────────────────────────────────────────────
    st.markdown('<div style="height:0.4rem;"></div>', unsafe_allow_html=True)
    missing = []
    if not customer_cfg:              missing.append("AOS 8 config")
    if not customer_name.strip():     missing.append("customer name")
    if not central_base.strip():      missing.append("Central base URL")
    if dest_type == "new":
        if not central_client_id.strip(): missing.append("client ID")
        if not have_secret:               missing.append("client secret")
    else:
        if not have_token:                missing.append("classic access token")
    if not fw_valid:                  missing.append("valid target firmware")

    col_l, col_r = st.columns([4, 1])
    if missing:
        with col_l:
            mono_caption(f"WAITING FOR: {', '.join(missing)}")

    with col_r:
        if st.button("Continue →", type="primary", disabled=bool(missing),
                     use_container_width=True):
            site = site_name.strip() or customer_name.lower().replace(" ", "-") + "-site"
            central_cfg = translate(
                customer_cfg,
                customer_name=customer_name.strip(),
                central_base_url=central_base,
                aos10_firmware=aos10_fw.strip(),
                site_name=site,
                gateway_mode=gw_strategy,
            )
            central_cfg.site_address = site_address.strip()
            central_cfg.site_city    = site_city.strip()
            central_cfg.site_state   = site_state.strip()
            central_cfg.site_country = site_country.strip()
            central_cfg.site_zipcode = site_zip.strip()
            central_cfg.destination  = dest_type

            # Re-Continuing with a CHANGED target invalidates everything the
            # previous target produced (preflight, provision_done, validation,
            # GLP results) — otherwise the wizard reports the old destination
            # as provisioned and never provisions the new one.
            prev = st.session_state.get("central_config")
            if prev is not None and (
                getattr(prev, "destination", None) != central_cfg.destination
                or getattr(prev, "gateways_retired", None) != central_cfg.gateways_retired
                or getattr(prev, "gw_cluster_name", None) != central_cfg.gw_cluster_name
                or getattr(prev, "sites", None) != central_cfg.sites
            ):
                st.session_state["_reset_downstream"]()
                st.session_state.pop("glp_use_central_creds", None)

            updates = {
                "customer_name":     customer_name.strip(),
                "gw_strategy":       gw_strategy,
                # persist only what the USER typed — an auto-generated site
                # name must re-derive when the customer name changes
                "site_name":         site_name.strip(),
                "site_address":      site_address,
                "site_city":         site_city,
                "site_state":        site_state,
                "site_country":      site_country,
                "site_zipcode":      site_zip,
                "central_client_id": central_client_id.strip(),
                "aos10_fw":          aos10_fw.strip(),
                "central_config":    central_cfg,
                "step":              1,
            }
            if dest_type == "classic":
                updates["central_base_classic"] = central_base.strip()
            else:
                updates["central_base"] = central_base.strip()
            st.session_state.update(updates)
            st.rerun()
