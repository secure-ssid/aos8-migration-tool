"""
HPE GreenLake Platform (GLP) client — device claiming + subscription assignment.

API mechanics (mirrors the working centralmcp GLP client):
  - Token:   POST https://sso.common.cloud.hpe.com/as/token.oauth2
             (GLP API client credentials, client_credentials grant)
  - Base:    https://global.api.greenlake.hpe.com
  - Claim:   POST /devices/v1/devices  {"network":[{serialNumber,macAddress}],...}
             → 202 Accepted, Location: /devices/v1/async-operations/{id}
             → poll until completed (returns successfulDevicesSerial /
               failedDevicesSerial)
  - Subs:    GET /subscriptions/v1/subscriptions (key → UUID resolve)
  - Assign:  PATCH /devices/v2beta1/devices?id=<device-uuid>
             {"subscription":[{"id": <subscription-uuid>}]}
             (merge-patch+json)

macAddress is REQUIRED by GLP when claiming network devices — discovery
captures the wired MAC from `show ap database long`.
"""
import re
import time
import uuid
from typing import Any, Optional

import requests

TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"
GLP_BASE_URL = "https://global.api.greenlake.hpe.com"

_POLL_INTERVAL = 10   # seconds
_POLL_TIMEOUT = 300   # 5 minutes

_SERIAL_SAFE = re.compile(r"^[A-Za-z0-9_-]+$")


class GLPAPIError(Exception):
    pass



def _normalize_base(url: str) -> str:
    """Ensure the base URL has a scheme and no trailing slash. Operators often
    paste a bare host (internal.api.central.arubanetworks.com) — default to
    https:// so requests don't fail with 'No scheme supplied'."""
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


class GLPClient:
    def __init__(self, client_id: str, client_secret: str,
                 base_url: str = GLP_BASE_URL, timeout: int = 30):
        self.base = _normalize_base(base_url)
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.token: Optional[str] = None
        self.session = requests.Session()
        self._device_id_cache: dict[str, str] = {}

    # ─────────────────── Auth / HTTP ───────────────────

    def authenticate(self) -> bool:
        resp = self.session.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        if not resp.ok:
            raise GLPAPIError(f"GLP token request failed {resp.status_code}: {resp.text[:300]}")
        self.token = resp.json()["access_token"]
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})
        return True

    def _request(self, method: str, path: str, json: Optional[dict] = None,
                 params: Optional[dict] = None, headers: Optional[dict] = None,
                 _retried: bool = False) -> requests.Response:
        try:
            resp = self.session.request(
                method, f"{self.base}{path}", json=json, params=params,
                headers=headers, timeout=self.timeout,
            )
        except requests.exceptions.Timeout:
            raise GLPAPIError(f"{method} {path}: request timed out after {self.timeout}s")
        except requests.exceptions.ConnectionError as e:
            raise GLPAPIError(f"{method} {path}: connection to GreenLake failed "
                              f"({type(e).__name__})")
        if resp.status_code == 401 and not _retried:
            self.authenticate()
            return self._request(method, path, json, params, headers, _retried=True)
        if resp.status_code == 429 and not _retried:
            time.sleep(min(int(resp.headers.get("Retry-After", 30)), 120))
            return self._request(method, path, json, params, headers, _retried=True)
        if not resp.ok and resp.status_code != 202:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:300]
            raise GLPAPIError(f"{method} {path} failed {resp.status_code}: {detail}")
        return resp

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = self._request("GET", path, params=params)
        if not resp.content:
            return {}
        body = resp.json()
        return {"items": body} if isinstance(body, list) else body

    # ─────────────────── Devices ───────────────────

    def list_devices(self, limit: int = 100, offset: int = 0,
                     filter: Optional[str] = None) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if filter:
            params["filter"] = filter
        result = self._get("/devices/v1/devices", params=params)
        return result.get("items", result.get("devices", []))

    def get_device(self, serial_number: str) -> Optional[dict]:
        if not _SERIAL_SAFE.match(serial_number or ""):
            return None
        items = self.list_devices(filter=f"serialNumber eq '{serial_number}'")
        return items[0] if items else None

    def add_devices(self, devices: list[dict[str, str]]) -> str:
        """Claim network devices into the workspace.

        devices: [{"serialNumber": ..., "macAddress": ...}] — MAC required.
        Returns the async-operation id for poll_task().
        """
        for d in devices:
            if not d.get("macAddress"):
                raise GLPAPIError(
                    f"macAddress is required to claim {d.get('serialNumber', '?')} — "
                    "re-discover with `show ap database long` (Wired MAC column)")
        body = {"network": devices, "compute": [], "storage": []}
        resp = self._request("POST", "/devices/v1/devices", json=body)
        location = resp.headers.get("Location", "")
        if not location:
            # some responses return the operation inline
            try:
                return resp.json().get("transactionId", "")
            except Exception:
                raise GLPAPIError("Claim accepted but no async-operation Location returned")
        return location.rstrip("/").split("/")[-1]

    def poll_task(self, task_id: str, timeout: int = _POLL_TIMEOUT,
                  interval: int = _POLL_INTERVAL, on_poll=None) -> dict:
        """Poll an async-operation until it completes; raises on failure/timeout.

        on_poll(attempt:int, status:str) is called after each poll so the UI
        can show progress while this blocks.
        """
        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            result = self._get(f"/devices/v1/async-operations/{task_id}")
            status = str(result.get("status", "")).lower()
            if on_poll:
                on_poll(attempt, status or "pending")
            if status in ("completed", "success", "succeeded"):
                return result
            if status in ("failed", "error", "timeout", "cancelled"):
                failed = (result.get("result") or {}).get("failedDevicesSerial") or []
                if failed:
                    raise GLPAPIError(
                        "GreenLake rejected these serials: " + ", ".join(failed) +
                        ". GLP only claims devices that exist in HPE's records — "
                        "fake/zztest serials will always fail here. Use real AP "
                        "serial+MAC pairs to test claiming.")
                raise GLPAPIError(f"GLP claim operation {task_id} failed: {result}")
            time.sleep(interval)
        raise GLPAPIError(f"GLP claim operation {task_id} timed out after {timeout}s")

    def resolve_device_id(self, serial_number: str) -> Optional[str]:
        if serial_number in self._device_id_cache:
            return self._device_id_cache[serial_number]
        device = self.get_device(serial_number)
        if device and device.get("id"):
            self._device_id_cache[serial_number] = device["id"]
            return device["id"]
        return None

    # ─────────────────── Subscriptions ───────────────────

    def list_subscriptions(self, limit: int = 100, offset: int = 0) -> list[dict]:
        result = self._get("/subscriptions/v1/subscriptions",
                           params={"limit": limit, "offset": offset})
        return result.get("items", result.get("subscriptions", []))

    def _resolve_subscription_id(self, key_or_id: str) -> str:
        # canonical UUIDs pass through; keys are resolved via OData filter
        try:
            uuid.UUID(key_or_id)
            return key_or_id
        except (ValueError, AttributeError, TypeError):
            pass
        if not _SERIAL_SAFE.match(key_or_id or ""):
            raise GLPAPIError(f"Subscription key {key_or_id!r} contains unexpected "
                              "characters — pass the GLP subscription UUID instead")
        result = self._get("/subscriptions/v1/subscriptions",
                           params={"filter": f"key eq '{key_or_id}'"})
        items = result.get("items", result.get("subscriptions", []))
        if not items:
            raise GLPAPIError(f"Subscription key {key_or_id!r} not found in this workspace")
        return items[0]["id"]

    def workspace_serials(self) -> set[str]:
        """All device serials currently in the workspace (uppercased)."""
        serials, offset = set(), 0
        while True:
            page = self.list_devices(limit=100, offset=offset)
            serials |= {str(d.get("serialNumber", "")).strip().upper()
                        for d in page if d.get("serialNumber")}
            if len(page) < 100:
                return serials
            offset += 100

    def assign_subscription(self, serial_number: str, subscription_key_or_id: str) -> dict:
        """Assign a subscription to a claimed device (v2beta1 merge-patch)."""
        sub_id = self._resolve_subscription_id(subscription_key_or_id)
        device_id = self.resolve_device_id(serial_number)
        if device_id is None:
            raise GLPAPIError(
                f"Device {serial_number} not found in the workspace — claim it first")
        resp = self._request(
            "PATCH", "/devices/v2beta1/devices",
            params={"id": device_id},
            json={"subscription": [{"id": sub_id}]},
            headers={"Content-Type": "application/merge-patch+json"},
        )
        try:
            return resp.json() if resp.content else {}
        except Exception:
            return {}

    # ─────────────────── Application assignment ───────────────────

    def list_service_managers(self) -> list[dict]:
        """Provisioned application instances (e.g. Aruba Central) in the
        workspace, with their region — needed to assign a device to Central.
        Returns [{id, name, region}]."""
        out: list[dict] = []
        for path in ("/service-catalog/v1/service-manager-provisions",
                     "/service-catalog/v1beta1/service-manager-provisions"):
            try:
                r = self._get(path)
            except GLPAPIError:
                continue
            for i in r.get("items", r.get("provisions", [])):
                sm = i.get("serviceManager", {}) if isinstance(i.get("serviceManager"), dict) else {}
                name = (i.get("name") or i.get("serviceManagerName")
                        or i.get("applicationName") or sm.get("name") or "Central")
                region = (i.get("region") or i.get("regionCode")
                          or i.get("regionName") or "")
                if i.get("id"):
                    out.append({"id": i["id"], "name": name, "region": region})
            if out:
                return out
        return out

    def _patch_device(self, device_id: str, body: dict) -> None:
        """One device merge-patch, async-aware (202 + Location → poll)."""
        resp = self._request(
            "PATCH", "/devices/v2beta1/devices",
            params={"id": device_id}, json=body,
            headers={"Content-Type": "application/merge-patch+json"},
        )
        location = resp.headers.get("Location", "")
        if resp.status_code == 202 and location:
            self.poll_task(location.rstrip("/").split("/")[-1])

    def assign_application(self, serial_number: str, application_id: str,
                           region: str,
                           subscription_key_or_id: Optional[str] = None) -> dict:
        """Assign a claimed device to a Central application instance + region —
        this is what makes the device appear in New Central (the GLP
        'Application' column). GreenLake rejects combining a device-update and a
        subscription op in one PATCH ("...should not be together"), so this is
        TWO sequential merge-patches: application+region first, then the
        subscription. Async: each is polled."""
        device_id = self.resolve_device_id(serial_number)
        if device_id is None:
            raise GLPAPIError(
                f"Device {serial_number} not found in the workspace — claim it first")
        # 1. device update — application + region
        self._patch_device(device_id, {"application": {"id": application_id},
                                        "region": region})
        # 2. subscription — separate operation
        if subscription_key_or_id:
            sub_id = self._resolve_subscription_id(subscription_key_or_id)
            self._patch_device(device_id, {"subscription": [{"id": sub_id}]})
        return {}
