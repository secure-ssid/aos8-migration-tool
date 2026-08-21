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
from urllib.parse import urlsplit

import requests

from .http_base import normalize_base

TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"
GLP_BASE_URL = "https://global.api.greenlake.hpe.com"

_POLL_INTERVAL = 10   # seconds
_POLL_TIMEOUT = 300   # 5 minutes

# HPE's AsyncOperationResource.status enum is INITIALIZED, RUNNING, FAILED,
# SUCCEEDED, TIMEDOUT, PAUSED — but HPE's own prose says TIMEOUT and the New
# Central guide says TIMED_OUT. Statuses are normalised to letters only before
# they are matched here, so every spelling of the same state lands in one set.
_TERMINAL_OK = {"completed", "success", "succeeded"}
_TERMINAL_FAIL = {"failed", "error", "timeout", "timedout", "cancelled"}

_SERIAL_SAFE = re.compile(r"^[A-Za-z0-9_-]+$")


class GLPAPIError(Exception):
    pass


class GLPClient:
    def __init__(self, client_id: str, client_secret: str,
                 base_url: str = GLP_BASE_URL, timeout: int = 30):
        self.base = normalize_base(base_url)
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.token: Optional[str] = None
        # GLP-scoped tokens live ~15 minutes and carry no refresh token
        self._token_expiry: float = 0.0
        self.session = requests.Session()
        self._device_id_cache: dict[str, str] = {}

    # ─────────────────── Auth / HTTP ───────────────────

    def authenticate(self) -> bool:
        try:
            # plain requests.post, NOT self.session — the session permanently
            # carries the previous Bearer header from the update below, and it
            # must never be sent to the token endpoint
            resp = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as e:
            raise GLPAPIError(
                f"GLP token request to GreenLake SSO failed ({type(e).__name__}) "
                "— check network reachability to sso.common.cloud.hpe.com")
        if not resp.ok:
            raise GLPAPIError(f"GLP token request failed {resp.status_code}: {resp.text[:300]}")
        try:
            body = resp.json()
            self.token = body["access_token"]
        except (ValueError, KeyError):
            raise GLPAPIError("GLP token endpoint returned an unexpected body "
                              "(no access_token)")
        # GLP-scoped clients get expires_in≈899 (15 min) and NO refresh token,
        # while poll_task alone can block for 5 minutes — without proactive
        # renewal a Step-4 run routinely dies on an expired token mid-claim.
        try:
            ttl = int(body.get("expires_in", 900))
        except (TypeError, ValueError):
            ttl = 900
        self._token_expiry = time.time() + ttl - 60   # 60s clock skew
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})
        return True

    def _request(self, method: str, path: str, json: Optional[dict] = None,
                 params: Optional[dict] = None, headers: Optional[dict] = None,
                 _auth_retried: bool = False,
                 _rate_retried: bool = False) -> requests.Response:
        # Proactive refresh. The `self._token_expiry and` guard is load-bearing:
        # a client whose token was injected (tests, restored session) has 0.0
        # here and must not fire a spurious token request.
        if self._token_expiry and time.time() >= self._token_expiry:
            self.authenticate()
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
        if resp.status_code == 401 and not _auth_retried:
            self.authenticate()
            return self._request(method, path, json, params, headers,
                                 _auth_retried=True, _rate_retried=_rate_retried)
        if resp.status_code == 429 and not _rate_retried:
            # Retry-After may be an HTTP-date (RFC 7231) — fall back to 30s
            # rather than crashing on int().
            retry_after = resp.headers.get("Retry-After", "30").strip()
            wait = int(retry_after) if retry_after.isdigit() else 30
            time.sleep(min(wait, 120))
            return self._request(method, path, json, params, headers,
                                 _auth_retried=_auth_retried, _rate_retried=True)
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
        try:
            body = resp.json()
        except ValueError:
            # an HTML interstitial from a corporate proxy would otherwise raise
            # JSONDecodeError — not a GLPAPIError — straight through poll_task's
            # loop and out as a Streamlit traceback
            raise GLPAPIError(f"GET {path}: GreenLake returned a non-JSON body "
                              f"({resp.status_code}): {resp.text[:200]}")
        return {"items": body} if isinstance(body, list) else body

    def _paginate(self, path: str, items_key: Optional[str] = None,
                  params: Optional[dict] = None, page_size: int = 100,
                  max_pages: int = 50) -> list:
        """Bounded offset pagination. An endpoint that ignores `offset` would
        otherwise loop forever inside a Streamlit spinner, and a truncated
        inventory read makes already-claimed APs look unclaimed — so both
        conditions raise instead of returning partial results."""
        items, offset = [], 0
        params = dict(params or {})
        first_of_prev_page = object()
        for _ in range(max_pages):
            params.update({"limit": page_size, "offset": offset})
            data = self._get(path, params=params)
            page = data.get(items_key) if items_key else None
            if page is None:
                page = (data.get("items") or data.get("devices")
                        or data.get("subscriptions") or [])
            if not isinstance(page, list):
                page = [page] if page else []
            if page and page[0] == first_of_prev_page:
                raise GLPAPIError(
                    f"GET {path}: server ignored offset={offset} (page repeated) "
                    "— cannot enumerate completely")
            first_of_prev_page = page[0] if page else None
            items.extend(page)
            if len(page) < page_size:
                return items
            offset += page_size
        raise GLPAPIError(
            f"GET {path}: more than {max_pages * page_size} items — pagination cap hit")

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
                task_id = resp.json().get("transactionId", "")
            except Exception:
                task_id = ""
            if not task_id:
                raise GLPAPIError("Claim accepted but no async-operation id "
                                  "(Location header or transactionId) returned")
            return task_id
        return location.rstrip("/").split("/")[-1]

    @staticmethod
    def failed_serials(result: dict) -> list[str]:
        """Serials GLP rejected in an async-operation result (the dict
        poll_task returns) — a 'completed' claim can still reject some."""
        return [str(s) for s in
                (result.get("result") or {}).get("failedDevicesSerial") or []]

    @staticmethod
    def _rejected_msg(failed: list[str]) -> str:
        msg = ("GreenLake rejected these serials: " + ", ".join(failed) +
               ". GLP only claims devices that exist in HPE's records — ")
        # only blame test data when the serials ARE test data; telling a field
        # engineer their production APs are "fake" buries the real problem
        if failed and all(str(s).upper().startswith("ZZTEST") for s in failed):
            msg += ("fake/zztest serials always fail here. Use real AP "
                    "serial+MAC pairs to test claiming.")
        else:
            msg += ("check each serial + wired-MAC pairing and that the "
                    "device isn't already owned by another workspace.")
        return msg

    def poll_task(self, task_id: str, timeout: int = _POLL_TIMEOUT,
                  interval: int = _POLL_INTERVAL, on_poll=None) -> dict:
        """Poll an async-operation until it completes.

        Returns the final async-op result dict on completion. A 'completed'
        claim can still reject individual serials — check failed_serials() on
        the return value. Raises GLPAPIError when the operation status is
        failed/error/timeout/cancelled, when polling times out, or when a
        'completed' operation rejected EVERY submitted device.

        on_poll(attempt:int, status:str) is called after each poll so the UI
        can show progress while this blocks.
        """
        if not task_id:
            raise GLPAPIError("poll_task called with an empty async-operation id")
        # accept a bare operation id (claims: /devices/v1 root) or a full
        # Location path/URL (other surfaces root the operation elsewhere).
        # RFC 7231 §7.1.2 permits a RELATIVE Location, so parse rather than
        # string-split: "devices/v1/..." must not become "…hpe.comdevices/…".
        if "/" in task_id:
            parts = urlsplit(task_id)
            path = parts.path or "/"
            if not path.startswith("/"):
                path = "/" + path
            if path == "/":
                raise GLPAPIError(
                    f"GLP returned an async-operation location with no path: {task_id!r}")
            if parts.query:
                path = f"{path}?{parts.query}"
        else:
            path = f"/devices/v1/async-operations/{task_id}"
        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            result = self._get(path)
            raw = str(result.get("status") or result.get("state") or "")
            if not raw:
                raise GLPAPIError(
                    f"GLP async-op {task_id}: unrecognised body {str(result)[:300]}")
            # letters only: TIMED_OUT / TIMEDOUT / TIMEOUT all collapse into the
            # failure set, so a timed-out claim reports GLP's reason instead of
            # polling to this method's own deadline
            status = re.sub(r"[^a-z]", "", raw.lower())
            if on_poll:
                on_poll(attempt, raw)
            failed = self.failed_serials(result)
            if status in _TERMINAL_OK or "partial" in status:
                succeeded = (result.get("result") or {}).get(
                    "successfulDevicesSerial") or []
                if failed and not succeeded:
                    # terminal body, but every submitted device was rejected
                    raise GLPAPIError(self._rejected_msg(failed))
                return result
            if status in _TERMINAL_FAIL:
                if failed:
                    raise GLPAPIError(self._rejected_msg(failed))
                raise GLPAPIError(f"GLP claim operation {task_id} failed: {result}")
            # PAUSED / RUNNING / INITIALIZED are non-terminal; on_poll above
            # keeps a paused operation visible instead of silently blocking
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

    def list_all_subscriptions(self) -> list[dict]:
        """Every subscription in the workspace — workspaces can hold more
        than the 100-per-page API limit, and a subscription the UI can't see
        can't be assigned."""
        return self._paginate("/subscriptions/v1/subscriptions", page_size=100)

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

    def list_all_devices(self) -> list[dict]:
        """Every device in the workspace. Raises rather than returning a
        partial list — a short read makes already-claimed APs look unclaimed
        and re-submits them."""
        return self._paginate("/devices/v1/devices", page_size=100)

    def workspace_serials(self) -> set[str]:
        """All device serials currently in the workspace (uppercased)."""
        return {str(d.get("serialNumber", "")).strip().upper()
                for d in self.list_all_devices()
                if d.get("serialNumber")}

    def assign_subscription(self, serial_number: str, subscription_key_or_id: str) -> dict:
        """Assign a subscription to a claimed device (v2beta1 merge-patch)."""
        sub_id = self._resolve_subscription_id(subscription_key_or_id)
        device_id = self.resolve_device_id(serial_number)
        if device_id is None:
            raise GLPAPIError(
                f"Device {serial_number} not found in the workspace — claim it first")
        # async-aware: a 202 must be polled to a terminal state, or a rejected
        # assignment is reported to the operator as success
        self._patch_device(device_id, {"subscription": [{"id": sub_id}]})
        return {}

    # ─────────────────── Application assignment ───────────────────

    def list_service_managers(self) -> list[dict]:
        """Provisioned application instances (e.g. Aruba Central) in the
        workspace, with their region — needed to assign a device to Central.
        Returns [{id, name, region}].

        One route only: HPE's /service-catalog/v1beta1/ was retired with EOL
        2025-06-30, so a fallback there could only add a failed round-trip and
        a misleading error. A 403 here RAISES — reporting "no Central instances"
        for a permission problem sends the operator to the wrong fix.
        """
        r = self._get("/service-catalog/v1/service-manager-provisions")
        out: list[dict] = []
        for i in r.get("items", r.get("provisions", [])):
            sm = i.get("serviceManager", {}) if isinstance(i.get("serviceManager"), dict) else {}
            name = (i.get("name") or i.get("serviceManagerName")
                    or i.get("applicationName") or sm.get("name") or "Central")
            region = (i.get("region") or i.get("regionCode")
                      or i.get("regionName") or "")
            if i.get("id"):
                out.append({"id": i["id"], "name": name, "region": region})
        return out

    def _patch_device(self, device_id: str, body: dict) -> None:
        """One device merge-patch, async-aware (202 → poll to a terminal state).

        A 202 carrying its operation id in the BODY rather than in Location
        must still be polled: returning here would report an unconfirmed —
        possibly rejected — assignment to the operator as success."""
        resp = self._request(
            "PATCH", "/devices/v2beta1/devices",
            params={"id": device_id}, json=body,
            headers={"Content-Type": "application/merge-patch+json"},
        )
        if resp.status_code != 202:
            return
        payload: dict = {}
        if resp.content:
            try:
                decoded = resp.json()
            except ValueError:
                decoded = None
            if isinstance(decoded, dict):
                payload = decoded
        # pass the full Location path — v2beta1 operations are not guaranteed
        # to live under the /devices/v1 async-operations root
        op = (resp.headers.get("Location", "") or payload.get("transactionId")
              or payload.get("id"))
        if not op:
            raise GLPAPIError(
                "PATCH /devices/v2beta1/devices returned 202 with no pollable "
                "operation id (no Location, transactionId or id) — the "
                "assignment cannot be confirmed and must not be reported as done")
        self.poll_task(str(op).rstrip("/"))

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
