"""
Shared client construction from Streamlit session state — keeps the
credential plumbing (and the classic rotating-refresh-token persistence)
identical between the Provision and Validate views.
"""
import hashlib

import streamlit as st

from .central_client import CentralClient
from .classic_central_client import ClassicCentralClient


def tenant_fingerprint() -> str:
    """Stable identity of the DESTINATION TENANT the session points at.
    Base URL alone is not enough (many tenants share a regional URL), so the
    API client id is included; for Classic, the client id may be absent, so
    the gateway base is the best available identity. Rotating tokens are
    deliberately excluded — a refreshed token is the same tenant."""
    ss = st.session_state
    if ss.get("dest_type", "new") == "new":
        raw = "new|" + ss.get("central_base", "") + "|" + ss.get("central_client_id", "")
    else:
        raw = ("classic|" + ss.get("central_base_classic", "")
               + "|" + ss.get("central_client_id", ""))
    return hashlib.sha1(raw.encode()).hexdigest()


_DEFAULT_CLASSIC_BASE = "https://apigw-uswest4.central.arubanetworks.com"


def have_classic_creds() -> bool:
    """True when enough is present to build a usable Classic client: an access
    token, or a refresh token + client id/secret (the client re-mints the
    access token on the first 401). The access token alone is enough because
    the base URL falls back to the default API-GW host."""
    if st.session_state.get("classic_access_token"):
        return True
    return bool(st.session_state.get("classic_refresh_token")
                and st.session_state.get("central_client_id")
                and st.session_state.get("central_secret"))


def use_classic_for_moves() -> bool:
    """Explicit hybrid gate for New-Central flows: route device-group
    creates/moves through the Classic API Gateway only when the operator
    marked the tenant hybrid AND classic creds are usable. Mere presence of a
    saved token (e.g. from a previous engagement) no longer flips the path."""
    return bool(st.session_state.get("hybrid_tenant")) and have_classic_creds()


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
    one strands later steps with a dead token. Returns True if it rotated.
    Also re-syncs the encrypted credstore when Remember is on, so the next
    launch doesn't auto-fill an already-spent token."""
    if client.refresh_token and client.refresh_token != \
            st.session_state.get("classic_refresh_token"):
        st.session_state["classic_refresh_token"] = client.refresh_token
        if st.session_state.get("remember_creds"):
            try:
                from . import credstore
                credstore.save_from_session(st.session_state,
                                            st.session_state.get("_user"))
            except Exception:
                pass  # never let credstore IO break the API flow
        return True
    return False
