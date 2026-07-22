"""Mocked-HTTP regression tests for the three API clients — the retry,
token-refresh, and cache behaviors that broke in the field. No real Aruba/HPE
endpoint is contacted: every test spins a local HTTP server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import lib.central_client as central_mod
import lib.classic_central_client as classic_mod
import lib.glp_client as glp_mod
from lib.central_client import CentralAPIError, CentralClient, _is_duplicate
from lib.classic_central_client import ClassicCentralClient
from lib.glp_client import GLPClient


class MockAPI:
    """Tiny per-test HTTP server. Set .app to a callable
    (method, path, query, body) -> (status, headers, obj). Every request is
    recorded in .calls as (method, path)."""

    def __init__(self):
        self.calls = []
        self.app = lambda m, p, q, b: (200, {}, {})
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                path, _, query = self.path.partition("?")
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw.decode() or "null")
                except ValueError:
                    body = raw.decode(errors="replace")
                outer.calls.append((self.command, path))
                status, headers, obj = outer.app(self.command, path, query, body)
                data = (obj if isinstance(obj, (bytes, str)) else json.dumps(obj))
                if isinstance(data, str):
                    data = data.encode()
                self.send_response(status)
                if "Content-Type" not in (headers or {}):
                    self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def mock_api(monkeypatch):
    api = MockAPI()
    # token endpoints -> the mock; sleeps -> recorded, not slept
    monkeypatch.setattr(central_mod, "TOKEN_URL", api.url + "/as/token.oauth2")
    monkeypatch.setattr(glp_mod, "TOKEN_URL", api.url + "/as/token.oauth2")
    sleeps = []
    monkeypatch.setattr(central_mod.time, "sleep", sleeps.append)
    monkeypatch.setattr(classic_mod.time, "sleep", sleeps.append)
    monkeypatch.setattr(glp_mod.time, "sleep", sleeps.append)
    api.sleeps = sleeps
    yield api
    api.close()


def _token_response():
    return (200, {}, {"access_token": "tok", "token_type": "Bearer",
                      "expires_in": 7199})


# ─────────────────── New Central ───────────────────

def _central(api) -> CentralClient:
    c = CentralClient(api.url, "id", "secret")
    c.token = "tok"
    c.session.headers.update({"Authorization": "Bearer tok"})
    return c


def test_central_429_http_date_retry_after_does_not_crash(mock_api):
    state = {"n": 0}

    def app(method, path, query, body):
        if path.endswith("/as/token.oauth2"):
            return _token_response()
        state["n"] += 1
        if state["n"] == 1:
            return (429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, {})
        return (200, {}, {"ok": True})

    mock_api.app = app
    c = _central(mock_api)
    assert c._get("/x")["ok"] is True
    assert mock_api.sleeps == [30]           # date form -> default backoff


def test_central_401_then_429_both_get_their_retry(mock_api):
    state = {"n": 0}

    def app(method, path, query, body):
        if path.endswith("/as/token.oauth2"):
            return _token_response()
        state["n"] += 1
        if state["n"] == 1:
            return (401, {}, {})
        if state["n"] == 2:
            return (429, {"Retry-After": "1"}, {})
        return (200, {}, {"ok": True})

    mock_api.app = app
    c = _central(mock_api)
    assert c._get("/x")["ok"] is True        # separate flags: both retried


def test_central_2xx_non_json_body_is_success(mock_api):
    mock_api.app = lambda m, p, q, b: (
        _token_response() if p.endswith("oauth2")
        else (200, {"Content-Type": "text/plain"}, "OK"))
    c = _central(mock_api)
    assert c._get("/x") == {"_raw": "OK"}


def test_is_duplicate_ignores_url_path():
    # customer named an object "duplicate-lab" — an unrelated 400 on its path
    # must NOT read as idempotent success
    e = CentralAPIError("POST /roles/duplicate-lab failed 400: invalid value")
    assert not _is_duplicate(e)
    e2 = CentralAPIError("POST /roles/corp failed 400: object already exists")
    assert _is_duplicate(e2)


def test_central_create_site_duplicate_resolves_via_relist(mock_api):
    def app(method, path, query, body):
        if path.endswith("/as/token.oauth2"):
            return _token_response()
        if method == "POST" and "sites" in path:
            return (400, {}, {"error": "site already exists"})
        if method == "GET" and "sites" in path:
            return (200, {}, {"items": [{"id": 42, "name": "branch-1"}]})
        return (200, {}, {})

    mock_api.app = app
    c = _central(mock_api)
    c._sites_cache = []                      # pre-list saw nothing
    assert c.create_site("branch-1") == "42"


# ─────────────────── Classic Central ───────────────────

def test_classic_create_site_finds_id_despite_stale_cache(mock_api):
    """Reproduces the field bug: POST doesn't echo site_id and the fallback
    list must bypass the pre-create cache."""
    created = {"done": False}

    def app(method, path, query, body):
        if path == "/central/v2/sites" and method == "GET":
            sites = ([{"site_id": 7, "site_name": "branch-1"}]
                     if created["done"] else [])
            return (200, {}, {"sites": sites, "total": len(sites)})
        if path == "/central/v2/sites" and method == "POST":
            created["done"] = True
            return (200, {}, {})             # no site_id echo
        return (200, {}, {})

    mock_api.app = app
    c = ClassicCentralClient(mock_api.url, "tok")
    assert c.create_site("branch-1") == 7


def test_classic_401_refreshes_and_rotates_token(mock_api):
    state = {"n": 0}

    def app(method, path, query, body):
        if path == "/oauth2/token":
            assert "refresh_token=old-rt" in query
            return (200, {}, {"access_token": "new-at",
                              "refresh_token": "new-rt"})
        state["n"] += 1
        if state["n"] == 1:
            return (401, {}, {})
        return (200, {}, {"data": [], "total": 0})

    mock_api.app = app
    c = ClassicCentralClient(mock_api.url, "expired-at",
                             client_id="cid", client_secret="cs",
                             refresh_token="old-rt")
    assert c.list_group_names() == []
    assert c.refresh_token == "new-rt"       # rotation captured


def test_classic_firmware_v2_falls_back_to_v1_on_404(mock_api):
    def app(method, path, query, body):
        if "firmware/v2" in path:
            return (404, {}, {"error": "not found"})
        if "firmware/v1" in path:
            return (200, {}, {})
        return (200, {}, {})

    mock_api.app = app
    c = ClassicCentralClient(mock_api.url, "tok")
    c.set_firmware_compliance("g1", "10.7.0.0")
    assert any("firmware/v1" in p for _m, p in mock_api.calls)


# ─────────────────── GreenLake ───────────────────

def test_glp_claim_returns_op_id_and_poll_completes(mock_api):
    def app(method, path, query, body):
        if path.endswith("/as/token.oauth2"):
            return _token_response()
        if path == "/devices/v1/devices" and method == "POST":
            return (202, {"Location": "/devices/v1/async-operations/op-9"}, {})
        if path == "/devices/v1/async-operations/op-9":
            return (200, {}, {"status": "SUCCEEDED",
                              "result": {"successfulDevicesSerial": ["S1"]}})
        return (200, {}, {})

    mock_api.app = app
    g = GLPClient("id", "secret", base_url=mock_api.url)
    g.token = "tok"
    g.session.headers.update({"Authorization": "Bearer tok"})
    op = g.add_devices([{"serialNumber": "S1", "macAddress": "aa:bb:cc:00:00:01"}])
    assert op == "op-9"
    result = g.poll_task(op)
    assert result["status"] == "SUCCEEDED"


def test_glp_assign_subscription_polls_202(mock_api):
    polled = {"n": 0}
    sub_id = "3f2e1d00-0000-4000-8000-000000000001"

    def app(method, path, query, body):
        if path.endswith("/as/token.oauth2"):
            return _token_response()
        if path == "/devices/v1/devices" and method == "GET":
            return (200, {}, {"items": [{"id": "dev-1", "serialNumber": "S1"}]})
        if path == "/devices/v2beta1/devices" and method == "PATCH":
            return (202, {"Location": "/devices/v2beta1/async-operations/op-5"}, {})
        if "async-operations/op-5" in path:
            polled["n"] += 1
            return (200, {}, {"status": "SUCCEEDED"})
        return (200, {}, {})

    mock_api.app = app
    g = GLPClient("id", "secret", base_url=mock_api.url)
    g.token = "tok"
    g.session.headers.update({"Authorization": "Bearer tok"})
    g.assign_subscription("S1", sub_id)
    # the 202 was polled to a terminal state, at the v2beta1 root it named
    assert polled["n"] >= 1
    assert any("v2beta1/async-operations" in p for _m, p in mock_api.calls)
