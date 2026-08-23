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


def classic_client_creds() -> tuple[str, str]:
    """The OAuth client id/secret for the CLASSIC API gateway token refresh.

    Classic and GreenLake/New Central are different issuers — in hybrid mode
    `central_client_id`/`central_secret` hold the GreenLake client and a
    Classic refresh attempted with them 401s ~2h into a cutover. Classic-mode
    destinations are the exception: there the Step 1 fields ARE labelled and
    used as the Classic client, so they remain the fallback."""
    cid = st.session_state.get("classic_client_id", "")
    csec = st.session_state.get("classic_client_secret", "")
    if cid and csec:
        return cid, csec
    if st.session_state.get("dest_type", "new") == "classic":
        return (st.session_state.get("central_client_id", ""),
                st.session_state.get("central_secret", ""))
    return "", ""


def have_classic_creds() -> bool:
    """True when enough is present to build a usable Classic client: an access
    token, or a refresh token + client id/secret (the client re-mints the
    access token on the first 401). The access token alone is enough because
    the base URL falls back to the default API-GW host."""
    if st.session_state.get("classic_access_token"):
        return True
    cid, csec = classic_client_creds()
    return bool(st.session_state.get("classic_refresh_token") and cid and csec)


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
    cid, csec = classic_client_creds()
    return ClassicCentralClient(
        base_url=st.session_state.get("central_base_classic") or _DEFAULT_CLASSIC_BASE,
        access_token=st.session_state.get("classic_access_token", ""),
        client_id=cid,
        client_secret=csec,
        refresh_token=st.session_state.get("classic_refresh_token", ""),
    )


def persist_classic_tokens(client: ClassicCentralClient) -> bool:
    """Write BOTH classic tokens back into the session. Returns True if either
    changed.

    The refresh token is single-use and rotates, so losing the new one strands
    every later step with a dead token. The access token matters just as much:
    nothing else in the repo writes it back, so each wizard step would rebuild
    its client with the stale one, eat a 401, and burn one more rotation.
    Also re-syncs the encrypted credstore when Remember is on, so the next
    launch doesn't auto-fill an already-spent token."""
    ss = st.session_state
    changed = False
    if client.access_token and client.access_token != ss.get("classic_access_token"):
        ss["classic_access_token"] = client.access_token
        changed = True
    if client.refresh_token and client.refresh_token != ss.get("classic_refresh_token"):
        ss["classic_refresh_token"] = client.refresh_token
        changed = True
    if changed and ss.get("remember_creds"):
        try:
            from . import credstore
            credstore.save_from_session(ss, ss.get("_user"))
        except Exception as e:
            # a lost rotated refresh token strands the NEXT session, so this
            # must be visible rather than swallowed
            st.warning(f"Rotated Classic refresh token could not be saved: {e} "
                       "— copy it from Step 1 before closing this session.")
    return changed
