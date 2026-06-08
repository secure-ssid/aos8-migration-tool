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
INCOMPATIBLE_MODELS = {
    "IAP-103", "IAP-104", "IAP-105",
    "IAP-134", "IAP-135",
    "IAP-175", "IAP-175P", "IAP-175AC",
    "IAP-204", "IAP-205",
    "IAP-214", "IAP-215",
    "IAP-224", "IAP-225",
    "IAP-274", "IAP-275",
    "IAP-315",
    "AP-204", "AP-205",
    "AP-214", "AP-215",
    "AP-224", "AP-225",
    "AP-274", "AP-275",
    "AP-315",
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
        """Fetch the first present key; AOS unwraps some values as {key: {key: val}}."""
        for n in names:
            if n in item:
                val = item[n]
                if isinstance(val, dict) and n in val:
                    return val[n]
                return val
        return default

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
            vaps = item.get("virtual_ap", [])
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
                "essid": str(self._field(item, "essid")),
                "opmode": opmode,
                "dtim_period": _safe_int(self._field(item, "dtim-period", default=0), 0),
                "max_clients": _safe_int(self._field(item, "max-clients-threshold", default=0), 0),
                "passphrase": str(self._field(item, "wpa-passphrase", "wpa-hexkey", default="")) or None,
            }
        return profiles

    def get_ssids(self) -> list[SSID]:
        ssid_profiles = {}
        try:
            ssid_profiles = self.get_ssid_profiles()
        except Exception:
            pass  # opmode/essid enrichment is best-effort

        ssids, seen = [], set()
        for item in self._get_object("wlan_virtual_ap"):
            name = self._field(item, "profile-name")
            if not name or name in seen:
                continue
            seen.add(name)

            vlan_token = self._field(item, "vlan", default=1)
            vlan = _safe_vlan(vlan_token)
            vlan_raw = str(vlan_token) if _vlan_is_named(vlan_token) else None
            fwd_raw = str(self._field(item, "forward-mode", default="tunnel")).lower()
            if "bridge" in fwd_raw:
                fwd = ForwardMode.BRIDGE
            elif "split" in fwd_raw:
                fwd = ForwardMode.SPLIT
            else:
                fwd = ForwardMode.TUNNEL

            prof_name = str(self._field(item, "ssid_prof", default=""))
            prof = ssid_profiles.get(prof_name, {})
            auth, auth_known = _opmode_to_auth(prof.get("opmode", ""))

            ssids.append(SSID(
                name=name,
                vlan=vlan,
                vlan_raw=vlan_raw,
                forward_mode=fwd,
                auth_type=auth,
                auth_known=auth_known,
                essid=prof.get("essid") or None,
                psk=prof.get("passphrase"),
                auth_server_group=str(self._field(item, "aaa_prof", default="")) or None,
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
            name = self._field(item, "profile-name")
            addr = str(self._field(item, "host", default=""))
            if name and addr:
                servers.append(RadiusServer(
                    name=name,
                    address=addr,
                    auth_port=_safe_int(self._field(item, "authport", default=1812), 1812),
                    acct_port=_safe_int(self._field(item, "acctport", default=1813), 1813),
                ))
        return servers

    def get_server_groups(self) -> list[ServerGroup]:
        groups = []
        for item in self._get_object("server_group_prof"):
            name = self._field(item, "profile-name")
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

    def pull_config(self) -> CustomerConfig:
        fw = self.get_mc_firmware()
        mc_ip, ctrl_vlan = self.get_controller_ip()
        ap_groups, vap_bindings = self.get_ap_groups()
        ssids = self.get_ssids()
        vlans = self.get_vlans()
        radius = self.get_radius_servers()
        sgroups = self.get_server_groups()
        aps = self.get_active_aps()
        cluster = self.get_cluster_info()

        # Attach APs to their groups; create groups for any AP whose group
        # wasn't in the configured list so no AP is dropped from provisioning.
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
_VLAN_ID_RE = re.compile(r"(?<![-\d])\d+")


def _safe_vlan(value: Any, default: int = 1) -> int:
    """VLAN fields can be '100', '100,200', or a named VLAN — take the first
    valid id (1-4094). Named VLANs return default; callers should also record
    the raw token (SSID.vlan_raw) so preflight can flag it."""
    for m in _VLAN_ID_RE.finditer(str(value)):
        vid = int(m.group())
        if 1 <= vid <= 4094:
            return vid
    return default


def _vlan_is_named(value: Any) -> bool:
    """True when the VLAN token has no usable numeric id (named VLAN pool)."""
    return not any(1 <= int(m.group()) <= 4094
                   for m in _VLAN_ID_RE.finditer(str(value)))


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
