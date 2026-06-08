from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ForwardMode(str, Enum):
    TUNNEL = "tunnel"
    BRIDGE = "bridge"
    SPLIT = "split"


class AuthType(str, Enum):
    OPEN = "open"
    WPA2_PSK = "wpa2-psk"
    WPA3_SAE = "wpa3-sae"
    WPA2_ENTERPRISE = "wpa2-enterprise"
    WPA3_ENTERPRISE = "wpa3-enterprise"
    MAC = "mac"


@dataclass
class SSID:
    name: str                       # virtual-ap profile name (binding key)
    vlan: int
    forward_mode: ForwardMode
    auth_type: AuthType
    essid: Optional[str] = None     # broadcast SSID; falls back to name
    psk: Optional[str] = None
    auth_server_group: Optional[str] = None
    role_name: Optional[str] = None
    broadcast: bool = True
    auth_known: bool = True         # False = auth type could not be determined
    vlan_raw: Optional[str] = None  # original VLAN token when non-numeric (named VLAN)
    # additional WLAN attributes migrated to New Central (0/"" = use default)
    rf_band: str = ""               # New Central rf-band enum (BAND_ALL, 5GHZ_6GHZ, …)
    dtim_period: int = 0
    max_clients: int = 0

    @property
    def display_name(self) -> str:
        return self.essid or self.name


@dataclass
class APGroup:
    name: str
    ssids: list[str] = field(default_factory=list)
    ap_serials: list[str] = field(default_factory=list)
    ap_models: list[str] = field(default_factory=list)


@dataclass
class AP:
    serial: str
    model: str
    mac: str
    name: str
    ap_group: str
    ip: str
    status: str
    lldp_switch: Optional[str] = None
    lldp_port: Optional[str] = None
    has_static_ip: bool = False


@dataclass
class VLAN:
    id: int
    name: str
    interface_ip: Optional[str] = None
    mask: Optional[str] = None


@dataclass
class RadiusServer:
    name: str
    address: str
    auth_port: int = 1812
    acct_port: int = 1813
    secret: str = ""


@dataclass
class ServerGroup:
    name: str
    servers: list[str] = field(default_factory=list)


@dataclass
class Role:
    name: str
    vlan: Optional[int] = None
    acl_rules: list[str] = field(default_factory=list)


@dataclass
class ClusterInfo:
    type: str  # "L2" or "L3"
    members: list[str] = field(default_factory=list)
    vrrp_vip: Optional[str] = None
    active_mc_ip: Optional[str] = None


@dataclass
class CustomerConfig:
    mc_ip: str
    mc_firmware: str
    controller_vlan: int
    # "controller" = Mobility Conductor/Controller (ap convert path)
    # "instant"    = IAP virtual-controller cluster (Central-driven conversion)
    source_type: str = "controller"
    ap_groups: list[APGroup] = field(default_factory=list)
    ssids: list[SSID] = field(default_factory=list)
    aps: list[AP] = field(default_factory=list)
    vlans: list[VLAN] = field(default_factory=list)
    radius_servers: list[RadiusServer] = field(default_factory=list)
    server_groups: list[ServerGroup] = field(default_factory=list)
    roles: list[Role] = field(default_factory=list)
    cluster: Optional[ClusterInfo] = None
    has_eap_offload: bool = False
    has_internal_auth: bool = False
    # True when SSID→AP-group bindings could not be discovered and all SSIDs
    # were assigned to every group as a fallback (surfaced as a preflight WARN).
    ssid_mapping_incomplete: bool = False

    def ssid_by_name(self, name: str) -> Optional[SSID]:
        return next((s for s in self.ssids if s.name == name), None)

    def ap_group_by_name(self, name: str) -> Optional[APGroup]:
        return next((g for g in self.ap_groups if g.name == name), None)


@dataclass
class CentralGroupConfig:
    name: str                       # Central device-group name (what we create)
    firmware_version: str
    site_name: str
    source_group: str = ""          # AOS 8 ap-group name (for `ap convert` + serial lookup)
    ssids: list[SSID] = field(default_factory=list)
    vlans: list[VLAN] = field(default_factory=list)
    roles: list[Role] = field(default_factory=list)
    has_tunnel_ssid: bool = False
    has_bridge_ssid: bool = False


@dataclass
class CentralConfig:
    customer_name: str
    base_url: str
    # "new" = New Central on GreenLake (network-config profiles + scope maps)
    # "classic" = classic Central (apigw, v3 groups with AOS10 architecture)
    destination: str = "new"
    groups: list[CentralGroupConfig] = field(default_factory=list)
    sites: list[str] = field(default_factory=list)
    radius_servers: list[RadiusServer] = field(default_factory=list)
    gw_cluster_name: Optional[str] = None
    gw_serial: Optional[str] = None
    # True when the customer chose to retire gateways: every tunnel/split SSID
    # was converted to bridge mode and no GW cluster will be created.
    gateways_retired: bool = False
    # Site address (New Central sites are geographic scopes)
    site_address: str = ""
    site_city: str = ""
    site_state: str = ""
    site_country: str = ""
    site_zipcode: str = ""
    site_timezone: str = "UTC"   # IANA zone id; New Central site-create requires it
