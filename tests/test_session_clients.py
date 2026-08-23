"""Hybrid-mode Classic credential routing (session_clients).

Classic Central and GreenLake/New Central are different OAuth issuers. Before
this fix, `build_classic_client()` refreshed the Classic token with the
GreenLake client id/secret in hybrid mode — the refresh 401'd ~2h into a
cutover, stranding it mid-move. Classic creds now live in their own session
keys; the GreenLake pair is only a fallback in CLASSIC-destination mode,
where the Step 1 fields are literally labelled as the Classic client.
"""
import pytest
import streamlit as st

from lib.session_clients import (build_classic_client, classic_client_creds,
                                 have_classic_creds)

_CRED_KEYS = ("dest_type", "central_client_id", "central_secret",
              "classic_client_id", "classic_client_secret",
              "classic_access_token", "classic_refresh_token",
              "central_base_classic")


@pytest.fixture(autouse=True)
def clean_session():
    for k in _CRED_KEYS:
        st.session_state.pop(k, None)
    yield
    for k in _CRED_KEYS:
        st.session_state.pop(k, None)


def test_hybrid_mode_uses_distinct_classic_client_creds():
    st.session_state.update({
        "dest_type": "new",
        "central_client_id": "glp-id", "central_secret": "glp-secret",
        "classic_client_id": "classic-id", "classic_client_secret": "classic-sec",
    })
    assert classic_client_creds() == ("classic-id", "classic-sec")
    client = build_classic_client()
    assert client.client_id == "classic-id"
    assert client.client_secret == "classic-sec"


def test_hybrid_mode_never_falls_back_to_greenlake_creds():
    """The exact reported bug: refresh token + GLP id/secret in hybrid mode
    must read as NOT usable, not as a working config that 401s at hour two."""
    st.session_state.update({
        "dest_type": "new",
        "central_client_id": "glp-id", "central_secret": "glp-secret",
        "classic_refresh_token": "rt-1",
    })
    assert classic_client_creds() == ("", "")
    assert not have_classic_creds()


def test_classic_destination_still_uses_step1_fields():
    """In Classic-destination mode the Step 1 id/secret fields ARE the Classic
    client (labelled 'needed for refresh') — the fallback stays."""
    st.session_state.update({
        "dest_type": "classic",
        "central_client_id": "classic-id", "central_secret": "classic-sec",
        "classic_refresh_token": "rt-1",
    })
    assert classic_client_creds() == ("classic-id", "classic-sec")
    assert have_classic_creds()


def test_access_token_alone_is_usable():
    st.session_state["classic_access_token"] = "tok"
    assert have_classic_creds()


def test_nothing_is_not_usable():
    assert not have_classic_creds()
