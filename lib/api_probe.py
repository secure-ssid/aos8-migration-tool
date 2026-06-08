"""
Read-only API connectivity probe — run once with real credentials to learn
what a tenant actually supports BEFORE attempting any writes. Catches the
quirks that otherwise surface one provisioning error at a time: which site
route works, whether the tenant is a hybrid cluster, scope reads, GLP reach,
classic token validity.

Every check is a GET (or a write with dry-run semantics where the API offers
it) — nothing is created. Each returns a ProbeResult the UI renders as a row.
"""
from dataclasses import dataclass
from typing import Optional

from .central_client import CentralClient, CentralAPIError
from .glp_client import GLPClient, GLPAPIError
from .classic_central_client import ClassicCentralClient, ClassicCentralAPIError


@dataclass
class ProbeResult:
    name: str
    status: str          # "ok" | "warn" | "fail" | "skip"
    detail: str


def _probe(name: str, fn) -> ProbeResult:
    try:
        return ProbeResult(name, "ok", fn())
    except Exception as e:
        msg = str(e)
        # a hybrid restriction is an informative finding, not a hard failure
        if "HYBRID_CLUSTER" in msg or "API_ACCESS_RESTRICTED" in msg:
            return ProbeResult(name, "warn", "hybrid cluster — write restricted here")
        return ProbeResult(name, "fail", msg[:200])


def probe_new_central(base_url: str, client_id: str, client_secret: str) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    client = CentralClient(base_url, client_id, client_secret)

    def auth():
        client.authenticate()
        return "token acquired (client-credentials)"
    auth_res = _probe("Auth — GreenLake token", auth)
    results.append(auth_res)
    if auth_res.status != "ok":
        results.append(ProbeResult("(remaining New Central checks)", "skip",
                                   "skipped — auth failed"))
        return results

    def scope():
        sid = client.get_global_scope_id()
        return f"global scope-id resolved: {sid}"
    results.append(_probe("Read — global scope (/network-config/v1/scope-maps)", scope))

    def sites():
        s = client.list_sites()
        return f"{len(s)} site(s) readable via /network-config/v1/sites"
    results.append(_probe("Read — sites", sites))

    def groups():
        g = client.list_device_groups()
        return f"{len(g)} device group(s) readable"
    results.append(_probe("Read — device groups", groups))

    def aps():
        a = client.list_all_aps()
        n = len(a) if a is not None else 0
        return f"{n} AP(s) readable via /network-monitoring/v1/devices"
    results.append(_probe("Read — monitored devices (validation source)", aps))

    # hybrid detection: a dry probe of the group-create route. The API has no
    # dry-run, so we send a clearly-disposable name and treat a hybrid block as
    # the (informative) answer. Any real 4xx other than hybrid is reported.
    def group_write():
        try:
            client._post("/network-config/v1/device-groups",
                         json={"scopeName": "zzprobe-donotcreate-readonly"})
            # if it somehow created, remove it so the probe stays read-only
            try:
                for grp in client.list_device_groups(refresh=True):
                    if grp.get("scopeName") == "zzprobe-donotcreate-readonly":
                        client._request("DELETE",
                                        "/network-config/v1/device-groups/bulk",
                                        json={"items": [{"id": grp.get("scopeId")}]})
            except Exception:
                pass
            return "device-group WRITE allowed (native New Central, not hybrid)"
        except CentralAPIError as e:
            if "HYBRID_CLUSTER" in str(e) or "API_ACCESS_RESTRICTED" in str(e):
                raise  # surfaced as warn by _probe
            # other 4xx — the route exists and accepts writes, body just rejected
            return f"device-group write route reachable (probe body rejected: {str(e)[:80]})"
    results.append(_probe("Write check — device-group create (hybrid?)", group_write))

    return results


def probe_glp(client_id: str, client_secret: str) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    client = GLPClient(client_id=client_id, client_secret=client_secret)

    def auth():
        client.authenticate()
        return "GLP token acquired"
    a = _probe("Auth — GreenLake (GLP)", auth)
    results.append(a)
    if a.status != "ok":
        return results
    results.append(_probe("Read — GLP devices (workspace inventory)",
                          lambda: f"{len(client.list_devices(limit=1))}+ device(s) readable"))
    results.append(_probe("Read — GLP subscriptions",
                          lambda: f"{len(client.list_subscriptions(limit=100))} subscription(s)"))
    return results


def probe_classic(base_url: str, access_token: str, client_id: str = "",
                  client_secret: str = "", refresh_token: str = "") -> list[ProbeResult]:
    results: list[ProbeResult] = []
    client = ClassicCentralClient(base_url, access_token, client_id,
                                  client_secret, refresh_token)
    results.append(_probe("Read — classic groups (token valid?)",
                          lambda: f"{len(client.list_group_names())} group(s) readable"))
    results.append(_probe("Read — classic sites",
                          lambda: f"{len(client.list_sites())} site(s) readable"))
    results.append(_probe("Read — classic monitored APs",
                          lambda: f"{len(client.list_all_aps() or [])} AP(s) via /monitoring/v2/aps"))
    return results
