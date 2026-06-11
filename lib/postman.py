"""
Build a Postman v2.1 collection from the shared API catalog (lib.api_catalog),
so the importable collection and the in-app curl docs never drift.
"""
import json

from lib.api_catalog import GROUPS, VARIABLES


def _body(b: dict) -> dict:
    if b["mode"] == "urlencoded":
        return {"mode": "urlencoded",
                "urlencoded": [{"key": k, "value": v} for k, v in b["data"].items()]}
    raw = b["data"] if isinstance(b["data"], str) else json.dumps(b["data"], indent=2)
    return {"mode": "raw", "raw": raw, "options": {"raw": {"language": "json"}}}


def _item(r: dict) -> dict:
    req = {
        "method": r["method"],
        "header": [{"key": k, "value": v} for k, v in (r.get("headers") or {}).items()],
        "url": {"raw": r["url"]},
    }
    if r.get("desc"):
        req["description"] = r["desc"]
    if r.get("body"):
        req["body"] = _body(r["body"])
    return {"name": r["name"], "request": req}


def build_collection() -> dict:
    return {
        "info": {
            "name": "AOS 8 → Aruba Central Migration",
            "description": "Generated from the migration tool's API catalog. Set the "
                           "collection variables (or a Postman environment) before sending. "
                           "Get a GreenLake token first, then paste it into central_token / "
                           "glp_token.",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": [
            {"name": g["name"], "description": g.get("blurb", ""),
             "item": [_item(r) for r in g["requests"]]}
            for g in GROUPS
        ],
        "variable": [{"key": k, "value": v} for k, v, _desc in VARIABLES],
    }


def collection_json() -> str:
    return json.dumps(build_collection(), indent=2)
