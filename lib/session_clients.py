"""
Shared client construction from Streamlit session state — keeps the
credential plumbing (and the classic rotating-refresh-token persistence)
identical between the Provision and Validate views.
"""
import streamlit as st

from .central_client import CentralClient
from .classic_central_client import ClassicCentralClient


_DEFAULT_CLASSIC_BASE = "https://apigw-uswest4.central.arubanetworks.com"


def have_classic_creds() -> bool:
    """True when enough is present to build a usable Classic client."""
    return bool(st.session_state.get("classic_access_token")
                and st.session_state.get("central_base_classic"))


def build_central_client() -> CentralClient:
    return CentralClient(
        base_url=st.session_state.get("central_base", ""),
        client_id=st.session_state.get("central_client_id", ""),
        client_secret=st.session_state.get("central_secret", ""),
    )


def build_classic_client() -> ClassicCentralClient:
    # .get() everywhere — never KeyError; the base URL falls back to the
    # default API-GW host if the operator only supplied a token
    return ClassicCentralClient(
        base_url=st.session_state.get("central_base_classic") or _DEFAULT_CLASSIC_BASE,
        access_token=st.session_state.get("classic_access_token", ""),
        client_id=st.session_state.get("central_client_id", ""),
        client_secret=st.session_state.get("central_secret", ""),
        refresh_token=st.session_state.get("classic_refresh_token", ""),
    )


def persist_rotated_refresh_token(client: ClassicCentralClient) -> bool:
    """The classic refresh token is single-use and rotates — losing the new
    one strands later steps with a dead token. Returns True if it rotated."""
    if client.refresh_token and client.refresh_token != \
            st.session_state.get("classic_refresh_token"):
        st.session_state["classic_refresh_token"] = client.refresh_token
        return True
    return False
