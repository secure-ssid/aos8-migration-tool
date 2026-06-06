import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(__file__))

STEPS = [
    ("1_connect",   "Connect"),
    ("2_preflight", "Preflight"),
    ("3_provision", "Provision"),
    ("4_greenlake", "GreenLake"),
    ("5_runbook",   "Runbook"),
    ("6_validate",  "Validate"),
]

if "step" not in st.session_state:
    st.session_state.step = 0
st.session_state.step = max(0, min(st.session_state.step, len(STEPS) - 1))

# The GreenLake step lives in HPE territory — the whole accent system
# (title bar, buttons, labels, stepper) migrates from Aruba orange to
# HPE green while the wizard is there.
on_greenlake = STEPS[st.session_state.step][0] == "4_greenlake"

st.set_page_config(
    page_title=("HPE GreenLake Onboarding · Migration Console" if on_greenlake
                else "AOS 8 → Central Migration Console"),
    page_icon="🌿" if on_greenlake else "📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

from lib.styles import inject, brand_header, step_progress, sidebar_summary, \
    ORANGE, HPE_GREEN
from lib.help_content import render_help

inject(accent="green" if on_greenlake else "aruba")


def reset_downstream_state() -> None:
    """Called when a new AOS 8 config is discovered — anything derived from the
    previous discovery is stale and must not leak into the new engagement."""
    for key in ("central_config", "preflight_results", "provision_done",
                "provision_results", "validation_results",
                "glp_existing", "glp_subscriptions", "glp_claim_result",
                "glp_sub_results", "validation_celebrated"):
        st.session_state.pop(key, None)


st.session_state["_reset_downstream"] = reset_downstream_state

brand_header(accent=HPE_GREEN if on_greenlake else ORANGE)
step_progress(st.session_state.step, STEPS)

# ── Route to current step ────────────────────────────────────────────────────
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
