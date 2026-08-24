"""Stream A / review finding #3: the migration manifest — ownership records for
everything provisioning creates, collision refusal, explicit adoption, and
manifest-scoped cleanup.

The pre-manifest behavior reused, patched, or deleted any same-named tenant
object without proving the tool created it. These tests pin the fail-closed
contract: unknown same-name objects are refused, adopted objects may be reused
but never deleted, and cleanup touches manifest-owned resources only."""
import json

import pytest

from lib.cleanup import cleanup
from lib.manifest import CollisionError, Manifest, payload_hash


@pytest.fixture
def manifest(tmp_path):
    return Manifest(tmp_path / "manifest.json")


# ─────────────────── core record behavior ───────────────────

def test_register_persists_ids_and_hashes(manifest):
    e = manifest.register("site", "hq", resource_id="42",
                          payload={"site_name": "hq", "address": "1 Main St"},
                          user="op@example.com")
    assert e.resource_id == "42"
    assert e.hash == payload_hash({"site_name": "hq", "address": "1 Main St"})
    assert not e.adopted
    assert e.created_by == "op@example.com"
    assert e.created_at  # timestamp recorded for the audit trail

    # survives a reload from disk — a rerun in a later session still knows
    # what it owns
    reloaded = Manifest(manifest.path)
    assert reloaded.lookup("site", "hq").resource_id == "42"


def test_lookup_is_kind_scoped(manifest):
    manifest.register("site", "hq")
    assert manifest.lookup("group", "hq") is None


def test_adoption_marks_preexisting_objects(manifest):
    e = manifest.adopt("group", "existing-grp", resource_id="9", user="op")
    assert e.adopted
    # adopted objects may be REUSED by provisioning …
    assert manifest.gate("group", "existing-grp", exists=True) == "reuse"
    # … but never DELETED by cleanup — they belong to someone else
    assert not manifest.may_delete("group", "existing-grp")


def test_remove_drops_the_entry(manifest):
    manifest.register("site", "hq")
    assert manifest.remove("site", "hq")
    assert manifest.lookup("site", "hq") is None
    assert not manifest.remove("site", "hq")  # already gone — False, no raise


# ─────────────────── collision gate ───────────────────

def test_gate_allows_create_when_absent(manifest):
    assert manifest.gate("site", "hq", exists=False) == "create"


def test_gate_refuses_unknown_same_name_object(manifest):
    with pytest.raises(CollisionError) as exc:
        manifest.gate("site", "hq", exists=True)
    # the refusal must name the object and the way out (explicit adoption)
    assert "hq" in str(exc.value)
    assert "adopt" in str(exc.value).lower()


def test_gate_allows_reuse_of_manifest_owned_object(manifest):
    manifest.register("site", "hq", resource_id="42")
    assert manifest.gate("site", "hq", exists=True) == "reuse"


def test_manifest_file_is_json_with_permissions(tmp_path):
    m = Manifest(tmp_path / "sub" / "m.json")   # parent dir created on save
    m.register("site", "hq")
    data = json.loads((tmp_path / "sub" / "m.json").read_text())
    assert data["entries"][0]["name"] == "hq"
    import os, stat
    if os.name != "nt":
        # POSIX: owner-only. Windows has no group/other mode bits (NTFS ACLs
        # govern access there) — st_mode is synthesized and always 0o666.
        mode = stat.S_IMODE(os.stat(tmp_path / "sub" / "m.json").st_mode)
        assert mode & 0o077 == 0  # tenant ownership data is not world-readable


# ─────────────────── client integration: classic group/site/wlan ───────────────────

def _classic_client():
    from lib.classic_central_client import ClassicCentralClient
    return ClassicCentralClient("http://classic.invalid", "tok")


def test_classic_group_collision_refused_without_manifest_entry(manifest):
    c = _classic_client()
    c.manifest = manifest
    c.list_group_names = lambda refresh=False: ["customer-grp"]
    with pytest.raises(CollisionError):
        c.create_group("customer-grp")


def test_classic_group_reuse_allowed_once_adopted(manifest):
    c = _classic_client()
    c.manifest = manifest
    manifest.adopt("group", "customer-grp", user="op")
    c.list_group_names = lambda refresh=False: ["customer-grp"]
    c._read_back_architecture = lambda name: ""
    assert c.create_group("customer-grp") == "customer-grp"


def test_classic_group_creation_registers_in_manifest(manifest):
    c = _classic_client()
    c.manifest = manifest
    c.list_group_names = lambda refresh=False: []
    c._read_back_architecture = lambda name: ""
    posts = []
    c._post = lambda path, json_body=None, params=None: posts.append(path) or {}
    c.create_group("campus-aps")
    assert posts  # actually created
    assert manifest.lookup("group", "campus-aps") is not None


def test_classic_wlan_duplicate_swallow_requires_manifest_entry(manifest):
    """Review: 'Classic duplicate WLAN creation is swallowed without
    reconciliation.' With a manifest attached, a duplicate error for an SSID
    the manifest doesn't own is a collision, not idempotent success."""
    from lib.classic_central_client import ClassicCentralAPIError
    from lib.models import AuthType, ForwardMode, SSID
    ssid = SSID(name="corp", vlan=10, forward_mode=ForwardMode.BRIDGE,
                auth_type=AuthType.WPA2_PSK, psk="passphrase1")
    c = _classic_client()
    c.manifest = manifest

    def dup(path, json_body=None, params=None):
        raise ClassicCentralAPIError(f"POST {path} failed 400: already exists")
    c._post = dup
    with pytest.raises(CollisionError):
        c.create_wlan("grp", ssid, 1)
    # once owned (or adopted), the same duplicate reads as idempotent reuse
    manifest.register("ssid", "corp")
    c.create_wlan("grp", ssid, 1)  # no raise


def test_no_manifest_keeps_legacy_behavior():
    """manifest=None must not change the pre-manifest idempotent behavior."""
    from lib.classic_central_client import ClassicCentralAPIError
    from lib.models import AuthType, ForwardMode, SSID
    ssid = SSID(name="corp", vlan=10, forward_mode=ForwardMode.BRIDGE,
                auth_type=AuthType.WPA2_PSK, psk="passphrase1")
    c = _classic_client()
    def dup(path, json_body=None, params=None):
        raise ClassicCentralAPIError(f"POST {path} failed 400: already exists")
    c._post = dup
    c.create_wlan("grp", ssid, 1)  # swallowed, as before


# ─────────────────── client integration: New Central SSID patch ───────────────────

def _central_client():
    from lib.central_client import CentralClient
    return CentralClient("http://central.invalid", "cid", "secret")


def test_new_central_ssid_patch_of_foreign_object_refused(manifest):
    """_upsert_ssid PATCHes same-name SSIDs so re-runs refresh bindings — but
    patching an SSID another administrator created is exactly finding #3."""
    from lib.central_client import CentralAPIError
    c = _central_client()
    c.manifest = manifest
    calls = []

    def fake_config(method, resource, **kw):
        calls.append(method)
        if method == "POST":
            raise CentralAPIError(f"POST {resource} failed 400: already exists")
        return {}
    c._config_request = fake_config
    with pytest.raises(CollisionError):
        c._upsert_ssid("corp", {"ssid": "corp"})
    assert calls == ["POST"]  # never reached the PATCH

    manifest.register("ssid", "corp", payload={"ssid": "corp"})
    c._upsert_ssid("corp", {"ssid": "corp"})
    assert calls == ["POST", "POST", "PATCH"]  # duplicate → PATCH, now allowed
    assert manifest.lookup("ssid", "corp") is not None


# ─────────────────── cleanup scoping ───────────────────

class _RecordingCentral:
    """Same shape as test_cleanup's fake: canned lists, recorded deletes."""

    def __init__(self):
        self.deleted = []
        self._groups = [{"scopeName": "zztest-owned", "scopeId": 1},
                        {"scopeName": "zztest-foreign", "scopeId": 2},
                        {"scopeName": "zztest-adopted", "scopeId": 3}]
        self._responses = {
            "/network-config/v1/wlan-ssids": [
                {"essid": {"name": "zztest-owned-ssid"}},
                {"essid": {"name": "zztest-foreign-ssid"}}],
            "/network-config/v1alpha1/captive-portal": [],
            "/network-config/v1alpha1/layer2-vlan": [],
            "/network-config/v1alpha1/policy-groups":
                {"policy-group": {"policy-group-list": []}},
            "/network-config/v1alpha1/policies": [],
            "/network-config/v1alpha1/roles": [],
            "/network-config/v1alpha1/server-groups": [],
            "/network-config/v1alpha1/auth-servers": [],
            "/network-config/v1alpha1/gateway-clusters": [],
        }

    def _get(self, path, params=None):
        if path in self._responses:
            return self._responses[path]
        raise RuntimeError(f"404 no such list: {path}")

    def _delete(self, path, json=None, params=None):
        self.deleted.append(path)
        return {}

    def list_device_groups(self, refresh=False):
        return self._groups

    def list_sites(self, refresh=False):
        return []


def test_cleanup_deletes_only_manifest_owned_resources(manifest):
    """Prefix matching alone let teardown delete any same-prefix object. With
    a manifest attached, only manifest-OWNED entries go — foreign objects and
    explicitly adopted ones survive."""
    manifest.register("device-group", "zztest-owned", resource_id="1")
    manifest.register("ssid", "zztest-owned-ssid")
    manifest.adopt("device-group", "zztest-adopted", resource_id="3", user="op")
    c = _RecordingCentral()
    cleanup("zztest", central=c, manifest=manifest)

    joined = "\n".join(c.deleted)
    assert "zztest-owned-ssid" in joined
    assert "zztest-foreign-ssid" not in joined   # not in the manifest
    # device groups: owned deleted (bulk by id 1), foreign + adopted kept
    assert not any("2" == p.rstrip("/").split("/")[-1] for p in c.deleted)
    assert not any("3" == p.rstrip("/").split("/")[-1] for p in c.deleted)
    # deleted resources leave the manifest — a second cleanup won't chase them
    assert manifest.lookup("device-group", "zztest-owned") is None
    assert manifest.lookup("device-group", "zztest-adopted") is not None


def test_cleanup_without_manifest_keeps_prefix_behavior():
    c = _RecordingCentral()
    cleanup("zztest", central=c)
    joined = "\n".join(c.deleted)
    assert "zztest-foreign-ssid" in joined  # legacy: prefix alone decides
