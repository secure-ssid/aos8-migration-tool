"""
Diagnostic: log into an AOS8 controller and dump raw API responses for the
objects the migration tool reads, across every config node. Shows exactly
what the API returns so we can see why SSIDs come back empty.

Usage:
    python debug_pull.py <controller-ip> <username> <password> [config_path]
"""
import json
import sys

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# virtual_ap is the primary object name (what the client reads);
# wlan_virtual_ap is the legacy alias some builds answer instead
OBJECTS = ["ap_group", "virtual_ap", "wlan_virtual_ap", "ssid_prof",
           "vlan_id", "rad_server", "aaa_prof", "server_group_prof"]


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    ip, user, pw = sys.argv[1], sys.argv[2], sys.argv[3]
    start_path = sys.argv[4] if len(sys.argv) > 4 else "/md"

    s = requests.Session()
    s.verify = False
    base = f"https://{ip}:4343"

    r = s.post(f"{base}/v1/api/login", data={"username": user, "password": pw}, timeout=15)
    r.raise_for_status()
    result = r.json().get("_global_result", {})
    if str(result.get("status", "1")) != "0":
        print(f"LOGIN FAILED: {result}")
        sys.exit(1)
    uid = result["UIDARUBA"]
    print(f"logged in, UIDARUBA={uid[:8]}...")

    # discover config nodes
    nodes = [start_path, "/md", "/mm", "/mm/mynode"]
    try:
        r = s.get(f"{base}/v1/configuration/object/node_hierarchy",
                  params={"UIDARUBA": uid}, timeout=15)
        tree = r.json()
        print("\n===== node_hierarchy (raw) =====")
        print(json.dumps(tree, indent=2)[:3000])

        def walk(node, prefix):
            if not isinstance(node, dict):
                return
            name = str(node.get("name", "")).strip("/")
            path = f"{prefix.rstrip('/')}/{name}" if name else prefix
            if path and path != "/" and path not in nodes:
                nodes.append(path)
            for child in (node.get("childnodes") or node.get("children") or []):
                walk(child, path or "/")

        t = tree.get("_data", tree)
        t = t.get("node_hierarchy", t)
        walk(t, "")
    except Exception as e:
        print(f"node_hierarchy failed: {e}")

    print(f"\nprobing nodes: {nodes}")

    for node in nodes:
        print(f"\n{'='*70}\nCONFIG NODE: {node}\n{'='*70}")
        for obj in OBJECTS:
            try:
                r = s.get(f"{base}/v1/configuration/object/{obj}",
                          params={"UIDARUBA": uid, "config_path": node}, timeout=15)
                data = r.json()
                if isinstance(data.get("_data"), dict):
                    data = data["_data"]
                items = data.get(obj, [])
                if not isinstance(items, list):
                    items = [items]
                print(f"\n--- {obj}: {len(items)} item(s) (HTTP {r.status_code}) ---")
                # print first 2 items in full so we can see exact field names
                for item in items[:2]:
                    print(json.dumps(item, indent=2)[:2000])
                if len(items) > 2:
                    names = [i.get("profile-name", "?") if isinstance(i, dict) else "?"
                             for i in items]
                    print(f"  all names: {names}")
            except Exception as e:
                print(f"\n--- {obj}: ERROR {e} ---")

    # also check what the full-config dump looks like
    print(f"\n{'='*70}\nshowcommand: show wlan virtual-ap\n{'='*70}")
    try:
        r = s.get(f"{base}/v1/configuration/showcommand",
                  params={"UIDARUBA": uid, "command": "show wlan virtual-ap"},
                  timeout=15)
        print(json.dumps(r.json(), indent=2)[:3000])
    except Exception as e:
        print(f"ERROR: {e}")


if __name__ == "__main__":
    main()
