"""
Teardown for lab/test objects — delete everything named with the test prefix
(default "zztest") so a tenant can be reset between runs without hunting through
the Central UI. Prefix-scoped and best-effort: each delete is reported per item,
404/'not found' counts as already-gone, and ONLY objects whose name starts with
the prefix are touched.

Deletion order matters — SSIDs before the captive-portal profiles / server
groups / VLANs they reference, firmware-compliance before the device group
scope it hangs off, policy-group entries before the policies they list, and
all of it before the device groups/sites everything binds to.

Endpoint coverage note: every list/delete below uses an operation present in
HPE's published New Central OpenAPI specs (cross-checked against the
hpe-networking-mcp manifest). Scope-map entries have NO spec-published delete
(POST-only resource) — they are orphaned when their scope dies and are
deliberately not touched here rather than removed by an invented call."""
from urllib.parse import quote
from typing import Callable, Optional

from .manifest import (KIND_AUTH_SERVER, KIND_CAPTIVE_PORTAL,
                       KIND_DEVICE_GROUP, KIND_GATEWAY_CLUSTER, KIND_GROUP,
                       KIND_POLICY, KIND_ROLE, KIND_SERVER_GROUP, KIND_SITE,
                       KIND_SSID, KIND_VLAN)


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
            on_step: Optional[Callable[[str, bool, str], None]] = None,
            manifest=None) -> list[tuple[str, bool, str]]:
    """Delete <prefix>* objects across New Central (central) and Classic
    (classic). Either client may be None. Returns [(label, ok, detail)].

    With a `manifest` attached (lib.manifest.Manifest), ONLY manifest-owned
    resources are deleted — same-prefix objects another administrator created,
    and objects this migration explicitly ADOPTED, are left alone. Without a
    manifest the legacy prefix-only behavior is kept (lab teardown of
    pre-manifest objects)."""
    # An empty prefix matches EVERY object in the tenant (startswith("") is
    # always True) — refuse outright rather than risk an account-wide wipe.
    if not (prefix or "").strip():
        raise ValueError("cleanup() requires a non-empty prefix — an empty "
                         "prefix would match every object in the tenant")
    results: list[tuple[str, bool, str]] = []

    def step(label: str, fn) -> bool:
        try:
            fn()
            results.append((label, True, ""))
            ok = True
        except Exception as e:
            msg = str(e)
            if "404" in msg or "not found" in msg.lower() or "does not exist" in msg.lower():
                results.append((label, True, "already gone"))
                ok = True
            else:
                results.append((label, False, msg[:200]))
                ok = False
        if on_step:
            on_step(*results[-1])
        return ok

    def _owned(kind: str, name: str) -> bool:
        """The ownership gate: without a manifest the prefix alone decides
        (legacy); with one, only resources this migration CREATED may be
        deleted — adopted objects belong to someone else."""
        if manifest is None or manifest.may_delete(kind, name):
            return True
        results.append((f"Keep {kind}: {name} (not manifest-owned)", True,
                        "skipped"))
        if on_step:
            on_step(*results[-1])
        return False

    def _deleted(kind: str, name: str, ok: bool) -> None:
        """Drop the manifest entry once the object is really gone so a second
        cleanup doesn't chase it."""
        if ok and manifest is not None:
            manifest.remove(kind, name)

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
        except Exception as e:
            ssids = []
            results.append(("List SSIDs", False, str(e)[:150]))
            if on_step:
                on_step(*results[-1])
        for s in ssids:
            essid = s.get("essid")
            name = (s.get("ssid") or (essid.get("name") if isinstance(essid, dict) else essid)
                    or s.get("name") or "")
            if _matches(name, prefix) and _owned(KIND_SSID, name):
                enc = quote(name, safe="")
                # best-effort — underlay SSIDs have no overlay-wlan. v1 is
                # where provisioning creates them; keep v1alpha1 as fallback.
                for _ow_path in (f"/network-config/v1/overlay-wlan/{enc}",
                                 f"/network-config/v1alpha1/overlay-wlan/{enc}"):
                    try:
                        central._delete(_ow_path)
                        break
                    except Exception:
                        continue
                _deleted(KIND_SSID, name,
                         step(f"Delete SSID: {name}",
                              lambda e=enc: central._delete(
                                  f"/network-config/v1/wlan-ssids/{e}")))

        # 1b. External captive-portal profiles (SHARED/global; SSIDs above
        #     referenced them by name, so they go after the SSIDs)
        try:
            portals = _list(central._get("/network-config/v1alpha1/captive-portal"),
                            "captive-portal", "captive-portals")
        except Exception as e:
            portals = []
            results.append(("List captive portals", False, str(e)[:150]))
            if on_step:
                on_step(*results[-1])
        for cp in portals:
            nm = cp.get("name", "")
            if _matches(nm, prefix) and _owned(KIND_CAPTIVE_PORTAL, nm):
                _deleted(KIND_CAPTIVE_PORTAL, nm,
                         step(f"Delete captive portal: {nm}",
                              lambda n=quote(nm, safe=""): central._delete(
                                  f"/network-config/v1alpha1/captive-portal/{n}")))

        # 1c. VLANs — layer2-vlan objects scope-mapped to the groups. VLAN 1
        #     is the built-in default ("aruba-vlan/1") and is never touched.
        try:
            try:
                vlans = _list(central._get("/network-config/v1/layer2-vlan"),
                              "l2-vlan", "layer2-vlan")
            except Exception:
                vlans = _list(central._get("/network-config/v1alpha1/layer2-vlan"),
                              "l2-vlan", "layer2-vlan")
        except Exception as e:
            vlans = []
            results.append(("List VLANs", False, str(e)[:150]))
            if on_step:
                on_step(*results[-1])
        for v in vlans:
            vname = v.get("name", "")
            vid = v.get("vlan") if v.get("vlan") is not None else v.get("id")
            try:
                vid = int(vid)
            except (TypeError, ValueError):
                continue
            if vid <= 1 or not _matches(vname, prefix):
                continue
            if not _owned(KIND_VLAN, vname):
                continue
            def _del_vlan(i=vid):
                try:
                    central._delete(f"/network-config/v1/layer2-vlan/{i}")
                except Exception:
                    central._delete(f"/network-config/v1alpha1/layer2-vlan/{i}")
            _deleted(KIND_VLAN, vname,
                     step(f"Delete VLAN {vid} ({vname})", _del_vlan))

        # 2. Server-groups (must go BEFORE auth-servers they reference)
        try:
            groups = _list(central._get("/network-config/v1alpha1/server-groups"),
                           "server-group", "server-groups")
        except Exception as e:
            groups = []
            results.append(("List server-groups", False, str(e)[:150]))
            if on_step:
                on_step(*results[-1])
        for g in groups:
            nm = g.get("name", "")
            if _matches(nm, prefix) and _owned(KIND_SERVER_GROUP, nm):
                _deleted(KIND_SERVER_GROUP, nm,
                         step(f"Delete server-group: {nm}",
                              lambda n=quote(nm, safe=""): central._delete(
                                  f"/network-config/v1alpha1/server-groups/{n}")))

        # 3. Auth servers (now unreferenced)
        try:
            servers = _list(central._get("/network-config/v1alpha1/auth-servers"),
                            "auth-server", "auth-servers")
        except Exception as e:
            servers = []
            results.append(("List auth servers", False, str(e)[:150]))
            if on_step:
                on_step(*results[-1])
        for sv in servers:
            nm = sv.get("name", "")
            if _matches(nm, prefix) and _owned(KIND_AUTH_SERVER, nm):
                _deleted(KIND_AUTH_SERVER, nm,
                         step(f"Delete auth server: {nm}",
                              lambda n=quote(nm, safe=""): central._delete(
                                  f"/network-config/v1alpha1/auth-servers/{n}")))

        # 3b. Roles + security policies (created by the overlay/tunnel SSID
        #     path, named after the SSID). Policy-group ENTRIES reference the
        #     policy, so the entry goes first — a referenced policy delete
        #     would 400. Roles/policies list under v1alpha1 even when created
        #     via the v1 path.
        try:
            pgroup = central._get("/network-config/v1alpha1/policy-groups")
            pg_entries = (pgroup.get("policy-group", {}) or {}
                          ).get("policy-group-list", []) \
                if isinstance(pgroup, dict) else []
        except Exception as e:
            pg_entries = []
            results.append(("List policy-groups", False, str(e)[:150]))
            if on_step:
                on_step(*results[-1])
        for entry in pg_entries:
            nm = entry.get("name", "") if isinstance(entry, dict) else ""
            if _matches(nm, prefix) and _owned(KIND_POLICY, nm):
                step(f"Remove policy-group entry: {nm}",
                     lambda n=quote(nm, safe=""): central._delete(
                         "/network-config/v1alpha1/policy-groups"
                         f"/policy-group/policy-group-list/{n}"))
        try:
            policies = _list(central._get("/network-config/v1alpha1/policies"),
                             "policies", "policy")
        except Exception as e:
            policies = []
            results.append(("List policies", False, str(e)[:150]))
            if on_step:
                on_step(*results[-1])
        for pol in policies:
            nm = pol.get("name", "")
            if _matches(nm, prefix) and _owned(KIND_POLICY, nm):
                _deleted(KIND_POLICY, nm,
                         step(f"Delete policy: {nm}",
                              lambda n=quote(nm, safe=""): central._delete(
                                  f"/network-config/v1alpha1/policies/{n}")))
        try:
            roles = _list(central._get("/network-config/v1alpha1/roles"),
                          "roles", "role")
        except Exception as e:
            roles = []
            results.append(("List roles", False, str(e)[:150]))
            if on_step:
                on_step(*results[-1])
        for role in roles:
            nm = role.get("name", "")
            if _matches(nm, prefix) and _owned(KIND_ROLE, nm):
                _deleted(KIND_ROLE, nm,
                         step(f"Delete role: {nm}",
                              lambda n=quote(nm, safe=""): central._delete(
                                  f"/network-config/v1alpha1/roles/{n}")))

        # 4. Device groups — on a HYBRID tenant these are Classic-owned, so the
        #    New Central delete 400s and the Classic delete (below) is what
        #    actually removes them. Treat a NC 400 as deferred-to-Classic.
        #    Firmware-compliance is a per-scope setting with its own
        #    spec-published DELETE — clear it BEFORE the group scope goes away.
        try:
            nc_groups = central.list_device_groups(refresh=True)
        except Exception as e:
            nc_groups = []
            results.append(("List device groups", False, str(e)[:150]))
            if on_step:
                on_step(*results[-1])
        for grp in nc_groups:
            gname = grp.get("scopeName", "")
            gid = grp.get("scopeId")
            if _matches(gname, prefix) and gid is not None \
                    and _owned(KIND_DEVICE_GROUP, gname):
                step(f"Delete firmware compliance → {gname}",
                     lambda i=gid: central._delete(
                         "/network-config/v1alpha1/firmware-compliance",
                         params={"scope-id": str(i), "object-type": "LOCAL",
                                 "device-function": "CAMPUS_AP"}))
        for grp in nc_groups:
            gname = grp.get("scopeName", "")
            gid = grp.get("scopeId")
            if not (_matches(gname, prefix) and gid is not None
                    and _owned(KIND_DEVICE_GROUP, gname)):
                continue
            def _del_group(i=gid):
                try:
                    central._delete("/network-config/v1/device-groups/bulk",
                                    json={"items": [{"id": i}]})
                except Exception:
                    # spec offers bulk deletes only; the single-id
                    # form is a last-resort for older tenants
                    central._delete(f"/network-config/v1/device-groups/{i}")
            if classic is not None:
                # hybrid: let the Classic delete handle it — but only
                # when the failure IS the hybrid restriction (or Classic
                # really owns the group). Anything else (auth/5xx/
                # timeout) is a real failure and must stay red-flagged.
                gone = False
                try:
                    _del_group()
                    results.append((f"Delete device group: {gname}", True, ""))
                    gone = True
                except Exception as e:
                    msg = str(e)
                    if "404" in msg or "not found" in msg.lower() \
                            or "does not exist" in msg.lower():
                        results.append((f"Delete device group: {gname}",
                                        True, "already gone"))
                        gone = True
                    elif ("HYBRID_CLUSTER" in msg or "API_ACCESS_RESTRICTED" in msg
                            or gname in _classic_group_names()):
                        results.append((f"Delete device group: {gname}", True,
                                        "deferred to Classic (hybrid)"))
                        # not deleted yet — the Classic pass below owns the
                        # manifest removal for hybrid groups
                    else:
                        results.append((f"Delete device group: {gname}", False,
                                        msg[:200]))
                if on_step:
                    on_step(*results[-1])
                _deleted(KIND_DEVICE_GROUP, gname, gone)
            else:
                _deleted(KIND_DEVICE_GROUP, gname,
                         step(f"Delete device group: {gname}", _del_group))

        # 4b. Gateway clusters — formed manually at cutover per the runbook;
        #     prefix-named lab clusters still need teardown
        try:
            clusters = _list(central._get("/network-config/v1alpha1/gateway-clusters"),
                             "gateway-clusters", "gateway-cluster")
        except Exception as e:
            clusters = []
            results.append(("List gateway clusters", False, str(e)[:150]))
            if on_step:
                on_step(*results[-1])
        for cl in clusters:
            nm = cl.get("name", "") or cl.get("cluster-name", "")
            if _matches(nm, prefix) and _owned(KIND_GATEWAY_CLUSTER, nm):
                _deleted(KIND_GATEWAY_CLUSTER, nm,
                         step(f"Delete gateway cluster: {nm}",
                              lambda n=quote(nm, safe=""): central._delete(
                                  f"/network-config/v1alpha1/gateway-clusters/{n}")))

        # 5. Sites (bulk-by-id, then single)
        try:
            for site in central.list_sites(refresh=True):
                sname = central._site_name(site)
                sid = central._site_id(site)
                if _matches(sname, prefix) and sid \
                        and _owned(KIND_SITE, sname):
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
                        # ^ spec offers bulk deletes only; the single-id form
                        # is a last-resort for older tenants
                    _deleted(KIND_SITE, sname,
                             step(f"Delete site: {sname}", _del_site))
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
            if _matches(gname, prefix) and _owned(KIND_GROUP, gname):
                _deleted(KIND_GROUP, gname,
                         step(f"Delete classic group: {gname}",
                              lambda n=gname: classic.delete_group(n)))
        # classic provisioning also creates sites — clean those up too
        try:
            for site in classic.list_sites(refresh=True):
                sname = site.get("site_name", "")
                sid = site.get("site_id")
                if _matches(sname, prefix) and sid is not None \
                        and _owned(KIND_SITE, sname):
                    _deleted(KIND_SITE, sname,
                             step(f"Delete classic site: {sname}",
                                  lambda i=sid: classic._request(
                                      "DELETE", f"/central/v2/sites/{i}")))
        except Exception as e:
            results.append(("List classic sites", False, str(e)[:150]))

    if not results:
        results.append((f"No objects named '{prefix}*' found to delete", True, ""))
        if on_step:
            on_step(*results[-1])
    return results
