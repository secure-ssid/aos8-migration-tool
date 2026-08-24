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
import re
import time
from typing import Callable, Optional
from urllib.parse import quote, unquote

import requests

from .http_base import normalize_base
from .manifest import KIND_SSID
from .models import CentralConfig, ForwardMode, AuthType, RadiusServer, SSID

TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"

# AOS 8 stores PSK/RADIUS secrets hashed — a valid placeholder lets the object
# be created, and the operator is told to set the real secret in Central.
PSK_PLACEHOLDER = "ChangeMe-SetInCentral"


def cp_profile_name(ssid: SSID) -> str:
    """Deterministic external-captive-portal profile name for an SSID, so the
    create and the SSID binding agree."""
    base = re.sub(r"[^A-Za-z0-9-]+", "-", (ssid.essid or ssid.name)).strip("-")
    return f"{base or 'guest'}-ecp"


def secret_looks_unusable(s: Optional[str]) -> bool:
    """True when a captured PSK/secret can't be pushed as-is: empty, longer
    than a WPA passphrase can be (so it must be a hash), or an encrypted hex
    blob. Real WPA passphrases are 8–63 printable ASCII chars."""
    s = (s or "").strip()
    if not s:
        return True
    if len(s) > 63:
        return True
    if len(s) >= 32 and re.fullmatch(r"[0-9a-fA-F]+", s):
        return True
    return False

# AuthType → New Central wlan-ssid opmode. Verified against the WLAN
# OpenAPI spec enum and live tenant SSIDs (2026-06). 802.1X SSIDs still
# need their auth server attached in Central — surfaced in preflight.
OPMODE = {
    AuthType.OPEN: "OPEN",
    AuthType.MAC: "OPEN",
    # ENHANCED_OPEN is a first-class wlan-ssid.opmode value in the New Central
    # Configuration API — OWE migrates encrypted, not as a bare OPEN SSID.
    AuthType.OWE: "ENHANCED_OPEN",
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
    # only inspect the response detail — the error prefix contains the URL
    # path, and a customer-named object ("duplicate-lab") in the path must
    # not make an unrelated failure read as idempotent success
    m = re.search(r"failed \d+: (.*)", msg, re.S)
    detail = m.group(1) if m else msg
    return "already exists" in detail or "duplicate" in detail


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


class CentralClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str,
                 timeout: int = 30):
        self.base = normalize_base(base_url)
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
        # GreenLake tokens carry expires_in and no refresh token — track it so
        # a long provision run refreshes before a write fails on a 401.
        self._token_expiry: float = 0.0
        # which /network-config API version this tenant answered on (ordering
        # hint for _config_request; see its docstring)
        self._config_version: Optional[str] = None
        # ownership registry (lib.manifest) attached by the Step 3 view —
        # None keeps the legacy name-only idempotency
        self.manifest = None

    # ─────────────────── Auth / HTTP ───────────────────

    def authenticate(self) -> bool:
        try:
            # plain requests.post, NOT self.session — the session carries the
            # previous (possibly expired) Bearer header, which must not be
            # sent to the token endpoint
            resp = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as e:
            raise CentralAPIError(
                f"Token request to GreenLake SSO failed ({type(e).__name__}) — "
                "check network reachability to sso.common.cloud.hpe.com")
        if not resp.ok:
            raise CentralAPIError(
                f"Token request failed {resp.status_code}: {resp.text[:300]}")
        try:
            body = resp.json()
            self.token = body["access_token"]
        except (ValueError, KeyError):
            raise CentralAPIError("Token endpoint returned an unexpected body "
                                  "(no access_token)")
        try:
            ttl = int(body.get("expires_in", 3600))
        except (TypeError, ValueError):
            ttl = 3600
        self._token_expiry = time.time() + ttl - 60   # 60s clock skew
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})
        return True

    def _request(self, method: str, path: str, json: Optional[dict] = None,
                 params: Optional[dict] = None, _auth_retried: bool = False,
                 _rate_retried: bool = False) -> dict:
        # Proactive refresh. The `self._token_expiry and` guard is load-bearing:
        # a client whose token was injected (tests, restored session) has 0.0
        # here and must not fire a spurious token request.
        if self._token_expiry and time.time() >= self._token_expiry:
            self.authenticate()
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
        if resp.status_code == 401 and not _auth_retried:
            # separate flags: a 429 backoff followed by a token expiry (or
            # vice versa) must still get its one retry of the other kind
            self.authenticate()
            return self._request(method, path, json, params,
                                 _auth_retried=True, _rate_retried=_rate_retried)
        if resp.status_code == 429 and not _rate_retried:
            # Retry-After may be an HTTP-date (RFC 7231) — fall back to 30s
            # rather than crashing on int().
            retry_after = resp.headers.get("Retry-After", "30").strip()
            wait = int(retry_after) if retry_after.isdigit() else 30
            time.sleep(min(wait, 120))
            return self._request(method, path, json, params,
                                 _auth_retried=_auth_retried, _rate_retried=True)
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:300]
            raise CentralAPIError(f"{method} {path} failed {resp.status_code}: {detail}")
        if not resp.content:
            return {}
        try:
            body = resp.json()
        except ValueError:
            # a 2xx with a non-JSON body is still a success — never let a
            # JSONDecodeError masquerade as a failed call
            return {"_raw": resp.text[:300]}
        return {"items": body} if isinstance(body, list) else body

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)

    def _delete(self, path, json=None, params=None):
        return self._request("DELETE", path, json=json, params=params)

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
            # An endpoint that ignores offset echoes page 1 forever. Truncating
            # here silently breaks every downstream idempotency check ("object
            # not in the list" -> re-create), so this is an error, not a result.
            if page and page[0] == first_of_prev_page:
                raise CentralAPIError(
                    f"GET {path}: server ignored offset={offset} (page repeated) "
                    "— cannot enumerate completely")
            first_of_prev_page = page[0] if page else None
            items.extend(page)
            if len(page) < page_size:
                return items
            offset += page_size
        raise CentralAPIError(
            f"GET {path}: more than {max_pages * page_size} items — pagination cap hit")

    # HPE's published v26.04 reference puts scope management on /v1 and feature
    # configuration on /v1alpha1, while this repo's paths were runtime-verified
    # against a live tenant serving /v1. The surface is Select Availability, so
    # both can be true — try one, fall through on 404 ONLY, and remember which
    # version answered so the rest of the run skips the dead route.
    _CONFIG_VERSIONS = ("v1", "v1alpha1")

    def _config_request(self, method: str, resource: str,
                        json: Optional[dict] = None,
                        params: Optional[dict] = None) -> dict:
        order = self._CONFIG_VERSIONS
        if self._config_version:
            order = (self._config_version,) + tuple(
                v for v in self._CONFIG_VERSIONS if v != self._config_version)
        errs = []
        for version in order:
            path = f"/network-config/{version}/{resource}"
            try:
                out = self._request(method, path, json=json, params=params)
            except CentralAPIError as e:
                # a 404 means "this tenant doesn't serve that version"; a 400
                # duplicate or a 403 is a real answer and must propagate
                if re.search(r"failed 404:", str(e)):
                    errs.append(str(e))
                    continue
                raise
            self._config_version = version
            return out
        raise CentralAPIError(
            f"{method} /network-config/<version>/{resource}: no configuration "
            "API version answered — " + " | ".join(errs))

    # ─────────────────── Scopes ───────────────────

    def get_global_scope_id(self) -> str:
        data = self._config_request("GET", "scope-maps")
        entries = data.get("scope-map", [])

        def _scope_of(entry: dict) -> str:
            # builds differ: some return scope-id, HPE's pycentral reads the
            # numeric identifier from scope-name — accept either
            sid = entry.get("scope-id")
            if sid is None:
                name = str(entry.get("scope-name", ""))
                sid = name if name.isdigit() else None
            return "" if sid is None else str(sid)

        for entry in entries:
            if entry.get("persona") == "SERVICE_PERSONA" and _scope_of(entry):
                return _scope_of(entry)
        # fallback: most frequent scope id across the map
        counts: dict[str, int] = {}
        for entry in entries:
            sid = _scope_of(entry)
            if sid:
                counts[sid] = counts.get(sid, 0) + 1
        if counts:
            return max(counts, key=counts.get)
        raise CentralAPIError("Could not determine global scope id from scope-maps")

    def map_to_scope(self, resource: str, scope_id: str, persona: str) -> None:
        try:
            numeric_scope = int(scope_id)
        except (TypeError, ValueError):
            # a non-numeric id here means an upstream lookup fell back to a
            # name — surface a clear error instead of a raw ValueError
            raise CentralAPIError(
                f"scope-map {resource}: scope id {scope_id!r} is not numeric "
                "— the owning scope was probably not resolved correctly")
        try:
            self._config_request("POST", "scope-maps", json={
                "scope-map": [{
                    "scope-name": str(scope_id),
                    "scope-id": numeric_scope,
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
        # monitoring route.
        if self._sites_cache is None or refresh:
            errs = []
            for path in ("/network-config/v1alpha1/sites",
                         "/network-config/v1/sites",
                         # not present in any published OpenAPI spec (checked
                         # 2026-08 against 31 vendored specs) — kept as a
                         # last-resort route for tenants that still serve it
                         "/network-monitoring/v1/sites"):
                try:
                    # documented page limit is 100 — paginate; _paginate now
                    # RAISES on a build that ignores offset rather than
                    # truncating, so a partial list can't be cached
                    self._sites_cache = self._paginate(path, page_size=100)
                    break
                except CentralAPIError as e:
                    errs.append(str(e))
                    if re.search(r"failed 404:", str(e)):
                        continue    # wrong route for this tenant — try the next
                    # a 403/500/timeout is a real answer ABOUT this tenant.
                    # Caching [] here makes create_site re-POST and the devices
                    # phase tell the operator to re-run a step that succeeded.
                    raise
            else:
                raise CentralAPIError(
                    "Site listing failed on all routes: " + " | ".join(errs))
        return self._sites_cache

    def create_site(self, name: str, address: str = "", city: str = "",
                    state: str = "", country: str = "", zipcode: str = "",
                    timezone_id: str = "UTC", lab_mode: bool = False) -> str:
        """Idempotent: returns the existing site's id when the name matches.

        Body shape from HPE's shipping New Central workflows (wpa3-psk-overlay /
        open-ssid-overlay Postman): POST /network-config/v1alpha1/sites with a
        REQUIRED timezone object. The address block is only sent when a full
        street address is given — partial/ISO-invalid address fields (e.g. a
        bare country code) are themselves a common 400 cause."""
        for site in self.list_sites():
            if self._site_name(site) == name:
                return self._site_id(site) or name
        if not all((address.strip(), city.strip(), country.strip())) \
                and not lab_mode:
            # blank site data must not silently become a REAL placeholder site
            # in a production tenant — the lab fixture below is for labs only
            raise CentralAPIError(
                f"Site '{name}' has incomplete address data (street address, "
                "city and country are required) — refusing to create a "
                "placeholder site in a production tenant. Supply the site "
                "address, or enable lab/test mode for a throwaway lab tenant.")
        # New Central requires the FULL geographic body (pycentral Site:
        # name+address+city+state+country+zipcode+timezone are all required and
        # country/state are ISO-3166 validated). Placeholder values are only
        # reachable in lab/test mode (gated above).
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
                     # not in any published spec (checked 2026-08) — kept as a
                     # last-resort route for tenants that still serve it
                     "/network-monitoring/v1/sites"):
            try:
                resp = self._post(path, json=body)
                break
            except CentralAPIError as e:
                errs.append(str(e))
                if _is_duplicate(e):
                    # the pre-list can miss a just-created site (pagination /
                    # eventual consistency) — a duplicate error IS the site
                    for site in self.list_sites(refresh=True):
                        if self._site_name(site) == name:
                            return self._site_id(site) or name
                    return name
                # match the status code the client itself formats ("failed 404:"),
                # not any '404' that happens to appear in a site name or detail
                if re.search(r"failed 404:", str(e)):
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
        # New Central config route (verified): POST /network-config/v1/site-add-devices
        # {"desScopeId": <site scopeId>, "devices": [serials]}. Older routes as fallback.
        candidates = [
            ("POST", "/network-config/v1/site-add-devices",
             lambda: {"desScopeId": str(site_id), "devices": serials}),
            ("POST", f"/network-monitoring/v1/sites/{site_id}/devices",
             lambda: {"serials": serials}),
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
                # if the route exists but rejected the request (not a 404),
                # don't keep trying other routes — surface the real error
                if "404" not in str(e) and "not found" not in str(e).lower() \
                        and not isinstance(e, ValueError):
                    if "DEVICE_MOVEMENT_FAILED" in str(e) or "not present" in str(e).lower():
                        raise CentralAPIError(
                            "APs aren't in the New Central workspace yet — claim + "
                            "subscribe them in Step 4 (GreenLake), then Reset & re-run "
                            "provisioning to finish site assignment.")
                    raise CentralAPIError(f"Site assignment failed: {e}")
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
        # body key is "device-id" (a LIST of serials), NOT "serial" —
        # verified against HPE's device-onboarding workflow + the
        # persona-assignment OpenAPI spec
        body = {
            "persona-device-list": [
                {"device-function": device_function, "device-id": list(serials)}
            ],
        }
        try:
            # the OpenAPI spec defines ONLY the path-parameter form
            # (/persona-assignment/{device-function}); the bare collection is
            # the form field-verified on earlier tenants. Spec shape first,
            # bare form on a routing 404.
            try:
                self._post(f"/network-config/v1alpha1/persona-assignment/"
                           f"{quote(device_function, safe='')}", json=body)
                return
            except CentralAPIError as e:
                if not re.search(r"failed 404:", str(e)):
                    raise
            self._post("/network-config/v1alpha1/persona-assignment", json=body)
        except CentralAPIError as e:
            if _is_duplicate(e):
                return
            if "not present" in str(e).lower() or "device entry" in str(e).lower():
                raise CentralAPIError(
                    "APs aren't in the New Central workspace yet — claim + subscribe "
                    "them in Step 4 (GreenLake), then Reset & re-run provisioning "
                    "(completed objects are reused) to finish persona assignment. "
                    f"[{', '.join(serials)}]")
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
            self._config_request("POST", f"layer2-vlan/{vlan_id}", json=body)
        except CentralAPIError as e:
            # bare "exists" also matches "does not exist" — use the shared
            # duplicate matcher so only a real duplicate takes the PUT path
            if not _is_duplicate(e):
                raise
            self._config_request("PUT", f"layer2-vlan/{vlan_id}", json=body)
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

    def _ssid_body(self, ssid: SSID, forward_mode: str,
                   server_group: str = "") -> dict:
        if ssid.auth_type == AuthType.WEP:
            # preflight FAILs WEP before provisioning; this is the last line
            # of defence — there is no AOS 10 WEP opmode to map to.
            raise CentralAPIError(
                f"SSID '{ssid.display_name}' uses WEP, which AOS 10 does not "
                "support. Re-key the source network to WPA2/WPA3 before "
                "migrating.")
        # Full WLAN attribute set so the migrated SSID is a complete, functional
        # WLAN — data rates, radio capabilities, DTIM, broadcast filters, WMM,
        # 802.11k. Defaults match the shape New Central uses on this tenant;
        # per-SSID source values (band/dtim/max-clients/rates) override below.
        rf_band = getattr(ssid, "rf_band", "") or "BAND_ALL"
        legacy = {
            "basic-rates": ["RATE_12MB", "RATE_24MB"],
            "tx-rates": ["RATE_12MB", "RATE_18MB", "RATE_24MB", "RATE_36MB",
                         "RATE_48MB", "RATE_54MB"],
        }
        body = {
            "ssid": ssid.display_name,
            # a source WLAN that was administratively disabled stays disabled
            "enable": bool(getattr(ssid, "enabled", True)),
            "forward-mode": forward_mode,
            "opmode": OPMODE.get(ssid.auth_type, "WPA2_PERSONAL"),
            "vlan-selector": "VLAN_RANGES",
            "vlan-id-range": [str(ssid.vlan)],
            "rf-band": rf_band,
            "essid": {"use-alias": False, "name": ssid.display_name},
            "hide-ssid": not ssid.broadcast,
            # transition mode lets WPA2 clients join a WPA3-capable network —
            # a WPA2-PSK compatibility feature. Enabling it on a WPA3-only
            # SSID would silently add a WPA2 fallback nobody asked for.
            "wpa3-transition-mode-enable": ssid.auth_type == AuthType.WPA2_PSK,
            # radio data rates (2.4GHz + 5GHz legacy basic/tx sets)
            "g-legacy-rates": dict(legacy),
            "a-legacy-rates": dict(legacy),
            # radio capabilities
            "high-throughput": {"enable": True, "very-high-throughput": True},
            "high-efficiency": {"enable": True},
            "extremely-high-throughput": {"enable": True, "mlo": False,
                                          "beacon-protection": False},
            # airtime / behaviour
            "dtim-period": int(getattr(ssid, "dtim_period", 0) or 1),
            "max-clients-threshold": int(getattr(ssid, "max_clients", 0) or 128),
            "inactivity-timeout": 1000,
            "broadcast-filter-ipv4": "BCAST_FILTER_ARP",
            "broadcast-filter-ipv6": "UCAST_FILTER_RA",
            "wmm-cfg": {"uapsd": True},
            "dot11k": True,
            "dmo": {"enable": False, "channel-utilization-threshold": 90,
                    "clients-threshold": 6},
        }
        if ssid.auth_type in (AuthType.WPA2_PSK, AuthType.WPA3_SAE):
            # use the real passphrase when it's usable; otherwise a placeholder
            # (AOS 8 shows the PSK hashed) — flagged as a manual follow-up
            usable = not secret_looks_unusable(ssid.psk)
            body["personal-security"] = {
                "passphrase-format": "STRING",
                "wpa-passphrase": ssid.psk if usable else PSK_PLACEHOLDER,
            }
        if ssid.auth_type in ENTERPRISE_AUTH:
            body["dot1x"] = True
            # AP-persona 802.1X: bind the RADIUS server-group directly on the
            # SSID (verified wlan.json fields; the group wins over primary/backup)
            if server_group:
                body["auth-server-group"] = server_group
                body["acct-server-group"] = server_group
                body["radius-accounting"] = True
        if ssid.auth_type == AuthType.MAC:
            # mac-authentication is a first-class wlan-ssid field in the New
            # Central spec — without it a MAC-auth SSID migrates wide open.
            body["mac-authentication"] = True
            if server_group:
                body["auth-server-group"] = server_group
        # External captive portal — layered on top of an OPEN SSID (verified).
        # Force OPEN and drop any L2-auth fields: the portal does the auth, and
        # the API rejects the data-rate/vlan-id-range fields under a non-OPEN
        # opmode ("when opmode ∈ {OPEN,WPA2_PERSONAL,…}" condition unmet).
        if getattr(ssid, "captive_portal_url", ""):
            body["opmode"] = "OPEN"
            body.pop("dot1x", None)
            body.pop("personal-security", None)
            body["captive-portal-type"] = "EXTERNAL_CP"
            body["captive-portal"] = cp_profile_name(ssid)
            if server_group:
                body["auth-server-group"] = server_group
                body["radius-accounting"] = True
        return body

    def create_captive_portal(self, name: str, external_url: str,
                              redirect_url: str = "") -> None:
        """Create a SHARED external captive-portal profile (verified body:
        {name, external-cp-server-url, redirect-url}). SHARED → no scope-map."""
        body = {"name": name, "external-cp-server-url": external_url}
        if redirect_url:
            body["redirect-url"] = redirect_url
        self._swallow_duplicate(lambda: self._post(
            f"/network-config/v1alpha1/captive-portal/{quote(name, safe='')}",
            json=body))

    def _upsert_ssid(self, encoded: str, body: dict) -> None:
        """Create the wlan-ssid, or if it already exists PATCH it with the same
        body so bindings (auth-server-group, VLAN, data rates) reflect the
        CURRENT config on re-runs — a plain duplicate-swallow would leave a
        stale binding (e.g. an SSID still pointing at a previous server-group).

        With a manifest attached, the PATCH path is gated: patching a same-name
        SSID another administrator created is finding #3, so only manifest-owned
        (or explicitly adopted) SSIDs are patched."""
        try:
            self._config_request("POST", f"wlan-ssids/{encoded}", json=body)
        except CentralAPIError as e:
            if not _is_duplicate(e):
                raise
            if self.manifest is not None:
                self.manifest.gate(KIND_SSID, unquote(encoded), exists=True)
            self._config_request("PATCH", f"wlan-ssids/{encoded}", json=body)
        else:
            if self.manifest is not None:
                self.manifest.register(KIND_SSID, unquote(encoded), payload=body)

    def create_underlay_ssid(self, ssid: SSID, scope_id: str,
                             server_group: str = "") -> None:
        name = ssid.display_name
        encoded = quote(name, safe="")
        # Upsert so a re-run updates the binding/attrs; then scope-map so this
        # group gets the WLAN (also covers the same-ESSID-in-two-groups case).
        self._upsert_ssid(encoded, self._ssid_body(ssid, "FORWARD_MODE_BRIDGE", server_group))
        self.map_to_scope(f"wlan-ssids/{name}", scope_id, "CAMPUS_AP")

    def create_overlay_ssid(self, ssid: SSID, group_scope: str, global_scope: str,
                            cluster_name: str, cluster_scope_id: str,
                            server_group: str = "") -> None:
        name = ssid.display_name
        encoded = quote(name, safe="")

        self._ensure_role(name, global_scope, group_scope)
        self._ensure_allow_all_policy(name, name, global_scope)

        body = self._ssid_body(ssid, "FORWARD_MODE_L2", server_group)
        body.update({
            "type": "EMPLOYEE",
            "default-role": name,
            "out-of-service": "TUNNEL_DOWN",
            "cluster-preemption": False,
        })
        # Upsert so re-runs refresh the binding/attrs (and same-ESSID-in-multiple
        # -groups reuses the object); scope-maps below still bind this group.
        self._upsert_ssid(encoded, body)
        # the API silently drops default-role on POST — re-apply
        self._config_request("PATCH", f"wlan-ssids/{encoded}",
                             json={"default-role": name})

        # bind to the gateway cluster (GRE tunnel)
        self._swallow_duplicate(lambda: self._config_request(
            "POST", f"overlay-wlan/{encoded}", json={
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

    # AOS 8 stores RADIUS/TACACS secrets hashed — they can't be recovered, so
    # we create the server with this placeholder and tell the operator to set
    # the real secret in Central/GreenLake (surfaced as a manual follow-up).
    SECRET_PLACEHOLDER = "CHANGEME-set-real-secret-in-central"

    def create_auth_server(self, server: RadiusServer) -> None:
        body = {
            "name": server.name,
            "type": "RADIUS",
            "radius-server-mode": "AUTH_AND_COA",
            "auth-server-address": server.address,
            "auth-port": server.auth_port,
            "acct-port": server.acct_port,
            "enable": True,
            "shared-secret-config": {
                "secret-type": "PLAIN_TEXT",
                "plaintext-value": server.secret or self.SECRET_PLACEHOLDER,
            },
        }
        self._swallow_duplicate(lambda: self._post(
            f"/network-config/v1alpha1/auth-servers/{quote(server.name, safe='')}",
            json=body))

    def create_server_group(self, name: str, server_names: list[str],
                            group_type: str = "RADIUS") -> None:
        """Create a server-group binding the named auth-servers. `position` is
        required on AP (the tenant enforces it even though the spec tags it
        GW-only) — 1 = top."""
        members = [{"server-name": s, "position": i}
                   for i, s in enumerate((x for x in server_names if x), start=1)]
        if not members:
            return
        self._swallow_duplicate(lambda: self._post(
            f"/network-config/v1alpha1/server-groups/{quote(name, safe='')}",
            json={"name": name, "type": group_type, "servers": members}))

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
            # compliance already set for this scope → PATCH to update the
            # version (412 precondition OR a 400 "duplicate/already exists")
            # match the status code the client itself formats ("failed 412:"),
            # not any '412' appearing in a version string or scope id
            if not re.search(r"failed 412:", str(e)) and not _is_duplicate(e):
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

    def _resolve_group_scope(self, name: str) -> str:
        for grp in self.list_device_groups(refresh=True):
            if grp.get("scopeName") == name:
                return str(grp.get("scopeId"))
        return ""

    def provision(
        self,
        central_config: CentralConfig,
        ap_serials: dict[str, list[str]],
        on_step: Optional[Callable[[str, bool], None]] = None,
        classic_client=None,
        phase: str = "config",
    ) -> list[tuple[str, bool, str]]:
        """Run the New Central provisioning sequence in two phases:

        phase="config" (default) — build the target config only: sites,
        auth-servers, server-groups, device GROUPS (empty), VLANs, SSIDs,
        scope-maps, firmware compliance. NON-DISRUPTIVE — nothing touches the
        production APs, so the operator can review everything in New Central
        before converting.

        phase="devices" — onboard the APs: move them into their device groups
        (Classic on hybrid), assign the CAMPUS_AP persona, assign to sites.
        Run AFTER the devices are claimed in GreenLake. Groups must already
        exist (config phase) — their scope-ids are resolved from the tenant.

        phase="all" — both, in one pass (legacy single-shot behavior).

        classic_client: optional ClassicCentralClient. On HYBRID-CLUSTER
        tenants New Central blocks device-group create/move, so those route
        through the Classic API; WLANs/VLANs/scope-maps stay on New Central.

        Returns [(step_label, ok, error_detail)]; failures are recorded and the
        flow continues so the operator gets a complete picture.
        """
        do_config = phase in ("config", "all")
        do_devices = phase in ("devices", "all")
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

        # Resolve the global scope — a cheap read that authenticates AND
        # proves scope-maps access before any write is attempted. (Roles /
        # policies for a future automated overlay bind would also need it.)
        scope_holder: dict[str, str] = {}
        if not step("Resolve global scope",
                    lambda: scope_holder.update(g=self.get_global_scope_id())):
            return results  # nothing else can proceed

        # Sites — created in the config phase; resolved from the tenant in the
        # devices phase (needed to assign APs to them).
        site_ids: dict[str, str] = {}
        cc = central_config
        if do_config:
            for site_name in cc.sites:
                step(f"Create site: {site_name}",
                     lambda s=site_name: site_ids.update({s: self.create_site(
                         s, cc.site_address, cc.site_city, cc.site_state,
                         cc.site_country, cc.site_zipcode,
                         timezone_id=getattr(cc, "site_timezone", "UTC"),
                         lab_mode=getattr(cc, "lab_mode", False))}))
        else:
            try:
                for site in self.list_sites(refresh=True):
                    site_ids[self._site_name(site)] = self._site_id(site)
            except Exception as e:
                # a failed site read must not read as "the tenant has no sites"
                # — every AP would then fall into the misleading "run Step 3
                # first" branch mid-cutover
                results.append(("Resolve sites in tenant", False, str(e)))
                if on_step:
                    on_step(results[-1][0], False)

        radius_group = ""
        _source_sg_names: set = set()

        def _ssid_server_group(s) -> str:
            # bind the SSID's own (recreated) source group when it exists;
            # fall back to the merged group otherwise
            sg = getattr(s, "auth_server_group", "") or ""
            return sg if sg in _source_sg_names else radius_group

        import re as _re
        _slug = _re.sub(r"[^a-z0-9-]+", "-", cc.customer_name.lower()).strip("-") or "migrated"
        has_enterprise = any(s.auth_type in (AuthType.WPA2_ENTERPRISE,
                                             AuthType.WPA3_ENTERPRISE)
                             for g in cc.groups for s in g.ssids)
        if cc.radius_servers and has_enterprise:
            radius_group = f"{_slug}-radius"

        if do_config:
            # RADIUS servers (library profiles)
            for server in cc.radius_servers:
                step(f"Create auth server: {server.name}",
                     lambda s=server: self.create_auth_server(s))
            # Source server-groups are recreated 1:1 so each 802.1X SSID
            # keeps ITS OWN RADIUS group (failover order / policy separation
            # survive). The merged fallback group only backs SSIDs whose
            # source group couldn't be discovered.
            for sg in getattr(cc, "server_groups", []) or []:
                if sg.name and sg.servers:
                    step(f"Create RADIUS server-group: {sg.name}",
                         lambda g=sg: self.create_server_group(g.name, g.servers))
                    _source_sg_names.add(sg.name)
            if radius_group:
                step(f"Create RADIUS server-group (fallback): {radius_group}",
                     lambda: self.create_server_group(
                         radius_group, [s.name for s in cc.radius_servers]))

            # Gateway cluster: a NATIVE New Central object (MOBILITY_GW) formed
            # by JOINING gateways. We deliberately do NOT build it during the
            # config phase, because:
            #  - it must not go through Classic — a WLAN group can't carry a
            #    branch-gateway role, so Classic v3 group-create 400s; and
            #  - in an AOS 8→10 migration the MCs only become gateways at the
            #    cutover, so there are no gateways to join yet.
            # Record an explicit follow-up (also on the runbook) so it's never
            # silently dropped; the overlay/tunnel SSIDs below defer to it.
            if cc.gw_cluster_name:
                results.append((
                    f"MANUAL FOLLOW-UP: form gateway cluster "
                    f"'{cc.gw_cluster_name}' in New Central and join the gateways "
                    f"at cutover — overlay/tunnel SSIDs bind to it (see runbook)",
                    True, ""))
                if on_step:
                    on_step(results[-1][0], True)

        for group_cfg in cc.groups:
            # serials are keyed by the AOS 8 source group name, not the
            # (possibly renamed) Central device-group name; merged generic
            # groups contribute serials from every folded source group
            _srcs = ([group_cfg.source_group or group_cfg.name]
                     + list(group_cfg.extra_source_groups))
            serials = [s for src in _srcs for s in ap_serials.get(src, [])]
            via = " (via Classic — hybrid)" if classic_client is not None else ""

            if do_config:
                # create the group container (empty — APs are moved in during
                # the devices phase, after they're claimed in GreenLake)
                grp_scope: dict[str, str] = {}
                if not step(f"Create device group: {group_cfg.name}" + via,
                            lambda g=group_cfg:
                                grp_scope.update(id=_make_group(g.name, None))):
                    continue  # group failed — skip its dependents
                # NB: AP/wireless groups are AccessPoints-only — gateways never
                # share a WLAN group (Central 3.x rejects a WLAN group that also
                # carries a gateway role). Gateways live in their own cluster.
                scope_id = grp_scope["id"]
            else:
                scope_id = self._resolve_group_scope(group_cfg.name)
                if not scope_id:
                    results.append((f"Resolve device group: {group_cfg.name}", False,
                                    "group not found in New Central — run the "
                                    "config phase (Step 3) first"))
                    if on_step:
                        on_step(results[-1][0], False)
                    continue

            # DEVICES phase: move the claimed APs into the group. Hybrid →
            # Classic move; native → add-devices. A move failure (serials not
            # yet in the workspace) doesn't block anything else.
            if do_devices and serials:
                if classic_client is not None:
                    step(f"Move {len(serials)} APs into group: {group_cfg.name} (Classic)",
                         lambda s=serials, g=group_cfg:
                             classic_client.move_devices(g.name, s))
                else:
                    step(f"Add {len(serials)} APs to group: {group_cfg.name}",
                         lambda s=serials, sid=scope_id:
                             self.add_devices_to_group(sid, s))

            if do_devices:
                # persona + site assignment belong to the devices phase (they
                # need claimed APs) — phase="all" runs them too, as documented
                if serials:
                    step(f"Assign CAMPUS_AP persona → {len(serials)} APs in {group_cfg.name}",
                         lambda s=serials: self.assign_persona(s))
                # site-device assignment is blocked on hybrid (API_ACCESS_RESTRICTED)
                # just like group create/move — route it through Classic there
                if serials and group_cfg.site_name:
                    if classic_client is not None:
                        def _site_hybrid(s=serials, sn=group_cfg.site_name):
                            sid = classic_client.create_site(
                                sn, cc.site_address, cc.site_city, cc.site_state,
                                cc.site_country, cc.site_zipcode)
                            classic_client.associate_site(sid, s)
                        step(f"Assign {len(serials)} APs to site: {group_cfg.site_name} (Classic)",
                             _site_hybrid)
                    elif group_cfg.site_name in site_ids:
                        step(f"Assign {len(serials)} APs to site: {group_cfg.site_name}",
                             lambda s=serials, sn=group_cfg.site_name:
                                 self.assign_devices_to_site(site_ids[sn], s))
                    else:
                        # never skip silently mid-cutover — the operator must
                        # know the APs are not on the site
                        results.append((
                            f"Assign {len(serials)} APs to site: {group_cfg.site_name}",
                            False,
                            "site not found in the tenant — run the config "
                            "phase (Step 3) first, or check the API client's "
                            "site permissions"))
                        if on_step:
                            on_step(results[-1][0], False)
            if not do_config:
                continue  # skip the config-only VLAN/SSID/firmware block

            for vlan in group_cfg.vlans:
                # VLAN 1 is New Central's built-in default ("aruba-vlan/1") —
                # it always exists and can't be created. SSIDs can still
                # reference it; just don't try to create it.
                if vlan.id <= 1:
                    results.append((
                        f"Create VLAN {vlan.id} ({vlan.name}) → {group_cfg.name} — "
                        "SKIPPED (VLAN 1 is the built-in default; already exists)",
                        True, ""))
                    if on_step:
                        on_step(results[-1][0], True)
                    continue
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
                    if on_step:
                        on_step(results[-1][0], True)
                    continue
                seen_essids.add(ssid.display_name)
                # external captive portal profile must exist before the SSID
                # references it by name
                if getattr(ssid, "captive_portal_url", ""):
                    step(f"Create external captive portal: {cp_profile_name(ssid)} "
                         f"→ {ssid.display_name}",
                         lambda s=ssid: self.create_captive_portal(
                             cp_profile_name(s), s.captive_portal_url,
                             s.captive_portal_redirect))
                    if ssid.auth_type is not AuthType.OPEN:
                        # the SSID body forces OPEN under an external portal
                        # (the API rejects L2-auth + portal fields together) —
                        # never let that opmode change go unremarked
                        results.append((
                            f"SSID {ssid.display_name} — NOTE: provisioned as "
                            f"OPEN + external captive portal (source auth "
                            f"'{ssid.auth_type.value}' is replaced by portal "
                            "auth; review if L2 encryption was intended)",
                            True, ""))
                        if on_step:
                            on_step(results[-1][0], True)
                # Tunnel/split SSIDs always defer to the runbook: the gateway
                # cluster only exists after the MCs convert at cutover, so
                # there is nothing to bind the overlay to during the config
                # phase (create_overlay_ssid is kept for a future automated
                # post-cutover bind).
                if ssid.forward_mode in (ForwardMode.TUNNEL, ForwardMode.SPLIT) \
                        and cc.gw_cluster_name:
                    results.append((
                        f"Overlay SSID {ssid.display_name} → {group_cfg.name} — "
                        f"DEFERRED: bind after gateway cluster "
                        f"'{cc.gw_cluster_name}' is formed at cutover (see runbook)",
                        True, ""))
                    if on_step:
                        on_step(results[-1][0], True)
                else:
                    step(f"Create underlay SSID: {ssid.display_name} → {group_cfg.name}",
                         lambda s=ssid, sid=scope_id:
                             self.create_underlay_ssid(s, sid,
                                                       _ssid_server_group(s)))

            step(f"Set firmware compliance {group_cfg.firmware_version} → {group_cfg.name}",
                 lambda g=group_cfg, sid=scope_id:
                     self.set_firmware_compliance(sid, g.firmware_version))
            # (persona + site assignment happen in the devices phase, after
            # the APs are claimed into the GreenLake workspace)

        # AOS 8 stores PSK/RADIUS secrets hashed — they can't be migrated, so
        # everything created with a placeholder is listed here as an explicit
        # manual step. (Also rendered on the Runbook as a checklist.)
        if do_config:
            radius_names = [s.name for s in cc.radius_servers if not s.secret]
            psk_ssids = sorted({s.display_name for g in cc.groups for s in g.ssids
                                if s.auth_type in (AuthType.WPA2_PSK, AuthType.WPA3_SAE)
                                and secret_looks_unusable(s.psk)})
            for nm in radius_names:
                results.append((
                    f"MANUAL FOLLOW-UP: set the RADIUS shared secret for '{nm}' in "
                    "Central (created with a placeholder — AOS 8 secrets are hashed)",
                    True, ""))
                if on_step:
                    on_step(results[-1][0], True)
            for nm in psk_ssids:
                results.append((
                    f"MANUAL FOLLOW-UP: set the WPA passphrase for SSID '{nm}' in "
                    f"Central (created with placeholder '{PSK_PLACEHOLDER}' — the AOS 8 "
                    "key was encrypted/not captured)", True, ""))
                if on_step:
                    on_step(results[-1][0], True)
            # external captive portal: the redirect + RADIUS bind are migrated,
            # but the pre-auth allowlist (walled garden) is tenant-specific and
            # not auto-built — flag it.
            cp_ssids = sorted({s.display_name for g in cc.groups for s in g.ssids
                               if getattr(s, "captive_portal_url", "")})
            for nm in cp_ssids:
                results.append((
                    f"MANUAL FOLLOW-UP: SSID '{nm}' has an external captive portal — "
                    "verify the pre-auth allowlist (walled garden: DNS + the portal "
                    "host) in Central; the redirect URL + RADIUS group were migrated",
                    True, ""))
                if on_step:
                    on_step(results[-1][0], True)

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
