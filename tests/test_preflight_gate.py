"""Stream B / finding #5: the Step 2 preflight gate is per-risk.

The single global `preflight_override` checkbox let one click waive EVERY
blocker — including ones that silently degrade security — and left no audit
trail. Blockers are now acknowledged individually with a written reason;
each acknowledgement is an audit record; critical blockers have no override
at all.

The gate logic is pure (views/p2_preflight.py) so it is tested directly,
the same way tests/test_cutover_gates.py tests the Step 4 gate."""
import pytest

from lib import audit
from lib.compatibility import CheckResult, Status
from views.p2_preflight import acknowledge_blocker, provision_blockers


def _fail(name, critical=False):
    return CheckResult(name=name, status=Status.FAIL, message=f"{name} broke")


def _critical(name):
    return CheckResult(name=name, status=Status.FAIL, message=f"{name} broke",
                       critical=True)


# ─────────────────── gate semantics ───────────────────

def test_no_failures_no_blockers():
    results = [CheckResult(name="ok", status=Status.PASS, message="fine")]
    assert provision_blockers(results, acks={}) == []


def test_critical_blocker_cannot_be_overridden():
    results = [_critical("WEP SSIDs Unsupported")]
    assert provision_blockers(results, acks={})
    # an acknowledgement for a critical blocker must NOT lift the gate
    assert provision_blockers(results, acks={"WEP SSIDs Unsupported": "i know"})


def test_noncritical_blocker_needs_acknowledgement():
    results = [_fail("ESSID Length")]
    assert provision_blockers(results, acks={})


def test_acknowledgement_with_reason_unblocks_noncritical():
    results = [_fail("ESSID Length")]
    assert provision_blockers(
        results, acks={"ESSID Length": "renamed in Central after provision"}) == []


def test_blank_reason_does_not_unblock():
    results = [_fail("ESSID Length")]
    assert provision_blockers(results, acks={"ESSID Length": "   "})


def test_each_blocker_acknowledged_individually():
    results = [_fail("ESSID Length"), _fail("Classic RADIUS Servers (manual step)")]
    # acknowledging one must not waive the other — that is the whole point
    # of replacing the global checkbox
    acks = {"ESSID Length": "handled"}
    remaining = provision_blockers(results, acks=acks)
    assert remaining
    assert any("RADIUS" in b for b in remaining)
    assert not any("ESSID" in b for b in remaining)


def test_critical_and_noncritical_mix():
    results = [_critical("WEP SSIDs Unsupported"), _fail("ESSID Length")]
    # even with the non-critical one acknowledged, the critical one holds
    blockers = provision_blockers(results, acks={"ESSID Length": "ok"})
    assert any("WEP" in b for b in blockers)


# ─────────────────── acknowledgement is an audit record ───────────────────

def test_acknowledgement_writes_audit_record():
    calls = []
    blocker = _fail("ESSID Length")
    reason = acknowledge_blocker(
        blocker, "  renamed post-provision  ",
        user="op@hpe.com", customer="acme",
        record=lambda action, **fields: calls.append((action, fields)))
    assert reason == "renamed post-provision"   # stored reason is stripped
    [(action, fields)] = calls
    assert action == "preflight-blocker-ack"
    assert fields["user"] == "op@hpe.com"
    assert fields["customer"] == "acme"
    assert fields["check"] == "ESSID Length"
    assert fields["reason"] == "renamed post-provision"


def test_blank_reason_is_rejected_before_any_audit_write():
    calls = []
    with pytest.raises(ValueError):
        acknowledge_blocker(_fail("ESSID Length"), "   ",
                            record=lambda action, **fields: calls.append(action))
    assert calls == []


def test_audit_write_failure_propagates():
    """The record() contract: AuditWriteError must surface (the UI catches
    and warns) — an ack that was not durably logged must not look logged."""
    def boom(action, **fields):
        raise audit.AuditWriteError("disk full")
    with pytest.raises(audit.AuditWriteError):
        acknowledge_blocker(_fail("ESSID Length"), "ok", record=boom)
