"""
Read-only API connectivity probe — run once with real credentials to learn
what a tenant actually supports BEFORE attempting any writes. Catches the
quirks that otherwise surface one provisioning error at a time: which site
route works, whether the tenant is a hybrid cluster, scope reads, GLP reach,
classic token validity.

Every check is a GET, with ONE exception: the device-group write check really
does create a disposable group (there is no dry-run on that route) and then
deletes it, reporting a WARNING if the deletion could not be confirmed. Each
check returns a ProbeResult the UI renders as a row.
"""
from dataclasses import dataclass

from .central_client import CentralClient, CentralAPIError
from .glp_client import GLPClient
from .classic_central_client import ClassicCentralClient


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
        return f"{len(s)} site(s) readable via /network-config sites routes"
    results.append(_probe("Read — sites", sites))

    def groups():
        g = client.list_device_groups()
        return f"{len(g)} device group(s) readable"
    results.append(_probe("Read — device groups", groups))

    def aps():
        a = client.list_all_aps()
        if a is None:
            # None is this client's "the read itself failed" signal — turning
            # it into "0 AP(s)" probes green on a 403 monitoring scope
            raise RuntimeError("monitoring read failed — check API-client "
                               "monitoring scope")
        return f"{len(a)} AP(s) readable via /network-monitoring/v1/devices"
    results.append(_probe("Read — monitored devices (validation source)", aps))

    # hybrid detection: a dry probe of the group-create route. The API has no
    # dry-run, so we send a clearly-disposable name and treat a hybrid block as
    # the (informative) answer. Any real 4xx other than hybrid is reported.
    _PROBE_GROUP = "zzprobe-donotcreate-readonly"

    def _delete_probe_group(scope_id) -> None:
        client._request("DELETE", "/network-config/v1/device-groups/bulk",
                        json={"items": [{"id": scope_id}]})

    def group_write():
        try:
            resp = client._post("/network-config/v1/device-groups",
                                json={"scopeName": _PROBE_GROUP})
        except CentralAPIError as e:
            if "HYBRID_CLUSTER" in str(e) or "API_ACCESS_RESTRICTED" in str(e):
                raise  # surfaced as warn by _probe
            # other 4xx — the route exists and accepts writes, body just rejected
            return f"device-group write route reachable (probe body rejected: {str(e)[:80]})"
        # The create went through, so a real group now exists in a production
        # tenant. New Central group propagation is not instant, so the id from
        # the POST body is authoritative — a re-list can legitimately miss the
        # group it just created, and "not in the list" must never be read as
        # "already gone".
        scope_id = resp.get("scopeId") or resp.get("id")
        try:
            if scope_id is None:
                for grp in client.list_device_groups(refresh=True):
                    if grp.get("scopeName") == _PROBE_GROUP:
                        scope_id = grp.get("scopeId")
                        break
            if scope_id is None:
                return ("device-group WRITE allowed — WARNING: the disposable "
                        f"'{_PROBE_GROUP}' group was created but its id could "
                        "not be resolved, so it was NOT deleted — remove it in "
                        "Central")
            _delete_probe_group(scope_id)
        except Exception as e:
            return ("device-group WRITE allowed — WARNING: the disposable "
                    f"'{_PROBE_GROUP}' group could not be deleted "
                    f"({str(e)[:80]}) — remove it in Central")
        # confirm the deletion actually took
        try:
            still_there = any(g.get("scopeName") == _PROBE_GROUP
                              for g in client.list_device_groups(refresh=True))
        except Exception as e:
            return ("device-group WRITE allowed — delete issued but could not "
                    f"be verified ({str(e)[:80]}) — confirm '{_PROBE_GROUP}' "
                    "is gone in Central")
        if still_there:
            return ("device-group WRITE allowed — WARNING: the disposable "
                    f"'{_PROBE_GROUP}' group is STILL present after the delete "
                    "— remove it in Central")
        return "device-group WRITE allowed (native New Central, not hybrid)"
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
                  client_secret: str = "", refresh_token: str = ""
                  ) -> tuple[list[ProbeResult], ClassicCentralClient]:
    """Returns (results, client). The client is returned because a probe on an
    expired access token triggers a refresh — and the classic refresh token is
    SINGLE-USE, so the caller must persist client.refresh_token or the rotated
    token is lost and every later step is stranded with a dead one."""
    results: list[ProbeResult] = []
    client = ClassicCentralClient(base_url, access_token, client_id,
                                  client_secret, refresh_token)
    results.append(_probe("Read — classic groups (token valid?)",
                          lambda: f"{len(client.list_group_names())} group(s) readable"))
    results.append(_probe("Read — classic sites",
                          lambda: f"{len(client.list_sites())} site(s) readable"))

    def classic_aps():
        a = client.list_all_aps()
        if a is None:
            raise RuntimeError("monitoring read failed — check the token's "
                               "monitoring scope")
        return f"{len(a)} AP(s) via /monitoring/v2/aps"
    results.append(_probe("Read — classic monitored APs", classic_aps))
    return results, client
