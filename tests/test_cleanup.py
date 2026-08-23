import pytest

from lib.cleanup import _matches, cleanup


def test_prefix_matching_is_case_insensitive_prefix_only():
    assert _matches("zztest-acme-corp", "zztest")
    assert _matches("ZZTEST-LAB", "zztest")
    assert not _matches("prod-zztest", "zztest")
    assert not _matches("", "zztest")


def test_empty_prefix_refused():
    # startswith("") matches EVERYTHING — cleanup must refuse outright
    with pytest.raises(ValueError):
        cleanup("")
    with pytest.raises(ValueError):
        cleanup("   ")


class _RecordingCentral:
    """Fake New Central client: records every DELETE, serves canned lists.

    Only the paths in `_responses` answer — anything else raises, which also
    exercises the v1→v1alpha1 list fallback for layer2-vlan."""

    def __init__(self):
        self.deleted = []          # [(path, params)]
        self._groups = [{"scopeName": "zztest-g1", "scopeId": 123},
                        {"scopeName": "prod-g1", "scopeId": 456}]
        self._responses = {
            "/network-config/v1/wlan-ssids": [],
            "/network-config/v1alpha1/captive-portal": [
                {"name": "zztest-guest-cp"}, {"name": "prod-portal"}],
            "/network-config/v1alpha1/layer2-vlan": [
                {"vlan": 100, "name": "zztest-v100"},
                {"vlan": 1, "name": "zztest-default"},   # built-in: never
                {"vlan": 200, "name": "prod-v200"}],
            "/network-config/v1alpha1/policy-groups":
                {"policy-group": {"policy-group-list": [
                    {"name": "zztest-pol", "position": 3},
                    {"name": "default-allow-all", "position": 1}]}},
            "/network-config/v1alpha1/policies": [
                {"name": "zztest-pol"}, {"name": "prod-pol"}],
            "/network-config/v1alpha1/roles": [
                {"name": "zztest-corp"}, {"name": "authenticated"}],
            "/network-config/v1alpha1/server-groups": [],
            "/network-config/v1alpha1/auth-servers": [],
            "/network-config/v1alpha1/gateway-clusters": [
                {"name": "zztest-cluster"}, {"name": "prod-cluster"}],
        }

    def _get(self, path, params=None):
        if path in self._responses:
            return self._responses[path]
        raise RuntimeError(f"404 no such list: {path}")

    def _delete(self, path, json=None, params=None):
        self.deleted.append((path, params))
        return {}

    def list_device_groups(self, refresh=False):
        return self._groups

    def list_sites(self, refresh=False):
        return []

    def _site_name(self, s):
        return ""

    def _site_id(self, s):
        return None


def test_cleanup_covers_every_provisioned_resource_type():
    """Provisioning creates SSIDs, captive portals, VLANs, server-groups,
    auth-servers, roles, policies, policy-group entries, firmware compliance
    and device groups — a prefix teardown that skips half of them leaves live
    leftover config in the tenant after every lab cycle."""
    c = _RecordingCentral()
    res = cleanup("zztest", central=c)
    paths = [p for p, _ in c.deleted]

    assert "/network-config/v1alpha1/captive-portal/zztest-guest-cp" in paths
    assert any("layer2-vlan/100" in p for p in paths)
    assert "/network-config/v1alpha1/policies/zztest-pol" in paths
    assert ("/network-config/v1alpha1/policy-groups"
            "/policy-group/policy-group-list/zztest-pol") in paths
    assert "/network-config/v1alpha1/roles/zztest-corp" in paths
    assert "/network-config/v1alpha1/gateway-clusters/zztest-cluster" in paths
    # firmware compliance goes out with the LOCAL scope params of its group
    assert ("/network-config/v1alpha1/firmware-compliance",
            {"scope-id": "123", "object-type": "LOCAL",
             "device-function": "CAMPUS_AP"}) in c.deleted
    assert not any(r[1] is False for r in res), \
        f"unexpected cleanup failures: {[r for r in res if not r[1]]}"


def test_cleanup_never_touches_non_prefix_or_builtin_objects():
    c = _RecordingCentral()
    cleanup("zztest", central=c)
    paths = [p for p, _ in c.deleted]
    assert not any("prod" in p for p in paths)
    assert not any("authenticated" in p for p in paths)
    assert not any("default-allow-all" in p for p in paths)
    # VLAN 1 is the built-in default — prefix-named or not, never deleted
    assert not any(p.rstrip("/").endswith("layer2-vlan/1") for p in paths)
    # the prod group's firmware compliance stays too
    assert not any(p == "/network-config/v1alpha1/firmware-compliance"
                   and (params or {}).get("scope-id") == "456"
                   for p, params in c.deleted)


def test_firmware_compliance_deleted_before_its_group():
    c = _RecordingCentral()
    cleanup("zztest", central=c)
    paths = [p for p, _ in c.deleted]
    fw = paths.index("/network-config/v1alpha1/firmware-compliance")
    grp = next(i for i, p in enumerate(paths) if "device-groups" in p)
    assert fw < grp, "compliance must be cleared before its scope disappears"
