"""
Classic Aruba Central REST API client (apigw-*.central.arubanetworks.com).

API mechanics (verified against pycentral classic SDK, HPE's
central-python-workflows, and cencli — see docs/notes in repo history):
  - Auth: access token from the API Gateway UI (~2h). No client_credentials
    grant exists on classic — refresh via
    POST /oauth2/token?client_id&client_secret&grant_type=refresh_token&refresh_token
    (params in the QUERY STRING, empty body). The refresh token ROTATES —
    the new one must be captured after every refresh.
  - Groups: POST /configuration/v3/groups (201) with per-section
    Architecture="AOS10"; existence via GET /configuration/v2/groups
    (returns a list of single-element name lists); verify the created
    group reads back Architecture==AOS10 (known API flaw returns 200
    without applying).
  - WLANs: POST /configuration/full_wlan/{group}/{name} with the body
    wrapped as {"value": json.dumps({"wlan": {...}, "access_rule": {...}})}.
    NOTE: the WLAN config APIs are allowlisted per tenant — a 403 here
    means the tenant needs the API enabled by an Aruba SE.
  - Sites: POST /central/v2/sites; associate via
    POST /central/v2/sites/associations {"site_id", "device_type":"IAP",
    "device_ids"}.
  - Firmware compliance: POST /firmware/v2/upgrade/compliance_version
    (v1 fallback). device_type for APs is "IAP" (also on AOS 10).
  - Monitoring: GET /monitoring/v2/aps → {"aps":[...]}, status "Up"/"Down".
"""
import copy
import json
import re
import time
from typing import Callable, Optional
from urllib.parse import quote

import requests

from .http_base import normalize_base
from .manifest import KIND_GROUP, KIND_SSID
from .models import AuthType, CentralConfig, ForwardMode, SSID
from .central_client import PSK_PLACEHOLDER, secret_looks_unusable

OPMODE_CLASSIC = {
    AuthType.OPEN: "opensystem",
    AuthType.MAC: "opensystem",
    # Enhanced-Open IS a valid Classic opmode. HPE's own Classic Central
    # reference ships it in both the WLAN API payload and the AP CLI config:
    #   central-python-workflows/Classic-Central/wlan_config/configurations/
    #     enhanced_captive.yaml            -> "opmode: enhanced-open"
    #   central-python-workflows/Classic-Central/ap_config/configurations/
    #     open-captive-portal.txt          -> "opmode enhanced-open"
    # Mapping OWE to opensystem here would strip the encryption OWE exists to
    # provide, turning a protected network into a plaintext one.
    AuthType.OWE: "enhanced-open",
    AuthType.WPA2_PSK: "wpa2-psk-aes",
    AuthType.WPA3_SAE: "wpa3-sae-aes",
    AuthType.WPA2_ENTERPRISE: "wpa2-aes",
    AuthType.WPA3_ENTERPRISE: "wpa3-aes-ccm-128",
}

ENTERPRISE = (AuthType.WPA2_ENTERPRISE, AuthType.WPA3_ENTERPRISE)

# full_wlan field set taken from HPE's published FullWlanData schema
# (developer.arubanetworks.com/central → Configuration → Create full WLAN).
# The API expects the COMPLETE flat object and its handler indexes the keys
# directly, so an omitted key surfaces as a bare KeyError repr in a 500
# ("description": "'server_group'") rather than a useful validation message.
# Per-SSID fields are overridden in create_wlan().
_BASE_WLAN = {
    "a_max_tx_rate": "54", "a_min_tx_rate": "6",
    "access_type": "unrestricted", "accounting_server1": "",
    "accounting_server2": "", "air_time_limit": "", "air_time_limit_cb": False,
    "auth_cache_timeout": 24, "auth_req_threshold": 0,
    "auth_server1": "", "auth_server2": "", "auth_survivability": False,
    "bandwidth_limit": "", "bandwidth_limit_cb": False, "blacklist": True,
    "broadcast_filter": "arp", "called_station_id_deli": 0,
    "called_station_id_incl_ssid": False, "called_station_id_type": "macaddr",
    "captive_exclude": [], "captive_portal": "disable",
    "captive_portal_proxy_ip": "", "captive_portal_proxy_port": "",
    "captive_profile_name": "", "cloud_guest": False, "cluster_name": "",
    "content_filtering": False, "deny_intra_vlan_traffic": False,
    "disable_ssid": False, "dmo_channel_util_threshold": 90, "dot11k": False,
    "dot11r": False,
    "dot11v": False, "download_role": False, "dtim_period": 1,
    "dynamic_multicast_optimization": False, "dynamic_vlans": [],
    "enforce_dhcp": False, "essid": "", "explicit_ageout_client": False,
    "g_max_tx_rate": "54", "g_min_tx_rate": "1", "gw_profile_name": "",
    "hide_ssid": False, "high_efficiency_disable": True,
    "high_throughput_disable": True, "inactivity_timeout": 1000, "index": 1,
    "l2_auth_failthrough": False, "l2switch_mode": False,
    "leap_use_session_key": False, "local_probe_req_threshold": 0,
    "mac_authentication": False, "mac_authentication_delimiter": "",
    "mac_authentication_upper_case": False, "management_frame_protection": False,
    "max_auth_failures": 0, "max_clients_threshold": 64, "mdid": "",
    "multicast_rate_optimization": False, "name": "", "okc": False,
    "okc_disable": False,
    "oos_def": "vpn-down", "oos_name": "none", "oos_time": 30,
    "opmode": "wpa2-psk-aes", "opmode_transition_disable": True,
    # per_user_limit must be "" — None serialises to JSON null and the
    # handler rejects it with "Invalid type for JSON key"
    "per_user_limit": "", "per_user_limit_cb": False,
    "radius_accounting": False, "radius_accounting_mode": "user-authentication",
    "radius_interim_accounting_interval": 0, "reauth_interval": 0,
    "rf_band": "all", "roles": [], "server_load_balancing": False,
    "set_role_mac_auth": "", "set_role_machine_auth_machine_only": "",
    "set_role_machine_auth_user_only": "", "set_role_pre_auth": "",
    "ssid_encoding": "utf8", "strict_svp": False, "termination": False,
    "time_range_profiles_status": [], "tspec": False, "tspec_bandwidth": 2000,
    "type": "employee", "use_ip_for_calling_station": False,
    "user_bridging": False, "very_high_throughput_disable": True, "vlan": "",
    "wep_index": 0, "wep_key": "", "wispr": False, "wmm_background_dscp": "",
    "wmm_background_share": 0, "wmm_best_effort_dscp": "",
    "wmm_best_effort_share": 0, "wmm_uapsd": True, "wmm_video_dscp": "",
    "wmm_video_share": 0, "wmm_voice_dscp": "", "wmm_voice_share": 0,
    "work_without_uplink": False, "wpa_passphrase": "",
    "wpa_passphrase_changed": False, "zone": "",
    "hotspot_profile": "",
}

# The access_rule shape HPE's schema documents, kept for reference only.
# create_wlan sends access_rule=None: a populated rule makes the handler
# resolve a server group, which fails on any SSID without an auth server.
_BASE_ACCESS_RULE = {
    "name": "", "action": "allow", "app_rf_mv_info": "", "blacklist": False,
    "classify_media": False, "disable_scanning": False, "dot1p_priority": "",
    "eport": "any", "ipaddr": "any", "log": False, "match": "match",
    "nat_ip": "", "nat_port": 0, "netmask": "any", "protocol": "any",
    "protocol_id": "", "service_name": "", "service_type": "network",
    "source": "default", "sport": "any", "throttle_downstream": "",
    "throttle_upstream": "", "time_range": "", "tos": "", "vlan": 0,
}


_JSON_TYPE_ERR = re.compile(r"Invalid type for JSON key:\s*'?([A-Za-z0-9_]+)")
_BARE_KEY_ERR = re.compile(r"'description':\s*\"'([A-Za-z0-9_]+)'\"")


def _type_error_key(msg: str) -> Optional[str]:
    m = _JSON_TYPE_ERR.search(msg)
    return m.group(1) if m else None


def _flip_scalar(value):
    """str <-> int for one payload scalar; None when it cannot be flipped."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _explain_wlan_error(name: str, err: Exception) -> str:
    m = _BARE_KEY_ERR.search(str(err))
    if not m:
        return str(err)
    return (
        f"SSID '{name}': Classic Central rejected the WLAN with a bare key "
        f"error for '{m.group(1)}' — the full_wlan handler raised a KeyError "
        f"instead of validating. If the key is 'server_group', the tenant "
        f"could not resolve an auth server for this SSID; check that the "
        f"group is an AOS 10 group and that any RADIUS server the SSID "
        f"references exists in it. ({err})")


def _vlan_token(ssid: SSID) -> str:
    """The full_wlan ``vlan`` field is a STRING (HPE FullWlanData schema).

    A named AOS 8 VLAN has no numeric ``vlan`` — it lives in ``vlan_raw``.
    Dropping it silently parked the WLAN on the AP's native VLAN, which is a
    misconfiguration that provisions "successfully".
    """
    if ssid.vlan is not None:
        return str(ssid.vlan)
    raw = getattr(ssid, "vlan_raw", None)
    return str(raw).strip() if raw else ""


class ClassicCentralAPIError(Exception):
    pass


def _is_duplicate(err: Exception) -> bool:
    msg = str(err).lower()
    # only inspect the response detail — the error prefix contains the URL
    # path, and a customer-named object ("duplicate-lab") in the path must
    # not make an unrelated failure read as idempotent success
    m = re.search(r"failed \d+: (.*)", msg, re.S)
    detail = m.group(1) if m else msg
    return ("already exists" in detail or "duplicate" in detail
            # Classic's full_wlan phrases it as "Cannot create existing SSID"
            or "existing ssid" in detail)


class ClassicCentralClient:
    def __init__(self, base_url: str, access_token: str,
                 client_id: str = "", client_secret: str = "",
                 refresh_token: str = "", timeout: int = 30):
        self.base = normalize_base(base_url)
        self.access_token = access_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token  # rotates — read back after runs
        self.refresh_token_rotated = False   # True once refresh() rotated it
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {access_token}"})
        # per-instance caches — a client is constructed per run, so these are
        # naturally fresh; created objects are appended locally
        self._group_names_cache: Optional[list[str]] = None
        self._sites_cache: Optional[list[dict]] = None
        # ownership registry (lib.manifest) attached by the Step 3 view —
        # None keeps the legacy name-only idempotency
        self.manifest = None

    # ─────────────────── Auth / HTTP ───────────────────

    def refresh(self) -> bool:
        """Refresh the access token. The refresh token is single-use and
        rotates — self.refresh_token holds the NEW one afterwards."""
        if not (self.client_id and self.client_secret and self.refresh_token):
            return False
        try:
            # plain requests.post, NOT self.session — the session carries the
            # expired Bearer header, which must never be sent to the token
            # endpoint (some gateways reject the request outright)
            resp = requests.post(
                f"{self.base}/oauth2/token",
                params={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException:
            # a transport failure mid-refresh must read as "refresh didn't
            # happen" (the caller then raises the clean 401 guidance), not as
            # a raw requests exception escaping the client's error type
            return False
        if not resp.ok:
            return False
        try:
            data = resp.json()
        except Exception:
            return False
        token = data.get("access_token")
        if not token:
            return False  # malformed 200 — let the caller surface the real 401
        self.access_token = token
        # an explicit null/empty refresh_token must not wipe the working one
        self.refresh_token = data.get("refresh_token") or self.refresh_token
        self.refresh_token_rotated = "refresh_token" in data
        self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})
        return True

    def _request(self, method: str, path: str, json_body=None,
                 params: Optional[dict] = None, _auth_retried: bool = False,
                 _rate_retried: bool = False) -> dict:
        try:
            resp = self.session.request(
                method, f"{self.base}{path}", json=json_body, params=params,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout:
            raise ClassicCentralAPIError(f"{method} {path}: timed out after {self.timeout}s")
        except requests.exceptions.ConnectionError as e:
            raise ClassicCentralAPIError(f"{method} {path}: connection failed — check the "
                                         f"apigw base URL ({type(e).__name__})")
        if resp.status_code == 401 and not _auth_retried and self.refresh():
            return self._request(method, path, json_body, params,
                                 _auth_retried=True, _rate_retried=_rate_retried)
        if resp.status_code == 401:
            have_refresh = bool(self.client_id and self.client_secret
                                and self.refresh_token)
            hint = ("the token auto-refresh failed — generate a fresh token"
                    if have_refresh else
                    "generate a fresh token in Classic API Gateway → System Apps & "
                    "Tokens, or add the refresh token + client id/secret in Step 1's "
                    "hybrid expander so the tool auto-refreshes (classic tokens "
                    "expire after ~2 hours)")
            raise ClassicCentralAPIError(
                f"Classic API token expired/invalid (401). {hint}.")
        if resp.status_code == 429 and not _rate_retried:
            # Retry-After may be an HTTP-date (RFC 7231) — fall back to 10s
            # rather than crashing on int().
            retry_after = resp.headers.get("Retry-After", "10").strip()
            wait = int(retry_after) if retry_after.isdigit() else 10
            time.sleep(min(wait, 60))
            return self._request(method, path, json_body, params,
                                 _auth_retried=_auth_retried, _rate_retried=True)
        if resp.status_code == 403 and "full_wlan" in path:
            raise ClassicCentralAPIError(
                f"{method} {path} → 403: the classic WLAN config APIs are "
                "allowlisted per tenant — ask your Aruba SE to enable them "
                "for this account.")
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:300]
            raise ClassicCentralAPIError(f"{method} {path} failed {resp.status_code}: {detail}")
        if not resp.content:
            return {}
        try:
            body = resp.json()
        except ValueError:
            # a 2xx with a non-JSON body is a protocol failure — every API
            # response here is JSON. Fail closed: flattening to {} made
            # list_group_names return [] and create_group re-POST.
            raise ClassicCentralAPIError(
                f"{method} {path} returned 2xx with a non-JSON body — "
                f"failures are not flattened: {resp.text[:300]}")
        return {"items": body} if isinstance(body, list) else body

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)

    def _post(self, path, json_body=None, params=None):
        return self._request("POST", path, json_body=json_body, params=params)

    def _paginate(self, path: str, items_key: Optional[str] = None,
                  params: Optional[dict] = None, page_size: int = 100,
                  max_pages: int = 50) -> list:
        """Bounded offset pagination. A gateway that ignores `offset` would
        otherwise loop forever inside a Streamlit spinner, and a truncated
        list silently breaks every "does this object already exist?" check —
        so both conditions raise instead of returning partial results."""
        items, offset = [], 0
        params = dict(params or {})
        first_of_prev_page = object()
        for _ in range(max_pages):
            params.update({"limit": page_size, "offset": offset})
            data = self._get(path, params=params)
            page = data.get(items_key) if items_key else None
            if page is None:
                page = (data.get("items") or data.get("data")
                        or data.get("output") or [])
            if not isinstance(page, list):
                page = [page] if page else []
            if page and page[0] == first_of_prev_page:
                raise ClassicCentralAPIError(
                    f"GET {path}: server ignored offset={offset} (page repeated) "
                    "— cannot enumerate completely")
            first_of_prev_page = page[0] if page else None
            items.extend(page)
            if len(page) < page_size:
                return items
            offset += page_size
        raise ClassicCentralAPIError(
            f"GET {path}: more than {max_pages * page_size} items — pagination cap hit")

    # ─────────────────── Groups ───────────────────

    def list_group_names(self, refresh: bool = False) -> list[str]:
        if self._group_names_cache is not None and not refresh:
            return self._group_names_cache
        # response "data"/"output": a list of single-element name lists
        raw = self._paginate("/configuration/v2/groups", page_size=20,
                             max_pages=100)
        flat = [g for sub in raw for g in (sub if isinstance(sub, list) else [sub])]
        self._group_names_cache = [n for n in flat if n and n != "unprovisioned"]
        return self._group_names_cache

    def _read_back_architecture(self, name: str) -> str:
        """Best-effort group-properties readback. Returns the architecture
        string ONLY when it confirms something other than AOS10; transport
        errors and missing data return '' (never fail on the readback)."""
        try:
            check = self._get("/configuration/v1/groups/properties",
                              params={"groups": name})
            for item in check.get("data", check.get("items", [])):
                if item.get("group") == name:
                    arch = (item.get("properties") or {}).get("Architecture", "")
                    if arch and arch != "AOS10":
                        return arch
        except Exception:
            pass
        return ""

    def create_group(self, name: str, include_gateways: bool = False,
                     new_central: bool = False) -> str:
        """Idempotent AOS 10 UI-group create; verifies Architecture readback.

        new_central=True sets the 'Allow New Central to overwrite' flag so the
        group becomes New-Central-managed and appears in New Central's
        device-collections — required for the hybrid path where SSIDs/VLANs are
        then configured on the New Central side. False = pure classic group."""
        if name in self.list_group_names():
            # a pre-existing group is reused — but only when the manifest
            # proves it's ours (created or explicitly adopted). Reusing a
            # same-named group another administrator owns is finding #3.
            if self.manifest is not None:
                self.manifest.gate(KIND_GROUP, name, exists=True)
            # a hybrid (New-Central-managed)
            # run must not silently reuse a group with the wrong architecture —
            # that dead-ends later with unrelated-looking SSID/VLAN errors
            wrong = self._read_back_architecture(name)
            if wrong:
                raise ClassicCentralAPIError(
                    f"Group '{name}' already exists with Architecture "
                    f"'{wrong}', not AOS10 — delete or rename it in Central "
                    "(the tool will then create it correctly).")
            return name
        props = {
            "AllowedDevTypes": ["AccessPoints"] + (["Gateways"] if include_gateways else []),
            "Architecture": "AOS10",
            "ApNetworkRole": "Standard",
            "NewCentral": bool(new_central),
        }
        # Deliberately NO GwNetworkRole here. "BranchGateway" is the SD-Branch
        # persona; Central 3.x rejects a WLAN/AOS10 group that carries a
        # branch-gateway role ("WLAN with branch gateways network role for
        # Gateways is not supported"). Wireless gateways are MOBILITY_GW and are
        # formed as a New Central gateway-cluster object, not via a Classic
        # group role — so this tool never stamps a gateway role on a WLAN group.
        self._post("/configuration/v3/groups", json_body={
            "group": name,
            "group_attributes": {
                "template_info": {"Wired": False, "Wireless": False},
                "group_properties": props,
            },
        })
        # known API flaw: invalid combos return success without applying —
        # verify the group actually reads back as AOS10. The readback itself
        # is best-effort (transport errors don't fail the step); only a
        # CONFIRMED wrong architecture raises.
        wrong_arch = self._read_back_architecture(name)
        if wrong_arch:
            raise ClassicCentralAPIError(
                f"Group '{name}' was created but Architecture reads back as "
                f"'{wrong_arch}', not AOS10 — delete it in Central and check "
                "the tenant supports AOS10 groups.")
        if self._group_names_cache is not None:
            self._group_names_cache.append(name)
        if self.manifest is not None:
            self.manifest.register(KIND_GROUP, name, payload=props)
        return name

    # ─────────────────── Inventory / devices ───────────────────

    def add_to_inventory(self, devices: list[dict]) -> None:
        """devices: [{"mac": ..., "serial": ...}] — already-present devices
        come back as duplicates, which is fine."""
        if not devices:
            return
        try:
            self._post("/platform/device_inventory/v1/devices", json_body=devices)
        except ClassicCentralAPIError as e:
            # "exist" also matches "does not exist" — only _is_duplicate may
            # swallow, and it inspects the response detail, not the URL
            if _is_duplicate(e):
                return
            if "INVALID_MAC_SN" in str(e) or "ATHENA_ERROR_NO_DEVICE" in str(e):
                # the inventory route only accepts devices HPE already shipped
                # to this customer; it cannot mint new ones
                raise ClassicCentralAPIError(
                    "Classic Central rejected every serial/MAC as unknown "
                    "(ATHENA_ERROR_NO_DEVICE / INVALID_MAC_SN). This route only "
                    "claims APs already registered to your HPE account — it "
                    "cannot create inventory entries. Check the serial/MAC pairs "
                    "against the AOS 8 controller, and note that placeholder or "
                    "lab serials will never onboard. Skip the Add-devices step to "
                    "provision groups, sites and WLANs without inventory.\n"
                    f"({e})") from e
            raise

    # HPE documents a hard cap on this route: more than 50 serials in one
    # request comes back 400 "More than 50 devices cannot be moved to a group".
    MOVE_BATCH = 50

    def move_devices(self, group: str, serials: list[str]) -> None:
        for i in range(0, len(serials), self.MOVE_BATCH):
            self._post("/configuration/v1/devices/move",
                       json_body={"group": group,
                                  "serials": serials[i:i + self.MOVE_BATCH]})

    def delete_group(self, name: str) -> None:
        self._request("DELETE", f"/configuration/v1/groups/{quote(name, safe='')}")
        if self._group_names_cache is not None and name in self._group_names_cache:
            self._group_names_cache.remove(name)

    # ─────────────────── Sites ───────────────────

    def list_sites(self, refresh: bool = False) -> list[dict]:
        if self._sites_cache is not None and not refresh:
            return self._sites_cache
        # only ever cache a list that came from a complete enumeration
        self._sites_cache = self._paginate("/central/v2/sites", items_key="sites",
                                           params={"calculate_total": True},
                                           page_size=100)
        return self._sites_cache

    def create_site(self, name: str, address: str = "", city: str = "",
                    state: str = "", country: str = "", zipcode: str = "",
                    lab_mode: bool = False) -> int:
        for site in self.list_sites():
            if site.get("site_name") == name:
                return int(site.get("site_id"))
        body: dict = {"site_name": name}
        if any((address, city, state, country, zipcode)):
            body["site_address"] = {k: v for k, v in {
                "address": address, "city": city, "state": state,
                "country": country, "zipcode": zipcode,
            }.items() if v}
        elif not lab_mode:
            # blank site data must not silently become a REAL 0.0,0.0 site in
            # a production tenant — placeholders need the explicit lab switch
            raise ClassicCentralAPIError(
                f"Site '{name}' has no address data — refusing to create a "
                "placeholder site (0.0, 0.0 geolocation) in a production "
                "tenant. Supply the site address, or enable lab/test mode for "
                "a throwaway lab tenant.")
        else:
            # site_address and geolocation are mutually exclusive but one is
            # required — default to a zeroed geolocation when no address given
            body["geolocation"] = {"latitude": "0.0", "longitude": "0.0"}
        resp = self._post("/central/v2/sites", json_body=body)
        self._sites_cache = None  # the pre-create list is stale now
        sid = resp.get("site_id")
        if sid is None:
            # POST didn't echo the id — re-list, bypassing the pre-create cache
            for site in self.list_sites(refresh=True):
                if site.get("site_name") == name:
                    return int(site.get("site_id"))
            raise ClassicCentralAPIError(f"Site '{name}' created but id not found")
        return int(sid)

    def associate_site(self, site_id: int, serials: list[str]) -> None:
        if not serials:
            return
        self._post("/central/v2/sites/associations", json_body={
            "site_id": int(site_id),
            "device_type": "IAP",
            "device_ids": serials,
        })

    # ─────────────────── WLANs ───────────────────

    def create_wlan(self, group: str, ssid: SSID, index: int,
                    cluster_name: str = "") -> None:
        name = ssid.display_name
        if ssid.captive_portal_url:
            # full_wlan has no external-captive-portal field this tool can
            # populate, so creating it here would provision a fully OPEN guest
            # network. Preflight (_check_captive_portal) blocks this earlier;
            # this is the last line of defence.
            raise ClassicCentralAPIError(
                f"SSID '{name}' uses an external captive portal "
                f"({ssid.captive_portal_url}), which Classic Central's full_wlan "
                "API cannot express — creating it would publish an OPEN guest "
                "network. Migrate this SSID to New Central, or build the portal "
                "by hand in Classic first.")
        if ssid.auth_type == AuthType.WEP:
            # preflight FAILs WEP before provisioning; last line of defence —
            # there is no AOS 10 WEP opmode to map to.
            raise ClassicCentralAPIError(
                f"SSID '{name}' uses WEP, which AOS 10 does not support. "
                "Re-key the source network to WPA2/WPA3 before migrating.")
        wlan = copy.deepcopy(_BASE_WLAN)
        wlan.update({
            "name": name,
            "essid": name,
            "index": index,
            "opmode": OPMODE_CLASSIC.get(ssid.auth_type, "wpa2-psk-aes"),
            "type": "employee",
            "vlan": _vlan_token(ssid),
            "hide_ssid": not ssid.broadcast,
            # a source WLAN that was administratively disabled stays disabled
            "disable_ssid": not getattr(ssid, "enabled", True),
            # _BASE_WLAN is verbatim from HPE's workflow YAML and DISABLES
            # 802.11n/ac/ax — shipping that caps every migrated client at
            # legacy ~54 Mbps rates. Real deployments leave these enabled
            # (the MCP's field-verified body omits the keys entirely).
            "high_throughput_disable": False,
            "very_high_throughput_disable": False,
            "high_efficiency_disable": False,
        })
        if ssid.auth_type in (AuthType.WPA2_PSK, AuthType.WPA3_SAE):
            # AOS 8 exports PSKs hashed/encrypted — pushing that verbatim
            # creates a WLAN with an unusable credential. Same placeholder
            # policy as the New Central client.
            usable = ssid.psk and not secret_looks_unusable(ssid.psk)
            wlan["wpa_passphrase"] = ssid.psk if usable else PSK_PLACEHOLDER
            # without this flag the handler keeps the previous/blank
            # passphrase and the WLAN comes up unjoinable
            wlan["wpa_passphrase_changed"] = True
        if ssid.auth_type in ENTERPRISE:
            wlan["access_type"] = "network_based"
            # full_wlan's auth_server1 references a single RADIUS server
            # OBJECT by name, and Classic Central exposes no REST API for
            # auth-server objects (verified against HPE's published API
            # reference and pycentral) — preflight FAILs enterprise SSIDs
            # on a Classic destination until the operator creates a server
            # with this exact name in the group by hand.
            wlan["auth_server1"] = ssid.auth_server_group or ""
        if ssid.auth_type == AuthType.MAC:
            # never emit a silently-open network for a MAC-auth SSID
            wlan["mac_authentication"] = True
            wlan["access_type"] = "network_based"
            wlan["auth_server1"] = ssid.auth_server_group or ""
        if cluster_name and ssid.forward_mode in (ForwardMode.TUNNEL, ForwardMode.SPLIT):
            # tunnel binding via cluster_name — verify in the Central UI after
            # provisioning (no verbatim reference example exists for this field)
            wlan["cluster_name"] = cluster_name
        # access_rule stays null, exactly as every one of HPE's verified
        # workflow samples sends it (open, psk AND enterprise). Sending a
        # populated rule makes the handler build a role that resolves a
        # server group, which only exists when auth_server1 is set — on an
        # SSID with no auth server that surfaced as KeyError 'server_group'.
        # access_type is already "unrestricted", so a permit-all rule adds
        # nothing.
        rule = None
        try:
            self._post_full_wlan(group, name, wlan, rule)
        except ClassicCentralAPIError as e:
            if not _is_duplicate(e):
                raise
            # a duplicate is idempotent reuse ONLY for an SSID the manifest
            # owns — swallowing it for a foreign same-name SSID means leaving
            # another administrator's WLAN in place and calling it migrated
            if self.manifest is not None:
                self.manifest.gate(KIND_SSID, name, exists=True)
        else:
            if self.manifest is not None:
                self.manifest.register(KIND_SSID, name, payload=wlan)

    def _post_full_wlan(self, group: str, name: str, wlan: dict,
                        rule: Optional[dict]) -> None:
        """POST a full_wlan body, healing the schema faults this API reports
        as opaque 500s.

        The handler indexes the payload directly, so it answers a wrong scalar
        type with ``Invalid type for JSON key: <key>`` and a key it expected
        but did not get with a bare KeyError repr (``"'server_group'"``).
        Neither says what to send, so retry once with the offending scalar
        flipped between str and int, and turn anything else into an
        actionable message instead of a raw vendor traceback fragment.
        """
        path = (f"/configuration/full_wlan/{quote(group, safe='')}/"
                f"{quote(name, safe='')}")

        def send(body: dict) -> None:
            # the body must be the JSON-stringified object under a "value" key
            self._post(path, json_body={
                "value": json.dumps({"wlan": body, "access_rule": rule})})

        try:
            send(wlan)
            return
        except ClassicCentralAPIError as e:
            if _is_duplicate(e):
                raise  # the caller decides whether reuse is legitimate
            key = _type_error_key(str(e))
            flipped = _flip_scalar(wlan.get(key)) if key else None
            if flipped is None:
                raise ClassicCentralAPIError(_explain_wlan_error(name, e)) from e
        retry = dict(wlan)
        retry[key] = flipped
        send(retry)
        # record what the tenant actually accepted so the manifest and any
        # later update send the same shape
        wlan[key] = flipped

    # ─────────────────── Firmware ───────────────────

    def list_supported_firmware(self, device_type: str = "IAP") -> list[str]:
        """Firmware versions this tenant can actually pin (best effort)."""
        for params in ({"device_type": device_type}, {"device_type": "IAP"}, {}):
            try:
                body = self._get("/firmware/v1/versions", params=params or None)
            except ClassicCentralAPIError:
                continue
            raw = (body.get("available_versions") or body.get("versions")
                   or body.get("firmware_versions") or body.get("items") or [])
            out = []
            for v in raw:
                if isinstance(v, str):
                    out.append(v)
                elif isinstance(v, dict):
                    name = (v.get("firmware_version") or v.get("version")
                            or v.get("name"))
                    if name:
                        out.append(str(name))
            if out:
                return out
        return []

    def set_firmware_compliance(self, group: str, version: str) -> None:
        try:
            self._apply_firmware_compliance(group, version)
        except ClassicCentralAPIError as e:
            if "does not exist" not in str(e).lower():
                raise
            # the tenant only accepts versions it actually publishes, and the
            # bare 400 names none of them — list them so the operator can pick
            available = self.list_supported_firmware()
            hint = (f" Versions available to this tenant: "
                    f"{', '.join(available[:12])}."
                    if available else
                    " The tool could not list this tenant's versions either, so "
                    "read the exact string from Central UI → Maintain → Firmware "
                    "(AOS 10 AP versions look like 10.7.x.x).")
            raise ClassicCentralAPIError(
                f"Firmware compliance {version} was rejected for group "
                f"'{group}': the tenant does not publish that version.{hint} "
                f"Set a published version in Step 2, or clear the firmware "
                f"field to skip compliance pinning — every other object in the "
                f"group provisions without it.") from e

    def _apply_firmware_compliance(self, group: str, version: str) -> None:
        body = {
            "device_type": "IAP",  # classic firmware enum — AOS10 APs are "IAP"
            "group": group,
            "firmware_compliance_version": version,
            "reboot": True,
            "allow_unsupported_version": False,
            "compliance_scheduled_at": 0,
        }
        try:
            self._post("/firmware/v2/upgrade/compliance_version", json_body=body)
        except ClassicCentralAPIError as e:
            # match the status code the client itself formats ("failed 404:"),
            # not any '404' that happens to appear in the error detail
            if not re.search(r"failed (404|405):", str(e)):
                raise
            try:
                self._post("/firmware/v1/upgrade/compliance_version",
                           json_body=body)
            except ClassicCentralAPIError as e2:
                if not re.search(r"failed (404|405):", str(e2)):
                    raise
                # some Classic tenants only serve the third form (verified in
                # HPE's own AOS 10 migration pipeline: device_type +
                # firmware_version + group, no scheduling fields)
                self._post("/firmware/v1/set-firmware-compliance", json_body={
                    "device_type": "IAP",
                    "firmware_version": version,
                    "group": group,
                })

    # ─────────────────── Monitoring ───────────────────

    def list_all_aps(self, group: Optional[str] = None) -> Optional[list[dict]]:
        params: dict = {"calculate_total": True}
        if group:
            params["group"] = group
        try:
            return self._paginate("/monitoring/v2/aps", items_key="aps",
                                  params=params, page_size=100)
        except ClassicCentralAPIError as e:
            # an expired token, or a listing this client knows is incomplete,
            # must surface as its own (actionable) message rather than being
            # flattened into the generic "check monitoring permissions" None
            msg = str(e)
            if ("401" in msg or "expired" in msg.lower()
                    or "ignored offset" in msg or "pagination cap" in msg):
                raise
            return None

    # ─────────────────── Full provision flow ───────────────────

    def provision(
        self,
        central_config: CentralConfig,
        ap_serials: dict[str, list[str]],
        ap_macs: Optional[dict[str, str]] = None,
        on_step: Optional[Callable[[str, bool], None]] = None,
        phase: str = "all",
    ) -> list[tuple[str, bool, str]]:
        """Classic AOS 10 provisioning in two phases (mirrors the New Central
        client):

        phase="config" — build configuration ONLY: inventory pre-add, site,
        groups, WLANs, firmware compliance. Nothing touches the APs, so Step
        3's "No APs are claimed, moved or rebooted" banner is actually true.

        phase="devices" — move APs into their groups and assign them to the
        site. Runs at the Step 4 cutover, after the APs are claimed in
        GreenLake. Fail-closed: groups/sites must already exist (the config
        phase created them) — a missing group aborts that group's move with a
        'run the config phase first' error instead of creating half a tenant.

        phase="all" (default) — both, in one pass (legacy single-shot).

        ap_macs maps serial → wired MAC for the inventory pre-add (skipped
        for serials without a MAC)."""
        if phase not in ("config", "devices", "all"):
            raise ValueError(f"phase must be 'config', 'devices' or 'all', "
                             f"got {phase!r}")
        do_config = phase in ("config", "all")
        do_devices = phase in ("devices", "all")
        results: list[tuple[str, bool, str]] = []
        cc = central_config
        ap_macs = ap_macs or {}

        def step(label: str, fn) -> bool:
            try:
                fn()
                results.append((label, True, ""))
                if on_step:
                    on_step(label, True)
                return True
            except Exception as e:
                results.append((label, False, str(e)))
                if on_step:
                    on_step(label, False)
                return False

        keep_gws = bool(cc.gw_cluster_name)

        if do_config:
            # 1. inventory pre-add (serial+MAC pairs we have)
            all_serials = sorted({s for ss in ap_serials.values() for s in ss})
            inv = [{"serial": s, "mac": ap_macs[s]} for s in all_serials
                   if ap_macs.get(s)]
            if inv:
                step(f"Add {len(inv)} devices to classic inventory",
                     lambda: self.add_to_inventory(inv))

        # 2. sites — created in the config phase; resolved from the tenant in
        #    the devices phase (APs are assigned to them at cutover)
        site_ids: dict[str, int] = {}
        if do_config:
            for site_name in cc.sites:
                step(f"Create site: {site_name}",
                     lambda s=site_name: site_ids.update({s: self.create_site(
                         s, cc.site_address, cc.site_city, cc.site_state,
                         cc.site_country, cc.site_zipcode,
                         lab_mode=getattr(cc, "lab_mode", False))}))
        elif do_devices:
            try:
                for site in self.list_sites(refresh=True):
                    if site.get("site_name") in cc.sites:
                        site_ids[site["site_name"]] = int(site.get("site_id"))
            except Exception as e:
                # a failed site read must not read as "no sites" — every group
                # would silently skip its site assignment mid-cutover
                results.append(("Resolve sites in tenant", False, str(e)))
                if on_step:
                    on_step("Resolve sites in tenant", False)

        for group_cfg in cc.groups:
            # serials are keyed by the AOS 8 source group name, not the
            # (possibly renamed) Central group name; merged generic groups
            # contribute serials from every folded source group
            _srcs = ([group_cfg.source_group or group_cfg.name]
                     + list(group_cfg.extra_source_groups))
            serials = [s for src in _srcs for s in ap_serials.get(src, [])]

            if do_config:
                if not step(f"Create AOS10 group: {group_cfg.name}"
                            + (" (APs+Gateways)" if keep_gws else " (APs)"),
                            lambda g=group_cfg: self.create_group(g.name, keep_gws)):
                    continue

                seen_essids: set[str] = set()
                idx = 0
                for ssid in group_cfg.ssids:
                    if ssid.display_name in seen_essids:
                        results.append((
                            f"SSID {ssid.display_name} → {group_cfg.name} — SKIPPED "
                            "(duplicate ESSID in group)", True, ""))
                        continue
                    seen_essids.add(ssid.display_name)
                    idx += 1
                    step(f"Create WLAN: {ssid.display_name} → {group_cfg.name}",
                         lambda s=ssid, g=group_cfg, i=idx: self.create_wlan(
                             g.name, s, i, cc.gw_cluster_name or ""))

                step(f"Set firmware compliance {group_cfg.firmware_version} → {group_cfg.name}",
                     lambda g=group_cfg: self.set_firmware_compliance(
                         g.name, g.firmware_version))

            if do_devices and serials:
                # fail closed: the group must already exist (config phase).
                # Moving APs into a group this run didn't verify would strand
                # them in a half-built tenant.
                def _move(g=group_cfg, s=serials):
                    if g.name not in self.list_group_names():
                        raise ClassicCentralAPIError(
                            f"Group '{g.name}' not found in the tenant — run "
                            "the config phase (Step 3) first; refusing to move "
                            "APs into a group this run cannot verify.")
                    try:
                        self.move_devices(g.name, s)
                    except ClassicCentralAPIError:
                        # serials without a MAC may not exist in inventory —
                        # retry with the inventory-added subset, then surface
                        # exactly which serials were left behind
                        subset = [x for x in s if ap_macs.get(x)]
                        if not subset or subset == s:
                            raise
                        self.move_devices(g.name, subset)
                        skipped = sorted(set(s) - set(subset))
                        raise ClassicCentralAPIError(
                            f"Moved {len(subset)} APs; {len(skipped)} serial(s) "
                            f"without a MAC weren't in inventory and were "
                            f"skipped: {', '.join(skipped[:10])}")
                step(f"Move {len(serials)} APs to group: {group_cfg.name}", _move)

            if do_devices and serials and group_cfg.site_name in site_ids:
                step(f"Assign {len(serials)} APs to site: {group_cfg.site_name}",
                     lambda s=serials, sn=group_cfg.site_name:
                         self.associate_site(site_ids[sn], s))

        # manual follow-ups the classic API can't automate — config-phase
        # output, so the cutover gate can track them as outstanding work
        if do_config:
            followups = []
            if any(s.auth_type in ENTERPRISE for g in cc.groups for s in g.ssids):
                followups.append("create the RADIUS auth-server(s) in each group "
                                 "(Group → Devices → Config → Security) — enterprise "
                                 "WLANs reference them by name")
            if keep_gws:
                followups.append(f"gateways auto-cluster when moved into the group — "
                                 f"verify tunnel SSIDs bind to cluster "
                                 f"'{cc.gw_cluster_name}' in the group WLAN config")
            for f in followups:
                results.append((f"MANUAL FOLLOW-UP: {f}", True, ""))
                if on_step:
                    on_step(f"MANUAL FOLLOW-UP: {f}", True)

        return results
