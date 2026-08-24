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


class _VerifyFailGLP:
    """Claim succeeds but the post-claim inventory read FAILS — finding #7:
    the run used to fall open and assign/move the submitted serials anyway."""

    def __init__(self):
        self.calls = []
        self._reads = 0

    def authenticate(self):
        self.calls.append("authenticate")

    def workspace_serials(self):
        self.calls.append("workspace_serials")
        self._reads += 1
        if self._reads == 1:
            return set()          # pre-claim read: nothing there yet
        raise RuntimeError("GLP inventory read failed 500")

    def add_devices(self, payload):
        self.calls.append("add_devices")
        return "task-1"

    def poll_task(self, task, on_poll=None):
        self.calls.append("poll_task")
        return {"status": "SUCCEEDED", "result": {}}

    def failed_serials(self, result):
        return []

    def assign_subscription(self, serial, key):
        self.calls.append(f"assign_subscription:{serial}")

    def assign_application(self, serial, app_id, region, sub):
        self.calls.append(f"assign_application:{serial}")


def test_verification_failure_aborts_before_any_mutation(monkeypatch):
    """A failed post-claim workspace read must ABORT the run: no subscription
    assignment, no group move — only serials positively confirmed in the
    workspace are ever mutated."""
    glp = _VerifyFailGLP()
    monkeypatch.setattr(ad, "_glp_client", lambda: glp)
    monkeypatch.setattr(ad, "use_classic_for_moves", lambda: False)
    results = []
    ad._run_add_body([{"serial": "S1", "mac": "aa:bb:cc:dd:ee:01",
                       "group": "g1"}],
                     {"key": "k"}, None, False, results)
    verify = next(r for r in results
                  if r[0].startswith("Verify workspace inventory"))
    assert verify[1] is False
    assert "abort" in verify[2].lower()
    # nothing beyond claim + the failed verify happened
    assert not any(c.startswith("assign_") for c in glp.calls)


def test_successful_verification_still_assigns(monkeypatch):
    """Fail-closed must not break the happy path: confirmed serials get their
    subscription as before."""

    class _OkGLP(_VerifyFailGLP):
        def workspace_serials(self):
            self.calls.append("workspace_serials")
            self._reads += 1
            return {"S1"} if self._reads > 1 else set()

    glp = _OkGLP()
    monkeypatch.setattr(ad, "_glp_client", lambda: glp)
    results = []
    ad._run_add_body([{"serial": "S1", "mac": "aa:bb:cc:dd:ee:01",
                       "group": "g1"}],
                     {"key": "k"}, None, False, results)
    assert "assign_subscription:S1" in glp.calls
    assert any(r[0].startswith("Claim verified") and r[1] for r in results)
