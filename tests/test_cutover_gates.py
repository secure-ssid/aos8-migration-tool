"""Stream A / review findings #2 + #6: the Step 4 cutover must be fail-closed.

#2 — a failed provisioning step downgraded to a warning let operators convert
APs into a partially configured tenant. The cutover is now blocked until every
failure is acknowledged individually with a free-text reason.

#6 — DEFERRED / MANUAL FOLLOW-UP steps (gateway cluster, overlay binding, PSK
and RADIUS-secret replacement, captive-portal validation) were counted as
successful provisioning. They now read as OUTSTANDING and each needs an
explicit operator confirmation before the cutover unblocks.

The gate logic is pure (views/p4_greenlake.py) so it is tested directly."""
from views.p4_greenlake import (cutover_blockers, failed_provisioning_steps,
                                outstanding_manual_steps)


# ─────────────────── #2: failed steps block the cutover ───────────────────

def test_failed_steps_are_surfaced():
    results = [("Create site: hq", True, ""),
               ("Create WLAN: corp → g1", False, "403 allowlisted"),
               ("Set firmware compliance → g1", False, "timed out")]
    failed = failed_provisioning_steps(results)
    assert [label for label, _ in failed] == [
        "Create WLAN: corp → g1", "Set firmware compliance → g1"]
    assert failed[0][1] == "403 allowlisted"


def test_cutover_blocked_while_any_failure_unacknowledged():
    results = [("Create site: hq", True, ""),
               ("Create WLAN: corp → g1", False, "403 allowlisted")]
    blockers = cutover_blockers(results, acks={}, confirmations={})
    assert blockers
    assert any("Create WLAN" in b for b in blockers)


def test_empty_acknowledgement_reason_does_not_unblock():
    results = [("Create WLAN: corp → g1", False, "403")]
    # whitespace-only reasons are not a justification
    blockers = cutover_blockers(results,
                                acks={"Create WLAN: corp → g1": "   "},
                                confirmations={})
    assert blockers


def test_per_failure_acknowledgement_unblocks():
    results = [("Create site: hq", True, ""),
               ("Create WLAN: corp → g1", False, "403 allowlisted")]
    blockers = cutover_blockers(
        results,
        acks={"Create WLAN: corp → g1": "tenant lacks the WLAN API; SSID built by hand"},
        confirmations={})
    assert blockers == []


def test_clean_provision_needs_no_acknowledgement():
    results = [("Create site: hq", True, ""), ("Create group: g1", True, "")]
    assert cutover_blockers(results, acks={}, confirmations={}) == []


# ─────────────────── #6: deferred / manual work is outstanding ───────────────────

def test_deferred_and_manual_steps_read_as_outstanding():
    results = [
        ("Create site: hq", True, ""),
        ("Overlay SSID corp-tun → g1 — DEFERRED: bind after gateway cluster "
         "'gw-cluster' is formed at cutover (see runbook)", True, ""),
        ("MANUAL FOLLOW-UP: set the WPA passphrase for SSID 'corp' in Central "
         "(created with placeholder 'ChangeMe-SetInCentral')", True, ""),
        ("MANUAL FOLLOW-UP: set the RADIUS shared secret for 'radius1' in Central",
         True, ""),
    ]
    outstanding = outstanding_manual_steps(results)
    assert len(outstanding) == 3
    assert not any(l.startswith("Create site") for l in outstanding)


def test_outstanding_manual_work_blocks_cutover_until_confirmed():
    results = [
        ("Create site: hq", True, ""),
        ("MANUAL FOLLOW-UP: set the WPA passphrase for SSID 'corp' in Central",
         True, ""),
        ("Overlay SSID tun → g1 — DEFERRED: bind after cluster", True, ""),
    ]
    blocked = cutover_blockers(results, acks={}, confirmations={})
    assert len(blocked) == 2
    # confirming only one leaves the other blocking
    still = cutover_blockers(
        results, acks={},
        confirmations={"MANUAL FOLLOW-UP: set the WPA passphrase for SSID "
                       "'corp' in Central": True})
    assert len(still) == 1
    done = cutover_blockers(
        results, acks={},
        confirmations={label: True for label in outstanding_manual_steps(results)})
    assert done == []


def test_deferred_steps_are_not_counted_as_successes():
    """The provision screen's 'steps completed' metric must not include work
    that only produced a manual follow-up — deferred != done."""
    results = [
        ("Create site: hq", True, ""),
        ("Overlay SSID tun → g1 — DEFERRED: bind after cluster", True, ""),
        ("Create WLAN: corp → g1", False, "403"),
    ]
    # ok=True on the DEFERRED row must not hide it from the gate
    assert outstanding_manual_steps(results) == [
        "Overlay SSID tun → g1 — DEFERRED: bind after cluster"]
    blockers = cutover_blockers(results, acks={"Create WLAN: corp → g1": "fixed by hand"},
                                confirmations={})
    assert len(blockers) == 1  # only the deferred row still blocks
