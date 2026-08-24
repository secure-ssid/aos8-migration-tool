"""
AOS 8 Mobility Controller / Mobility Conductor REST API client.

API mechanics (ArubaOS 8 REST API guide):
  - Login:   POST https://<ip>:4343/v1/api/login   (form-encoded username/password)
             Response carries a UIDARUBA session token in _global_result.
  - Reads:   GET  https://<ip>:4343/v1/configuration/object/<name>
             GET  https://<ip>:4343/v1/configuration/showcommand?command=...
             Every request needs UIDARUBA (query param + session cookie) and,
             on a Mobility Conductor, a config_path (e.g. /md). Standalone
             controllers use /mm/mynode.

Falls back to CLI paste mode (aos8_parser) if the API is unreachable.
"""
import os
import re
import requests
import urllib3
from typing import Any, Optional

from .models import (
    AP, APGroup, ClusterInfo, CustomerConfig, ForwardMode,
    AuthType, RadiusServer, SSID, ServerGroup, VLAN,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

AOS8_API_PORT = 4343
LOGIN_PATH = "/v1/api/login"
CONFIG_PATH_PREFIX = "/v1/configuration"

# sentinel: `config_path=None` means "send no config_path at all", which is
# different from "use the client's configured node"
_UNSET = object()

# AP models known to be incompatible with AOS 10.
# NOTE: verify against Aruba's official AOS 10 supported-platform matrix for
# each release; matching is exact-token (country variants like -US stripped).
# From Wi-Fi 5 (802.11ac), ONLY the 303 series (303/303H/303P), AP-318, the
# 340 series (344/345) and the 370 series (374/375/377) made it into AOS 10;
# every other 2xx/3xx (and all Wi-Fi 4 and older) did not. AP-/IAP- prefixes
# are treated as interchangeable by the lookup, so one prefix per model is
# enough — both are listed for the models operators paste most.
INCOMPATIBLE_MODELS = {
    # Wi-Fi 4 and older
    "AP-92", "AP-93", "AP-93H",
    "AP-103", "AP-103H", "AP-104", "AP-105",
    "AP-114", "AP-115",
    "AP-134", "AP-135",
    "AP-175", "AP-175P", "AP-175AC", "AP-175DC",
    "IAP-103", "IAP-104", "IAP-105",
    "IAP-134", "IAP-135",
    "IAP-175", "IAP-175P", "IAP-175AC",
    "RAP-3WN", "RAP-3WNP", "RAP-108", "RAP-109", "RAP-155", "RAP-155P",
    # 200 series (Wi-Fi 5 wave 1 + hospitality/remote)
    "AP-203H", "AP-203R", "AP-203RP",
    "AP-204", "AP-205", "AP-205H", "AP-207",
    "AP-214", "AP-215",
    "AP-224", "AP-225", "AP-228",
    "AP-274", "AP-275", "AP-277",
    "IAP-204", "IAP-205", "IAP-205H", "IAP-207",
    "IAP-214", "IAP-215",
    "IAP-224", "IAP-225", "IAP-228",
    "IAP-274", "IAP-275", "IAP-277",
    # 300 series models NOT carried into AOS 10 (303/318/34x/37x are OK)
    "AP-304", "AP-305",
    "AP-314", "AP-315",
    "AP-324", "AP-325",
    "AP-334", "AP-335",
    "AP-365", "AP-367",
    "IAP-304", "IAP-305",
    "IAP-314", "IAP-315",
    "IAP-324", "IAP-325",
    "IAP-334", "IAP-335",
    "IAP-365", "IAP-367",
}

_COUNTRY_SUFFIXES = ("-US", "-RW", "-JP", "-IL", "-EG")


class AOS8APIError(Exception):
    pass


class AOS8Client:
    def __init__(self, ip: str, username: str, password: str,
                 config_path: str = "/md", timeout: int = 15,
                 port: int = AOS8_API_PORT):
        self.base = f"https://{ip}:{port}"
        self.ip = ip
        self.port = port
        self.username = username
        self.password = password
        self.config_path = config_path
        self.timeout = timeout
        self.uidaruba: Optional[str] = None
        self.pull_method = "object-api"  # or "showcommand" after a fallback pull
        # degradation records — a zero result must never be indistinguishable
        # from "this controller has nothing configured"
        self.node_scan_error = ""
        self.ap_scan_error = ""
        self.object_read_error = ""
        self.running_config_error = ""
        self.show_errors: dict[str, str] = {}
        self.session = requests.Session()
        # Controllers ship self-signed certs, but certificate verification is
        # ON by default (review finding 10): operator credentials never ride
        # an unverified TLS channel. Operators who deployed their own CA point
        # AOS8_CA_BUNDLE at the bundle path; AOS8_DEV_MODE (local lab / test
        # harness) is the only opt-out from verification.
        _bundle = os.environ.get("AOS8_CA_BUNDLE", "").strip()
        _dev = os.environ.get("AOS8_DEV_MODE", "").strip().lower() in (
            "1", "true", "yes", "on")
        if _bundle:
            self.session.verify = _bundle
        elif _dev:
            self.session.verify = False
        else:
            self.session.verify = True

    # ─────────────────── Auth ───────────────────

    def connect(self) -> bool:
        # Release any previous session FIRST. AOS 8 caps concurrent API
        # sessions (64 across CLI + WebUI + API, 900s idle default), so a
        # re-login without a logout burns one every time — including the
        # re-login _get_json performs on a 401.
        if self.uidaruba:
            self.logout()
        try:
            resp = self.session.post(
                f"{self.base}{LOGIN_PATH}",
                data={"username": self.username, "password": self.password},
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout:
            raise AOS8APIError(f"POST {LOGIN_PATH}: timed out after {self.timeout}s")
        except requests.exceptions.ConnectionError as e:
            raise AOS8APIError(f"POST {LOGIN_PATH}: connection failed — is TCP "
                               f"{self.port} reachable from here? "
                               f"({type(e).__name__})")
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            if resp.status_code in (401, 403):
                raise AOS8APIError(
                    f"Login rejected (HTTP {resp.status_code}) — check the "
                    "controller username/password and that the account has "
                    "API access") from None
            raise AOS8APIError(
                f"POST {LOGIN_PATH}: HTTP {resp.status_code}") from None
        try:
            data = resp.json()
        except ValueError:
            raise AOS8APIError(
                f"{LOGIN_PATH}: expected JSON, got "
                f"{resp.headers.get('Content-Type', '?')} — the REST API is "
                f"probably disabled on this controller (port {self.port} "
                "answered with the WebUI)")
        result = data.get("_global_result", {})
        # status comes back as int 0 or string "0" depending on build
        if str(result.get("status", "1")) != "0":
            raise AOS8APIError(f"Login failed: {result.get('status_str', data)}")
        self.uidaruba = result.get("UIDARUBA")
        if not self.uidaruba:
            raise AOS8APIError("Login succeeded but no UIDARUBA token returned")
        return True

    def logout(self) -> None:
        """Best-effort session release. AOS 8 caps concurrent API sessions per
        user — leaking one per pull eventually locks the account out of the
        API until the old sessions age out."""
        if not self.uidaruba:
            return
        try:
            self.session.get(f"{self.base}/v1/api/logout",
                             params={"UIDARUBA": self.uidaruba}, timeout=5)
        except Exception:
            pass
        self.uidaruba = None

    def _params(self, extra: Optional[dict] = None,
                config_path: Any = _UNSET) -> dict:
        params = {"UIDARUBA": self.uidaruba}
        cp = self.config_path if config_path is _UNSET else config_path
        if cp:
            params["config_path"] = cp
        if extra:
            params.update(extra)
        return params

    def _get_json(self, path: str, extra_params: Optional[dict] = None,
                  _retried: bool = False, config_path: Any = _UNSET,
                  timeout: Optional[int] = None) -> dict:
        """Authenticated GET with ONE re-login retry on 401. The UIDARUBA
        session can be invalidated out-of-band mid-pull (an admin clearing
        mgmt-user sessions, conductor failover) — without the replay that
        degrades into an empty/partial discovery instead of an error.

        AOS 8 answers a bad config_path, an unknown object and an expired
        session with HTTP 200 plus a _global_result error payload. Decoding
        that into an empty list is what makes a wrong node indistinguishable
        from "this controller has no WLANs", so it raises here.
        """
        timeout = self.timeout if timeout is None else timeout
        try:
            resp = self.session.get(
                f"{self.base}{path}",
                params=self._params(extra_params, config_path=config_path),
                timeout=timeout,
            )
        except requests.exceptions.Timeout:
            raise AOS8APIError(f"GET {path}: timed out after {timeout}s")
        except requests.exceptions.ConnectionError as e:
            raise AOS8APIError(f"GET {path}: connection failed — is TCP "
                               f"{self.port} reachable from here? "
                               f"({type(e).__name__})")
        if resp.status_code == 401 and not _retried:
            # connect() releases the dead session before re-logging in
            self.connect()
            return self._get_json(path, extra_params, _retried=True,
                                  config_path=config_path, timeout=timeout)
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError:
            raise AOS8APIError(
                f"{path}: expected JSON, got "
                f"{resp.headers.get('Content-Type', '?')} — the REST API is "
                f"probably disabled on this controller (port {self.port} "
                "answered with the WebUI)")
        if not isinstance(payload, dict):
            raise AOS8APIError(f"{path}: expected a JSON object, got "
                               f"{type(payload).__name__}")
        gr = payload.get("_global_result") or {}
        if gr and str(gr.get("status", "0")) != "0":
            raise AOS8APIError(f"{path}: {gr.get('status_str') or gr}")
        return payload

    def _get_object(self, name: str, config_path: Any = _UNSET) -> list[dict]:
        """GET a configuration object; returns its instance list."""
        data = self._get_json(f"{CONFIG_PATH_PREFIX}/object/{name}",
                              config_path=config_path)
        # Object payloads come back either under "_data" -> {name: [...]}
        # or directly under the object name.
        if isinstance(data.get("_data"), dict):
            data = data["_data"]
        items = data.get(name, [])
        return items if isinstance(items, list) else [items]

    def _show(self, command: str, config_path: Any = _UNSET,
              timeout: Optional[int] = None) -> dict:
        """Run a show command; returns the parsed JSON document.

        HPE documents only `command` + `UIDARUBA` for showcommand, but this
        controller's behaviour is not guaranteed — the caller decides whether
        to send a config_path (None = send none)."""
        return self._get_json(f"{CONFIG_PATH_PREFIX}/showcommand",
                              {"command": command},
                              config_path=config_path, timeout=timeout)

    def _show_text(self, command: str, config_path: Any = _UNSET,
                   timeout: Optional[int] = None) -> str:
        """Run a show command; flatten its _data block to plain text."""
        data = self._show(command, config_path=config_path,
                          timeout=timeout).get("_data", "")
        if isinstance(data, list):
            return "\n".join(str(line) for line in data)
        return str(data)

    @staticmethod
    def _field(item: dict, *names: str, default: Any = "") -> Any:
        """Fetch the first present key. AOS key spelling varies by build
        (hyphen vs underscore), and scalar params arrive wrapped one level
        deep — e.g. {"rad_authport": {"authport": 1812}} — so each name is
        tried in both spellings on the item and inside a matched sub-dict."""
        keys: list[str] = []
        for n in names:
            for k in (n, n.replace("-", "_"), n.replace("_", "-")):
                if k not in keys:
                    keys.append(k)
        for k in keys:
            if k not in item:
                continue
            val = item[k]
            if isinstance(val, dict):
                for inner in keys:
                    if inner in val:
                        return val[inner]
                continue  # flag/_present dict with no scalar — keep looking
            return val
        return default

    @staticmethod
    def _profile_ref(item: dict, name: str) -> str:
        """Resolve a sub-profile reference. Unlike scalar params, AOS returns
        these as dicts keyed by 'profile-name' (same shape as the virtual_ap
        and auth_server members), so _field's {key: {key: val}} unwrap misses."""
        ref = item.get(name)
        if isinstance(ref, dict):
            return str(ref.get("profile-name", "") or "")
        return str(ref or "")

    # ─────────────────── Config-node discovery ───────────────────

    _DEFAULT_GROUPS = ("default", "default-campus-ap-group", "NoAuthApGroup")

    def list_config_nodes(self) -> list[str]:
        """Node paths from the configuration hierarchy (MM only; returns []
        on standalone controllers / managed devices).

        A failure here is recorded in node_scan_error rather than swallowed:
        an empty list collapses find_config_node's candidate set to three
        literals, so a conductor whose config lives at /md/<Group> would never
        be probed and the UI would report "no config exists"."""
        self.node_scan_error = ""
        try:
            tree = self._get_json(f"{CONFIG_PATH_PREFIX}/object/node_hierarchy",
                                  config_path="/mm")
        except (AOS8APIError, requests.RequestException, ValueError) as e:
            self.node_scan_error = str(e)
            return []
        if isinstance(tree.get("_data"), dict):
            tree = tree["_data"]
        if isinstance(tree.get("node_hierarchy"), dict):
            tree = tree["node_hierarchy"]
        paths: list[str] = []

        def walk(node, prefix):
            if not isinstance(node, dict):
                return
            name = str(node.get("name", "")).strip("/")
            path = f"{prefix.rstrip('/')}/{name}" if name else prefix
            if path and path != "/":
                paths.append(path)
            for child in (node.get("childnodes") or node.get("children") or []):
                walk(child, path or "/")

        walk(tree, "")
        # deepest first — real config lives at leaf nodes, not the /md root
        paths.sort(key=lambda p: p.count("/"), reverse=True)
        return paths

    def _get_virtual_aps(self) -> list[dict]:
        """Virtual-AP profiles. The object is named "virtual_ap" (matching
        the key AOS embeds in ap_group responses); some builds answer the
        legacy "wlan_virtual_ap" name instead, so try both. Either name can
        404 on builds that don't expose it — an unknown-object error on one
        name must not kill the pull while the other would have answered."""
        try:
            vaps = self._get_object("virtual_ap")
        except Exception:
            vaps = []
        if vaps:
            return vaps
        try:
            return self._get_object("wlan_virtual_ap")
        except Exception:
            return []

    def _node_has_config(self) -> bool:
        """True when the CURRENT config_path holds real (non-default) AP
        groups or virtual APs."""
        try:
            for item in self._get_object("ap_group"):
                if self._field(item, "profile-name") not in self._DEFAULT_GROUPS:
                    return True
            for item in self._get_virtual_aps():
                if self._field(item, "profile-name") not in ("default",):
                    return True
        except Exception:
            pass
        return False

    def node_candidates(self) -> list[str]:
        """Config-node paths worth probing, deepest first, with the standard
        conductor/managed-device fallbacks appended."""
        candidates = list(self.list_config_nodes())
        for p in ("/mm/mynode", "/mm", "/md"):
            if p not in candidates:
                candidates.append(p)
        return candidates

    def find_config_node(self) -> Optional[str]:
        """When the configured node has no config objects — typical when the
        operator points at a Managed Device, or at the /md root while the
        config lives on a child node — probe the hierarchy + the standard
        fallbacks and return the first node that actually holds config.
        Leaves config_path untouched; returns None when nothing is found."""
        candidates = self.node_candidates()
        original = self.config_path
        try:
            for path in candidates:
                if path == original:
                    continue
                self.config_path = path
                if self._node_has_config():
                    return path
            return None
        finally:
            self.config_path = original

    # ─────────────────── Discovery ───────────────────

    def get_ap_groups(self) -> tuple[list[APGroup], dict[str, list[str]]]:
        """Returns (groups, {group_name: [virtual-ap profile names]})."""
        items = self._get_object("ap_group")
        groups, bindings = [], {}
        for item in items:
            name = self._field(item, "profile-name")
            if not name or name in ("default", "default-campus-ap-group", "NoAuthApGroup"):
                continue
            groups.append(APGroup(name=name))
            # AOS8 returns the VAP list as "virtual_ap" or "virtual-ap" depending on build
            vaps = item.get("virtual_ap") or item.get("virtual-ap") or []
            if isinstance(vaps, dict):
                vaps = [vaps]
            bindings[name] = [v.get("profile-name", "") for v in vaps if v.get("profile-name")]
        return groups, bindings

    def get_ssid_profiles(self) -> dict[str, dict]:
        """wlan ssid-profile data keyed by profile name: essid, opmode, passphrase."""
        profiles = {}
        for item in self._get_object("ssid_prof"):
            name = self._field(item, "profile-name")
            if not name:
                continue
            opmode = ""
            raw_opmode = item.get("opmode", {})
            if isinstance(raw_opmode, dict):
                # opmode arrives as a flag dict, e.g. {"wpa2-psk-aes": true}.
                # A transition-mode profile carries TWO true flags — reducing
                # to flags[0] picks by JSON key order (non-deterministic and
                # invisible to preflight). Rank by security strength and take
                # the strongest; alphabetical tie-break keeps it stable.
                flags = [k for k, v in raw_opmode.items() if v is True]
                opmode = max(flags, key=_opmode_rank) if flags else ""
            elif isinstance(raw_opmode, str):
                opmode = raw_opmode
            profiles[name] = {
                "essid": str(self._field(item, "essid", "wlan-essid")),
                "opmode": opmode,
                "dtim_period": _safe_int(self._field(item, "dtim-period", default=0), 0),
                "max_clients": _safe_int(self._field(item, "max-clients", "max-clients-threshold", default=0), 0),
                "passphrase": str(self._field(item, "wpa-passphrase", "wpa-hexkey", default="")) or None,
                # hidden SSID (no beaconed ESSID). AOS builds expose this as a
                # bare flag dict ({"hide": {...}}) or a boolean-ish scalar
                # under hide/hide_ssid spellings; absence means broadcast.
                "hidden": _flag_or_bool(item, "hide", "hide_ssid", "hide-ssid"),
            }
        return profiles

    def get_ssids(self, captive_portals: Optional[dict[str, dict]] = None) -> list[SSID]:
        """Virtual APs as SSIDs.

        captive_portals maps aaa-profile → {url, redirect} (from
        aos8_parser.mc_captive_portals on the running-config): the object API
        does not expose the external-captive-portal chain, and dropping it
        migrates a guest SSID as a fully open network."""
        captive_portals = captive_portals or {}
        ssid_profiles = {}
        try:
            ssid_profiles = self.get_ssid_profiles()
        except Exception:
            pass  # opmode/essid enrichment is best-effort
        aaa_sgs: dict[str, str] = {}
        aaa_mac_sgs: dict[str, str] = {}
        aaa_resolved = True
        try:
            aaa_sgs = self.get_aaa_server_groups()
            aaa_mac_sgs = self.get_aaa_mac_server_groups()
        except Exception:
            # NOT best-effort anymore: with this read failed, an opensystem
            # SSID's MAC-auth binding is unprovable and an enterprise SSID's
            # server group is a guess. get_ssids marks those SSIDs
            # auth_unprovable and preflight hard-blocks them (#4).
            aaa_resolved = False

        ssids, seen = [], set()
        for item in self._get_virtual_aps():
            name = self._field(item, "profile-name")
            if not name or name in seen:
                continue
            seen.add(name)

            vlan_token = self._field(item, "vlan", default=1)
            vlan = _safe_vlan(vlan_token)
            vlan_raw = (str(vlan_token)
                        if _vlan_is_named(vlan_token) or _vlan_is_pool(vlan_token)
                        else None)
            aaa_ref = self._profile_ref(item, "aaa_prof")
            fwd_raw = str(self._field(item, "forward-mode", "forward_mode", default="tunnel")).lower()
            if "bridge" in fwd_raw:
                fwd = ForwardMode.BRIDGE
            elif "split" in fwd_raw:
                fwd = ForwardMode.SPLIT
            else:
                fwd = ForwardMode.TUNNEL

            # AOS8 returns the SSID profile ref as "ssid_prof", "ssid-profile",
            # or "ssid-prof" depending on firmware build — try all three
            prof_name = (self._profile_ref(item, "ssid_prof")
                         or self._profile_ref(item, "ssid-profile")
                         or self._profile_ref(item, "ssid-prof"))
            prof = ssid_profiles.get(prof_name, {})
            auth, auth_known = _opmode_to_auth(prof.get("opmode", ""))
            mac_sg = aaa_mac_sgs.get(aaa_ref, "")
            if auth == AuthType.OPEN and mac_sg:
                # opensystem + mac-server-group on the bound aaa-profile is a
                # MAC-auth network (legacy printer/IoT SSIDs are exactly this)
                # — migrating it as OPEN publishes a wide-open network.
                auth = AuthType.MAC
            # #4: with the aaa-profile read failed, opensystem may be masked
            # MAC-auth and enterprise RADIUS binding is a guess — NEVER hand
            # such an SSID to provision. PSK/OWE carry no RADIUS binding, so
            # their (known) opmode stands on its own.
            auth_unprovable = (
                not aaa_resolved and bool(aaa_ref)
                and auth in (AuthType.OPEN, AuthType.WPA2_ENTERPRISE,
                             AuthType.WPA3_ENTERPRISE))

            # per-VAP band selection ("all"/"a"/"g") → New Central rf-band enum,
            # mirroring paste mode's allowed-band mapping
            band_raw = str(self._field(item, "rf_band_tristate", "vap_rf_band",
                                       default="")).lower()
            rf_band = {"all": "BAND_ALL", "a": "5GHZ", "g": "24GHZ"}.get(band_raw, "")

            cp = captive_portals.get(aaa_ref, {})
            ssids.append(SSID(
                name=name,
                vlan=vlan,
                vlan_raw=vlan_raw,
                forward_mode=fwd,
                auth_type=auth,
                auth_known=auth_known,
                auth_unprovable=auth_unprovable,
                essid=prof.get("essid") or None,
                broadcast=not prof.get("hidden", False),
                psk=prof.get("passphrase"),
                # prefer the real server group from inside the aaa-profile;
                # the profile name is only a last-resort placeholder. A
                # MAC-auth SSID's group is its mac-server-group.
                auth_server_group=(mac_sg if auth == AuthType.MAC and mac_sg
                                   else aaa_sgs.get(aaa_ref) or aaa_ref or None),
                rf_band=rf_band,
                dtim_period=int(prof.get("dtim_period", 0) or 0),
                max_clients=int(prof.get("max_clients", 0) or 0),
                # an administratively disabled virtual-AP must not migrate as
                # active. A build exposing neither key falls back to the
                # current behaviour (enabled) rather than disabling live WLANs.
                enabled=bool(self._field(item, "vap-enable", "vap_enable",
                                         default=True)),
                captive_portal_url=cp.get("url", ""),
                captive_portal_redirect=cp.get("redirect", ""),
            ))
        return ssids

    def get_vlans(self) -> list[VLAN]:
        vlans = []
        for item in self._get_object("vlan_id"):
            vid = _safe_vlan(self._field(item, "id", default=0), default=0)
            if vid > 0:
                vlans.append(VLAN(
                    id=vid,
                    name=str(self._field(item, "description", "name", default="")) or f"vlan{vid}",
                ))
        return vlans

    def get_radius_servers(self) -> list[RadiusServer]:
        servers = []
        for item in self._get_object("rad_server"):
            name = self._field(item, "rad_server_name", "profile-name")
            addr = str(self._field(item, "host", "rad_host", default=""))
            if name and addr:
                servers.append(RadiusServer(
                    name=name,
                    address=addr,
                    auth_port=_safe_int(self._field(item, "authport", "rad_authport", default=1812), 1812),
                    acct_port=_safe_int(self._field(item, "acctport", "rad_acctport", default=1813), 1813),
                ))
        return servers

    def get_aaa_server_groups(self) -> dict[str, str]:
        """aaa-profile name → RADIUS server-group name. The virtual-ap
        references an aaa-profile, but the actual server group hangs off the
        profile's dot1x-server-group (802.1X) or mac-server-group (MAC auth)
        — the aaa-profile name itself is NOT a server group."""
        out: dict[str, str] = {}
        for item in self._get_object("aaa_prof"):
            name = self._field(item, "profile-name")
            if not name:
                continue
            sg = ""
            for key in ("dot1x_server_group", "dot1x-server-group",
                        "mac_server_group", "mac-server-group"):
                ref = item.get(key)
                if isinstance(ref, dict):
                    # reference dicts vary by build: profile-name / srv-group
                    sg = str(ref.get("profile-name") or ref.get("srv-group")
                             or ref.get("srv_group") or "")
                    if not sg:
                        strs = [v for v in ref.values() if isinstance(v, str)]
                        sg = strs[0] if len(strs) == 1 else ""
                elif isinstance(ref, str):
                    sg = ref
                if sg:
                    break
            if sg:
                out[str(name)] = sg
        return out

    def get_aaa_mac_server_groups(self) -> dict[str, str]:
        """aaa-profile name → MAC-auth server-group name (mac-server-group).
        Separate from get_aaa_server_groups: an opensystem SSID bound to a
        profile with this set is a MAC-auth network, not an open one."""
        out: dict[str, str] = {}
        for item in self._get_object("aaa_prof"):
            name = self._field(item, "profile-name")
            if not name:
                continue
            sg = ""
            for key in ("mac_server_group", "mac-server-group"):
                ref = item.get(key)
                if isinstance(ref, dict):
                    sg = str(ref.get("profile-name") or ref.get("srv-group")
                             or ref.get("srv_group") or "")
                    if not sg:
                        strs = [v for v in ref.values() if isinstance(v, str)]
                        sg = strs[0] if len(strs) == 1 else ""
                elif isinstance(ref, str):
                    sg = ref
                if sg:
                    break
            if sg:
                out[str(name)] = sg
        return out

    def get_server_groups(self) -> list[ServerGroup]:
        groups = []
        for item in self._get_object("server_group_prof"):
            name = self._field(item, "sg_name", "profile-name")
            if name in ("default", "internal"):
                continue  # built-in server groups — noise, not customer config
            servers = item.get("auth_server", [])
            if isinstance(servers, dict):
                servers = [servers]
            names = [s.get("name", "") for s in servers if isinstance(s, dict)]
            if name:
                groups.append(ServerGroup(name=name, servers=[s for s in names if s]))
        return groups

    def get_active_aps(self) -> list[AP]:
        """AP inventory from `show ap database long` (includes serial + group)."""
        doc = self._show("show ap database long")
        rows = []
        for key, val in doc.items():
            if key.startswith("AP Database") and isinstance(val, list):
                rows = val
                break
        aps = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            serial = str(row.get("Serial #", row.get("Serial#", ""))).strip().upper()
            model = str(row.get("AP Type", "")).strip()
            name = str(row.get("Name", "")).strip()
            if not (name or serial):
                continue
            status_raw = str(row.get("Status", ""))
            aps.append(AP(
                serial=serial,
                model=_normalize_model(model),
                mac=str(row.get("Wired MAC Address", "")).strip(),
                name=name or serial,
                # `-`/blank means "no group" in `show ap database` output —
                # normalize like paste mode, or a device group literally
                # named "-" gets provisioned (B14)
                ap_group=_normalize_group_cell(row.get("Group", "")),
                ip=str(row.get("IP Address", "")).strip(),
                status="Up" if status_raw.lower().startswith("up") else (status_raw or "unknown"),
            ))
        return aps

    def get_controller_ip(self) -> tuple[str, int]:
        try:
            text = self._show_text("show controller-ip")
            ip, vlan = self.ip, 1
            m = re.search(r"Switch IP Address:\s*([\d.]+)", text, re.IGNORECASE)
            if m:
                ip = m.group(1)
            m = re.search(r"Vlan Interface:\s*(\d+)", text, re.IGNORECASE)
            if m:
                vlan = int(m.group(1))
            return ip, vlan
        except Exception:
            return self.ip, 1

    def get_mc_firmware(self) -> str:
        try:
            text = self._show_text("show version")
            m = re.search(r"Version\s+(\d+\.\d+\.\d+\.\d+)", text)
            if m:
                return m.group(1)
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", text)
            if m:
                return m.group(1)
        except Exception:
            pass
        return "unknown"

    def get_cluster_info(self) -> Optional[ClusterInfo]:
        try:
            text = self._show_text("show lc-cluster group-membership")
            members, ctype = [], "L2"
            for line in text.splitlines():
                # e.g.: "peer  10.17.65.34  128  L2-Connected  CONNECTED (Leader...)"
                m = re.match(r"\s*(self|peer)\s+(\d+\.\d+\.\d+\.\d+)", line)
                if m and m.group(2) not in members:
                    members.append(m.group(2))
                if re.search(r"L3-Connected", line, re.IGNORECASE):
                    ctype = "L3"
            if len(members) > 1:
                return ClusterInfo(type=ctype, members=members, active_mc_ip=self.ip)
        except Exception:
            pass
        return None

    # ─────────────────── Full pull ───────────────────

    def _pull_objects(self, captive_portals: Optional[dict[str, dict]] = None):
        """The config_path-sensitive object reads."""
        ap_groups, vap_bindings = self.get_ap_groups()
        ssids = self.get_ssids(captive_portals)
        vlans = self.get_vlans()
        radius = self.get_radius_servers()
        sgroups = self.get_server_groups()
        return ap_groups, vap_bindings, ssids, vlans, radius, sgroups

    @staticmethod
    def _attach_aps(ap_groups: list[APGroup], aps: list[AP]) -> None:
        """Attach APs to their groups; create groups for any AP whose group
        wasn't in the configured list so no AP is dropped from provisioning."""
        by_name = {g.name: g for g in ap_groups}
        for ap in aps:
            grp = by_name.get(ap.ap_group)
            if grp is None and ap.ap_group:
                grp = APGroup(name=ap.ap_group)
                ap_groups.append(grp)
                by_name[ap.ap_group] = grp
            if grp is not None:
                if ap.serial and ap.serial not in grp.ap_serials:
                    grp.ap_serials.append(ap.serial)
                if ap.model and ap.model not in grp.ap_models:
                    grp.ap_models.append(ap.model)

    _SHOW_COMMANDS = (
        ("running_config", "show running-config"),
        ("ap_group", "show ap-group"),
        ("ap_database", "show ap database long"),
        ("aaa_auth_server", "show aaa authentication-server radius"),
        ("lc_cluster", "show lc-cluster group-membership"),
        ("controller_ip", "show controller-ip"),
        ("version", "show version"),
    )

    def _show_outputs(self, config_path: Any) -> tuple[dict, dict]:
        """Every discovery show command at one node. Returns (outputs, errors)
        — a per-command failure must be recorded, not flattened into "" and
        reported as "this controller has no config"."""
        outputs: dict[str, str] = {}
        errors: dict[str, str] = {}
        for key, cmd in self._SHOW_COMMANDS:
            # `show running-config` routinely takes 30-60s on a production
            # conductor; self.timeout (15s) would turn that into an empty parse
            timeout = max(self.timeout, 120) if key == "running_config" else None
            try:
                outputs[key] = self._show_text(cmd, config_path=config_path,
                                               timeout=timeout)
            except (AOS8APIError, requests.RequestException, ValueError) as e:
                outputs[key] = ""
                errors[key] = str(e)
        return outputs, errors

    def pull_config_via_show(self) -> CustomerConfig:
        """Fallback discovery from the same CLI outputs paste mode parses,
        fetched over the API's showcommand endpoint.

        HPE documents only `command` + `UIDARUBA` for showcommand, so a
        config_path is not guaranteed to be ignored — re-running the fallback
        at the same failing node returns the same empty parse. Try every node
        find_config_node would probe, then None (send no config_path at all),
        and take the first parse that actually yields config."""
        from .aos8_parser import parse_customer_config
        candidates: list = []
        for cp in [self.config_path] + self.node_candidates() + [None]:
            if cp not in candidates:
                candidates.append(cp)
        first: Optional[CustomerConfig] = None
        for cp in candidates:
            outputs, errors = self._show_outputs(cp)
            cfg = parse_customer_config(outputs, mc_ip=self.ip)
            if cfg.ap_groups or cfg.ssids:
                self.show_errors = errors
                return cfg
            if first is None:
                first = cfg
                self.show_errors = errors
        return first

    def pull_config(self) -> CustomerConfig:
        from .aos8_parser import detect_auth_flags, mc_captive_portals
        self.pull_method = "object-api"
        fw = self.get_mc_firmware()
        mc_ip, ctrl_vlan = self.get_controller_ip()
        # The running-config text carries what the object API does not expose:
        # EAP termination and internal-auth usage (both BLOCKING preflight
        # checks, which could never fire on an API pull) and the external
        # captive-portal chain.
        self.running_config_error = ""
        try:
            running = self._show_text("show running-config",
                                      timeout=max(self.timeout, 120))
        except (AOS8APIError, requests.RequestException, ValueError) as e:
            running = ""
            self.running_config_error = str(e)
        has_eap, has_internal = detect_auth_flags(running)
        captive_portals = mc_captive_portals(running) if running else {}
        # A wrong config_path answers HTTP 200 + a _global_result error, which
        # _get_json now raises on. That is a reason to PROBE other nodes, not
        # to abort — but the error must not be forgotten either: if nothing
        # answers, it is re-raised instead of reporting an empty controller.
        node_error: Optional[Exception] = None
        try:
            ap_groups, vap_bindings, ssids, vlans, radius, sgroups = \
                self._pull_objects(captive_portals)
        except AOS8APIError as e:
            node_error = e
            ap_groups, vap_bindings, ssids, vlans, radius, sgroups = \
                [], {}, [], [], [], []
        if not ssids:
            # SSIDs missing (even if groups came back) — the WLAN config may
            # live at a different node than the AP group config. Re-probe.
            detected = self.find_config_node()
            if detected:
                self.config_path = detected
                # running-config was fetched at the ORIGINAL node — everything
                # derived from it (external captive-portal chain, EAP offload,
                # internal-auth flags) belongs to that node. Re-fetch at the
                # detected node or the re-probed SSIDs inherit stale flags;
                # worst case an external-captive-portal guest SSID migrates
                # as a fully open network while both BLOCKING preflight
                # checks silently pass. Same failure semantics as the initial
                # fetch: empty parse, recorded error, never stale data.
                try:
                    running = self._show_text(
                        "show running-config",
                        timeout=max(self.timeout, 120))
                    self.running_config_error = ""
                except (AOS8APIError, requests.RequestException,
                        ValueError) as e:
                    running = ""
                    self.running_config_error = str(e)
                has_eap, has_internal = detect_auth_flags(running)
                captive_portals = mc_captive_portals(running) if running else {}
                ap_groups, vap_bindings, ssids, vlans, radius, sgroups = \
                    self._pull_objects(captive_portals)
                node_error = None
        if not ssids:
            # Last resort: the object API exposes no WLAN config on this box
            # (managed devices often don't) — parse the CLI show output
            # instead, exactly like paste mode.
            cfg = self.pull_config_via_show()
            if cfg.ap_groups or cfg.ssids:
                self.pull_method = "showcommand"
                # the structured AP-database read is more reliable than the
                # text table; backfill if the text parse came up empty
                if not cfg.aps:
                    try:
                        cfg.aps = self.get_active_aps()
                    except Exception:
                        cfg.aps = []
                    self._attach_aps(cfg.ap_groups, cfg.aps)
                if not cfg.server_groups:
                    cfg.server_groups = sgroups
                if not cfg.cluster:
                    cfg.cluster = self.get_cluster_info()
                if cfg.mc_firmware in ("", "unknown"):
                    cfg.mc_firmware = fw
                # the object-API run may have read a running-config the show
                # fallback's node could not — never downgrade a True flag
                cfg.has_eap_offload = cfg.has_eap_offload or has_eap
                cfg.has_internal_auth = cfg.has_internal_auth or has_internal
                # the CLI fallback answered, but the object API did not — the
                # operator still needs to know WHY, or a group-only pull with
                # no SSIDs looks like the controller simply has none
                self.object_read_error = str(node_error) if node_error else ""
                return cfg
        if node_error is not None and not (ap_groups or ssids):
            # nothing anywhere answered — surface the controller's own reason
            raise node_error
        self.ap_scan_error = ""
        try:
            aps = self.get_active_aps()
        except Exception as e:
            # one hiccup on `show ap database long` must not discard a fully
            # successful group/SSID/VLAN/RADIUS pull — but the degradation is
            # recorded so a zero-AP result isn't read as "no APs on this box"
            aps = []
            self.ap_scan_error = str(e)
        cluster = self.get_cluster_info()
        self._attach_aps(ap_groups, aps)

        # The factory "default" virtual-AP (essid aruba-ap) exists on every
        # controller; keep it only when a real (non-default) AP group binds it.
        bound_vaps = {n for names in vap_bindings.values() for n in names}
        ssids = [s for s in ssids if s.name != "default" or s.name in bound_vaps]

        # Per-group SSID membership from the discovered virtual-ap bindings;
        # fall back to "all SSIDs" only when a group has no binding data.
        mapping_incomplete = False
        all_ssid_names = [s.name for s in ssids]
        for grp in ap_groups:
            bound = vap_bindings.get(grp.name)
            if bound:
                grp.ssids = [n for n in bound if n in all_ssid_names]
            else:
                grp.ssids = list(all_ssid_names)
                mapping_incomplete = True

        return CustomerConfig(
            mc_ip=mc_ip,
            mc_firmware=fw,
            controller_vlan=ctrl_vlan,
            ap_groups=ap_groups,
            ssids=ssids,
            aps=aps,
            vlans=vlans,
            radius_servers=radius,
            server_groups=sgroups,
            cluster=cluster,
            has_eap_offload=has_eap,
            has_internal_auth=has_internal,
            ssid_mapping_incomplete=mapping_incomplete,
        )


# ─────────────────── Helpers ───────────────────

def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# digits not preceded by '-' (negatives) or another digit (mid-number)
# A VLAN token is only numeric when the whole (comma/space-separated) token is
# a number or a numeric range — digits INSIDE a name ("guest2020") must not be
# mistaken for a VLAN id.
_VLAN_TOKEN_RE = re.compile(r"^(\d+)(?:-\d+)?$")


def _vlan_tokens(value: Any) -> list[str]:
    return [t for t in re.split(r"[,\s]+", str(value).strip()) if t]


def _safe_vlan(value: Any, default: int = 1) -> int:
    """VLAN fields can be '100', '100,200', '100-105', or a named VLAN — take
    the first valid id (1-4094). Named VLANs (even ones containing digits,
    like 'guest2020') return default; callers should also record the raw
    token (SSID.vlan_raw) so preflight can flag it."""
    for tok in _vlan_tokens(value):
        m = _VLAN_TOKEN_RE.match(tok)
        if m:
            vid = int(m.group(1))
            if 1 <= vid <= 4094:
                return vid
    return default


def _vlan_is_named(value: Any) -> bool:
    """True when the VLAN token has no usable numeric id (named VLAN pool)."""
    for tok in _vlan_tokens(value):
        m = _VLAN_TOKEN_RE.match(tok)
        if m and 1 <= int(m.group(1)) <= 4094:
            return False
    return True


def _vlan_is_pool(value: Any) -> bool:
    """True when the token carries more than one numeric id — a comma list
    ('100,200') or a range ('100-105'). _safe_vlan collapses these to the
    first id, so callers must record the raw token (SSID.vlan_raw) and let
    preflight force an operator mapping, exactly like a named VLAN."""
    ids = set()
    for tok in _vlan_tokens(value):
        m = re.match(r"^(\d+)(?:-(\d+))?$", tok)
        if not m:
            continue
        ids.add(int(m.group(1)))
        if m.group(2):
            ids.add(int(m.group(2)))
    return len(ids) > 1


_GROUP_CELL_PLACEHOLDERS = {"", "-", "--", "\u2014", "n/a", "na", "none"}


def _normalize_group_cell(token: Any) -> str:
    """AP-database Group column placeholders mean the default group — mirror
    the paste parser's normalization so API and paste discovery agree."""
    t = str(token or "").strip()
    return "default" if t.lower() in _GROUP_CELL_PLACEHOLDERS else t


def _normalize_model(model: Any) -> str:
    """'205' -> 'AP-205'; leaves 'AP-515'/'IAP-315' untouched."""
    model = str(model or "").strip().upper().replace(" ", "-")
    if re.fullmatch(r"\d+[A-Z]*", model):
        return f"AP-{model}"
    return model


def _flag_or_bool(item: dict, *names: str) -> bool:
    """AOS flag-or-scalar: a directive present as a sub-dict (flag style,
    e.g. {"hide": {...}}) counts as set; scalars accept the usual truthy
    spellings. Absent = False."""
    for n in names:
        for k in (n, n.replace("-", "_"), n.replace("_", "-")):
            if k not in item:
                continue
            v = item[k]
            if isinstance(v, dict):
                return True
            if isinstance(v, str):
                return v.strip().lower() in ("true", "enable", "enabled", "yes", "1")
            return bool(v)
    return False


def _opmode_rank(token: str) -> tuple[int, str]:
    """Security-strength rank for an AOS 8 opmode flag token — used to reduce
    a transition-mode flag dict deterministically (strongest wins). The token
    itself is the tie-break so equal-strength sets always resolve the same
    way. Unknown/future tokens rank BELOW known ones so they are never
    silently preferred, but also never silently mask a known flag."""
    op = (token or "").lower()
    if "sae" in op or "wpa3" in op:
        return 40, op
    if "owe" in op or "enhanced-open" in op:
        return 30, op
    if "psk" in op or "dot1x" in op or "8021x" in op or "enterprise" in op:
        return 20, op
    if "opensystem" in op or op == "open":
        return 10, op
    return 0, op


def _opmode_to_auth(opmode: str) -> tuple[AuthType, bool]:
    """Map an AOS 8 ssid-profile opmode to an AuthType. Returns (auth, known)."""
    op = (opmode or "").lower()
    if not op:
        return AuthType.WPA2_ENTERPRISE, False
    if "opensystem" in op or op == "open":
        return AuthType.OPEN, True
    if "enhanced-open" in op or "owe" in op:
        # OWE / Enhanced Open is ENCRYPTED. Mapping it to OPEN silently
        # publishes the migrated network with no encryption at all, and
        # known=True keeps preflight quiet while it happens.
        return AuthType.OWE, True
    if "wep" in op:
        # static-wep / dynamic-wep — no AOS 10 equivalent. Marked known so the
        # generic "auth unresolved" warning stays quiet; preflight FAILs WEP
        # explicitly and both clients refuse to provision it.
        return AuthType.WEP, True
    if "sae" in op or "wpa3-personal" in op:
        return AuthType.WPA3_SAE, True
    if "psk" in op:
        return AuthType.WPA2_PSK, True
    if "wpa3" in op or "ccm" in op or "gcm" in op:
        return AuthType.WPA3_ENTERPRISE, True
    return AuthType.WPA2_ENTERPRISE, True


def is_model_compatible(model: Any) -> bool:
    if not model:
        return True  # unknown model — don't block; preflight flags blanks
    norm = _normalize_model(model)
    for suffix in _COUNTRY_SUFFIXES:
        if norm.endswith(suffix):
            norm = norm[: -len(suffix)]
            break
    if norm in INCOMPATIBLE_MODELS:
        return False
    # IAP/AP prefixes are interchangeable hardware-wise
    alt = "IAP-" + norm[3:] if norm.startswith("AP-") else "AP-" + norm[4:] if norm.startswith("IAP-") else norm
    return alt not in INCOMPATIBLE_MODELS
