"""Mocked-HTTP regression tests for the three API clients — the retry,
token-refresh, and cache behaviors that broke in the field. No real Aruba/HPE
endpoint is contacted: every test spins a local HTTP server."""
import ipaddress
import json
import os
import shutil
import ssl
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import lib.central_client as central_mod
import lib.classic_central_client as classic_mod
import lib.glp_client as glp_mod
from lib.aos8_client import AOS8APIError, AOS8Client
from lib.central_client import CentralAPIError, CentralClient, _is_duplicate
from lib.classic_central_client import ClassicCentralAPIError, ClassicCentralClient
from lib.glp_client import GLPAPIError, GLPClient


def _localhost_tls_files() -> tuple[str, str, str]:
    """Ephemeral self-signed cert + key for the AOS 8 mock, in a temp dir.

    AOS 8 controllers ship a self-signed cert and AOS8Client sets
    session.verify = False, so serving real TLS here exercises the production
    https:// code path instead of pretending port 4343 speaks plain HTTP."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False)
            .sign(key, hashes.SHA256()))
    tmp = tempfile.mkdtemp()
    cert_path, key_path = os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem")
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    return cert_path, key_path, tmp


class MockAPI:
    """Tiny per-test HTTP server. Set .app to a callable
    (method, path, query, body) -> (status, headers, obj). Every request is
    recorded in .calls as (method, path) and, in full, in .requests as
    {"method", "path", "query", "headers", "body"}."""

    def __init__(self, tls: bool = False):
        self.calls = []
        self.requests = []
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
                outer.requests.append({
                    "method": self.command, "path": path, "query": query,
                    "headers": dict(self.headers), "body": body,
                })
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
        self._tmpdir = None
        if tls:
            cert_path, key_path, self._tmpdir = _localhost_tls_files()
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert_path, key_path)
            self.server.socket = ctx.wrap_socket(self.server.socket,
                                                 server_side=True)
        self.port = self.server.server_port
        self.url = f"{'https' if tls else 'http'}://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)


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


@pytest.fixture()
def aos8_api(monkeypatch):
    """HTTPS mock for the AOS 8 controller API (port 4343 is TLS-only).

    The mock serves a self-signed cert, and AOS8Client now verifies by
    default — so the test harness opts out through AOS8_DEV_MODE exactly
    like a local lab would."""
    monkeypatch.setenv("AOS8_DEV_MODE", "true")
    api = MockAPI(tls=True)
    yield api
    api.close()


def _aos8(api) -> AOS8Client:
    c = AOS8Client("127.0.0.1", "admin", "pw", port=api.port)
    c.uidaruba = "sess-1"          # skip the login round trip
    return c


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


def test_central_2xx_non_json_body_fails_closed(mock_api):
    """A 2xx that is not JSON is a protocol violation, not a success — the
    corrupt body must fail closed instead of being flattened into a dict."""
    mock_api.app = lambda m, p, q, b: (
        _token_response() if p.endswith("oauth2")
        else (200, {"Content-Type": "text/plain"}, "OK"))
    c = _central(mock_api)
    with pytest.raises(CentralAPIError, match="non-JSON body"):
        c._get("/x")


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
    assert c.create_site("branch-1", address="1 Main St", city="San Jose",
                         country="US") == "42"


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
    assert c.create_site("branch-1", address="1 Main St", city="San Jose",
                         country="US") == 7


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


def test_classic_2xx_non_json_body_fails_closed(mock_api):
    """A 2xx that is not JSON must raise, not flatten to {} — otherwise a
    corrupt body reads as success and create_group re-POSTs."""
    mock_api.app = lambda m, p, q, b: (
        200, {"Content-Type": "text/plain"}, "OK")
    c = ClassicCentralClient(mock_api.url, "tok")
    with pytest.raises(ClassicCentralAPIError, match="non-JSON body"):
        c._get("/x")


# ─────────────────── GreenLake ───────────────────

def _glp(api) -> GLPClient:
    g = GLPClient("id", "secret", base_url=api.url)
    g.token = "tok"
    g.session.headers.update({"Authorization": "Bearer tok"})
    return g


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


def test_central_scope_resolution_accepts_scope_name_only(mock_api):
    """pycentral-shaped responses carry the numeric id in scope-name."""
    mock_api.app = lambda m, p, q, b: (
        _token_response() if p.endswith("oauth2")
        else (200, {}, {"scope-map": [{"scope-name": "123",
                                       "persona": "SERVICE_PERSONA",
                                       "resource": "example"}]}))
    c = _central(mock_api)
    assert c.get_global_scope_id() == "123"


def test_central_list_sites_paginates_past_100(mock_api):
    def app(method, path, query, body):
        if path.endswith("/as/token.oauth2"):
            return _token_response()
        if "sites" in path and method == "GET":
            offset = 0
            for part in query.split("&"):
                if part.startswith("offset="):
                    offset = int(part.split("=")[1])
            n = 100 if offset == 0 else 40
            items = [{"id": offset + i, "name": f"site-{offset + i}"}
                     for i in range(n)]
            return (200, {}, {"items": items})
        return (200, {}, {})

    mock_api.app = app
    c = _central(mock_api)
    assert len(c.list_sites(refresh=True)) == 140


def test_glp_timed_out_is_terminal(mock_api):
    def app(method, path, query, body):
        if path.endswith("/as/token.oauth2"):
            return _token_response()
        if "async-operations" in path:
            return (200, {}, {"status": "TIMED_OUT",
                              "result": {"failedDevicesSerial": ["S1"]}})
        return (200, {}, {})

    mock_api.app = app
    g = _glp(mock_api)
    with pytest.raises(GLPAPIError, match="GreenLake rejected these serials"):
        g.poll_task("op-1", timeout=5, interval=0)


def test_glp_list_all_subscriptions_paginates(mock_api):
    def app(method, path, query, body):
        if path.endswith("/as/token.oauth2"):
            return _token_response()
        if "subscriptions" in path:
            offset = 0
            for part in query.split("&"):
                if part.startswith("offset="):
                    offset = int(part.split("=")[1])
            n = 100 if offset == 0 else 20
            return (200, {}, {"items": [{"id": f"s{offset + i}"} for i in range(n)]})
        return (200, {}, {})

    mock_api.app = app
    g = _glp(mock_api)
    assert len(g.list_all_subscriptions()) == 120


def test_classic_hashed_psk_replaced_with_placeholder(mock_api):
    from lib.central_client import PSK_PLACEHOLDER
    from lib.models import AuthType, ForwardMode, SSID
    captured = {}

    def app(method, path, query, body):
        if "full_wlan" in path:
            import json as _json
            captured.update(_json.loads(body["value"]) if isinstance(body, dict) else {})
            return (200, {}, {})
        return (200, {}, {})

    mock_api.app = app
    c = ClassicCentralClient(mock_api.url, "tok")
    hashed = "a" * 64          # 64-hex-char AOS 8 hash — unusable as a PSK
    ssid = SSID(name="corp", essid="Corp", vlan=10,
                forward_mode=ForwardMode.BRIDGE,
                auth_type=AuthType.WPA2_PSK, psk=hashed)
    c.create_wlan("g1", ssid, 0)
    assert captured["wlan"]["wpa_passphrase"] == PSK_PLACEHOLDER


def test_cleanup_records_listing_failures():
    from lib import cleanup as cl

    class FailingCentral:
        def _get(self, path, params=None):
            raise RuntimeError("403 forbidden")
        def list_device_groups(self, refresh=False):
            return []
        def list_sites(self, refresh=False):
            return []
        def _site_name(self, s): return ""
        def _site_id(self, s): return None

    res = cl.cleanup("zztest", central=FailingCentral())
    failed = [r for r in res if not r[1]]
    assert failed, "listing failures must be recorded as failed results"
    assert not any("No objects named" in r[0] and r[1] for r in res if not failed)


# ─────────────── New behaviour: failures must not look like empty results ───

def test_glp_401_reauth_does_not_send_stale_bearer(mock_api):
    """The token endpoint must never see the expired session Bearer."""
    state = {"n": 0}

    def app(method, path, query, body):
        if path.endswith("/as/token.oauth2"):
            return _token_response()
        state["n"] += 1
        if state["n"] == 1:
            return (401, {}, {})
        return (200, {}, {"items": [{"id": "d1", "serialNumber": "S1"}]})

    mock_api.app = app
    g = _glp(mock_api)
    g.token = "stale"
    g.session.headers.update({"Authorization": "Bearer stale"})
    assert g.list_devices() == [{"id": "d1", "serialNumber": "S1"}]
    token_reqs = [r for r in mock_api.requests
                  if r["path"].endswith("/as/token.oauth2")]
    assert token_reqs, "the 401 must have triggered a token request"
    assert all("Authorization" not in r["headers"] for r in token_reqs)


def test_central_list_sites_raises_when_all_routes_fail(mock_api):
    """A 403 is an answer about this tenant — caching [] makes create_site
    re-POST and the devices phase blame a step that already succeeded."""
    mock_api.app = lambda m, p, q, b: (
        _token_response() if p.endswith("oauth2") else (403, {}, {"error": "forbidden"}))
    c = _central(mock_api)
    with pytest.raises(CentralAPIError):
        c.list_sites(refresh=True)
    assert c._sites_cache is None


def test_central_paginate_raises_when_offset_ignored(mock_api):
    """A server that echoes page 1 forever used to silently return 100 rows."""
    page = [{"id": i} for i in range(100)]
    mock_api.app = lambda m, p, q, b: (
        _token_response() if p.endswith("oauth2") else (200, {}, {"items": page}))
    c = _central(mock_api)
    with pytest.raises(CentralAPIError, match="ignored offset"):
        c._paginate("/network-config/v1/sites", page_size=100)


def test_classic_is_duplicate_ignores_url_path():
    from lib.classic_central_client import _is_duplicate as classic_is_duplicate
    e = ClassicCentralAPIError(
        "POST /configuration/full_wlan/duplicate-lab/x failed 500: internal error")
    assert not classic_is_duplicate(e)
    e2 = ClassicCentralAPIError(
        "POST /configuration/full_wlan/corp/x failed 400: object already exists")
    assert classic_is_duplicate(e2)


def test_glp_assign_application_two_sequential_patches(mock_api):
    """GreenLake rejects a combined device+subscription patch, so this must be
    two merge-patches in order."""
    sub_id = "3f2e1d00-0000-4000-8000-000000000001"

    def app(method, path, query, body):
        if path.endswith("/as/token.oauth2"):
            return _token_response()
        if path == "/devices/v1/devices" and method == "GET":
            return (200, {}, {"items": [{"id": "dev-1", "serialNumber": "S1"}]})
        return (200, {}, {})

    mock_api.app = app
    g = _glp(mock_api)
    g.assign_application("S1", "app-1", "us-west", sub_id)
    patches = [r for r in mock_api.requests if r["method"] == "PATCH"]
    assert [p["body"] for p in patches] == [
        {"application": {"id": "app-1"}, "region": "us-west"},
        {"subscription": [{"id": sub_id}]},
    ]
    assert all(p["headers"].get("Content-Type") == "application/merge-patch+json"
               for p in patches)


def test_glp_partial_batch_failure_is_returned_not_raised(mock_api):
    """A SUCCEEDED batch can still reject devices — the caller needs the body
    to record the failed step."""
    def app(method, path, query, body):
        if path.endswith("/as/token.oauth2"):
            return _token_response()
        return (200, {}, {"status": "SUCCEEDED",
                          "result": {"successfulDevicesSerial": ["S1"],
                                     "failedDevicesSerial": ["S2"]}})

    mock_api.app = app
    g = _glp(mock_api)
    result = g.poll_task("op-1", timeout=5, interval=0)
    assert GLPClient.failed_serials(result) == ["S2"]


@pytest.mark.parametrize("spelling", ["TIMEDOUT", "TIMED_OUT", "TIMEOUT"])
def test_glp_timedout_enum_is_terminal(mock_api, spelling):
    """HPE spells this three ways; all must fail on the FIRST poll."""
    def app(method, path, query, body):
        if path.endswith("/as/token.oauth2"):
            return _token_response()
        return (200, {}, {"status": spelling,
                          "result": {"failedDevicesSerial": ["S1"]}})

    mock_api.app = app
    g = _glp(mock_api)
    with pytest.raises(GLPAPIError, match="GreenLake rejected these serials"):
        g.poll_task("op-1", timeout=300, interval=10)
    assert mock_api.sleeps == []


def test_glp_202_without_pollable_id_is_not_success(mock_api):
    def app(method, path, query, body):
        if path.endswith("/as/token.oauth2"):
            return _token_response()
        if path == "/devices/v1/devices" and method == "GET":
            return (200, {}, {"items": [{"id": "dev-1", "serialNumber": "S1"}]})
        if method == "PATCH":
            return (202, {}, {})          # no Location, no transactionId
        return (200, {}, {})

    mock_api.app = app
    g = _glp(mock_api)
    with pytest.raises(GLPAPIError, match="no pollable operation id"):
        g.assign_subscription("S1", "3f2e1d00-0000-4000-8000-000000000001")


def test_central_profile_route_falls_back_to_v1alpha1(mock_api):
    """v1 and v1alpha1 legitimately disagree per tenant — fall through on 404
    only, and reuse whichever version answered."""
    def app(method, path, query, body):
        if path.endswith("/as/token.oauth2"):
            return _token_response()
        if path.startswith("/network-config/v1/wlan-ssids"):
            return (404, {}, {"error": "not found"})
        return (200, {}, {})

    mock_api.app = app
    c = _central(mock_api)
    c._upsert_ssid("Corp", {"ssid": "Corp"})
    assert [p for _m, p in mock_api.calls] == [
        "/network-config/v1/wlan-ssids/Corp",
        "/network-config/v1alpha1/wlan-ssids/Corp",
    ]
    mock_api.calls.clear()
    c._upsert_ssid("Corp2", {"ssid": "Corp2"})
    assert [p for _m, p in mock_api.calls] == [
        "/network-config/v1alpha1/wlan-ssids/Corp2",
    ]


def test_classic_move_devices_batches_at_fifty(mock_api):
    """HPE returns 400 'More than 50 devices cannot be moved to a group'."""
    mock_api.app = lambda m, p, q, b: (200, {}, {})
    c = ClassicCentralClient(mock_api.url, "tok")
    c.move_devices("g1", [f"S{i}" for i in range(120)])
    moves = [r for r in mock_api.requests
             if r["path"] == "/configuration/v1/devices/move"]
    assert len(moves) == 3
    assert [len(r["body"]["serials"]) for r in moves] == [50, 50, 20]


def test_classic_create_group_raises_when_architecture_reads_back_wrong(mock_api):
    """Known API flaw: the create returns 200 without applying AOS10."""
    def app(method, path, query, body):
        if path == "/configuration/v2/groups":
            return (200, {}, {"data": [], "total": 0})
        if path == "/configuration/v3/groups":
            return (200, {}, {})
        if path == "/configuration/v1/groups/properties":
            return (200, {}, {"data": [{"group": "g1",
                                        "properties": {"Architecture": "AOS8"}}]})
        return (200, {}, {})

    mock_api.app = app
    c = ClassicCentralClient(mock_api.url, "tok")
    with pytest.raises(ClassicCentralAPIError, match="AOS8"):
        c.create_group("g1")


def test_classic_create_group_survives_readback_failure(mock_api):
    """The readback is best-effort — a 500 on it must not fail the create."""
    def app(method, path, query, body):
        if path == "/configuration/v2/groups":
            return (200, {}, {"data": [], "total": 0})
        if path == "/configuration/v1/groups/properties":
            return (500, {}, {"error": "boom"})
        return (200, {}, {})

    mock_api.app = app
    c = ClassicCentralClient(mock_api.url, "tok")
    assert c.create_group("g1") == "g1"


def test_owe_migrates_as_owe_and_blocks_on_classic():
    """OWE is encrypted — mapping it to OPEN publishes an unencrypted SSID."""
    from lib.aos8_client import _opmode_to_auth
    from lib.central_client import OPMODE
    from lib import compatibility
    from lib.models import (AuthType, CentralConfig, CustomerConfig,
                            ForwardMode, SSID)

    assert _opmode_to_auth("wpa3-owe") == (AuthType.OWE, True)
    assert _opmode_to_auth("enhanced-open") == (AuthType.OWE, True)
    assert OPMODE[AuthType.OWE] == "ENHANCED_OPEN"

    ssid = SSID(name="guest", essid="Guest", vlan=20,
                forward_mode=ForwardMode.BRIDGE, auth_type=AuthType.OWE)
    customer = CustomerConfig(mc_ip="10.0.0.1", mc_firmware="8.10.0.12",
                              controller_vlan=1, ssids=[ssid])

    def _dest(kind: str) -> CentralConfig:
        return CentralConfig(customer_name="acme", base_url="https://example",
                             destination=kind)

    fails = [r for r in compatibility.run_all(customer, _dest("classic"))
             if r.status == compatibility.Status.FAIL]
    assert any("Guest" in r.message and "Enhanced Open" in r.name for r in fails)
    # New Central has a real opmode for it, so it must NOT be blocked there
    new_fails = [r for r in compatibility.run_all(customer, _dest("new"))
                 if r.status == compatibility.Status.FAIL]
    assert not any("Enhanced Open" in r.name for r in new_fails)


def test_classic_captive_portal_ssid_is_refused(mock_api):
    """full_wlan cannot express an external portal — creating it anyway would
    publish a fully open guest network."""
    from lib.models import AuthType, ForwardMode, SSID

    mock_api.app = lambda m, p, q, b: (200, {}, {})
    c = ClassicCentralClient(mock_api.url, "tok")
    ssid = SSID(name="guest", essid="Guest", vlan=30,
                forward_mode=ForwardMode.BRIDGE, auth_type=AuthType.OPEN,
                captive_portal_url="https://portal.example.com/login")
    with pytest.raises(ClassicCentralAPIError, match="captive portal"):
        c.create_wlan("g1", ssid, 0)
    assert not any("full_wlan" in p for _m, p in mock_api.calls)


# ─────────────────── AOS 8 controller ───────────────────

def test_aos8_object_error_payload_raises(aos8_api):
    """AOS 8 answers a bad config_path / unknown object with HTTP 200 plus a
    _global_result error. Decoding that into [] is exactly what made a wrong
    node indistinguishable from "this controller has no WLANs"."""
    aos8_api.app = lambda m, p, q, b: (
        200, {}, {"_global_result": {"status": 1,
                                     "status_str": "config path invalid"}})
    c = _aos8(aos8_api)
    with pytest.raises(AOS8APIError, match="config path invalid"):
        c.get_ap_groups()


def test_aos8_html_response_gives_actionable_error(aos8_api):
    """Port 4343 answering with the WebUI must not surface as a JSON traceback."""
    aos8_api.app = lambda m, p, q, b: (
        200, {"Content-Type": "text/html"},
        "<html><body>Aruba login</body></html>")
    c = _aos8(aos8_api)
    with pytest.raises(AOS8APIError, match="REST API is probably disabled"):
        c.get_ap_groups()


def test_aos8_showcommand_config_path_is_caller_controlled(aos8_api):
    """The show fallback must be able to run WITHOUT the config_path that made
    the object reads fail — otherwise it re-runs at the same dead node."""
    aos8_api.app = lambda m, p, q, b: (200, {}, {"_data": ["ok"]})
    c = _aos8(aos8_api)
    c._show_text("show running-config")
    assert "config_path=%2Fmd" in aos8_api.requests[-1]["query"]
    c._show_text("show running-config", config_path=None)
    assert "config_path" not in aos8_api.requests[-1]["query"]


def test_aos8_list_config_nodes_records_why_it_failed(aos8_api):
    """An unreadable hierarchy must not read as "no config nodes exist"."""
    aos8_api.app = lambda m, p, q, b: (
        200, {}, {"_global_result": {"status": 1, "status_str": "no such object"}})
    c = _aos8(aos8_api)
    assert c.list_config_nodes() == []
    assert "no such object" in c.node_scan_error


# ─────────────────── Review findings (H1-L4, B1/B4) ───────────────────

def _full_wlan_body(api) -> dict:
    """The most recent full_wlan POST body, unwrapped from {"value": json}."""
    posts = [r for r in api.requests
             if r["method"] == "POST" and "/configuration/full_wlan/" in r["path"]]
    assert posts, "no full_wlan POST recorded"
    return json.loads(posts[-1]["body"]["value"])["wlan"]


def test_classic_wlan_enables_modern_phy(mock_api):
    """H1: a migrated WLAN must not come up with 802.11n/ac/ax disabled —
    that caps every client at legacy ~54 Mbps rates."""
    from lib.models import AuthType, ForwardMode, SSID
    mock_api.app = lambda m, p, q, b: (200, {}, {})
    c = ClassicCentralClient(mock_api.url, "tok")
    ssid = SSID(name="corp", essid="Corp", vlan=100,
                forward_mode=ForwardMode.BRIDGE, auth_type=AuthType.WPA2_PSK,
                psk="SecretPass123")
    c.create_wlan("g1", ssid, 1)
    wlan = _full_wlan_body(mock_api)
    assert wlan["high_throughput_disable"] is False
    assert wlan["very_high_throughput_disable"] is False
    assert wlan["high_efficiency_disable"] is False


def test_nc_mac_auth_ssid_body(mock_api):
    """H2 (New Central): a MAC-auth SSID must carry mac-authentication and its
    server group — opmode OPEN alone publishes an open network."""
    from lib.models import AuthType, ForwardMode, SSID
    c = _central(mock_api)
    ssid = SSID(name="iot", essid="IoT", vlan=40,
                forward_mode=ForwardMode.BRIDGE, auth_type=AuthType.MAC,
                auth_server_group="cppm-sg")
    body = c._ssid_body(ssid, "FORWARD_MODE_BRIDGE", server_group="cppm-sg")
    assert body["opmode"] == "OPEN"
    assert body["mac-authentication"] is True
    assert body["auth-server-group"] == "cppm-sg"


def test_classic_mac_auth_wlan(mock_api):
    """H2 (Classic): the never-open guard must actually fire for MAC auth."""
    from lib.models import AuthType, ForwardMode, SSID
    mock_api.app = lambda m, p, q, b: (200, {}, {})
    c = ClassicCentralClient(mock_api.url, "tok")
    ssid = SSID(name="iot", essid="IoT", vlan=40,
                forward_mode=ForwardMode.BRIDGE, auth_type=AuthType.MAC,
                auth_server_group="cppm-sg")
    c.create_wlan("g1", ssid, 1)
    wlan = _full_wlan_body(mock_api)
    assert wlan["opmode"] == "opensystem"
    assert wlan["mac_authentication"] is True
    assert wlan["access_type"] == "network_based"
    assert wlan["auth_server1"] == "cppm-sg"


def test_mac_auth_preflight_flagged():
    """H2 (preflight): MAC-auth SSIDs need an explicit check — FAIL when no
    server group was discovered (would be open), WARN otherwise."""
    from lib import compatibility
    from lib.models import (AuthType, CentralConfig, CustomerConfig,
                            ForwardMode, SSID)
    with_group = SSID(name="iot", essid="IoT", vlan=40,
                      forward_mode=ForwardMode.BRIDGE, auth_type=AuthType.MAC,
                      auth_server_group="cppm-sg")
    without = SSID(name="iot2", essid="IoT2", vlan=41,
                   forward_mode=ForwardMode.BRIDGE, auth_type=AuthType.MAC)
    customer = CustomerConfig(mc_ip="10.0.0.1", mc_firmware="8.10.0.12",
                              controller_vlan=1, ssids=[with_group, without])
    central = CentralConfig(customer_name="acme", base_url="https://x",
                            destination="new")
    results = compatibility.run_all(customer, central)
    fails = [r for r in results if r.status == compatibility.Status.FAIL]
    warns = [r for r in results if r.status == compatibility.Status.WARN]
    assert any("MAC" in r.name and "IoT2" in r.message for r in fails)
    assert any("MAC" in r.name and "IoT" in r.message for r in warns)


def test_wep_opmode_is_rejected_everywhere(mock_api):
    """M1: WEP must fail preflight and be refused by both clients — never
    silently become WPA2-Enterprise."""
    from lib.aos8_client import _opmode_to_auth
    from lib import compatibility
    from lib.models import (AuthType, CentralConfig, CustomerConfig,
                            ForwardMode, SSID)
    assert _opmode_to_auth("static-wep") == (AuthType.WEP, True)
    assert _opmode_to_auth("dynamic-wep") == (AuthType.WEP, True)

    ssid = SSID(name="legacy", essid="Legacy", vlan=10,
                forward_mode=ForwardMode.BRIDGE, auth_type=AuthType.WEP)
    customer = CustomerConfig(mc_ip="10.0.0.1", mc_firmware="8.10.0.12",
                              controller_vlan=1, ssids=[ssid])
    for dest in ("new", "classic"):
        central = CentralConfig(customer_name="acme",
                                base_url="https://example", destination=dest)
        fails = [r for r in compatibility.run_all(customer, central)
                 if r.status == compatibility.Status.FAIL]
        assert any("WEP" in r.name for r in fails), dest
    mock_api.app = lambda m, p, q, b: (200, {}, {})
    classic = ClassicCentralClient(mock_api.url, "tok")
    with pytest.raises(ClassicCentralAPIError, match="WEP"):
        classic.create_wlan("g1", ssid, 1)
    assert not any("full_wlan" in p for _m, p in mock_api.calls)
    nc = _central(mock_api)
    with pytest.raises(CentralAPIError, match="WEP"):
        nc._ssid_body(ssid, "FORWARD_MODE_BRIDGE")


def test_wpa3_sae_does_not_gain_wpa2_transition_mode(mock_api):
    """M2: transition mode is a WPA2-PSK compatibility feature; a WPA3-only
    network must not silently gain a WPA2 fallback."""
    from lib.models import AuthType, ForwardMode, SSID
    c = _central(mock_api)
    sae = SSID(name="sae", essid="SAE", vlan=10,
               forward_mode=ForwardMode.BRIDGE, auth_type=AuthType.WPA3_SAE,
               psk="SecretPass123")
    assert c._ssid_body(sae, "FORWARD_MODE_BRIDGE")[
        "wpa3-transition-mode-enable"] is False
    psk = SSID(name="psk", essid="PSK", vlan=10,
               forward_mode=ForwardMode.BRIDGE, auth_type=AuthType.WPA2_PSK,
               psk="SecretPass123")
    assert c._ssid_body(psk, "FORWARD_MODE_BRIDGE")[
        "wpa3-transition-mode-enable"] is True


def test_vlan_pool_tokens_are_flagged_for_mapping():
    """M3: '100,200' and '100-105' must surface in preflight like named VLANs —
    collapsing silently to the first id strands clients on the wrong VLAN."""
    from lib.aos8_client import _safe_vlan, _vlan_is_named, _vlan_is_pool
    assert _safe_vlan("100-105") == 100        # deterministic first id
    assert _vlan_is_pool("100-105")
    assert _vlan_is_pool("100,200")
    assert _vlan_is_pool("100, 200")
    assert not _vlan_is_pool("100")
    assert not _vlan_is_pool("guest2020")      # named, not a pool
    assert not _vlan_is_named("100-105")       # numeric, but ambiguous


def test_named_vlan_preflight_reports_actual_collapse_target():
    """M3: a pool collapses to its first id, not VLAN 1 — the preflight
    detail must name the real target."""
    from lib import compatibility
    from lib.models import AuthType, CustomerConfig, ForwardMode, SSID
    s = SSID(name="p", essid="P", vlan=100, vlan_raw="100-105",
             forward_mode=ForwardMode.BRIDGE, auth_type=AuthType.OPEN)
    customer = CustomerConfig(mc_ip="10.0.0.1", mc_firmware="8.10.0.12",
                              controller_vlan=1, ssids=[s])
    [check] = compatibility._check_named_vlans(customer)
    assert check.status == compatibility.Status.FAIL
    assert "100-105" in (check.detail or "")
    assert "VLAN 100" in (check.detail or "")


def test_enterprise_ssid_without_radius_servers_fails():
    """M4: dot1x SSIDs with zero discovered RADIUS servers would come up with
    nothing to authenticate against."""
    from lib import compatibility
    from lib.models import (AuthType, CentralConfig, CustomerConfig,
                            ForwardMode, SSID)
    ssid = SSID(name="corp", essid="Corp", vlan=100,
                forward_mode=ForwardMode.TUNNEL,
                auth_type=AuthType.WPA2_ENTERPRISE)
    customer = CustomerConfig(mc_ip="10.0.0.1", mc_firmware="8.10.0.12",
                              controller_vlan=1, ssids=[ssid],
                              radius_servers=[])
    central = CentralConfig(customer_name="acme", base_url="https://x",
                            destination="new")
    fails = [r for r in compatibility.run_all(customer, central)
             if r.status == compatibility.Status.FAIL]
    assert any("RADIUS" in r.name for r in fails)


def test_classic_enterprise_requires_manual_radius_setup():
    """B2: the Classic path cannot create RADIUS server objects — full_wlan
    references auth_server1 by name, so preflight must force the operator to
    create the server by hand before cutover. New Central has no such gate."""
    from lib import compatibility
    from lib.models import (AuthType, CentralConfig, CustomerConfig,
                            ForwardMode, RadiusServer, SSID)
    ssid = SSID(name="corp", essid="Corp", vlan=100,
                forward_mode=ForwardMode.TUNNEL,
                auth_type=AuthType.WPA2_ENTERPRISE, auth_server_group="cp-sg")
    customer = CustomerConfig(mc_ip="10.0.0.1", mc_firmware="8.10.0.12",
                              controller_vlan=1, ssids=[ssid],
                              radius_servers=[RadiusServer("cp-1", "10.0.0.50")])
    classic = CentralConfig(customer_name="acme", base_url="https://x",
                            destination="classic")
    fails = [r for r in compatibility.run_all(customer, classic)
             if r.status == compatibility.Status.FAIL]
    assert any("RADIUS" in r.name.upper() for r in fails)
    new = CentralConfig(customer_name="acme", base_url="https://x",
                        destination="new")
    new_fails = [r for r in compatibility.run_all(customer, new)
                 if r.status == compatibility.Status.FAIL]
    assert not any("RADIUS" in r.name.upper() for r in new_fails)


def test_classic_mac_auth_requires_manual_radius_setup():
    """R1 (Forge review of PR #7): the B2 manual-RADIUS gate sat under
    `elif enterprise` — a MAC-only SSID set on a Classic destination never
    hit it, yet the Classic client emits the same dangling auth_server1
    reference for MAC-auth WLANs. The gate must fire for MAC-only too."""
    from lib import compatibility
    from lib.models import (AuthType, CentralConfig, CustomerConfig,
                            ForwardMode, RadiusServer, SSID)
    ssid = SSID(name="legacy-iot", essid="Legacy-IoT", vlan=100,
                forward_mode=ForwardMode.TUNNEL,
                auth_type=AuthType.MAC, auth_server_group="mac-sg")
    customer = CustomerConfig(mc_ip="10.0.0.1", mc_firmware="8.10.0.12",
                              controller_vlan=1, ssids=[ssid],
                              radius_servers=[RadiusServer("cp-1", "10.0.0.50")])
    classic = CentralConfig(customer_name="acme", base_url="https://x",
                            destination="classic")
    fails = [r for r in compatibility.run_all(customer, classic)
             if r.status == compatibility.Status.FAIL]
    assert any("RADIUS" in r.name.upper() for r in fails)
    new = CentralConfig(customer_name="acme", base_url="https://x",
                        destination="new")
    new_fails = [r for r in compatibility.run_all(customer, new)
                 if r.status == compatibility.Status.FAIL]
    assert not any("RADIUS" in r.name.upper() for r in new_fails)


def test_persona_assignment_prefers_spec_path(mock_api):
    """B1: the spec only defines POST /persona-assignment/{device-function};
    the bare collection must be the fallback, not the first try."""
    def app(method, path, query, body):
        if path.endswith("/persona-assignment/CAMPUS_AP"):
            return (200, {}, {})
        if path.endswith("/persona-assignment"):
            return (404, {}, {"error": "not found"})
        return (200, {}, {})
    mock_api.app = app
    c = _central(mock_api)
    c.assign_persona(["CN1", "CN2"])
    paths = [p for _m, p in mock_api.calls]
    assert any(p.endswith("/persona-assignment/CAMPUS_AP") for p in paths)
    assert not any(p.endswith("/persona-assignment") for p in paths)


def test_persona_assignment_falls_back_to_bare_collection(mock_api):
    """B1: tenants 404ing the spec path still get the legacy bare form."""
    def app(method, path, query, body):
        if path.endswith("/persona-assignment/CAMPUS_AP"):
            return (404, {}, {"error": "not found"})
        if path.endswith("/persona-assignment"):
            return (200, {}, {})
        return (200, {}, {})
    mock_api.app = app
    c = _central(mock_api)
    c.assign_persona(["CN1"])
    assert any(p.endswith("/persona-assignment") for _m, p in mock_api.calls)


def test_classic_firmware_falls_back_to_set_compliance(mock_api):
    """B4: some Classic tenants only serve the MCP-verified
    /firmware/v1/set-firmware-compliance form."""
    def app(method, path, query, body):
        if "upgrade/compliance_version" in path:
            return (404, {}, {"error": "not found"})
        if path == "/firmware/v1/set-firmware-compliance":
            return (200, {}, {})
        return (200, {}, {})
    mock_api.app = app
    c = ClassicCentralClient(mock_api.url, "tok")
    c.set_firmware_compliance("g1", "10.7.0.0")
    reqs = [r for r in mock_api.requests
            if "set-firmware-compliance" in r["path"]]
    assert reqs
    assert reqs[-1]["body"]["group"] == "g1"
    assert reqs[-1]["body"]["firmware_version"] == "10.7.0.0"


def test_aos8_login_401_reports_auth_failure(aos8_api):
    """L3: a bad MC password must read as an auth failure, not a generic
    'verify port 4343' connection error."""
    aos8_api.app = lambda m, p, q, b: (401, {}, {"error": "unauthorized"})
    c = AOS8Client("127.0.0.1", "admin", "wrong", port=aos8_api.port)
    with pytest.raises(AOS8APIError, match="(?i)credential|auth|password"):
        c.connect()


def test_aos8_tls_verification_defaults_on(monkeypatch):
    """L4 flip: cert verification is ON by default; the CA-bundle env still
    selects a specific chain, and AOS8_DEV_MODE is the ONLY opt-out (test
    harness / local self-signed controllers)."""
    monkeypatch.delenv("AOS8_CA_BUNDLE", raising=False)
    monkeypatch.delenv("AOS8_DEV_MODE", raising=False)
    c = AOS8Client("10.0.0.1", "admin", "pw")
    assert c.session.verify is True


def test_aos8_ca_bundle_env_enables_verification(monkeypatch):
    """L4: an operator-deployed CA bundle is honored for MITM protection."""
    monkeypatch.setenv("AOS8_CA_BUNDLE", "/path/to/ca.pem")
    monkeypatch.delenv("AOS8_DEV_MODE", raising=False)
    c = AOS8Client("10.0.0.1", "admin", "pw")
    assert c.session.verify == "/path/to/ca.pem"


def test_aos8_dev_mode_opt_out_disables_verification(monkeypatch):
    """L4: dev/test mode is the only verify=False escape hatch."""
    monkeypatch.setenv("AOS8_DEV_MODE", "true")
    monkeypatch.delenv("AOS8_CA_BUNDLE", raising=False)
    c = AOS8Client("10.0.0.1", "admin", "pw")
    assert c.session.verify is False


def test_aos8_api_mac_auth_ssid_detected(aos8_api):
    """H2 (API path): opensystem + mac-server-group inside the bound
    aaa-profile is MAC auth, not an open network."""
    from lib.models import AuthType

    def app(method, path, query, body):
        if path.endswith("/object/ssid_prof"):
            return (200, {}, {"ssid_prof": [
                {"profile-name": "iot-ssid", "essid": "IoT",
                 "opmode": {"opensystem": True}}]})
        if path.endswith("/object/aaa_prof"):
            return (200, {}, {"aaa_prof": [
                {"profile-name": "iot-aaa",
                 "mac-server-group": {"profile-name": "cppm-sg"}}]})
        if path.endswith("/object/virtual_ap"):
            return (200, {}, {"virtual_ap": [
                {"profile-name": "iot-vap", "vlan": "40",
                 "aaa_prof": {"profile-name": "iot-aaa"},
                 "ssid_prof": {"profile-name": "iot-ssid"},
                 "forward-mode": "bridge"}]})
        return (200, {}, {})
    aos8_api.app = app
    c = _aos8(aos8_api)
    [iot] = c.get_ssids()
    assert iot.auth_type is AuthType.MAC
    assert iot.auth_known
    assert iot.auth_server_group == "cppm-sg"


def _opensystem_vap_app(aaa_status):
    """virtual-ap + ssid-profile reads succeed (opensystem); the aaa_prof
    read answers with `aaa_status` — the best-effort server-group resolution
    path in get_ssids."""
    def app(method, path, query, body):
        if path.endswith("/object/ssid_prof"):
            return (200, {}, {"ssid_prof": [
                {"profile-name": "guest-ssid", "essid": "Guest",
                 "opmode": {"opensystem": True}}]})
        if path.endswith("/object/aaa_prof"):
            return (aaa_status, {}, {"error": "internal"})
        if path.endswith("/object/virtual_ap"):
            return (200, {}, {"virtual_ap": [
                {"profile-name": "guest-vap", "vlan": "50",
                 "aaa_prof": {"profile-name": "guest-aaa"},
                 "ssid_prof": {"profile-name": "guest-ssid"},
                 "forward-mode": "bridge"}]})
        return (200, {}, {})
    return app


def test_aos8_api_opensystem_with_failed_aaa_read_is_unprovable(aos8_api):
    """#4: the aaa_prof read is best-effort — when it fails, an opensystem
    SSID's mac-server-group cannot be resolved, so MAC auth can neither be
    confirmed nor ruled out. The SSID must surface as UNKNOWN auth
    (auth_known=False), never as a provable OPEN network."""
    from lib.models import AuthType

    aos8_api.app = _opensystem_vap_app(aaa_status=500)
    c = _aos8(aos8_api)
    [guest] = c.get_ssids()
    assert guest.auth_type is AuthType.OPEN   # raw opmode is genuinely opensystem
    assert not guest.auth_known               # …but MAC-auth cannot be ruled out


def test_opensystem_unprovable_auth_fails_preflight_critical(aos8_api):
    """#4 contract: an opensystem SSID whose server-group resolution cannot
    be proven is a CRITICAL preflight blocker — FAIL, not WARN, and never
    handed to provisioning as OPEN."""
    from lib import compatibility
    from lib.models import AuthType, CentralConfig, CustomerConfig

    aos8_api.app = _opensystem_vap_app(aaa_status=500)
    ssids = _aos8(aos8_api).get_ssids()
    customer = CustomerConfig(mc_ip="10.0.0.1", mc_firmware="8.10.0.12",
                              controller_vlan=1, ssids=ssids)
    central = CentralConfig(customer_name="acme", base_url="https://x",
                            destination="new")
    results = compatibility.run_all(customer, central)
    fails = [r for r in results if r.status == compatibility.Status.FAIL]
    auth_fail = next((r for r in fails if "Guest" in r.message), None)
    assert auth_fail is not None, "unprovable opensystem auth must FAIL preflight"
    assert auth_fail.critical, "unknown-auth blocker must be non-overridable"
    # and it must NOT be reported as a benign 'provisioned as WPA2-Enterprise'
    # warning on top of the blocker
    warns = [r for r in results if r.status == compatibility.Status.WARN]
    assert not any("Guest" in r.message and "Auth Detection" in r.name
                   for r in warns)

    # sanity: the same SSID with a healthy aaa_prof read is provably OPEN —
    # no unknown-auth blocker
    aos8_api.app = _opensystem_vap_app(aaa_status=200)
    ssids_ok = _aos8(aos8_api).get_ssids()
    assert ssids_ok[0].auth_type is AuthType.OPEN and ssids_ok[0].auth_known
    customer_ok = CustomerConfig(mc_ip="10.0.0.1", mc_firmware="8.10.0.12",
                                 controller_vlan=1, ssids=ssids_ok)
    results_ok = compatibility.run_all(customer_ok, central)
    assert not any("Guest" in r.message
                   for r in results_ok
                   if r.status == compatibility.Status.FAIL)


def test_p4_group_names_are_html_escaped():
    """L1: device-group names come from the controller — raw interpolation
    into unsafe_allow_html is stored HTML injection."""
    from views.p4_greenlake import _esc_join
    assert _esc_join(["<script>x</script>", "b"]) == \
        "&lt;script&gt;x&lt;/script&gt;, b"
    assert _esc_join([]) == "—"


def test_validate_status_badge_treats_online_as_up():
    """L2: New Central reports 'ONLINE', Classic 'Up' — both are online."""
    from views.p6_validate import _status_is_up
    assert _status_is_up("Up")
    assert _status_is_up("ONLINE")
    assert _status_is_up("online")
    assert not _status_is_up("Down")


def test_aos8_api_hidden_ssid_does_not_default_to_broadcast(aos8_api):
    """#9 (API path): hide-ssid on the ssid profile is a hidden-but-active
    WLAN — parity with the paste path, which parses the same flag from the
    running-config. Defaulting broadcast=True would publish it."""
    def app(method, path, query, body):
        if path.endswith("/object/ssid_prof"):
            return (200, {}, {"ssid_prof": [
                {"profile-name": "corp-ssid", "essid": "Corp",
                 "opmode": {"wpa2-aes": True}, "hide-ssid": True}]})
        if path.endswith("/object/aaa_prof"):
            return (200, {}, {"aaa_prof": []})
        if path.endswith("/object/virtual_ap"):
            return (200, {}, {"virtual_ap": [
                {"profile-name": "corp-vap", "vlan": "100",
                 "ssid_prof": {"profile-name": "corp-ssid"},
                 "forward-mode": "tunnel"}]})
        return (200, {}, {})
    aos8_api.app = app
    c = _aos8(aos8_api)
    [corp] = c.get_ssids()
    assert corp.broadcast is False
    assert corp.enabled is True      # hidden ≠ administratively disabled
