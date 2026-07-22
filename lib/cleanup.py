"""
Teardown for lab/test objects — delete everything named with the test prefix
(default "zztest") so a tenant can be reset between runs without hunting through
the Central UI. Prefix-scoped and best-effort: each delete is reported per item,
404/'not found' counts as already-gone, and ONLY objects whose name starts with
the prefix are touched.

Deletion order matters — scope-mapped resources (WLANs/overlays) before the
device groups/sites they bind to, then auth-servers, then classic groups.
"""
from urllib.parse import quote
from typing import Callable, Optional


def _list(data, *keys) -> list:
    if not isinstance(data, dict):
        return data if isinstance(data, list) else []
    for k in keys:
        v = data.get(k)
        if isinstance(v, list):
            return v
    return data.get("items") or data.get("data") or []


def _matches(name: str, prefix: str) -> bool:
    return bool(name) and name.lower().startswith(prefix.lower())


def cleanup(prefix: str, central=None, classic=None,
            on_step: Optional[Callable[[str, bool, str], None]] = None
            ) -> list[tuple[str, bool, str]]:
    """Delete <prefix>* objects across New Central (central) and Classic
    (classic). Either client may be None. Returns [(label, ok, detail)]."""
    # An empty prefix matches EVERY object in the tenant (startswith("") is
    # always True) — refuse outright rather than risk an account-wide wipe.
    if not (prefix or "").strip():
        raise ValueError("cleanup() requires a non-empty prefix — an empty "
                         "prefix would match every object in the tenant")
    results: list[tuple[str, bool, str]] = []

    def step(label: str, fn) -> None:
        try:
            fn()
            results.append((label, True, ""))
        except Exception as e:
            msg = str(e)
            if "404" in msg or "not found" in msg.lower() or "does not exist" in msg.lower():
                results.append((label, True, "already gone"))
            else:
                results.append((label, False, msg[:200]))
        if on_step:
            results and on_step(*results[-1])

    # Classic group names, fetched once on first use — used to decide whether
    # a failed New Central group delete can really be deferred to Classic.
    _classic_names: Optional[list] = None

    def _classic_group_names() -> list:
        nonlocal _classic_names
        if _classic_names is None:
            try:
                _classic_names = classic.list_group_names(refresh=True)
            except Exception:
                _classic_names = []
        return _classic_names

    # ── New Central ──────────────────────────────────────────────────────
    # Deletion order respects dependencies: overlay-wlan → wlan-ssid →
    # server-group → auth-server → device-group → site. (An auth-server can't
    # be deleted while a server-group still references it.)
    if central is not None:
        # 1. WLAN SSIDs — overlay-wlan first (only exists for tunnel SSIDs;
        #    a 400 just means there's no overlay, so swallow it silently)
        try:
            ssids = _list(central._get("/network-config/v1/wlan-ssids"),
                          "wlan-ssid", "wlan-ssids")
        except Exception:
            ssids = []
        for s in ssids:
            essid = s.get("essid")
            name = (s.get("ssid") or (essid.get("name") if isinstance(essid, dict) else essid)
                    or s.get("name") or "")
            if _matches(name, prefix):
                enc = quote(name, safe="")
                try:  # best-effort — underlay SSIDs have no overlay-wlan
                    central._delete(f"/network-config/v1alpha1/overlay-wlan/{enc}")
                except Exception:
                    pass
                step(f"Delete SSID: {name}",
                     lambda e=enc: central._delete(f"/network-config/v1/wlan-ssids/{e}"))

        # 2. Server-groups (must go BEFORE auth-servers they reference)
        try:
            groups = _list(central._get("/network-config/v1alpha1/server-groups"),
                           "server-group", "server-groups")
        except Exception:
            groups = []
        for g in groups:
            nm = g.get("name", "")
            if _matches(nm, prefix):
                step(f"Delete server-group: {nm}",
                     lambda n=quote(nm, safe=""): central._delete(
                         f"/network-config/v1alpha1/server-groups/{n}"))

        # 3. Auth servers (now unreferenced)
        try:
            servers = _list(central._get("/network-config/v1alpha1/auth-servers"),
                            "auth-server", "auth-servers")
        except Exception:
            servers = []
        for sv in servers:
            nm = sv.get("name", "")
            if _matches(nm, prefix):
                step(f"Delete auth server: {nm}",
                     lambda n=quote(nm, safe=""): central._delete(
                         f"/network-config/v1alpha1/auth-servers/{n}"))

        # 4. Device groups — on a HYBRID tenant these are Classic-owned, so the
        #    New Central delete 400s and the Classic delete (below) is what
        #    actually removes them. Treat a NC 400 as deferred-to-Classic.
        try:
            for grp in central.list_device_groups(refresh=True):
                gname = grp.get("scopeName", "")
                gid = grp.get("scopeId")
                if _matches(gname, prefix) and gid is not None:
                    def _del_group(i=gid):
                        try:
                            central._delete("/network-config/v1/device-groups/bulk",
                                            json={"items": [{"id": i}]})
                        except Exception:
                            central._delete(f"/network-config/v1/device-groups/{i}")
                    if classic is not None:
                        # hybrid: let the Classic delete handle it — but only
                        # when the failure IS the hybrid restriction (or Classic
                        # really owns the group). Anything else (auth/5xx/
                        # timeout) is a real failure and must stay red-flagged.
                        try:
                            _del_group()
                            results.append((f"Delete device group: {gname}", True, ""))
                        except Exception as e:
                            msg = str(e)
                            if "404" in msg or "not found" in msg.lower() \
                                    or "does not exist" in msg.lower():
                                results.append((f"Delete device group: {gname}",
                                                True, "already gone"))
                            elif ("HYBRID_CLUSTER" in msg or "API_ACCESS_RESTRICTED" in msg
                                    or gname in _classic_group_names()):
                                results.append((f"Delete device group: {gname}", True,
                                                "deferred to Classic (hybrid)"))
                            else:
                                results.append((f"Delete device group: {gname}", False,
                                                msg[:200]))
                        if on_step:
                            on_step(*results[-1])
                    else:
                        step(f"Delete device group: {gname}", _del_group)
        except Exception as e:
            results.append(("List device groups", False, str(e)[:150]))

        # 5. Sites (bulk-by-id, then single)
        try:
            for site in central.list_sites(refresh=True):
                sname = central._site_name(site)
                sid = central._site_id(site)
                if _matches(sname, prefix) and sid:
                    def _del_site(i=sid):
                        for path, body in (
                            ("/network-config/v1alpha1/sites/bulk", {"items": [{"id": i}]}),
                            ("/network-config/v1/sites/bulk", {"items": [{"id": i}]}),
                        ):
                            try:
                                central._delete(path, json=body)
                                return
                            except Exception:
                                continue
                        central._delete(f"/network-config/v1alpha1/sites/{i}")
                    step(f"Delete site: {sname}", _del_site)
        except Exception as e:
            results.append(("List sites", False, str(e)[:150]))

    # ── Classic (hybrid groups created via /configuration/v3/groups) ─────
    if classic is not None:
        try:
            names = classic.list_group_names(refresh=True)
        except Exception as e:
            names = []
            results.append(("List classic groups", False, str(e)[:150]))
        for gname in names:
            if _matches(gname, prefix):
                step(f"Delete classic group: {gname}",
                     lambda n=gname: classic.delete_group(n))

    if not results:
        results.append((f"No objects named '{prefix}*' found to delete", True, ""))
        if on_step:
            on_step(*results[-1])
    return results
