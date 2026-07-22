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
                 config_path: str = "/md", timeout: int = 15):
        self.base = f"https://{ip}:{AOS8_API_PORT}"
        self.ip = ip
        self.username = username
        self.password = password
        self.config_path = config_path
        self.timeout = timeout
        self.uidaruba: Optional[str] = None
        self.pull_method = "object-api"  # or "showcommand" after a fallback pull
        self.session = requests.Session()
        self.session.verify = False

    # ─────────────────── Auth ───────────────────

    def connect(self) -> bool:
        resp = self.session.post(
            f"{self.base}{LOGIN_PATH}",
            data={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
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

    def _params(self, extra: Optional[dict] = None) -> dict:
        params = {"UIDARUBA": self.uidaruba}
        if self.config_path:
            params["config_path"] = self.config_path
        if extra:
            params.update(extra)
        return params

    def _get_object(self, name: str) -> list[dict]:
        """GET a configuration object; returns its instance list."""
        resp = self.session.get(
            f"{self.base}{CONFIG_PATH_PREFIX}/object/{name}",
            params=self._params(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # Object payloads come back either under "_data" -> {name: [...]}
        # or directly under the object name.
        if isinstance(data.get("_data"), dict):
            data = data["_data"]
        items = data.get(name, [])
        return items if isinstance(items, list) else [items]

    def _show(self, command: str) -> dict:
        """Run a show command; returns the parsed JSON document."""
        resp = self.session.get(
            f"{self.base}{CONFIG_PATH_PREFIX}/showcommand",
            params=self._params({"command": command}),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _show_text(self, command: str) -> str:
        """Run a show command; flatten its _data block to plain text."""
        data = self._show(command).get("_data", "")
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
        """Node paths from the configuration hierarchy (MM only; best-effort —
        returns [] on standalone controllers / managed devices)."""
        try:
            resp = self.session.get(
                f"{self.base}{CONFIG_PATH_PREFIX}/object/node_hierarchy",
                params={"UIDARUBA": self.uidaruba},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            tree = resp.json()
        except Exception:
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

    def find_config_node(self) -> Optional[str]:
        """When the configured node has no config objects — typical when the
        operator points at a Managed Device, or at the /md root while the
        config lives on a child node — probe the hierarchy + the standard
        fallbacks and return the first node that actually holds config.
        Leaves config_path untouched; returns None when nothing is found."""
        candidates = list(self.list_config_nodes())
        for p in ("/mm/mynode", "/mm", "/md"):
            if p not in candidates:
                candidates.append(p)
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
                # opmode arrives as a flag dict, e.g. {"wpa2-psk-aes": true}
                flags = [k for k, v in raw_opmode.items() if v is True]
                opmode = flags[0] if flags else ""
            elif isinstance(raw_opmode, str):
                opmode = raw_opmode
            profiles[name] = {
                "essid": str(self._field(item, "essid", "wlan-essid")),
                "opmode": opmode,
                "dtim_period": _safe_int(self._field(item, "dtim-period", default=0), 0),
                "max_clients": _safe_int(self._field(item, "max-clients", "max-clients-threshold", default=0), 0),
                "passphrase": str(self._field(item, "wpa-passphrase", "wpa-hexkey", default="")) or None,
            }
        return profiles

    def get_ssids(self) -> list[SSID]:
        ssid_profiles = {}
        try:
            ssid_profiles = self.get_ssid_profiles()
        except Exception:
            pass  # opmode/essid enrichment is best-effort
        aaa_sgs: dict[str, str] = {}
        try:
            aaa_sgs = self.get_aaa_server_groups()
        except Exception:
            pass  # server-group resolution is best-effort

        ssids, seen = [], set()
        for item in self._get_virtual_aps():
            name = self._field(item, "profile-name")
            if not name or name in seen:
                continue
            seen.add(name)

            vlan_token = self._field(item, "vlan", default=1)
            vlan = _safe_vlan(vlan_token)
            vlan_raw = str(vlan_token) if _vlan_is_named(vlan_token) else None
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

            # per-VAP band selection ("all"/"a"/"g") → New Central rf-band enum,
            # mirroring paste mode's allowed-band mapping
            band_raw = str(self._field(item, "rf_band_tristate", "vap_rf_band",
                                       default="")).lower()
            rf_band = {"all": "BAND_ALL", "a": "5GHZ", "g": "24GHZ"}.get(band_raw, "")

            ssids.append(SSID(
                name=name,
                vlan=vlan,
                vlan_raw=vlan_raw,
                forward_mode=fwd,
                auth_type=auth,
                auth_known=auth_known,
                essid=prof.get("essid") or None,
                psk=prof.get("passphrase"),
                # prefer the real server group from inside the aaa-profile;
                # the profile name is only a last-resort placeholder
                auth_server_group=aaa_sgs.get(aaa_ref) or aaa_ref or None,
                rf_band=rf_band,
                dtim_period=int(prof.get("dtim_period", 0) or 0),
                max_clients=int(prof.get("max_clients", 0) or 0),
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
                ap_group=str(row.get("Group", "")).strip(),
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

    def _pull_objects(self):
        """The config_path-sensitive object reads (show commands run box-wide
        and don't care about the node)."""
        ap_groups, vap_bindings = self.get_ap_groups()
        ssids = self.get_ssids()
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

    def pull_config_via_show(self) -> CustomerConfig:
        """Fallback discovery from the same CLI outputs paste mode parses,
        fetched over the API's showcommand endpoint — show commands run on
        any box (conductor, managed device, standalone) regardless of
        config_path, unlike the configuration-object API."""
        from .aos8_parser import parse_customer_config
        outputs = {}
        for key, cmd in (
            ("running_config", "show running-config"),
            ("ap_group", "show ap-group"),
            ("ap_database", "show ap database long"),
            ("aaa_auth_server", "show aaa authentication-server radius"),
            ("lc_cluster", "show lc-cluster group-membership"),
            ("controller_ip", "show controller-ip"),
            ("version", "show version"),
        ):
            try:
                outputs[key] = self._show_text(cmd)
            except Exception:
                outputs[key] = ""
        return parse_customer_config(outputs, mc_ip=self.ip)

    def pull_config(self) -> CustomerConfig:
        self.pull_method = "object-api"
        fw = self.get_mc_firmware()
        mc_ip, ctrl_vlan = self.get_controller_ip()
        ap_groups, vap_bindings, ssids, vlans, radius, sgroups = self._pull_objects()
        if not ssids:
            # SSIDs missing (even if groups came back) — the WLAN config may
            # live at a different node than the AP group config. Re-probe.
            detected = self.find_config_node()
            if detected:
                self.config_path = detected
                ap_groups, vap_bindings, ssids, vlans, radius, sgroups = \
                    self._pull_objects()
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
                return cfg
        aps = self.get_active_aps()
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


def _normalize_model(model: Any) -> str:
    """'205' -> 'AP-205'; leaves 'AP-515'/'IAP-315' untouched."""
    model = str(model or "").strip().upper().replace(" ", "-")
    if re.fullmatch(r"\d+[A-Z]*", model):
        return f"AP-{model}"
    return model


def _opmode_to_auth(opmode: str) -> tuple[AuthType, bool]:
    """Map an AOS 8 ssid-profile opmode to an AuthType. Returns (auth, known)."""
    op = (opmode or "").lower()
    if not op:
        return AuthType.WPA2_ENTERPRISE, False
    if "opensystem" in op or op == "open":
        return AuthType.OPEN, True
    if "enhanced-open" in op or "owe" in op:
        # OWE (Enhanced Open) — no AuthType member for it, so map to OPEN
        return AuthType.OPEN, True
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
