"""
Design system + HTML component helpers for the migration tool UI.

Aesthetic: dark mission-control console. Deep navy surfaces, Aruba orange
accent, IBM Plex Sans/Mono with Barlow Condensed display headers, telemetry
chips for live values. All dynamic values must pass through esc().
"""
import html

import streamlit as st

# ── Palette ──────────────────────────────────────────────────────────────────
BG        = "#0B1220"   # page base
SURFACE   = "#121C30"   # cards
SURFACE_2 = "#0E1626"   # inset panels
BORDER    = "#1F2D4A"
BORDER_HI = "#2C3F66"
TEXT      = "#E6EDF7"
MUTED     = "#8FA3C0"
FAINT     = "#7A8FB0"   # lightened from #5A6E8F for WCAG AA at small sizes
ORANGE    = "#FF8300"   # Aruba accent
ORANGE_DK = "#D96E00"
HPE_GREEN    = "#01A982"   # HPE GreenLake accent (step 4)
HPE_GREEN_DK = "#017A5F"
OK        = "#2DD4A7"
WARN      = "#FFB224"
FAIL      = "#FF5C5C"
INFO      = "#4DA3FF"

def _build_css(accent: str, accent_dk: str, accent_fg: str, accent_rgb: str) -> str:
    """Design-system CSS with a swappable accent (Aruba orange / HPE green)."""
    return f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');

html, body, [class*="css"], .stApp {{
    font-family: 'IBM Plex Sans', -apple-system, sans-serif !important;
}}

/* ── Atmosphere: layered grid + radial glow ── */
.stApp {{
    background:
        radial-gradient(1100px 500px at 75% -10%, rgba({accent_rgb},0.07), transparent 60%),
        radial-gradient(900px 450px at -10% 0%, rgba(77,163,255,0.06), transparent 55%),
        repeating-linear-gradient(0deg, rgba(143,163,192,0.025) 0 1px, transparent 1px 36px),
        repeating-linear-gradient(90deg, rgba(143,163,192,0.025) 0 1px, transparent 1px 36px),
        {BG} !important;
}}

.block-container {{
    padding-top: 1.1rem !important;
    padding-bottom: 4rem !important;
    max-width: 1080px !important;
}}

h1, h2, h3 {{ color: {TEXT} !important; }}

/* ── Sidebar ── */
[data-testid="stSidebar"] {{
    background: linear-gradient(180deg, #0D1626 0%, #0A1120 100%) !important;
    border-right: 1px solid {BORDER} !important;
}}
[data-testid="stSidebar"] * {{ color: {MUTED}; }}

/* ── Buttons ── */
.stButton > button {{
    border-radius: 6px !important;
    font-weight: 600 !important;
    font-size: 13.5px !important;
    font-family: 'IBM Plex Sans', sans-serif !important;
    padding: 0.5rem 1.25rem !important;
    transition: all 0.15s ease !important;
    letter-spacing: 0.02em !important;
}}
.stButton > button[kind="primary"] {{
    background: linear-gradient(135deg, {accent} 0%, {accent_dk} 100%) !important;
    color: {accent_fg} !important;
    border: none !important;
    box-shadow: 0 0 0 1px rgba({accent_rgb},0.45), 0 4px 18px rgba({accent_rgb},0.22) !important;
}}
.stButton > button[kind="primary"]:hover {{
    box-shadow: 0 0 0 1px rgba({accent_rgb},0.8), 0 6px 26px rgba({accent_rgb},0.38) !important;
    transform: translateY(-1px) !important;
}}
.stButton > button[kind="primary"]:disabled {{
    background: {SURFACE} !important;
    color: {FAINT} !important;
    box-shadow: none !important;
    border: 1px solid {BORDER} !important;
}}
.stButton > button:not([kind="primary"]) {{
    background: {SURFACE} !important;
    color: {MUTED} !important;
    border: 1px solid {BORDER_HI} !important;
}}
.stButton > button:not([kind="primary"]):hover {{
    border-color: {accent} !important;
    color: {accent} !important;
}}

.stButton > button {{ white-space: nowrap !important; }}

/* keyboard focus ring (custom button CSS suppresses the default) */
.stButton > button:focus-visible,
.stDownloadButton > button:focus-visible,
.stTextInput input:focus-visible,
.stTextArea textarea:focus-visible {{
    outline: 2px solid {accent} !important;
    outline-offset: 2px !important;
}}

/* ── Download button ── */
.stDownloadButton > button {{
    border-radius: 6px !important;
    font-weight: 600 !important;
    background: {SURFACE} !important;
    color: {OK} !important;
    border: 1px solid rgba(45,212,167,0.4) !important;
}}
.stDownloadButton > button:hover {{
    border-color: {OK} !important;
    box-shadow: 0 0 14px rgba(45,212,167,0.25) !important;
}}

/* ── Inputs ── */
.stTextInput > div > div > input,
.stTextArea > div > div > textarea,
.stNumberInput input {{
    border-radius: 6px !important;
    border: 1px solid {BORDER_HI} !important;
    font-size: 13.5px !important;
    background: {SURFACE_2} !important;
    color: {TEXT} !important;
    transition: border-color 0.15s, box-shadow 0.15s !important;
    padding: 0.5rem 0.75rem !important;
}}
.stTextArea > div > div > textarea {{ font-family: 'IBM Plex Mono', monospace !important; font-size: 12px !important; }}
.stTextInput > div > div > input:focus,
.stTextArea > div > div > textarea:focus {{
    border-color: {accent} !important;
    box-shadow: 0 0 0 3px rgba({accent_rgb},0.18) !important;
}}
.stTextInput label, .stTextArea label, .stSelectbox label, .stRadio > label {{
    color: {MUTED} !important;
    font-size: 12px !important;
    font-weight: 600 !important;
    letter-spacing: 0.04em !important;
}}

/* ── Metrics ── */
[data-testid="stMetric"] {{
    background: linear-gradient(180deg, {SURFACE} 0%, {SURFACE_2} 100%) !important;
    border-radius: 10px !important;
    padding: 1rem 1.1rem !important;
    border: 1px solid {BORDER} !important;
    box-shadow: inset 0 1px 0 rgba(230,237,247,0.04) !important;
}}
[data-testid="stMetricValue"] {{
    font-size: 1.9rem !important;
    font-weight: 600 !important;
    font-family: 'IBM Plex Mono', monospace !important;
    color: {TEXT} !important;
}}
[data-testid="stMetricLabel"] {{
    font-size: 0.72rem !important;
    font-weight: 600 !important;
    color: {FAINT} !important;
    text-transform: uppercase !important;
    letter-spacing: 0.12em !important;
}}

/* ── Expanders ── */
[data-testid="stExpander"] {{
    border-radius: 10px !important;
    border: 1px solid {BORDER} !important;
    background: {SURFACE} !important;
    overflow: hidden !important;
    margin-bottom: 0.6rem !important;
}}
[data-testid="stExpander"] summary {{
    font-weight: 600 !important;
    font-size: 13.5px !important;
    color: {TEXT} !important;
    padding: 0.8rem 1rem !important;
}}
[data-testid="stExpander"] summary:hover {{ color: {accent} !important; }}

/* ── Alerts ── */
[data-testid="stAlert"] {{
    border-radius: 8px !important;
    font-size: 13.5px !important;
    border: 1px solid {BORDER_HI} !important;
    background: {SURFACE} !important;
    color: {TEXT} !important;
}}

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {{
    gap: 2px;
    background: {SURFACE_2};
    border-radius: 8px;
    padding: 3px;
    border: 1px solid {BORDER};
}}
.stTabs [data-baseweb="tab"] {{
    border-radius: 6px;
    color: {MUTED};
    font-weight: 600;
    font-size: 13px;
}}
.stTabs [aria-selected="true"] {{
    background: {SURFACE} !important;
    color: {accent} !important;
}}

/* ── Radio / checkbox ── */
.stRadio [role="radiogroup"] label {{ color: {TEXT} !important; }}
.stCheckbox label, .stCheckbox p {{ font-size: 13.5px !important; color: {TEXT} !important; }}
/* checked boxes/radios follow the page accent (theme primaryColor is static).
   baseweb markup: <label><input/><span box/>…</label> — input is a SIBLING. */
[data-testid="stCheckbox"] label:has(input:checked) > span:first-of-type,
[data-testid="stCheckbox"] input:checked + span {{
    background-color: {accent} !important;
    border-color: {accent} !important;
}}
.stRadio label:has(input:checked) > div:first-of-type {{
    background-color: {accent} !important;
    border-color: {accent} !important;
}}

/* ── Code ── */
[data-testid="stCode"] {{
    border-radius: 10px !important;
    border: 1px solid {BORDER} !important;
}}
pre, code {{ font-family: 'IBM Plex Mono', monospace !important; font-size: 12.3px !important; }}

/* ── Divider ── */
hr {{ border-color: {BORDER} !important; margin: 1.2rem 0 !important; }}

/* ── Spinner ── */
[data-testid="stSpinner"] > div {{ border-top-color: {accent} !important; }}

/* ── Pulse animation for the active pipeline node ── */
@keyframes amt-pulse {{
    0%   {{ box-shadow: 0 0 0 0 rgba({accent_rgb},0.45); }}
    70%  {{ box-shadow: 0 0 0 9px rgba({accent_rgb},0); }}
    100% {{ box-shadow: 0 0 0 0 rgba({accent_rgb},0); }}
}}
@keyframes amt-fade-up {{
    from {{ opacity: 0; transform: translateY(6px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
}}
.amt-reveal {{ animation: amt-fade-up 0.35s ease both; }}
</style>
"""


def inject(accent: str = "aruba"):
    """accent="green" switches the whole accent system to HPE GreenLake green
    (used while the wizard is on the GreenLake step)."""
    if accent == "green":
        css = _build_css(HPE_GREEN, HPE_GREEN_DK, "#FFFFFF", "1,169,130")
    else:
        css = _build_css(ORANGE, ORANGE_DK, "#14100A", "255,131,0")
    st.markdown(css, unsafe_allow_html=True)


def esc(value) -> str:
    """HTML-escape any value rendered inside unsafe_allow_html markup."""
    return html.escape(str(value), quote=True)


# ── Components ───────────────────────────────────────────────────────────────

def brand_header(accent: str = ORANGE) -> None:
    green = accent == HPE_GREEN
    accent_dk = HPE_GREEN_DK if green else ORANGE_DK
    chip_fg = "#FFFFFF" if green else "#14100A"
    glow = "rgba(1,169,130,0.35)" if green else "rgba(255,131,0,0.35)"
    title_hl = "GreenLake Onboarding" if green else "Migration Console"
    subtitle = ("HPE GREENLAKE · DEVICE ONBOARDING" if green
                else "AOS 8 → AOS 10 · CENTRAL MIGRATION")
    st.markdown(f"""
    <div style="display:flex;align-items:center;justify-content:space-between;
                padding:0.15rem 0 0.7rem;border-bottom:1px solid {BORDER};margin-bottom:0.9rem;">
        <div style="display:flex;align-items:center;gap:13px;">
            <div style="width:40px;height:40px;border-radius:9px;position:relative;
                        background:linear-gradient(135deg,{accent} 0%,{accent_dk} 100%);
                        display:flex;align-items:center;justify-content:center;
                        box-shadow:0 0 22px {glow};transition:background 0.3s;">
                <span style="font-family:'IBM Plex Mono',monospace;font-weight:600;
                             font-size:15px;color:{chip_fg};">▲▼</span>
            </div>
            <div>
                <div style="font-family:'Barlow Condensed',sans-serif;font-size:1.5rem;
                            font-weight:700;color:{TEXT};letter-spacing:0.04em;
                            text-transform:uppercase;line-height:1.05;">
                    AOS 8 → Central <span style="color:{accent};">{title_hl}</span>
                </div>
                <div style="font-family:'IBM Plex Mono',monospace;font-size:10.5px;
                            color:{FAINT};letter-spacing:0.14em;margin-top:2px;">
                    {subtitle}
                </div>
            </div>
        </div>
        <div style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:{FAINT};
                    text-align:right;letter-spacing:0.1em;line-height:1.7;">
            FIELD OPS TOOL<br>
            <span style="color:{OK};">●</span> LOCAL SESSION
        </div>
    </div>
    """, unsafe_allow_html=True)


def telemetry_chip(label: str, value: str, color: str = MUTED) -> str:
    return (
        f'<span style="display:inline-flex;align-items:center;gap:7px;background:{SURFACE_2};'
        f'border:1px solid {BORDER};border-radius:5px;padding:3px 10px;margin:0 6px 6px 0;">'
        f'<span style="font-family:\'IBM Plex Mono\',monospace;font-size:9.5px;color:{FAINT};'
        f'letter-spacing:0.12em;text-transform:uppercase;">{esc(label)}</span>'
        f'<span style="font-family:\'IBM Plex Mono\',monospace;font-size:12px;color:{color};'
        f'font-weight:600;">{esc(value)}</span></span>'
    )


def page_header(step_no, title: str, subtitle: str = "",
                accent: str = ORANGE) -> None:
    # step_no=None → the standalone (non-wizard) pages, which aren't part of
    # the 01-06 sequence: show a "+" glyph instead of a bogus "00"
    num = "+" if step_no is None else f"{step_no:02d}"
    sub = (f'<div style="color:{MUTED};font-size:0.86rem;margin-top:3px;">{esc(subtitle)}</div>'
           if subtitle else "")
    st.markdown(
        f'<div class="amt-reveal" style="display:flex;align-items:baseline;gap:14px;'
        f'margin:0.4rem 0 1.1rem;">'
        f'<div aria-hidden="true" style="font-family:\'Barlow Condensed\',sans-serif;'
        f'font-size:2.6rem;font-weight:700;color:transparent;'
        f'-webkit-text-stroke:1.5px {accent};line-height:0.9;">{num}</div>'
        f'<div>'
        # real <h2> so screen readers get heading navigation
        f'<h2 style="font-family:\'Barlow Condensed\',sans-serif;font-size:1.55rem;'
        f'font-weight:600;color:{TEXT};text-transform:uppercase;letter-spacing:0.05em;'
        f'line-height:1.1;margin:0;padding:0;">{esc(title)}</h2>'
        f'{sub}</div></div>',
        unsafe_allow_html=True,
    )


def mono_caption(text: str, color: str = "") -> None:
    """Small uppercase monospace caption (the 'WAITING FOR: ...' style)."""
    st.markdown(
        f'<div style="font-size:11.5px;color:{color or FAINT};padding-top:0.4rem;'
        f'font-family:\'IBM Plex Mono\',monospace;letter-spacing:0.06em;'
        f'text-transform:uppercase;">{esc(text)}</div>',
        unsafe_allow_html=True,
    )


def section_label(text: str, color: str = ORANGE) -> None:
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:10px;margin:0.4rem 0 0.6rem;">'
        f'<span style="font-family:\'IBM Plex Mono\',monospace;font-size:10.5px;font-weight:600;'
        f'color:{color};letter-spacing:0.18em;text-transform:uppercase;">{esc(text)}</span>'
        f'<span style="flex:1;height:1px;background:linear-gradient(90deg,{BORDER_HI},transparent);"></span>'
        f'</div>',
        unsafe_allow_html=True,
    )


def badge(text: str, variant: str = "gray") -> str:
    palettes = {
        "green":  ("rgba(45,212,167,0.12)",  OK),
        "yellow": ("rgba(255,178,36,0.12)",  WARN),
        "red":    ("rgba(255,92,92,0.12)",   FAIL),
        "blue":   ("rgba(77,163,255,0.12)",  INFO),
        "orange": ("rgba(255,131,0,0.12)",   ORANGE),
        "gray":   (SURFACE_2, MUTED),
    }
    bg, fg = palettes.get(variant, palettes["gray"])
    return (f'<span style="background:{bg};color:{fg};padding:2px 9px;border-radius:4px;'
            f'border:1px solid {fg}33;font-size:11px;font-weight:600;'
            f'font-family:\'IBM Plex Mono\',monospace;letter-spacing:0.04em;'
            f'display:inline-block;">{esc(text)}</span>')


def check_card(icon: str, name: str, message: str, detail: str = "",
               variant: str = "gray") -> None:
    # Emitted as ONE line with no leading whitespace/newlines: indented or
    # blank lines make Streamlit's markdown parser treat the closing tags as
    # an indented code block (renders literal "</div>"). Newlines inside the
    # detail <pre> are encoded as &#10; for the same reason.
    accents = {"green": OK, "yellow": WARN, "red": FAIL, "gray": FAINT}
    accent = accents.get(variant, FAINT)
    # NOTE: a styled <div>, not <pre> — Streamlit's markdown pipeline rewrites
    # raw <pre> into its own stMarkdownPre div and DROPS the inline style.
    detail_html = (
        f'<div style="background:{SURFACE_2};border-radius:6px;padding:0.6rem 0.8rem;'
        f'font-size:11.5px;margin:0.6rem 0 0;white-space:pre-wrap;color:{MUTED};'
        f'font-family:\'IBM Plex Mono\',monospace;'
        f'border:1px solid {BORDER};overflow:auto;">'
        f'{esc(detail).replace(chr(10), "&#10;")}</div>'
    ) if detail else ""
    st.markdown(
        f'<div class="amt-reveal" style="background:{SURFACE};border:1px solid {BORDER};'
        f'border-left:3px solid {accent};border-radius:8px;padding:0.8rem 1rem;'
        f'margin-bottom:0.5rem;">'
        f'<div style="display:flex;align-items:flex-start;gap:10px;">'
        f'<span style="font-size:15px;line-height:1.5;">{esc(icon)}</span>'
        f'<div style="flex:1;">'
        f'<div style="font-weight:600;font-size:13.5px;color:{TEXT};">{esc(name)}</div>'
        f'<div style="font-size:13px;color:{MUTED};margin-top:2px;">{esc(message)}</div>'
        f'{detail_html}</div></div></div>',
        unsafe_allow_html=True,
    )


def provision_step_line(label: str, ok: bool) -> None:
    color = OK if ok else FAIL
    icon = "&#9670;" if ok else "&#10005;"  # ◆ / ✕ as entities — never raw markup
    st.markdown(
        f'<div style="background:{SURFACE};border:1px solid {BORDER};border-left:3px solid {color};'
        f'border-radius:6px;padding:0.4rem 0.8rem;margin-bottom:3px;display:flex;'
        f'align-items:center;gap:9px;">'
        f'<span style="color:{color};font-size:11px;">{icon}</span>'
        f'<span style="font-size:12.5px;color:{TEXT};font-family:\'IBM Plex Mono\',monospace;">'
        f'{esc(label)}</span></div>',
        unsafe_allow_html=True,
    )


def step_progress(current_step: int, steps: list) -> None:
    # the GreenLake node lives in HPE territory — its accent (and the
    # connectors into/out of it) migrate from Aruba orange to HPE green
    glake = next((i for i, (key, _) in enumerate(steps) if "greenlake" in key), -1)
    parts = []
    n = len(steps)
    for i, (_, label) in enumerate(steps):
        done   = i < current_step
        active = i == current_step
        node_accent = HPE_GREEN if i == glake else ORANGE
        node_fg = "#FFFFFF" if i == glake else "#14100A"
        if done:
            circle_bg, circle_fg, ring = "rgba(45,212,167,0.15)", OK, f"1px solid {OK}"
            anim = ""
        elif active:
            circle_bg, circle_fg, ring = node_accent, node_fg, f"1px solid {node_accent}"
            anim = "animation:amt-pulse 2s infinite;"
        else:
            ring_col = f"{HPE_GREEN}55" if i == glake else BORDER_HI
            circle_bg, circle_fg, ring = SURFACE_2, FAINT, f"1px solid {ring_col}"
            anim = ""
        text_col = OK if done else (node_accent if active else FAINT)
        num = "✓" if done else str(i + 1)

        parts.append(f"""
        <div style="display:flex;flex-direction:column;align-items:center;flex:0 0 auto;">
            <div style="width:30px;height:30px;border-radius:50%;background:{circle_bg};
                        border:{ring};{anim}display:flex;align-items:center;justify-content:center;
                        color:{circle_fg};font-weight:600;font-size:12.5px;
                        font-family:'IBM Plex Mono',monospace;">{num}</div>
            <div style="margin-top:6px;font-size:10px;color:{text_col};font-weight:600;
                        font-family:'IBM Plex Mono',monospace;text-transform:uppercase;
                        letter-spacing:0.1em;white-space:nowrap;">{esc(label)}</div>
        </div>""")

        if i < n - 1:
            if i == glake - 1:
                # orange → green: the literal migration into GreenLake
                a = OK if done else f"rgba(255,131,0,{1 if done else 0.45})"
                line_bg = f"linear-gradient(90deg,{OK if done else 'rgba(255,131,0,0.45)'},{HPE_GREEN})"
                height = "2px"
            elif i == glake:
                # green → back to the Aruba/controller world
                line_bg = f"linear-gradient(90deg,{HPE_GREEN},{OK if done else 'rgba(255,131,0,0.45)'})"
                height = "2px"
            else:
                line_bg = OK if done else BORDER_HI
                height = "1px"
            parts.append(
                f'<div style="flex:1;height:{height};background:{line_bg};'
                f'margin-top:15px;border-radius:2px;"></div>'
            )

    st.markdown(
        '<div style="display:flex;align-items:flex-start;gap:10px;padding:0.4rem 0 0.9rem;">'
        + "".join(parts) + "</div>",
        unsafe_allow_html=True,
    )


def ssid_tag(name: str, mode: str) -> str:
    colors = {"tunnel": INFO, "split": WARN, "bridge": OK}
    color = colors.get(mode, MUTED)
    return (f'<span style="background:{SURFACE_2};color:{color};padding:3px 9px;'
            f'border-radius:4px;border:1px solid {color}44;font-size:11.5px;font-weight:600;'
            f'font-family:\'IBM Plex Mono\',monospace;margin:2px 3px;display:inline-block;">'
            f'{esc(name)} <span style="opacity:0.65;font-size:9.5px;">{esc(mode.upper())}</span></span>')


def mono_row(cells: list[tuple[str, str]], border: bool = True,
             trailing_html: str = "") -> str:
    """One row of mono-spaced data cells: [(text, color), ...].
    trailing_html: pre-built safe HTML (e.g. badge()) appended inside the row."""
    inner = "".join(
        f'<span style="color:{color};font-family:\'IBM Plex Mono\',monospace;'
        f'font-size:12px;">{esc(text)}</span>'
        for text, color in cells
    )
    b = f"border-bottom:1px solid {BORDER};" if border else ""
    return (f'<div style="display:flex;align-items:center;gap:14px;padding:5px 2px;{b}">'
            f'{inner}{trailing_html}</div>')


def info_banner(text_html: str, color: str = INFO) -> None:
    """Banner with PRE-ESCAPED html content (caller escapes dynamic parts)."""
    st.markdown(
        f'<div style="background:{SURFACE};border:1px solid {color}55;border-left:3px solid {color};'
        f'border-radius:8px;padding:0.8rem 1rem;margin-bottom:1rem;font-size:13.5px;'
        f'color:{TEXT};">{text_html}</div>',
        unsafe_allow_html=True,
    )


def sidebar_summary() -> None:
    """Engagement context panel — always visible so no step feels 'blank'."""
    cfg = st.session_state.get("customer_config")
    with st.sidebar:
        st.markdown(
            f'<div style="font-family:\'Barlow Condensed\',sans-serif;font-size:1.05rem;'
            f'font-weight:700;color:{TEXT};text-transform:uppercase;letter-spacing:0.08em;'
            f'margin-bottom:0.6rem;">Engagement</div>',
            unsafe_allow_html=True,
        )
        customer = st.session_state.get("customer_name", "")
        rows = [
            ("CUSTOMER", customer or "—", TEXT if customer else FAINT),
            ("MC", st.session_state.get("mc_ip") or "—",
             TEXT if st.session_state.get("mc_ip") else FAINT),
        ]
        if cfg:
            if getattr(cfg, "source_type", "controller") == "instant":
                topo = ("SOURCE", "Instant VC", OK)
            else:
                topo = ("CLUSTER", cfg.cluster.type if cfg.cluster else "single MC",
                        WARN if cfg.cluster else OK)
            rows += [
                ("FIRMWARE", cfg.mc_firmware, WARN if cfg.mc_firmware == "unknown" else OK),
                ("APS", str(len(cfg.aps)), TEXT),
                ("SSIDS", str(len(cfg.ssids)), TEXT),
                ("GROUPS", str(len(cfg.ap_groups)), TEXT),
                topo,
            ]
        body = "".join(
            f'<div style="display:flex;justify-content:space-between;gap:10px;'
            f'padding:4px 0;border-bottom:1px solid {BORDER};">'
            f'<span style="font-family:\'IBM Plex Mono\',monospace;font-size:9.5px;'
            f'color:{FAINT};letter-spacing:0.12em;flex-shrink:0;">{esc(k)}</span>'
            f'<span style="font-family:\'IBM Plex Mono\',monospace;font-size:11.5px;'
            f'color:{c};font-weight:600;min-width:0;text-align:right;'
            f'word-break:break-word;">{esc(v)}</span></div>'
            for k, v, c in rows
        )
        st.markdown(body, unsafe_allow_html=True)

        badges = []
        if st.session_state.get("provision_done"):
            badges.append(badge("CENTRAL PROVISIONED", "green"))
        if st.session_state.get("glp_claim_result"):
            badges.append(badge("GLP CLAIMED", "green"))
        if badges:
            st.markdown(
                f'<div style="margin-top:0.8rem;display:flex;flex-direction:column;'
                f'gap:4px;align-items:flex-start;">{"".join(badges)}</div>',
                unsafe_allow_html=True,
            )
        if st.session_state.get("remember_creds"):
            note = ('DEST API CREDS SAVED ON<br>THIS MACHINE (0600)<br>'
                    '~/.aos8-migration · SOURCE<br>SECRETS NEVER WRITTEN')
        else:
            note = 'CREDENTIALS STAY IN THIS<br>SESSION — NOTHING IS<br>WRITTEN TO DISK'
        st.markdown(
            f'<div style="margin-top:1.2rem;font-family:\'IBM Plex Mono\',monospace;'
            f'font-size:9px;color:{FAINT};letter-spacing:0.1em;line-height:1.8;">'
            f'{note}</div>',
            unsafe_allow_html=True,
        )
