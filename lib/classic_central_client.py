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
import time
from typing import Callable, Optional
from urllib.parse import quote

import requests

from .models import AuthType, CentralConfig, ForwardMode, SSID

OPMODE_CLASSIC = {
    AuthType.OPEN: "opensystem",
    AuthType.MAC: "opensystem",
    AuthType.WPA2_PSK: "wpa2-psk-aes",
    AuthType.WPA3_SAE: "wpa3-sae-aes",
    AuthType.WPA2_ENTERPRISE: "wpa2-aes",
    AuthType.WPA3_ENTERPRISE: "wpa3-aes-ccm-128",
}

ENTERPRISE = (AuthType.WPA2_ENTERPRISE, AuthType.WPA3_ENTERPRISE)

# Verbatim full_wlan field set from HPE's central-python-workflows
# (Classic-Central/wlan_config/configurations/*.yaml) — the API expects the
# complete flat object; per-SSID fields are overridden in create_wlan().
_BASE_WLAN = {
    "access_type": "unrestricted", "air_time_limit": "", "air_time_limit_cb": False,
    "auth_server1": "", "auth_server2": "", "auth_survivability": False,
    "bandwidth_limit": "", "bandwidth_limit_cb": False, "blacklist": True,
    "broadcast_filter": "arp", "called_station_id_deli": 0,
    "called_station_id_incl_ssid": False, "called_station_id_type": "macaddr",
    "captive_exclude": [], "captive_portal": "disable",
    "captive_portal_proxy_ip": "", "captive_portal_proxy_port": "",
    "captive_profile_name": "", "cloud_guest": False, "cluster_name": "",
    "content_filtering": False, "deny_intra_vlan_traffic": False,
    "disable_ssid": False, "dmo_channel_util_threshold": 90, "dot11k": False,
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
    "oos_def": "vpn-down", "oos_name": "none", "oos_time": 30,
    "opmode": "wpa2-psk-aes", "opmode_transition_disable": True,
    "per_user_limit": None, "per_user_limit_cb": False,
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
    "work_without_uplink": False, "wpa_passphrase": "", "zone": "",
    "hotspot_profile": "",
}

_BASE_ACCESS_RULE = {
    "name": "", "action": "allow", "app_rf_mv_info": "", "blacklist": False,
    "classify_media": False, "disable_scanning": False, "dot1p_priority": "",
    "eport": "any", "ipaddr": "any", "log": False, "match": "match",
    "nat_ip": "", "nat_port": 0, "netmask": "any", "protocol": "any",
    "protocol_id": "", "service_name": "", "service_type": "network",
    "source": "default", "sport": "any", "throttle_downstream": "",
    "throttle_upstream": "", "time_range": "", "tos": "", "vlan": 0,
}


class ClassicCentralAPIError(Exception):
    pass


def _is_duplicate(err: Exception) -> bool:
    msg = str(err).lower()
    return "already exists" in msg or "duplicate" in msg



def _normalize_base(url: str) -> str:
    """Ensure the base URL has a scheme and no trailing slash. Operators often
    paste a bare host (internal.api.central.arubanetworks.com) — default to
    https:// so requests don't fail with 'No scheme supplied'."""
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


class ClassicCentralClient:
    def __init__(self, base_url: str, access_token: str,
                 client_id: str = "", client_secret: str = "",
                 refresh_token: str = "", timeout: int = 30):
        self.base = _normalize_base(base_url)
        self.access_token = access_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token  # rotates — read back after runs
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {access_token}"})
        # per-instance caches — a client is constructed per run, so these are
        # naturally fresh; created objects are appended locally
        self._group_names_cache: Optional[list[str]] = None
        self._sites_cache: Optional[list[dict]] = None

    # ─────────────────── Auth / HTTP ───────────────────

    def refresh(self) -> bool:
        """Refresh the access token. The refresh token is single-use and
        rotates — self.refresh_token holds the NEW one afterwards."""
        if not (self.client_id and self.client_secret and self.refresh_token):
            return False
        resp = self.session.post(
            f"{self.base}/oauth2/token",
            params={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=self.timeout,
        )
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
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})
        return True

    def _request(self, method: str, path: str, json_body=None,
                 params: Optional[dict] = None, _retried: bool = False) -> dict:
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
        if resp.status_code == 401 and not _retried and self.refresh():
            return self._request(method, path, json_body, params, _retried=True)
        if resp.status_code == 429 and not _retried:
            time.sleep(min(int(resp.headers.get("Retry-After", 10)), 60))
            return self._request(method, path, json_body, params, _retried=True)
        if resp.status_code == 403 and "wlan" in path:
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
        except Exception:
            return {}
        return {"items": body} if isinstance(body, list) else body

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)

    def _post(self, path, json_body=None, params=None):
        return self._request("POST", path, json_body=json_body, params=params)

    # ─────────────────── Groups ───────────────────

    def list_group_names(self, refresh: bool = False) -> list[str]:
        if self._group_names_cache is not None and not refresh:
            return self._group_names_cache
        names, offset = [], 0
        while True:
            data = self._get("/configuration/v2/groups",
                             params={"offset": offset, "limit": 20})
            # response "data"/"output": a list of single-element name lists
            raw = data.get("data") or data.get("output") or data.get("items") or []
            page = [g for sub in raw for g in (sub if isinstance(sub, list) else [sub])]
            names.extend(n for n in page if n and n != "unprovisioned")
            total = data.get("total", 0)
            offset += 20
            if len(page) < 20 or (total and offset >= total):
                self._group_names_cache = names
                return names

    def create_group(self, name: str, include_gateways: bool = False,
                     new_central: bool = False) -> str:
        """Idempotent AOS 10 UI-group create; verifies Architecture readback.

        new_central=True sets the 'Allow New Central to overwrite' flag so the
        group becomes New-Central-managed and appears in New Central's
        device-collections — required for the hybrid path where SSIDs/VLANs are
        then configured on the New Central side. False = pure classic group."""
        if name in self.list_group_names():
            return name
        props = {
            "AllowedDevTypes": ["AccessPoints"] + (["Gateways"] if include_gateways else []),
            "Architecture": "AOS10",
            "ApNetworkRole": "Standard",
            "NewCentral": bool(new_central),
        }
        if include_gateways:
            # "BranchGateway" is the only documented GwNetworkRole group
            # property value in HPE's onboarding workflows/docs (the "WLAN
            # gateway" term is a runtime cluster role, not this enum)
            props["GwNetworkRole"] = "BranchGateway"
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
        wrong_arch = ""
        try:
            check = self._get("/configuration/v1/groups/properties",
                              params={"groups": name})
            for item in check.get("data", check.get("items", [])):
                if item.get("group") == name:
                    arch = (item.get("properties") or {}).get("Architecture", "")
                    if arch and arch != "AOS10":
                        wrong_arch = arch
        except Exception:
            pass
        if wrong_arch:
            raise ClassicCentralAPIError(
                f"Group '{name}' was created but Architecture reads back as "
                f"'{wrong_arch}', not AOS10 — delete it in Central and check "
                "the tenant supports AOS10 groups.")
        if self._group_names_cache is not None:
            self._group_names_cache.append(name)
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
            if not _is_duplicate(e) and "exist" not in str(e).lower():
                raise

    def move_devices(self, group: str, serials: list[str]) -> None:
        if not serials:
            return
        self._post("/configuration/v1/devices/move",
                   json_body={"group": group, "serials": serials})

    def delete_group(self, name: str) -> None:
        self._request("DELETE", f"/configuration/v1/groups/{quote(name, safe='')}")
        if self._group_names_cache is not None and name in self._group_names_cache:
            self._group_names_cache.remove(name)

    # ─────────────────── Sites ───────────────────

    def list_sites(self, refresh: bool = False) -> list[dict]:
        if self._sites_cache is not None and not refresh:
            return self._sites_cache
        sites, offset = [], 0
        while True:
            data = self._get("/central/v2/sites",
                             params={"offset": offset, "limit": 100,
                                     "calculate_total": True})
            page = data.get("sites", [])
            sites.extend(page)
            if len(page) < 100:
                self._sites_cache = sites
                return sites
            offset += 100

    def create_site(self, name: str, address: str = "", city: str = "",
                    state: str = "", country: str = "", zipcode: str = "") -> int:
        for site in self.list_sites():
            if site.get("site_name") == name:
                return int(site.get("site_id"))
        body: dict = {"site_name": name}
        if any((address, city, state, country, zipcode)):
            body["site_address"] = {k: v for k, v in {
                "address": address, "city": city, "state": state,
                "country": country, "zipcode": zipcode,
            }.items() if v}
        else:
            # site_address and geolocation are mutually exclusive but one is
            # required — default to a zeroed geolocation when no address given
            body["geolocation"] = {"latitude": "0.0", "longitude": "0.0"}
        resp = self._post("/central/v2/sites", json_body=body)
        sid = resp.get("site_id")
        if sid is None:
            for site in self.list_sites():
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
        wlan = copy.deepcopy(_BASE_WLAN)
        wlan.update({
            "name": name,
            "essid": name,
            "index": index,
            "opmode": OPMODE_CLASSIC.get(ssid.auth_type, "wpa2-psk-aes"),
            "type": "employee",
            "vlan": str(ssid.vlan) if ssid.vlan else "",
            "hide_ssid": not ssid.broadcast,
        })
        if ssid.auth_type in (AuthType.WPA2_PSK, AuthType.WPA3_SAE):
            wlan["wpa_passphrase"] = ssid.psk or ""
        if ssid.auth_type in ENTERPRISE:
            wlan["access_type"] = "network_based"
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
        rule = copy.deepcopy(_BASE_ACCESS_RULE)
        rule["name"] = name
        # the body must be the JSON-stringified object under a "value" key
        payload = {"value": json.dumps({"wlan": wlan, "access_rule": rule})}
        try:
            self._post(f"/configuration/full_wlan/{quote(group, safe='')}/"
                       f"{quote(name, safe='')}", json_body=payload)
        except ClassicCentralAPIError as e:
            if not _is_duplicate(e):
                raise

    # ─────────────────── Firmware ───────────────────

    def set_firmware_compliance(self, group: str, version: str) -> None:
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
            if "404" not in str(e) and "405" not in str(e):
                raise
            self._post("/firmware/v1/upgrade/compliance_version", json_body=body)

    # ─────────────────── Monitoring ───────────────────

    def list_all_aps(self, group: Optional[str] = None) -> Optional[list[dict]]:
        try:
            aps, offset = [], 0
            params: dict = {"limit": 100, "calculate_total": True}
            if group:
                params["group"] = group
            while True:
                params["offset"] = offset
                data = self._get("/monitoring/v2/aps", params=params)
                page = data.get("aps", [])
                aps.extend(page)
                if len(page) < 100:
                    return aps
                offset += 100
        except ClassicCentralAPIError:
            return None

    # ─────────────────── Full provision flow ───────────────────

    def provision(
        self,
        central_config: CentralConfig,
        ap_serials: dict[str, list[str]],
        ap_macs: Optional[dict[str, str]] = None,
        on_step: Optional[Callable[[str, bool], None]] = None,
    ) -> list[tuple[str, bool, str]]:
        """Classic AOS 10 provisioning. ap_macs maps serial → wired MAC for
        the inventory pre-add (skipped for serials without a MAC)."""
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

        # 1. inventory pre-add (serial+MAC pairs we have)
        all_serials = sorted({s for ss in ap_serials.values() for s in ss})
        inv = [{"serial": s, "mac": ap_macs[s]} for s in all_serials if ap_macs.get(s)]
        if inv:
            step(f"Add {len(inv)} devices to classic inventory",
                 lambda: self.add_to_inventory(inv))

        # 2. site
        site_ids: dict[str, int] = {}
        for site_name in cc.sites:
            step(f"Create site: {site_name}",
                 lambda s=site_name: site_ids.update({s: self.create_site(
                     s, cc.site_address, cc.site_city, cc.site_state,
                     cc.site_country, cc.site_zipcode)}))

        keep_gws = bool(cc.gw_cluster_name)

        for group_cfg in cc.groups:
            serials = ap_serials.get(group_cfg.name, [])
            if not step(f"Create AOS10 group: {group_cfg.name}"
                        + (" (APs+Gateways)" if keep_gws else " (APs)"),
                        lambda g=group_cfg: self.create_group(g.name, keep_gws)):
                continue

            if serials:
                def _move(g=group_cfg, s=serials):
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

            if serials and group_cfg.site_name in site_ids:
                step(f"Assign {len(serials)} APs to site: {group_cfg.site_name}",
                     lambda s=serials, sn=group_cfg.site_name:
                         self.associate_site(site_ids[sn], s))

        # manual follow-ups the classic API can't automate
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
