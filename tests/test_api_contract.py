"""Contract tests against HPE's published reference implementation.

Ground truth for these assertions is the "Open SSID (OWE)" Postman collection
and the Classic-Central wlan_config samples in
aruba/central-python-workflows@v2, plus the New Central developer portal.

The New Central configuration APIs are ALPHA, so the client negotiates the
documented request shape first and falls back to the legacy one this tool
originally shipped. These tests pin the negotiation itself — not a guess about
which shape a given tenant exposes.
"""
import json

import pytest

from lib.central_client import CentralClient, CentralAPIError
from lib.classic_central_client import ClassicCentralClient
from lib.models import AuthType, CentralConfig, ForwardMode, SSID


def _client(fail_paths=()):
    """CentralClient with the transport replaced by a recorder.

    `fail_paths` are substrings; any request whose path contains one raises,
    which is how we simulate a tenant that only exposes the other shape.
    """
    c = CentralClient(base_url="https://example.invalid",
                      client_id="id", client_secret="secret")
    c.calls = []

    def fake_request(method, path, json=None, params=None, _retried=False):
        c.calls.append((method, path, json))
        for frag in fail_paths:
            if frag in path:
                raise CentralAPIError(f"{method} {path} failed 404: not found")
        return {}

    c._request = fake_request
    return c


# ── scope-maps: documented resource format ──────────────────────────────────

def test_scope_map_uses_absolute_resource_and_alpha_route():
    """HPE sends resource as an absolute path under v1alpha1:
        POST /network-config/v1alpha1/scope-maps
        {"scope-map":[{"scope-name","persona","resource":"/wlan-ssids/x"}]}
    The bare relative resource this tool used would not match."""
    c = _client()
    c.map_to_scope("wlan-ssids/guest", "42", "CAMPUS_AP")
    method, path, body = c.calls[0]
    assert method == "POST"
    assert path == "/network-config/v1alpha1/scope-maps"
    entry = body["scope-map"][0]
    assert entry["resource"] == "/wlan-ssids/guest"
    assert entry["scope-name"] == "42"
    assert entry["persona"] == "CAMPUS_AP"


def test_scope_map_falls_back_to_legacy_v1_shape():
    c = _client(fail_paths=("v1alpha1/scope-maps",))
    c.map_to_scope("wlan-ssids/guest", "42", "CAMPUS_AP")
    paths = [p for _, p, _ in c.calls]
    assert paths == ["/network-config/v1alpha1/scope-maps",
                     "/network-config/v1/scope-maps"]
    legacy = c.calls[-1][2]["scope-map"][0]
    assert legacy["resource"] == "wlan-ssids/guest"   # relative, no slash
    assert legacy["scope-id"] == 42                    # int, legacy-only field


def test_scope_map_swallows_duplicates():
    c = CentralClient("https://example.invalid", "id", "secret")

    def dup(method, path, json=None, params=None, _retried=False):
        raise CentralAPIError("already exists")

    c._request = dup
    c.map_to_scope("roles/x", "1", "CAMPUS_AP")  # must not raise


def test_scope_map_rejects_non_numeric_scope_without_masking_the_error():
    """Legacy shape casts scope-id to int; a non-numeric id must surface as an
    API error rather than a bare TypeError escaping the client."""
    c = _client(fail_paths=("v1alpha1/scope-maps",))
    with pytest.raises(CentralAPIError):
        c.map_to_scope("roles/x", "not-a-number", "CAMPUS_AP")


# ── profile writes: collection + wrapper array ──────────────────────────────

def test_wlan_ssid_create_uses_documented_collection_shape():
    c = _client()
    c._upsert_ssid("guest", {"ssid": "guest", "enable": True})
    method, path, body = c.calls[0]
    assert (method, path) == ("POST", "/network-config/v1alpha1/wlan-ssids")
    assert body == {"wlan-ssid": [{"ssid": "guest", "enable": True}]}


def test_wlan_ssid_create_falls_back_to_legacy_named_route():
    c = _client(fail_paths=("v1alpha1/wlan-ssids",))
    c._upsert_ssid("guest", {"ssid": "guest"})
    assert c.calls[-1][1] == "/network-config/v1/wlan-ssids/guest"
    assert c.calls[-1][2] == {"ssid": "guest"}   # flat, not wrapped


def test_profile_name_is_url_encoded_on_the_legacy_route():
    c = _client(fail_paths=("v1alpha1/wlan-ssids",))
    c._upsert_ssid("guest wifi/2", {"ssid": "x"})
    assert c.calls[-1][1] == "/network-config/v1/wlan-ssids/guest%20wifi%2F2"


def test_negotiated_style_is_reused_for_later_profiles():
    """Pinning the winner avoids paying a failed round-trip per profile."""
    c = _client(fail_paths=("v1alpha1/wlan-ssids",))
    c._upsert_ssid("a", {"ssid": "a"})
    first = len(c.calls)
    c._upsert_ssid("b", {"ssid": "b"})
    assert len(c.calls) == first + 1          # one call, not a retry pair
    assert c.calls[-1][1].startswith("/network-config/v1/")


def test_role_and_policy_use_their_documented_wrapper_keys():
    c = _client()
    c._ensure_role("guest", "1", "2")
    role_call = next(x for x in c.calls if "roles" in x[1])
    assert role_call[1] == "/network-config/v1alpha1/roles"
    assert "role" in role_call[2] and isinstance(role_call[2]["role"], list)

    c2 = _client()
    c2._ensure_allow_all_policy("guest", "guest", "1")
    pol = next(x for x in c2.calls if x[1].endswith("/policies"))
    assert pol[1] == "/network-config/v1alpha1/policies"
    assert isinstance(pol[2]["policy"], list)
    assert pol[2]["policy"][0]["type"] == "POLICY_TYPE_SECURITY"


def test_policy_group_prefers_post_then_falls_back_to_patch():
    c = _client()
    c._ensure_allow_all_policy("guest", "guest", "1")
    pg = [x for x in c.calls if x[1].endswith("policy-groups")]
    assert pg and pg[0][0] == "POST"

    c2 = _client()
    seen = []
    orig = c2._request

    def only_patch(method, path, json=None, params=None, _retried=False):
        seen.append((method, path))
        if path.endswith("policy-groups") and method == "POST":
            raise CentralAPIError("405 method not allowed")
        return orig(method, path, json, params, _retried)

    c2._request = only_patch
    c2._ensure_allow_all_policy("guest", "guest", "1")
    verbs = [m for m, p in seen if p.endswith("policy-groups")]
    assert verbs == ["POST", "PATCH"]


# ── OWE / Enhanced Open must survive translation ────────────────────────────

def _ssid(name, auth, **kw):
    return SSID(name=name, vlan=10, forward_mode=ForwardMode.BRIDGE,
                auth_type=auth, **kw)


def test_new_central_owe_ssid_body_is_enhanced_open():
    c = _client()
    c.create_underlay_ssid(_ssid("guest", AuthType.OWE), "7")
    body = c.calls[0][2]["wlan-ssid"][0]
    assert body["opmode"] == "ENHANCED_OPEN"


def test_classic_owe_wlan_body_is_enhanced_open():
    """The Classic full_wlan payload is a JSON string under 'value'."""
    c = ClassicCentralClient(base_url="https://example.invalid",
                             access_token="t", client_id="", client_secret="",
                             refresh_token="")
    sent = {}

    def fake(method, path, json_body=None, params=None, _retried=False):
        sent["path"], sent["body"] = path, json_body
        return {}

    c._request = fake
    c.create_wlan("grp", _ssid("guest", AuthType.OWE), index=1)
    wlan = json.loads(sent["body"]["value"])["wlan"]
    assert wlan["opmode"] == "enhanced-open"
    # transition mode disabled => legacy open clients cannot join the OWE SSID
    assert wlan["opmode_transition_disable"] is True


# ── Classic Central as a full migration destination ─────────────────────────

def _classic_cfg():
    from lib.models import CentralGroupConfig
    return CentralConfig(
        customer_name="acme",
        base_url="https://apigw.example.invalid",
        destination="classic",
        sites=["HQ"],
        groups=[CentralGroupConfig(
            name="hq", firmware_version="10.6.0.0", site_name="HQ",
            source_group="hq",
            ssids=[_ssid("corp", AuthType.WPA2_PSK, psk="a-passphrase"),
                   _ssid("guest", AuthType.OWE)],
        )],
    )


def test_classic_destination_provisions_groups_sites_and_wlans():
    """Classic Central must remain a first-class destination, not just a
    hybrid helper for device moves."""
    c = ClassicCentralClient(base_url="https://example.invalid",
                             access_token="t", client_id="", client_secret="",
                             refresh_token="")
    calls = []

    def fake(method, path, json_body=None, params=None, _retried=False):
        calls.append((method, path))
        if path.startswith("/configuration/v2/groups"):
            return {"data": []}
        if path.startswith("/central/v2/sites"):
            return {"sites": [], "site_id": 5}
        return {}

    c._request = fake
    results = c.provision(_classic_cfg(), ap_serials={"hq": ["SN1"]}, ap_macs={})

    assert results, "provision produced no steps"
    failed = [r for r in results if not r[1]]
    assert not failed, f"classic provisioning failed: {failed}"
    paths = " ".join(p for _, p in calls)
    assert "/configuration/v3/groups" in paths      # group create
    assert "/central/v2/sites" in paths             # site create
    assert "/configuration/full_wlan/" in paths     # WLAN create
    assert "/configuration/v1/devices/move" in paths  # AP move


def test_classic_destination_skips_alpha_api_warning():
    """The alpha-API caveat applies to New Central only."""
    from lib import compatibility
    classic = CentralConfig(customer_name="a", base_url="b",
                            destination="classic")
    new = CentralConfig(customer_name="a", base_url="b", destination="new")
    assert compatibility._check_config_api_maturity(classic) == []
    warn = compatibility._check_config_api_maturity(new)
    assert warn and warn[0].status is compatibility.Status.WARN


# ── read-only shape detection ───────────────────────────────────────────────

def test_detect_profile_style_prefers_documented_and_pins_it():
    c = _client()
    style, seen = c.detect_profile_style()
    assert style == CentralClient._DOC_STYLE
    assert seen[CentralClient._DOC_STYLE] is True
    # pinned: the next write goes straight to the documented route
    before = len(c.calls)
    c._upsert_ssid("guest", {"ssid": "guest"})
    assert len(c.calls) == before + 1
    assert c.calls[-1][1] == "/network-config/v1alpha1/wlan-ssids"


def test_detect_profile_style_falls_back_to_legacy():
    c = _client(fail_paths=("v1alpha1/wlan-ssids",))
    style, seen = c.detect_profile_style()
    assert style == CentralClient._LEGACY_STYLE
    assert seen[CentralClient._DOC_STYLE] is False


def test_detect_profile_style_uses_only_get_requests():
    """The probe must stay read-only — it runs against live tenants."""
    c = _client()
    c.detect_profile_style()
    assert {m for m, _, _ in c.calls} == {"GET"}


def test_detect_profile_style_reports_when_neither_route_answers():
    c = _client(fail_paths=("wlan-ssids",))
    style, seen = c.detect_profile_style()
    assert style == ""
    assert seen[CentralClient._DOC_STYLE] is False
    assert seen[CentralClient._LEGACY_STYLE] is False
