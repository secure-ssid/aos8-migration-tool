"""
HPE Aruba Networking Central (New Central / GreenLake) REST API client.

Targets the New Central API surface:
  - Token:      POST https://sso.common.cloud.hpe.com/as/token.oauth2
                (GreenLake API client credentials, client_credentials grant)
  - Base URL:   regional New Central base, e.g.
                https://us4.api.central.arubanetworks.com
  - Config:     /network-config/v1 + /network-config/v1alpha1
                (library profiles bound to scopes via scope-maps)
  - Monitoring: /network-monitoring/v1

Call patterns mirror the working centralmcp pipeline. Errors are NEVER
swallowed here — every failure raises CentralAPIError so the provisioning
orchestrator records and displays it.
"""
import time
from typing import Callable, Optional
from urllib.parse import quote

import requests

from .models import CentralConfig, ForwardMode, AuthType, RadiusServer, SSID

TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"

# AuthType → New Central wlan-ssid opmode. Verified against the WLAN
# OpenAPI spec enum and live tenant SSIDs (2026-06). 802.1X SSIDs still
# need their auth server attached in Central — surfaced in preflight.
OPMODE = {
    AuthType.OPEN: "OPEN",
    AuthType.MAC: "OPEN",
    AuthType.WPA2_PSK: "WPA2_PERSONAL",
    AuthType.WPA3_SAE: "WPA3_SAE",
    AuthType.WPA2_ENTERPRISE: "WPA2_ENTERPRISE",
    AuthType.WPA3_ENTERPRISE: "WPA3_ENTERPRISE_CCM_128",
}

ENTERPRISE_AUTH = (AuthType.WPA2_ENTERPRISE, AuthType.WPA3_ENTERPRISE)


class CentralAPIError(Exception):
    pass


def _is_duplicate(err: Exception) -> bool:
    msg = str(err).lower()
    return "already exists" in msg or "duplicate" in msg


def _timezone(timezone_id: str) -> dict:
    """Build the {rawOffset, timezoneId, timezoneName} object New Central's
    site-create requires — computed from a real IANA zone exactly the way
    pycentral's Site class does (offset in ms, tzname abbreviation)."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone_id)
    except Exception:
        from zoneinfo import ZoneInfo
        timezone_id = "UTC"
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    return {
        "rawOffset": int(now.utcoffset().total_seconds() * 1000),
        "timezoneId": timezone_id,
        "timezoneName": now.tzname(),
    }


# New Central validates country against ISO 3166 short names — a bare code
# like "US" is risky, so normalize the common ones to the canonical name.
_COUNTRY_NORM = {
    "us": "United States", "usa": "United States", "u.s.": "United States",
    "uk": "United Kingdom", "gb": "United Kingdom",
    "ca": "Canada", "au": "Australia", "de": "Germany",
}


def _norm_country(country: str) -> str:
    c = (country or "").strip()
    return _COUNTRY_NORM.get(c.lower(), c) if c else "United States"



def _normalize_base(url: str) -> str:
    """Ensure the base URL has a scheme and no trailing slash. Operators often
    paste a bare host (internal.api.central.arubanetworks.com) — default to
    https:// so requests don't fail with 'No scheme supplied'."""
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


class CentralClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str,
                 timeout: int = 30):
        self.base = _normalize_base(base_url)
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.token: Optional[str] = None
        self.session = requests.Session()
        # per-provision-run caches: avoid O(n) re-listing and duplicate
        # role/policy ensure-sequences (reset at the start of provision())
        self._groups_cache: Optional[list[dict]] = None
        self._sites_cache: Optional[list[dict]] = None
        self._ensured_roles: set[str] = set()
        self._ensured_policies: set[str] = set()

    # ─────────────────── Auth / HTTP ───────────────────

    def authenticate(self) -> bool:
        resp = self.session.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        if not resp.ok:
            raise CentralAPIError(
                f"Token request failed {resp.status_code}: {resp.text[:300]}")
        self.token = resp.json()["access_token"]
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})
        return True

    def _request(self, method: str, path: str, json: Optional[dict] = None,
                 params: Optional[dict] = None, _retried: bool = False) -> dict:
        try:
            resp = self.session.request(
                method, f"{self.base}{path}", json=json, params=params,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout:
            raise CentralAPIError(f"{method} {path}: request timed out after {self.timeout}s")
        except requests.exceptions.ConnectionError as e:
            raise CentralAPIError(f"{method} {path}: connection failed — check the base URL "
                                  f"and network reachability ({type(e).__name__})")
        if resp.status_code == 401 and not _retried:
            self.authenticate()
            return self._request(method, path, json, params, _retried=True)
        if resp.status_code == 429 and not _retried:
            wait = min(int(resp.headers.get("Retry-After", 30)), 120)
            time.sleep(wait)
            return self._request(method, path, json, params, _retried=True)
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:300]
            raise CentralAPIError(f"{method} {path} failed {resp.status_code}: {detail}")
        if not resp.content:
            return {}
        body = resp.json()
        return {"items": body} if isinstance(body, list) else body

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)

    def _post(self, path, json=None, params=None):
        return self._request("POST", path, json=json, params=params)

    def _put(self, path, json=None, params=None):
        return self._request("PUT", path, json=json, params=params)

    def _patch(self, path, json=None, params=None):
        return self._request("PATCH", path, json=json, params=params)

    def _paginate(self, path: str, items_key: Optional[str] = None,
                  params: Optional[dict] = None, page_size: int = 200,
                  max_pages: int = 50) -> list:
        items, offset = [], 0
        params = dict(params or {})
        first_of_prev_page = object()
        for _ in range(max_pages):
            params.update({"limit": page_size, "offset": offset})
            data = self._get(path, params=params)
            page = data.get(items_key) if items_key else None
            if page is None:
                page = (data.get("items") or data.get("sites")
                        or data.get("data") or [])
            if not isinstance(page, list):
                page = [page] if page else []
            # guard against endpoints that ignore offset and echo the same page
            if page and page[0] == first_of_prev_page:
                return items
            first_of_prev_page = page[0] if page else None
            items.extend(page)
            if len(page) < page_size:
                return items
            offset += page_size
        return items

    # ─────────────────── Scopes ───────────────────

    def get_global_scope_id(self) -> str:
        data = self._get("/network-config/v1/scope-maps")
        entries = data.get("scope-map", [])
        for entry in entries:
            if entry.get("persona") == "SERVICE_PERSONA":
                return str(entry.get("scope-id"))
        # fallback: most frequent scope-id across the map
        counts: dict[str, int] = {}
        for entry in entries:
            sid = str(entry.get("scope-id", ""))
            if sid:
                counts[sid] = counts.get(sid, 0) + 1
        if counts:
            return max(counts, key=counts.get)
        raise CentralAPIError("Could not determine global scope id from scope-maps")

    def map_to_scope(self, resource: str, scope_id: str, persona: str) -> None:
        try:
            self._post("/network-config/v1/scope-maps", json={
                "scope-map": [{
                    "scope-name": str(scope_id),
                    "scope-id": int(scope_id),
                    "persona": persona,
                    "resource": resource,
                }],
            })
        except CentralAPIError as e:
            # duplicate scope-maps come back as errors — that's idempotent success
            if "already exists" not in str(e).lower() and "duplicate" not in str(e).lower():
                raise

    # ─────────────────── Sites ───────────────────

    @staticmethod
    def _site_name(site: dict) -> str:
        return site.get("scopeName") or site.get("siteName") or site.get("name", "")

    @staticmethod
    def _site_id(site: dict) -> Optional[str]:
        sid = site.get("scopeId") or site.get("siteId") or site.get("id")
        return str(sid) if sid is not None else None

    def list_sites(self, refresh: bool = False) -> list[dict]:
        # Read from the config surface (works across tenants incl. hybrid).
        # v1alpha1 is where creates land, so prefer it, then v1, then the
        # monitoring route. These endpoints ignore limit/offset — fetch whole.
        if self._sites_cache is None or refresh:
            self._sites_cache = []
            for path in ("/network-config/v1alpha1/sites", "/network-config/v1/sites"):
                try:
                    data = self._get(path)
                    self._sites_cache = data.get("items") or data.get("sites") or []
                    break
                except CentralAPIError:
                    continue
            else:
                try:
                    self._sites_cache = self._paginate("/network-monitoring/v1/sites",
                                                       page_size=100)
                except CentralAPIError:
                    self._sites_cache = []
        return self._sites_cache

    def create_site(self, name: str, address: str = "", city: str = "",
                    state: str = "", country: str = "", zipcode: str = "",
                    timezone_id: str = "UTC") -> str:
        """Idempotent: returns the existing site's id when the name matches.

        Body shape from HPE's shipping New Central workflows (wpa3-psk-overlay /
        open-ssid-overlay Postman): POST /network-config/v1alpha1/sites with a
        REQUIRED timezone object. The address block is only sent when a full
        street address is given — partial/ISO-invalid address fields (e.g. a
        bare country code) are themselves a common 400 cause."""
        for site in self.list_sites():
            if self._site_name(site) == name:
                return self._site_id(site) or name
        # New Central requires the FULL geographic body (pycentral Site:
        # name+address+city+state+country+zipcode+timezone are all required and
        # country/state are ISO-3166 validated). Fall back to valid placeholders
        # for a lab/test site when the operator didn't supply an address.
        body: dict = {
            "name": name,
            "address": address or "1 Lab Street",
            "city": city or "San Jose",
            "state": state or "California",
            "country": _norm_country(country),
            "zipcode": zipcode or "95002",
            "timezone": _timezone(timezone_id),
        }
        # v1alpha1 is the create route HPE's workflows use; fall back to v1 /
        # monitoring on a routing (404) error only.
        resp = None
        errs = []
        for path in ("/network-config/v1alpha1/sites", "/network-config/v1/sites",
                     "/network-monitoring/v1/sites"):
            try:
                resp = self._post(path, json=body)
                break
            except CentralAPIError as e:
                errs.append(str(e))
                if "404" in str(e) or "not found" in str(e).lower():
                    continue
                raise
        if resp is None:
            raise CentralAPIError("Site create failed: " + " | ".join(errs))
        self._sites_cache = None  # invalidate after create
        site_id = resp.get("scopeId") or resp.get("siteId") or resp.get("id")
        if site_id:
            return str(site_id)
        # POST bodies don't always echo the id — re-list to resolve it
        for site in self.list_sites(refresh=True):
            if self._site_name(site) == name:
                return self._site_id(site) or name
        return name

    def assign_devices_to_site(self, site_id: str, serials: list[str],
                               device_type: str = "AP") -> None:
        """Site association still routes through classic-style endpoints on
        some tenants — try the New Central path first, then fall back."""
        if not serials:
            return
        errors = []
        candidates = [
            ("POST", f"/network-monitoring/v1/sites/{site_id}/devices",
             lambda: {"serials": serials}),
            # classic fallback needs a numeric site id — skipped if not numeric
            ("POST", "/central/v2/sites/associate",
             lambda: {"site_id": int(site_id), "device_id": serials,
                      "device_type": device_type}),
        ]
        for method, path, make_body in candidates:
            try:
                self._request(method, path, json=make_body())
                return
            except (CentralAPIError, ValueError) as e:
                errors.append(str(e))
        raise CentralAPIError("Site assignment failed: " + " | ".join(errors))

    # ─────────────────── Device groups ───────────────────

    def list_device_groups(self, refresh: bool = False) -> list[dict]:
        if self._groups_cache is None or refresh:
            self._groups_cache = self._paginate("/network-config/v1/device-groups",
                                                page_size=100)
        return self._groups_cache

    def create_device_group(self, name: str, serials: Optional[list[str]] = None) -> str:
        """Idempotent create; returns the group's scope id."""
        for grp in self.list_device_groups():
            if grp.get("scopeName") == name:
                scope_id = str(grp.get("scopeId"))
                if serials:
                    self.add_devices_to_group(scope_id, serials)
                return scope_id
        try:
            if serials:
                self._post("/network-config/v1/device-groups-create-and-add-devices",
                           json={"scopeName": name, "devices": serials})
            else:
                self._post("/network-config/v1/device-groups", json={"scopeName": name})
        except CentralAPIError as e:
            if "HYBRID_CLUSTER" in str(e) or "API_ACCESS_RESTRICTED" in str(e):
                raise CentralAPIError(
                    "This tenant is a HYBRID CLUSTER — New Central blocks device-group "
                    "creation here (API_ACCESS_RESTRICTED_IN_HYBRID_CLUSTER). Add a "
                    "Classic API Gateway token in Step 1 → 'Hybrid cluster? Classic API "
                    "Gateway' (base URL + token): group create/move will route through "
                    "Classic while SSIDs/VLANs stay on New Central. (You don't need to "
                    "switch the destination to Classic.)"
                ) from e
            raise
        for grp in self.list_device_groups(refresh=True):
            if grp.get("scopeName") == name:
                return str(grp.get("scopeId"))
        raise CentralAPIError(f"Group '{name}' was created but not found on re-list")

    def add_devices_to_group(self, scope_id: str, serials: list[str]) -> None:
        if not serials:
            return
        self._post("/network-config/v1/device-groups-add-devices",
                   json={"desScopeId": str(scope_id), "devices": serials})

    def assign_persona(self, serials: list[str], device_function: str = "CAMPUS_AP") -> None:
        """Explicit persona assignment, mirroring HPE's onboarding workflow."""
        if not serials:
            return
        try:
            # body key is "device-id" (a LIST of serials), NOT "serial" —
            # verified against HPE's device-onboarding workflow + the
            # persona-assignment OpenAPI spec
            self._post("/network-config/v1alpha1/persona-assignment", json={
                "persona-device-list": [
                    {"device-function": device_function, "device-id": list(serials)}
                ],
            })
        except CentralAPIError as e:
            if not _is_duplicate(e):
                raise

    @staticmethod
    def _swallow_duplicate(fn) -> bool:
        """Run fn(); returns True if it succeeded or the object already existed."""
        try:
            fn()
            return True
        except CentralAPIError as e:
            if _is_duplicate(e):
                return True
            raise

    # ─────────────────── VLANs ───────────────────

    def create_vlan(self, vlan_id: int, name: str, scope_id: str,
                    persona: str = "CAMPUS_AP") -> None:
        body = {"vlan": vlan_id, "name": name or f"vlan_{vlan_id}", "enable": True}
        try:
            self._post(f"/network-config/v1/layer2-vlan/{vlan_id}", json=body)
        except CentralAPIError as e:
            if "duplicate" not in str(e).lower() and "exists" not in str(e).lower():
                raise
            self._put(f"/network-config/v1/layer2-vlan/{vlan_id}", json=body)
        self.map_to_scope(f"layer2-vlan/{vlan_id}", scope_id, persona)

    # ─────────────────── Roles / policies (overlay prereqs) ───────────────────

    def _ensure_role(self, name: str, global_scope: str, group_scope: str) -> None:
        cache_key = f"{name}|{group_scope}"
        if cache_key in self._ensured_roles:
            return  # already created + mapped during this run
        encoded = quote(name, safe="")
        try:
            self._post(f"/network-config/v1/roles/{encoded}",
                       json={"name": name, "utf8": True})
        except CentralAPIError as e:
            if not _is_duplicate(e):
                raise
        for scope, persona in ((global_scope, "CAMPUS_AP"),
                               (global_scope, "MOBILITY_GW"),
                               (group_scope, "MOBILITY_GW")):
            self.map_to_scope(f"roles/{name}", scope, persona)
            self.map_to_scope(f"role-gpids/{name}", scope, persona)
        self._ensured_roles.add(cache_key)

    def _ensure_allow_all_policy(self, name: str, role: str, global_scope: str) -> None:
        if name in self._ensured_policies:
            return
        encoded = quote(name, safe="")
        try:
            self._post(f"/network-config/v1alpha1/policies/{encoded}", json={
                "name": name,
                "type": "POLICY_TYPE_SECURITY",
                "security-policy": {
                    "type": "SECURITY_POLICY_TYPE_DEFAULT",
                    "policy-rule": [{
                        "position": 1,
                        "description": "Allow All",
                        "condition": {
                            "type": "CONDITION_DEFAULT",
                            "rule-type": "RULE_ANY",
                            "source": {"type": "ADDRESS_ROLE", "role": role},
                            "destination": {"type": "ADDRESS_ANY"},
                        },
                        "action": {"type": "ACTION_ALLOW"},
                    }],
                },
            })
        except CentralAPIError as e:
            if not _is_duplicate(e):
                raise
        try:
            self._patch("/network-config/v1alpha1/policy-groups", json={
                "policy-group": {"policy-group-list": [{"name": name, "position": 3}]},
            })
        except CentralAPIError as e:
            if not _is_duplicate(e):
                raise
        for persona in ("CAMPUS_AP", "MOBILITY_GW"):
            self.map_to_scope(f"policies/{name}", global_scope, persona)
        self._ensured_policies.add(name)

    # ─────────────────── SSIDs ───────────────────

    def _ssid_body(self, ssid: SSID, forward_mode: str) -> dict:
        body = {
            "ssid": ssid.display_name,
            "enable": True,
            "forward-mode": forward_mode,
            "opmode": OPMODE.get(ssid.auth_type, "WPA2_PERSONAL"),
            "vlan-selector": "VLAN_RANGES",
            "vlan-id-range": [str(ssid.vlan)],
            "rf-band": "BAND_ALL",
            "essid": {"use-alias": False, "name": ssid.display_name},
            "hide-ssid": not ssid.broadcast,
            "wpa3-transition-mode-enable": ssid.auth_type in
                (AuthType.WPA2_PSK, AuthType.WPA3_SAE),
        }
        if ssid.auth_type in (AuthType.WPA2_PSK, AuthType.WPA3_SAE) and ssid.psk:
            body["personal-security"] = {
                "passphrase-format": "STRING",
                "wpa-passphrase": ssid.psk,
            }
        if ssid.auth_type in ENTERPRISE_AUTH:
            body["dot1x"] = True
        return body

    def create_underlay_ssid(self, ssid: SSID, scope_id: str) -> None:
        name = ssid.display_name
        encoded = quote(name, safe="")
        # Duplicate = the SSID object already exists (idempotent re-run, or the
        # same ESSID bound to a second AP group) — proceed to scope-map it so
        # this group still gets the WLAN.
        self._swallow_duplicate(lambda: self._post(
            f"/network-config/v1/wlan-ssids/{encoded}",
            json=self._ssid_body(ssid, "FORWARD_MODE_BRIDGE")))
        self.map_to_scope(f"wlan-ssids/{name}", scope_id, "CAMPUS_AP")

    def create_overlay_ssid(self, ssid: SSID, group_scope: str, global_scope: str,
                            cluster_name: str, cluster_scope_id: str) -> None:
        name = ssid.display_name
        encoded = quote(name, safe="")

        self._ensure_role(name, global_scope, group_scope)
        self._ensure_allow_all_policy(name, name, global_scope)

        body = self._ssid_body(ssid, "FORWARD_MODE_L2")
        body.update({
            "type": "EMPLOYEE",
            "default-role": name,
            "out-of-service": "TUNNEL_DOWN",
            "cluster-preemption": False,
        })
        # Duplicates are fine — re-runs and same-ESSID-in-multiple-groups both
        # reuse the existing objects; the scope-maps below still bind this group.
        self._swallow_duplicate(lambda: self._post(
            f"/network-config/v1/wlan-ssids/{encoded}", json=body))
        # the API silently drops default-role on POST — re-apply
        self._patch(f"/network-config/v1/wlan-ssids/{encoded}",
                    json={"default-role": name})

        # bind to the gateway cluster (GRE tunnel)
        self._swallow_duplicate(lambda: self._post(
            f"/network-config/v1/overlay-wlan/{encoded}", json={
                "profile": name,
                "overlay-profile-type": "WIRELESS_PROFILE",
                "essid-name": name,
                "gw-cluster-list": [{
                    "cluster-redundancy-type": "PRIMARY",
                    "cluster": cluster_name,
                    "cluster-scope-id": cluster_scope_id,
                    "cluster-type": "CLUSTER_ID",
                    "tunnel-type": "GRE",
                }],
            }))
        self.map_to_scope(f"wlan-ssids/{name}", group_scope, "CAMPUS_AP")
        self.map_to_scope(f"overlay-wlan/{name}", group_scope, "CAMPUS_AP")

    # ─────────────────── GW cluster ───────────────────

    def create_gw_cluster(self, cluster_name: str, scope_id: str) -> None:
        try:
            self._post(
                f"/network-config/v1alpha1/gateway-clusters/{quote(cluster_name, safe='')}",
                json={"name": cluster_name, "ipv6-enable": False, "auto-cluster": False},
                params={"object-type": "LOCAL", "scope-id": str(scope_id),
                        "device-function": "MOBILITY_GW"},
            )
        except CentralAPIError as e:
            if "duplicate" not in str(e).lower() and "exists" not in str(e).lower():
                raise

    # ─────────────────── Auth servers ───────────────────

    def create_auth_server(self, server: RadiusServer) -> None:
        body = {
            "name": server.name,
            "type": "RADIUS",
            "radius-server-mode": "AUTH_AND_COA",
            "auth-server-address": server.address,
            "auth-port": server.auth_port,
            "acct-port": server.acct_port,
            "enable": True,
        }
        if server.secret:
            body["shared-secret-config"] = {
                "secret-type": "PLAIN_TEXT",
                "plaintext-value": server.secret,
            }
        try:
            self._post(f"/network-config/v1alpha1/auth-servers/{quote(server.name, safe='')}",
                       json=body)
        except CentralAPIError as e:
            if "duplicate" not in str(e).lower() and "exists" not in str(e).lower():
                raise

    # ─────────────────── Firmware ───────────────────

    def set_firmware_compliance(self, scope_id: str, version: str,
                                device_function: str = "CAMPUS_AP") -> None:
        body = {
            "name": f"compliance-{device_function.lower()}",
            "enable": True,
            "version-chart": {"version": version},
            "upgrade-mode": "REGULAR",
            "enforcement-schedule": {
                "upgrade-schedule": {"upgrade-schedule-mode": "IMMEDIATE"},
                "reboot-schedule": {"reboot-schedule-mode": "IMMEDIATE"},
            },
        }
        params = {"scope-id": str(scope_id), "object-type": "LOCAL",
                  "device-function": device_function}
        try:
            self._post("/network-config/v1alpha1/firmware-compliance",
                       json=body, params=params)
        except CentralAPIError as e:
            if "412" not in str(e):
                raise
            self._patch("/network-config/v1alpha1/firmware-compliance",
                        json=body, params=params)

    # ─────────────────── Full provision flow ───────────────────

    def _create_group_hybrid(self, classic, name: str,
                             include_gateways: bool) -> str:
        """Hybrid-cluster path: create the device group via the CLASSIC API
        (New Central blocks that write on hybrid tenants) and resolve its New
        Central scope-id so SSIDs/VLANs can still be scope-mapped to it.

        Device MOVE is a SEPARATE provision step — a move failure (e.g. serials
        not yet in inventory) must not block WLAN/VLAN config for the group."""
        # new_central=True flags the group "Allow New Central to overwrite" so
        # it becomes New-Central-managed and shows up in device-collections.
        classic.create_group(name, include_gateways=include_gateways,
                             new_central=True)
        # Classic→New-Central propagation isn't instant — poll the New Central
        # device-collections for the group to appear before resolving scope-id.
        import time
        for attempt in range(6):
            for grp in self.list_device_groups(refresh=True):
                if grp.get("scopeName") == name:
                    return str(grp.get("scopeId"))
            if attempt < 5:
                time.sleep(5)
        raise CentralAPIError(
            f"Group '{name}' was created via Classic (NewCentral=true) but hasn't "
            "appeared in New Central after ~25s. It usually lands within a minute — "
            "use 'Reset & re-run provisioning' shortly and it'll resolve (the group "
            "already exists, so create is a no-op).")

    def provision(
        self,
        central_config: CentralConfig,
        ap_serials: dict[str, list[str]],
        on_step: Optional[Callable[[str, bool], None]] = None,
        classic_client=None,
    ) -> list[tuple[str, bool, str]]:
        """Run the full New Central provisioning sequence.

        classic_client: optional ClassicCentralClient. On HYBRID-CLUSTER
        tenants New Central blocks device-group create/move, so when this is
        supplied those two operations route through the Classic API (HPE's own
        onboarding pattern); WLANs/VLANs/scope-maps stay on New Central.

        Returns [(step_label, ok, error_detail)]. Failures are recorded and
        the flow continues so the operator gets a complete picture.
        """
        results: list[tuple[str, bool, str]] = []

        def _make_group(name, serials, include_gateways=False) -> str:
            # hybrid: create group only (move is a separate, independently
            # failable step). native: atomic create-and-add.
            if classic_client is not None:
                return self._create_group_hybrid(classic_client, name, include_gateways)
            return self.create_device_group(name, serials)

        # fresh caches per run — lists are fetched once, not per object
        self._groups_cache = None
        self._sites_cache = None
        self._ensured_roles.clear()
        self._ensured_policies.clear()

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

        # Resolve the global scope (needed for roles/policies)
        scope_holder: dict[str, str] = {}
        if not step("Resolve global scope",
                    lambda: scope_holder.update(g=self.get_global_scope_id())):
            return results  # nothing else can proceed
        global_scope = scope_holder["g"]

        # Sites
        site_ids: dict[str, str] = {}
        cc = central_config
        for site_name in cc.sites:
            step(f"Create site: {site_name}",
                 lambda s=site_name: site_ids.update({s: self.create_site(
                     s, cc.site_address, cc.site_city, cc.site_state,
                     cc.site_country, cc.site_zipcode,
                     timezone_id=getattr(cc, "site_timezone", "UTC"))}))

        # RADIUS servers (library profiles)
        for server in cc.radius_servers:
            step(f"Create auth server: {server.name}",
                 lambda s=server: self.create_auth_server(s))

        # Gateway cluster lives in its own device group (MOBILITY_GW persona)
        gw_scope: dict[str, str] = {}
        if cc.gw_cluster_name:
            gw_group = f"{cc.gw_cluster_name}-gws"
            step(f"Create gateway device group: {gw_group}",
                 lambda: gw_scope.update(id=_make_group(gw_group, None)))
            if gw_scope.get("id"):
                step(f"Create GW cluster: {cc.gw_cluster_name}",
                     lambda: self.create_gw_cluster(cc.gw_cluster_name, gw_scope["id"]))

        for group_cfg in cc.groups:
            # serials are keyed by the AOS 8 source group name, not the
            # (possibly renamed) Central device-group name
            serials = ap_serials.get(group_cfg.source_group or group_cfg.name, [])
            grp_scope: dict[str, str] = {}
            via = " (via Classic — hybrid)" if classic_client is not None else ""
            if not step(f"Create device group: {group_cfg.name}"
                        + (f" (+{len(serials)} APs)" if serials else "") + via,
                        lambda g=group_cfg, s=serials:
                            grp_scope.update(id=_make_group(g.name, s,
                                                            bool(cc.gw_cluster_name)))):
                continue  # group failed — skip its dependents
            scope_id = grp_scope["id"]

            # hybrid: move devices as a separate step so a move failure (e.g.
            # serials not yet claimed into inventory) doesn't block WLAN/VLAN
            if classic_client is not None and serials:
                step(f"Move {len(serials)} APs into group: {group_cfg.name} (Classic)",
                     lambda s=serials, g=group_cfg:
                         classic_client.move_devices(g.name, s))

            for vlan in group_cfg.vlans:
                step(f"Create VLAN {vlan.id} ({vlan.name}) → {group_cfg.name}",
                     lambda v=vlan, sid=scope_id:
                         self.create_vlan(v.id, v.name, sid))

            seen_essids: set[str] = set()
            for ssid in group_cfg.ssids:
                # Central keys SSIDs by ESSID — a second virtual-ap with the
                # same broadcast name in this group can't be a separate object
                if ssid.display_name in seen_essids:
                    results.append((
                        f"SSID {ssid.display_name} → {group_cfg.name} — SKIPPED "
                        "(duplicate ESSID in group; first definition applies)",
                        True, "",
                    ))
                    continue
                seen_essids.add(ssid.display_name)
                if ssid.forward_mode in (ForwardMode.TUNNEL, ForwardMode.SPLIT) \
                        and cc.gw_cluster_name and gw_scope.get("id"):
                    step(f"Create overlay SSID: {ssid.display_name} → {group_cfg.name}",
                         lambda s=ssid, sid=scope_id:
                             self.create_overlay_ssid(s, sid, global_scope,
                                                      cc.gw_cluster_name,
                                                      gw_scope["id"]))
                elif ssid.forward_mode in (ForwardMode.TUNNEL, ForwardMode.SPLIT) \
                        and cc.gw_cluster_name:
                    results.append((
                        f"Create overlay SSID: {ssid.display_name} → {group_cfg.name}",
                        False,
                        "Skipped — gateway device group/cluster creation failed earlier",
                    ))
                else:
                    step(f"Create underlay SSID: {ssid.display_name} → {group_cfg.name}",
                         lambda s=ssid, sid=scope_id:
                             self.create_underlay_ssid(s, sid))

            step(f"Set firmware compliance {group_cfg.firmware_version} → {group_cfg.name}",
                 lambda g=group_cfg, sid=scope_id:
                     self.set_firmware_compliance(sid, g.firmware_version))

            if serials:
                step(f"Assign CAMPUS_AP persona → {len(serials)} APs in {group_cfg.name}",
                     lambda s=serials: self.assign_persona(s))

            if serials and group_cfg.site_name in site_ids:
                step(f"Assign {len(serials)} APs to site: {group_cfg.site_name}",
                     lambda s=serials, sn=group_cfg.site_name:
                         self.assign_devices_to_site(site_ids[sn], s))

        return results

    # ─────────────────── Validation ───────────────────

    def list_all_aps(self) -> Optional[list[dict]]:
        """All APs with status; None means the fetch itself failed."""
        try:
            devices = self._paginate("/network-monitoring/v1/devices", page_size=100)
        except CentralAPIError:
            return None
        return [d for d in devices
                if str(d.get("deviceType", "")).upper() in ("ACCESS_POINT", "AP", "IAP")]
