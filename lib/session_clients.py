"""
Shared client construction from Streamlit session state — keeps the
credential plumbing (and the classic rotating-refresh-token persistence)
identical between the Provision and Validate views.
"""
import streamlit as st

from .central_client import CentralClient
from .classic_central_client import ClassicCentralClient


def build_central_client() -> CentralClient:
    return CentralClient(
        base_url=st.session_state["central_base"],
        client_id=st.session_state["central_client_id"],
        client_secret=st.session_state["central_secret"],
    )


def build_classic_client() -> ClassicCentralClient:
    return ClassicCentralClient(
        base_url=st.session_state["central_base_classic"],
        access_token=st.session_state["classic_access_token"],
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
