"""
Parses AOS 8 CLI show command output into CustomerConfig.
Used as fallback when the REST API is unreachable.

Preferred AP source is `show ap database long` (includes Group, Serial #,
Wired MAC). Tables are parsed by anchoring on the dash separator row, so
column boundaries are exact instead of guessed from whitespace splits.
"""
import re
from typing import Optional

from .models import (
    AP, APGroup, ClusterInfo, CustomerConfig, ForwardMode,
    AuthType, RadiusServer, SSID, VLAN,
)
from .aos8_client import _opmode_to_auth, _normalize_model, _safe_vlan, _vlan_is_named


def parse_customer_config(pasted_outputs: dict[str, str], mc_ip: str = "") -> CustomerConfig:
    """
    pasted_outputs: dict mapping command name to pasted CLI text.
    Recognized keys: "ap_group", "running_config", "ap_database", "ap_active",
                     "aaa_auth_server", "lc_cluster", "controller_ip"
    """
    running = pasted_outputs.get("running_config", "")

    ap_groups = _parse_ap_groups(pasted_outputs.get("ap_group", ""))
    ssid_profiles = _parse_ssid_profiles(running)
    ssids = _parse_ssids_from_running(running, ssid_profiles)
    vap_bindings = _parse_group_vap_bindings(running)
    aps = _parse_ap_database(pasted_outputs.get("ap_database", ""))
    if not aps:
        aps = _parse_ap_active(pasted_outputs.get("ap_active", ""))
    radius = _parse_radius_servers(pasted_outputs.get("aaa_auth_server", ""), running)
    vlans = _parse_vlans(running)
    cluster = _parse_cluster(pasted_outputs.get("lc_cluster", ""), mc_ip)
    mc_ip_parsed, ctrl_vlan = _parse_controller_ip(pasted_outputs.get("controller_ip", ""), mc_ip)
    fw = _parse_firmware(running, pasted_outputs.get("version", ""))

    # Merge groups discovered from running-config bindings and the AP list
    by_name = {g.name: g for g in ap_groups}
    for gname in vap_bindings:
        if gname not in by_name:
            grp = APGroup(name=gname)
            ap_groups.append(grp)
            by_name[gname] = grp

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

    # Per-group SSIDs from virtual-ap bindings; fall back to all SSIDs
    mapping_incomplete = False
    all_ssid_names = [s.name for s in ssids]
    for grp in ap_groups:
        bound = vap_bindings.get(grp.name)
        if bound:
            grp.ssids = [n for n in bound if n in all_ssid_names]
        else:
            grp.ssids = list(all_ssid_names)
            mapping_incomplete = True

    has_eap = "aaa-fastconnect" in running.lower()
    has_internal = "internal" in pasted_outputs.get("aaa_auth_server", "").lower()

    return CustomerConfig(
        mc_ip=mc_ip_parsed or mc_ip,
        mc_firmware=fw,
        controller_vlan=ctrl_vlan,
        ap_groups=ap_groups,
        ssids=ssids,
        aps=aps,
        vlans=vlans,
        radius_servers=radius,
        cluster=cluster,
        has_eap_offload=has_eap,
        has_internal_auth=has_internal,
        ssid_mapping_incomplete=mapping_incomplete,
    )


# ─────────────────── CLI table parsing ───────────────────

def parse_cli_table(text: str, required_cols: tuple[str, ...]) -> list[dict[str, str]]:
    """
    Parse an AOS-style table by locating the dash separator row under the
    header and slicing every data line at the dash-run boundaries:

        Name    Group     AP Type  IP Address    Status        ...
        ----    -----     -------  ----------    ------        ...
        ap-1    campus    335      10.0.0.5      Up 10d:2h     ...
    """
    lines = text.splitlines()
    for i in range(1, len(lines)):
        sep = lines[i]
        if not re.fullmatch(r"[-\s]+", sep) or "-" not in sep:
            continue
        header = lines[i - 1]
        if not all(c.lower() in header.lower() for c in required_cols):
            continue
        starts = [m.start() for m in re.finditer(r"-+", sep)]
        if len(starts) < 2:
            continue
        bounds = list(zip(starts, starts[1:] + [None]))
        cols = [header[s:e].strip() if e else header[s:].strip() for s, e in bounds]

        rows = []
        for line in lines[i + 1:]:
            if not line.strip():
                break  # blank line ends the table
            if re.fullmatch(r"[-\s]+", line):
                continue
            if line.strip().startswith(("Flags:", "Total")):
                break
            row = {}
            for col, (s, e) in zip(cols, bounds):
                row[col] = (line[s:e] if e else line[s:]).strip()
            rows.append(row)
        return rows
    return []


def _row_get(row: dict[str, str], *names: str) -> str:
    for n in names:
        for k, v in row.items():
            if k.lower() == n.lower():
                return v
    return ""


# `show ap database` shows a placeholder in the Group column for APs that are
# in the default group / unprovisioned — never let that become a literal
# device-group name like "-" in Central.
_GROUP_PLACEHOLDERS = {"", "-", "--", "—", "n/a", "na", "none"}


def _clean_group(token: str) -> str:
    t = (token or "").strip()
    return "default" if t.lower() in _GROUP_PLACEHOLDERS else t


# ─────────────────── AP inventory ───────────────────

def _parse_ap_database(text: str) -> list[AP]:
    """`show ap database long` — includes Group, Serial # and Wired MAC."""
    rows = parse_cli_table(text, ("Name", "Group"))
    aps = []
    for row in rows:
        name = _row_get(row, "Name")
        if not name:
            continue
        status_raw = _row_get(row, "Status")
        serial = _row_get(row, "Serial #", "Serial#", "Serial").strip().upper()
        # Column overflow in fixed-width tables can shift trailing cells —
        # blank out values that don't look like what the column should hold
        # (preflight then flags the AP for manual review).
        if serial and not re.fullmatch(r"[A-Z0-9]{6,16}", serial):
            serial = ""
        mac = _row_get(row, "Wired MAC Address", "MAC").strip().lower()
        if mac and not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
            mac = ""
        aps.append(AP(
            serial=serial,  # left empty when the column is absent — never the name
            model=_normalize_model(_row_get(row, "AP Type", "Model")),
            mac=mac,
            name=name,
            ap_group=_clean_group(_row_get(row, "Group")),
            ip=_row_get(row, "IP Address", "IP-Address", "IP"),
            status="Up" if status_raw.lower().startswith("up") else (status_raw or "unknown"),
        ))
    return aps


def _parse_ap_active(text: str) -> list[AP]:
    """`show ap active` fallback — no serial column exists in this output."""
    rows = parse_cli_table(text, ("Name", "IP Address"))
    aps = []
    for row in rows:
        name = _row_get(row, "Name")
        ip = _row_get(row, "IP Address", "IP-Address")
        if not name or not re.match(r"\d+\.\d+\.\d+\.\d+", ip or ""):
            continue
        aps.append(AP(
            serial="",  # `show ap active` has no serial — flagged in preflight
            model=_normalize_model(_row_get(row, "AP Type", "Model", "Type")),
            mac="",
            name=name,
            ap_group=_clean_group(_row_get(row, "Group")),
            ip=ip,
            status="Up",
        ))
    return aps


# ─────────────────── Config blocks ───────────────────

def _parse_ap_groups(text: str) -> list[APGroup]:
    groups, seen = [], set()
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"(?:AP Group|ap-group)\s*[:\s]\s*\"?([\w\-. ]+?)\"?\s*$", line, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            if name and name.lower() not in ("default", "noauthapgroup") and name not in seen:
                seen.add(name)
                groups.append(APGroup(name=name))
    if not groups:
        # plain list output: one group name per line
        for line in text.splitlines():
            line = line.strip()
            if (line and not line.startswith(("#", "-")) and "group" not in line.lower()
                    and len(line.split()) == 1 and re.fullmatch(r"[\w\-.]+", line)
                    and line not in seen):
                seen.add(line)
                groups.append(APGroup(name=line))
    return groups


def _iter_blocks(text: str, opener: str):
    """
    Yield (name, block_lines) for running-config blocks like `wlan ssid-profile "x"`.
    Block contents are the following indented lines (AOS indents members); a
    bare `!` or any non-indented line ends the block.
    """
    pattern = re.compile(rf"^{opener}\s+\"?([^\"\n]+?)\"?\s*$", re.IGNORECASE)
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = pattern.match(lines[i].strip())
        if not m or lines[i][:1].isspace():
            i += 1
            continue
        name = m.group(1).strip()
        block = []
        i += 1
        while i < len(lines):
            raw = lines[i]
            stripped = raw.strip()
            if not stripped or stripped == "!" or not raw[:1].isspace():
                break
            block.append(stripped)
            i += 1
        yield name, block


def _parse_ssid_profiles(running: str) -> dict[str, dict]:
    profiles = {}
    # AOS8 'a-band'/'g-band'/'wmm' → New Central rf-band enum
    _BAND = {("a", "g"): "BAND_ALL", ("a",): "5GHZ", ("g",): "24GHZ"}
    for name, block in _iter_blocks(running, r"wlan ssid-profile"):
        info = {"essid": "", "opmode": "", "passphrase": None,
                "rf_band": "", "dtim_period": 0, "max_clients": 0}
        bands = set()
        for line in block:
            m = re.match(r"essid\s+\"?(.+?)\"?\s*$", line, re.IGNORECASE)
            if m:
                info["essid"] = m.group(1)
            m = re.match(r"opmode\s+([\w\-]+)", line, re.IGNORECASE)
            if m:
                info["opmode"] = m.group(1)
            m = re.match(r"wpa-passphrase\s+(\S+)", line, re.IGNORECASE)
            if m:
                info["passphrase"] = m.group(1)
            m = re.match(r"dtim-period\s+(\d+)", line, re.IGNORECASE)
            if m:
                info["dtim_period"] = int(m.group(1))
            m = re.match(r"max-clients-threshold\s+(\d+)", line, re.IGNORECASE)
            if m:
                info["max_clients"] = int(m.group(1))
            # AOS8 radio enablement: 'allowed-band a g' or 'a-basic-rates …'
            m = re.match(r"allowed-band\s+(.+)$", line, re.IGNORECASE)
            if m:
                bands = {b for b in m.group(1).lower().split() if b in ("a", "g")}
        if bands:
            info["rf_band"] = _BAND.get(tuple(sorted(bands)), "BAND_ALL")
        profiles[name] = info
    return profiles


def _mc_captive_portals(running: str) -> dict[str, dict]:
    """Resolve the MC external-captive-portal chain to aaa-profile → {url,
    redirect}: virtual-ap names an aaa-profile → its initial-role → that
    user-role's `captive-portal <cp>` → the `aaa authentication captive-portal`
    profile (external when its login-page is an off-box URL)."""
    cps: dict[str, dict] = {}                       # cp-profile → {url, redirect}
    for name, block in _iter_blocks(running, r"aaa authentication captive-portal"):
        url, redirect = "", ""
        for line in block:
            m = re.match(r'login-page\s+"?(\S+?)"?\s*$', line, re.IGNORECASE)
            if m and m.group(1).lower().startswith(("http://", "https://")):
                url = m.group(1)
            m = re.match(r'redirect-url\s+"?(\S+?)"?\s*$', line, re.IGNORECASE)
            if m:
                redirect = m.group(1)
        if url:                                     # off-box login-page ⇒ external
            cps[name] = {"url": url, "redirect": redirect}
    role_cp: dict[str, str] = {}                    # user-role → cp-profile
    for name, block in _iter_blocks(running, r"user-role"):
        for line in block:
            m = re.match(r'captive-portal\s+"?(.+?)"?\s*$', line, re.IGNORECASE)
            if m:
                role_cp[name] = m.group(1)
    aaa_role: dict[str, str] = {}                   # aaa-profile → initial-role
    for name, block in _iter_blocks(running, r"aaa profile"):
        for line in block:
            m = re.match(r'initial-role\s+"?(.+?)"?\s*$', line, re.IGNORECASE)
            if m:
                aaa_role[name] = m.group(1)
    aaa_cp: dict[str, dict] = {}                    # aaa-profile → {url, redirect}
    for aaa, role in aaa_role.items():
        cp = cps.get(role_cp.get(role, ""))
        if cp:
            aaa_cp[aaa] = cp
    return aaa_cp


def _parse_ssids_from_running(running: str, ssid_profiles: dict[str, dict]) -> list[SSID]:
    ssids = []
    mc_cps = _mc_captive_portals(running)
    for name, block in _iter_blocks(running, r"wlan virtual-ap"):
        vlan = 1
        vlan_raw = None
        fwd = ForwardMode.TUNNEL
        prof_ref = ""
        aaa_ref = ""
        for line in block:
            low = line.lower()
            m = re.match(r"vlan\s+(\S+)", line, re.IGNORECASE)
            if m:
                vlan = _safe_vlan(m.group(1))
                vlan_raw = m.group(1) if _vlan_is_named(m.group(1)) else None
            elif "forward-mode" in low:
                if "bridge" in low:
                    fwd = ForwardMode.BRIDGE
                elif "split" in low:
                    fwd = ForwardMode.SPLIT
            else:
                m = re.match(r"ssid-profile\s+\"?(.+?)\"?\s*$", line, re.IGNORECASE)
                if m:
                    prof_ref = m.group(1)
                m = re.match(r"aaa-profile\s+\"?(.+?)\"?\s*$", line, re.IGNORECASE)
                if m:
                    aaa_ref = m.group(1)

        prof = ssid_profiles.get(prof_ref, {})
        auth, auth_known = _opmode_to_auth(prof.get("opmode", ""))
        cp = mc_cps.get(aaa_ref, {})
        ssids.append(SSID(
            name=name,
            vlan=vlan,
            vlan_raw=vlan_raw,
            forward_mode=fwd,
            auth_type=auth,
            auth_known=auth_known,
            essid=prof.get("essid") or None,
            psk=prof.get("passphrase"),
            auth_server_group=aaa_ref or None,
            rf_band=prof.get("rf_band", ""),
            dtim_period=prof.get("dtim_period", 0),
            max_clients=prof.get("max_clients", 0),
            captive_portal_url=cp.get("url", ""),
            captive_portal_redirect=cp.get("redirect", ""),
        ))
    return ssids


def _parse_group_vap_bindings(running: str) -> dict[str, list[str]]:
    """ap-group blocks → list of bound virtual-ap profile names."""
    bindings = {}
    for name, block in _iter_blocks(running, r"ap-group"):
        vaps = []
        for line in block:
            m = re.match(r"virtual-ap\s+\"?(.+?)\"?\s*$", line, re.IGNORECASE)
            if m:
                vaps.append(m.group(1))
        if vaps:
            bindings[name] = vaps
    return bindings


def _parse_radius_servers(auth_text: str, running_text: str) -> list[RadiusServer]:
    servers, seen = [], set()
    for name, block in _iter_blocks(running_text, r"aaa authentication-server radius"):
        host, auth_port, acct_port = "", 1812, 1813
        for line in block:
            m = re.match(r"host\s+\"?([\w\-.]+)\"?", line, re.IGNORECASE)
            if m:
                host = m.group(1)
            m = re.match(r"authport\s+(\d+)", line, re.IGNORECASE)
            if m:
                auth_port = int(m.group(1))
            m = re.match(r"acctport\s+(\d+)", line, re.IGNORECASE)
            if m:
                acct_port = int(m.group(1))
        if host and name not in seen:
            seen.add(name)
            servers.append(RadiusServer(name=name, address=host,
                                        auth_port=auth_port, acct_port=acct_port))

    # `show aaa authentication-server radius` summary table fallback
    for line in auth_text.splitlines():
        m = re.match(r"([\w\-.]+)\s+([\d.]+)\s+\d+", line.strip())
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            servers.append(RadiusServer(name=m.group(1), address=m.group(2)))
    return servers


def _expand_vlan_spec(spec: str) -> list[int]:
    """'1,10-12, 200' → [1, 10, 11, 12, 200]; bad tokens are skipped."""
    ids = []
    for token in spec.split(","):
        token = token.strip()
        m = re.fullmatch(r"(\d+)-(\d+)", token)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if 0 < lo <= hi <= 4094:
                ids.extend(range(lo, hi + 1))
        elif token.isdigit():
            ids.append(int(token))
    return ids


def _parse_vlans(text: str) -> list[VLAN]:
    vlans, seen = [], {}

    def add(vid: int, name: str = ""):
        if 0 < vid <= 4094 and vid not in seen:
            seen[vid] = VLAN(id=vid, name=name or f"vlan{vid}")
            vlans.append(seen[vid])

    for line in text.splitlines():
        line = line.strip()
        # single VLAN with optional name: vlan 100 "User-VLAN"
        m = re.match(r"vlan\s+(\d+)(?:\s+\"?([\w\-. ]+?)\"?)?\s*$", line, re.IGNORECASE)
        if m:
            add(int(m.group(1)), m.group(2) or "")
            continue
        # bulk declarations: vlan 1,10-20,200
        m = re.match(r"vlan\s+([\d,\-\s]+)$", line, re.IGNORECASE)
        if m:
            for vid in _expand_vlan_spec(m.group(1)):
                add(vid)
            continue
        m = re.match(r"interface vlan\s+(\d+)", line, re.IGNORECASE)
        if m:
            add(int(m.group(1)))
    return vlans


def _parse_cluster(text: str, mc_ip: str) -> Optional[ClusterInfo]:
    if not text.strip():
        return None
    members, ctype = [], "L2"
    for line in text.splitlines():
        m = re.match(r"\s*(self|peer)\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m and m.group(2) not in members:
            members.append(m.group(2))
        if re.search(r"L3-Connected", line, re.IGNORECASE):
            ctype = "L3"
    if not members:  # older formats: any IPs in the member table
        for line in text.splitlines():
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if m and m.group(1) not in members:
                members.append(m.group(1))
    if len(members) >= 2:
        return ClusterInfo(type=ctype, members=members, active_mc_ip=mc_ip)
    return None


def _parse_controller_ip(text: str, mc_ip: str) -> tuple[str, int]:
    ip, vlan = mc_ip, 1
    m = re.search(r"Switch IP Address:\s*([\d.]+)", text, re.IGNORECASE)
    if m:
        ip = m.group(1)
    else:
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", text)
        if m:
            ip = m.group(1)
    m = re.search(r"Vlan Interface:\s*(\d+)", text, re.IGNORECASE)
    if m:
        vlan = int(m.group(1))
    return ip, vlan


def _parse_firmware(text: str, version_text: str = "") -> str:
    # Prefer `show version` output — running-config only carries "version 8.10"
    for source in (version_text, text):
        for line in source.splitlines():
            m = re.search(r"(?:ArubaOS|Version)\s*[^\d]*(\d+\.\d+\.\d+[\.\d]*)", line)
            if m:
                return m.group(1)
    # running-config headers carry a lowercase `version 8.10.0.6` (Instant)
    # or partial `version 8.10` (controller) — accept 2-4 components
    m = re.search(r"^\s*version\s+(\d+\.\d+(?:\.\d+){0,2})\s*$", text,
                  re.MULTILINE | re.IGNORECASE)
    if m:
        return m.group(1)
    return "unknown"


# ═══════════════════ Instant (IAP) cluster parsing ═══════════════════
#
# Instant has no virtual-ap / ap-group layer: each `wlan ssid-profile`
# block IS the WLAN (essid, opmode, vlan, passphrase inline). Optional
# zones map SSIDs to subsets of APs; with no zones, one synthetic group
# covers the whole cluster. SSIDs are bridge-forwarded — there is no
# gateway in the design, before or after migration.

def parse_instant_config(pasted_outputs: dict[str, str], vc_ip: str = "") -> CustomerConfig:
    """
    pasted_outputs keys: "running_config" (from the virtual controller),
    "show_aps", "version".
    """
    running = pasted_outputs.get("running_config", "")

    auth_servers = _parse_instant_auth_servers(running)
    ssids = _parse_instant_ssids(running)
    aps = _parse_instant_aps(pasted_outputs.get("show_aps", ""))
    fw = _parse_firmware(running, pasted_outputs.get("version", ""))

    # zones → groups; SSIDs with no zone broadcast everywhere.
    # Zone matching is case-insensitive (Instant operators typo case freely).
    zones = sorted({ap.ap_group for ap in aps if ap.ap_group})
    ssid_zones = {k: [z.lower() for z in v]
                  for k, v in _parse_instant_ssid_zones(running).items()}
    ap_groups: list[APGroup] = []
    mapping_incomplete = False
    if zones:
        for zone in zones:
            grp = APGroup(name=zone)
            grp.ssids = [s.name for s in ssids
                         if not ssid_zones.get(s.name)
                         or zone.lower() in ssid_zones[s.name]]
            ap_groups.append(grp)
        # APs with no zone go to a catch-all group with the zoneless SSIDs
        if any(not ap.ap_group for ap in aps):
            grp = APGroup(name="instant-default")
            grp.ssids = [s.name for s in ssids if not ssid_zones.get(s.name)]
            ap_groups.append(grp)
            for ap in aps:
                if not ap.ap_group:
                    ap.ap_group = "instant-default"
        # SSIDs zoned to a zone with no checked-in AP would otherwise be
        # silently dropped — park them in a catch-all group and flag it
        orphans = [s.name for s in ssids
                   if not any(s.name in g.ssids for g in ap_groups)]
        if orphans:
            mapping_incomplete = True
            by_name = {g.name: g for g in ap_groups}
            grp = by_name.get("instant-default")
            if grp is None:
                grp = APGroup(name="instant-default")
                ap_groups.append(grp)
            grp.ssids.extend(orphans)
    else:
        grp = APGroup(name="instant-cluster")
        grp.ssids = [s.name for s in ssids]
        ap_groups.append(grp)
        for ap in aps:
            ap.ap_group = "instant-cluster"

    by_name = {g.name: g for g in ap_groups}
    for ap in aps:
        grp = by_name.get(ap.ap_group)
        if grp is not None:
            if ap.serial and ap.serial not in grp.ap_serials:
                grp.ap_serials.append(ap.serial)
            if ap.model and ap.model not in grp.ap_models:
                grp.ap_models.append(ap.model)

    vlans = _parse_instant_vlans(running, ssids)

    return CustomerConfig(
        mc_ip=vc_ip,
        mc_firmware=fw,
        controller_vlan=1,
        source_type="instant",
        ap_groups=ap_groups,
        ssids=ssids,
        aps=aps,
        vlans=vlans,
        radius_servers=auth_servers,
        cluster=None,
        ssid_mapping_incomplete=mapping_incomplete,
    )


def _parse_instant_external_cp(running: str) -> dict[str, dict]:
    """`wlan external-captive-portal "<name>"` blocks → {name: {url, redirect}}.
    Builds the full portal URL from server/url/port."""
    cps: dict[str, dict] = {}
    for name, block in _iter_blocks(running, r"wlan external-captive-portal"):
        server, path, port, redirect = "", "", 0, ""
        for line in block:
            m = re.match(r"server\s+(\S+)", line, re.IGNORECASE)
            if m:
                server = m.group(1)
            m = re.match(r"url\s+\"?(.+?)\"?\s*$", line, re.IGNORECASE)
            if m:
                path = m.group(1)
            m = re.match(r"port\s+(\d+)", line, re.IGNORECASE)
            if m:
                port = int(m.group(1))
            m = re.match(r"redirect-url\s+\"?(.+?)\"?\s*$", line, re.IGNORECASE)
            if m:
                redirect = m.group(1)
        if server:
            scheme = "http" if port and port not in (443, 8443) else "https"
            portpart = f":{port}" if port else ""
            if path and not path.startswith("/"):
                path = "/" + path
            cps[name] = {"url": f"{scheme}://{server}{portpart}{path}",
                         "redirect": redirect}
    return cps


def _parse_instant_ssids(running: str) -> list[SSID]:
    ssids = []
    ext_cps = _parse_instant_external_cp(running)
    for name, block in _iter_blocks(running, r"wlan ssid-profile"):
        essid = name
        opmode = ""
        passphrase = None
        vlan, vlan_raw = 1, None
        auth_server = None
        enabled = True
        cp_ref, cp_external = "", False
        for line in block:
            m = re.match(r"essid\s+\"?(.+?)\"?\s*$", line, re.IGNORECASE)
            if m:
                essid = m.group(1)
            m = re.match(r"opmode\s+([\w\-]+)", line, re.IGNORECASE)
            if m:
                opmode = m.group(1)
            m = re.match(r"wpa-passphrase\s+(\S+)", line, re.IGNORECASE)
            if m:
                passphrase = m.group(1)
            m = re.match(r"vlan\s+(\S+)", line, re.IGNORECASE)
            if m:
                vlan = _safe_vlan(m.group(1))
                vlan_raw = m.group(1) if _vlan_is_named(m.group(1)) else None
            m = re.match(r"auth-server\s+\"?(.+?)\"?\s*$", line, re.IGNORECASE)
            if m:
                auth_server = m.group(1)
            # external captive portal: `captive-portal external [profile "X"]`
            # or `captive-portal-profile "X"`
            m = re.match(r"captive-portal\s+external(?:\s+profile\s+\"?(.+?)\"?)?\s*$",
                         line, re.IGNORECASE)
            if m:
                cp_external = True
                cp_ref = m.group(1) or cp_ref
            m = re.match(r"captive-portal-profile\s+\"?(.+?)\"?\s*$", line, re.IGNORECASE)
            if m:
                cp_external = True
                cp_ref = m.group(1)
            if re.match(r"^disable\s*$", line, re.IGNORECASE):
                enabled = False
        auth, auth_known = _opmode_to_auth(opmode)
        cp_url, cp_redirect = "", ""
        if cp_external:
            cp = ext_cps.get(cp_ref) or (next(iter(ext_cps.values())) if ext_cps else None)
            if cp:
                cp_url, cp_redirect = cp["url"], cp["redirect"]
        ssids.append(SSID(
            name=name,
            essid=essid,
            vlan=vlan,
            vlan_raw=vlan_raw,
            forward_mode=ForwardMode.BRIDGE,  # Instant SSIDs forward locally
            auth_type=auth,
            auth_known=auth_known,
            psk=passphrase,
            auth_server_group=auth_server,
            broadcast=enabled,
            captive_portal_url=cp_url,
            captive_portal_redirect=cp_redirect,
        ))
    return ssids


def _parse_instant_ssid_zones(running: str) -> dict[str, list[str]]:
    zones: dict[str, list[str]] = {}
    for name, block in _iter_blocks(running, r"wlan ssid-profile"):
        for line in block:
            m = re.match(r"zone\s+\"?(.+?)\"?\s*$", line, re.IGNORECASE)
            if m:
                zones[name] = [z.strip() for z in m.group(1).split(",") if z.strip()]
    return zones


def _parse_instant_auth_servers(running: str) -> list[RadiusServer]:
    servers = []
    for name, block in _iter_blocks(running, r"wlan auth-server"):
        host, auth_port, acct_port = "", 1812, 1813
        for line in block:
            m = re.match(r"ip\s+([\w\-.]+)", line, re.IGNORECASE)
            if m:
                host = m.group(1)
            m = re.match(r"port\s+(\d+)", line, re.IGNORECASE)
            if m:
                auth_port = int(m.group(1))
            m = re.match(r"acctport\s+(\d+)", line, re.IGNORECASE)
            if m:
                acct_port = int(m.group(1))
        if host:
            servers.append(RadiusServer(name=name, address=host,
                                        auth_port=auth_port, acct_port=acct_port))
    return servers


def _parse_instant_aps(text: str) -> list[AP]:
    """`show aps` from the virtual controller. Column names vary across
    Instant builds — Serial/MAC are captured when the columns exist."""
    rows = parse_cli_table(text, ("Name", "IP Address"))
    aps = []
    for row in rows:
        name = _row_get(row, "Name")
        ip = _row_get(row, "IP Address", "IP-Address")
        if not name or not re.match(r"\d+\.\d+\.\d+\.\d+", ip or ""):
            continue
        serial = _row_get(row, "Serial #", "Serial#", "Serial").strip().upper()
        if serial and not re.fullmatch(r"[A-Z0-9]{6,16}", serial):
            serial = ""
        mac = _row_get(row, "MAC", "MAC Address", "Wired MAC Address").strip().lower()
        if mac and not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
            mac = ""
        aps.append(AP(
            serial=serial,
            model=_normalize_model(_row_get(row, "Type", "AP Type", "Model")),
            mac=mac,
            name=name,
            ap_group=_clean_group(_row_get(row, "Zone")),
            ip=ip,
            status="Up",
        ))
    return aps


def _parse_instant_vlans(running: str, ssids: list[SSID]) -> list[VLAN]:
    vlans, seen = [], set()
    for s in ssids:
        if s.vlan and s.vlan not in seen:
            seen.add(s.vlan)
            vlans.append(VLAN(id=s.vlan, name=f"vlan{s.vlan}"))
    return vlans
