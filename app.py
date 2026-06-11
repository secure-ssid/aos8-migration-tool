import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(__file__))

STEPS = [
    ("1_connect",   "Connect"),
    ("2_preflight", "Preflight"),
    ("3_provision", "Build Config"),
    ("4_greenlake", "Onboard APs"),
    ("5_runbook",   "Runbook"),
    ("6_validate",  "Validate"),
]

if "step" not in st.session_state:
    st.session_state.step = 0
st.session_state.step = max(0, min(st.session_state.step, len(STEPS) - 1))

# Mode is read from the prior render (the sidebar toggle below sets it);
# set_page_config must be the first st command, so the radio can't precede it.
app_mode = st.session_state.get("app_mode", "wizard")

# The GreenLake step lives in HPE territory — the whole accent system
# (title bar, buttons, labels, stepper) migrates from Aruba orange to
# HPE green while the wizard is there (and for the Add-devices mode).
on_greenlake = app_mode == "wizard" and STEPS[st.session_state.step][0] == "4_greenlake"

st.set_page_config(
    page_title=("Add Devices · Migration Console" if app_mode == "add_devices"
                else "HPE GreenLake Onboarding · Migration Console" if on_greenlake
                else "AOS 8 → Central Migration Console"),
    page_icon=("➕" if app_mode == "add_devices" else "🌿" if on_greenlake else "📡"),
    layout="wide",
    initial_sidebar_state="expanded",
)

from lib.styles import inject, brand_header, step_progress, sidebar_summary, \
    ORANGE, HPE_GREEN
from lib.help_content import render_help
from lib import identity, auth_ui

inject(accent="green" if (on_greenlake or app_mode == "add_devices") else "aruba")

# ── Operator identity & auth gate ────────────────────────────────────────────
# The identity scopes the per-user encrypted credential store and the audit
# log, so it must be resolved before any page renders. accounts mode draws the
# built-in login/registration gate; proxy mode requires a trusted header; local
# mode is a fixed principal.
_user = identity.current_user()
st.session_state["_user"] = _user
_mode = identity.auth_mode()
if identity.requires_login():        # 'password' or 'accounts' — in-app login gate
    if not auth_ui.render_gate():
        st.stop()
    st.session_state["_user"] = identity.current_user()
elif _mode == "proxy" and not _user:
    st.error(
        "🔒 Not authenticated — no verified identity header was provided. Reach "
        "this tool through the configured reverse proxy, not the container "
        "directly."
    )
    st.stop()


def reset_downstream_state() -> None:
    """Called when a new AOS 8 config is discovered — anything derived from the
    previous discovery is stale and must not leak into the new engagement."""
    for key in ("central_config", "preflight_results", "provision_done",
                "provision_results", "validation_results",
                "glp_existing", "glp_subscriptions", "glp_claim_result",
                "glp_sub_results", "glp_service_managers", "onboard_results",
                "probe_results", "validation_celebrated"):
        st.session_state.pop(key, None)
    # the Step 6 closeout checklist is mirrored into durable chk_* keys —
    # a new engagement starts with an unticked checklist
    for key in [k for k in st.session_state.keys() if str(k).startswith("chk_")]:
        st.session_state.pop(key, None)


st.session_state["_reset_downstream"] = reset_downstream_state

# Widget-keyed state is garbage-collected by Streamlit at the end of any run
# where its widget wasn't instantiated (other step / other mode). Re-asserting
# the value each run promotes it to durable app state, so cross-page settings
# (the Remember toggle, hybrid gate, GLP cred source, Add-devices inputs)
# survive navigation instead of silently resetting.
for _k in ("remember_creds", "hybrid_tenant", "glp_use_central_creds",
           "glp_client_id", "add_input_src", "add_scope",
           "add_apdb", "add_list"):
    if _k in st.session_state:
        st.session_state[_k] = st.session_state[_k]

# Deferred from "Forget token" (Step 1): widget keys can't be assigned in the
# same run after their widgets rendered, so the disarm + input clearing lands
# here, before any widget exists. Popping the keyed token inputs is what stops
# text still sitting in the boxes from re-installing the token next render.
if st.session_state.pop("_forget_classic", False):
    for _k in ("classic_access_token", "classic_refresh_token",
               "p1_classic_token_input", "p1_classic_refresh_input"):
        st.session_state.pop(_k, None)
    st.session_state["hybrid_tenant"] = False

# ── Mode toggle ──────────────────────────────────────────────────────────────
with st.sidebar:
    _mode_label = st.radio(
        "Mode",
        ["Full migration", "Add devices only", "Help & Docs"],
        index={"wizard": 0, "add_devices": 1, "help": 2}.get(app_mode, 0),
        key="app_mode_radio",
        help="Add devices only: onboard APs into groups that already exist in "
             "the tenant — claim → assign → move → persona, skipping "
             "discovery/config.  •  Help & Docs: how each page works, the scripts "
             "behind it, curl + Postman, and how to create the API keys.",
    )
app_mode = ("help" if "Help" in _mode_label
            else "add_devices" if "Add" in _mode_label else "wizard")
st.session_state["app_mode"] = app_mode

# set_page_config + the CSS inject above ran with the PREVIOUS render's mode
# (they must precede the radio). One immediate rerun on an actual mode change
# keeps the accent/title/icon in step with the page instead of lagging until
# the next interaction.
if app_mode != st.session_state.get("_last_app_mode"):
    st.session_state["_last_app_mode"] = app_mode
    st.rerun()

brand_header(accent=HPE_GREEN if (on_greenlake or app_mode == "add_devices") else ORANGE)

if app_mode == "help":
    from lib import help_docs
    help_docs.render()
elif app_mode == "add_devices":
    import views.add_devices as page
    page.render()
else:
    step_progress(st.session_state.step, STEPS)
    current = STEPS[st.session_state.step][0]
    if current == "1_connect":
        import views.p1_connect as page
    elif current == "2_preflight":
        import views.p2_preflight as page
    elif current == "3_provision":
        import views.p3_provision as page
    elif current == "4_greenlake":
        import views.p4_greenlake as page
    elif current == "5_runbook":
        import views.p5_runbook as page
    else:
        import views.p6_validate as page
    page.render()
    st.divider()
    render_help(st.session_state.step)

# Sidebar renders last so it reflects state changes made during this run
sidebar_summary()
