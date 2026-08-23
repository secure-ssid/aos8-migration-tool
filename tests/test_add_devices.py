"""add_devices run ordering: the Classic client must authenticate BEFORE any
GreenLake mutation (claim/subscription). A classic credential that only fails
at the first move leaves a partial run — claimed + subscribed, never moved."""
import streamlit as st

import views.add_devices as ad


class _FakeGLP:
    def __init__(self):
        self.calls = []

    def authenticate(self):
        self.calls.append("authenticate")

    def workspace_serials(self):
        self.calls.append("workspace_serials")
        return set()


class _FakeCentral:
    def authenticate(self):
        pass


class _FakeClassic:
    def __init__(self, fail):
        self.fail = fail
        self.calls = []

    def list_group_names(self, refresh=False):
        self.calls.append("list_group_names")
        if self.fail:
            raise RuntimeError("Classic API token expired/invalid (401)")
        return ["default"]


def _run(monkeypatch, classic_fail):
    glp = _FakeGLP()
    monkeypatch.setattr(ad, "_glp_client", lambda: glp)
    monkeypatch.setattr(ad, "build_central_client", lambda: _FakeCentral())
    monkeypatch.setattr(ad, "use_classic_for_moves", lambda: True)
    monkeypatch.setattr(ad, "build_classic_client",
                        lambda: _FakeClassic(classic_fail))
    monkeypatch.setattr(ad, "persist_classic_tokens", lambda c: None)
    st.session_state.pop("remember_creds", None)
    results = []
    ad._run_add_body([{"serial": "S1", "mac": "aa:bb", "group": "g1"}],
                     {"key": "k"}, None, True, results)
    return glp, results


def test_classic_auth_failure_aborts_before_any_claim(monkeypatch):
    glp, results = _run(monkeypatch, classic_fail=True)
    labels = [r[0] for r in results]
    assert any(l.startswith("Authenticate Classic API Gateway") for l in labels)
    classic_step = next(r for r in results
                        if r[0].startswith("Authenticate Classic API Gateway"))
    assert classic_step[1] is False
    assert "aborted before claiming" in classic_step[2]
    # nothing in GreenLake was touched beyond the up-front authenticate
    assert glp.calls == ["authenticate"]


def test_classic_auth_success_proceeds_to_inventory_read(monkeypatch):
    glp, results = _run(monkeypatch, classic_fail=False)
    assert "workspace_serials" in glp.calls
