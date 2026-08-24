"""Migration manifest — the ownership record for everything provisioning
creates in the destination tenant (review finding #3).

Before this module, idempotency was name-based only: existing sites/groups
were silently reused, same-name SSIDs were patched in place, duplicate WLAN
creations were swallowed without reconciliation, and cleanup deleted by name
prefix. Any same-named object created by another administrator could be
modified, adopted, or deleted without the tool proving it owned the object.

The manifest changes the default to FAIL-CLOSED:

  - Every object provisioning creates is registered here with its tenant
    resource id and a hash of the payload it was created with.
  - gate() REFUSES to reuse/patch a same-named tenant object that has no
    manifest entry (CollisionError) — the operator must explicitly adopt it.
  - adopt() records that explicit, per-resource decision (who + when).
    Adopted objects may be REUSED by provisioning but are NEVER deleted by
    cleanup — they belong to someone else.
  - cleanup(manifest=...) deletes manifest-OWNED resources only.

Persistence: plaintext JSON (no secrets — names, ids, hashes), written
atomically with owner-only permissions under ~/.aos8-migration/manifests/.
"""
import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

MANIFEST_VERSION = 1

MANIFEST_ROOT = Path.home() / ".aos8-migration" / "manifests"

# Resource kinds recorded in the manifest. Cleanup maps the same kinds back
# to tenant objects, so both sides must use these exact strings.
KIND_SITE = "site"
KIND_GROUP = "group"                    # classic device group
KIND_DEVICE_GROUP = "device-group"      # New Central device group
KIND_SSID = "ssid"
KIND_VLAN = "vlan"
KIND_AUTH_SERVER = "auth-server"
KIND_SERVER_GROUP = "server-group"
KIND_CAPTIVE_PORTAL = "captive-portal"
KIND_POLICY = "policy"
KIND_ROLE = "role"
KIND_GATEWAY_CLUSTER = "gateway-cluster"


class CollisionError(Exception):
    """A same-named object already exists in the tenant and the manifest has
    no entry for it — the tool refuses to modify or silently adopt it."""

    def __init__(self, message: str, kind: str = "", name: str = ""):
        super().__init__(message)
        self.kind = kind
        self.name = name


_COLLISION_RE = re.compile(
    r"^(\S+) '(.+)' already exists in the tenant but is not in the "
    r"migration manifest")


def parse_collision(message: str) -> Optional[tuple[str, str]]:
    """Recover (kind, name) from a CollisionError message — the Step 3 UI
    uses this to offer per-resource adoption for exactly the objects the
    gate refused. None when the message isn't a collision refusal."""
    m = _COLLISION_RE.match(message or "")
    return (m.group(1), m.group(2)) if m else None


def payload_hash(payload) -> str:
    """Stable content hash of the payload an object was created with, so a
    later run can prove the tenant object still matches what we built."""
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


@dataclass
class ManifestEntry:
    kind: str
    name: str
    resource_id: str = ""      # tenant-side id (scope id, site id) when known
    hash: str = ""             # payload_hash of the creation payload
    adopted: bool = False      # True = pre-existing, explicitly adopted
    created_by: str = ""       # operator identity (audit)
    created_at: str = ""       # ISO-8601 UTC


class Manifest:
    """Ownership registry for one migration (one tenant + customer).

    `path` may be None for a purely in-memory manifest (tests). All mutating
    methods persist immediately — a crash mid-provision must not lose the
    record of what the tenant now contains."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self._entries: dict[tuple[str, str], ManifestEntry] = {}
        if self.path and self.path.is_file():
            self._load()

    # ─────────────────── persistence ───────────────────

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (ValueError, OSError) as e:
            # a manifest we can't read must not read as "we own nothing" —
            # that would re-open every collision gate. Fail loudly instead.
            raise CollisionError(
                f"Migration manifest {self.path} is unreadable ({e}) — "
                "refusing to provision without a trustworthy ownership record. "
                "Fix or remove the file (removing it means re-adopting every "
                "pre-existing object explicitly).")
        for raw in data.get("entries", []):
            try:
                e = ManifestEntry(**{k: v for k, v in raw.items()
                                     if k in ManifestEntry.__dataclass_fields__})
            except TypeError:
                continue  # unknown future schema version — skip, don't crash
            self._entries[(e.kind, e.name)] = e

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        blob = json.dumps({
            "version": MANIFEST_VERSION,
            "entries": [asdict(e) for e in self._entries.values()],
        }, indent=2).encode()
        tmp = self.path.with_suffix(".json.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, self.path)
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    # ─────────────────── queries ───────────────────

    def lookup(self, kind: str, name: str) -> Optional[ManifestEntry]:
        return self._entries.get((kind, name))

    def entries(self) -> list[ManifestEntry]:
        return list(self._entries.values())

    def may_delete(self, kind: str, name: str) -> bool:
        """True only for objects this migration CREATED. Adopted objects were
        someone else's before we arrived and are never cleanup's business."""
        e = self.lookup(kind, name)
        return bool(e) and not e.adopted

    # ─────────────────── mutation ───────────────────

    def register(self, kind: str, name: str, resource_id: str = "",
                 payload=None, adopted: bool = False,
                 user: str = "") -> ManifestEntry:
        e = ManifestEntry(
            kind=kind, name=name, resource_id=str(resource_id or ""),
            hash=payload_hash(payload) if payload is not None else "",
            adopted=adopted, created_by=user or "",
            created_at=datetime.now(timezone.utc).isoformat())
        self._entries[(kind, name)] = e
        self.save()
        return e

    def adopt(self, kind: str, name: str, resource_id: str = "",
              user: str = "") -> ManifestEntry:
        """Explicitly take responsibility for a pre-existing tenant object.
        The adoption is recorded with the operator's identity — this is the
        deliberate, auditable act the collision gate demands."""
        return self.register(kind, name, resource_id=resource_id,
                             adopted=True, user=user)

    def remove(self, kind: str, name: str) -> bool:
        """Drop an entry (after a successful delete). False when absent."""
        if self._entries.pop((kind, name), None) is None:
            return False
        self.save()
        return True

    # ─────────────────── the gate ───────────────────

    def gate(self, kind: str, name: str, exists: bool) -> str:
        """Decide how provisioning may treat `name`: 'create' (absent from the
        tenant) or 'reuse' (present AND already manifest-owned/adopted).

        A same-named tenant object with NO manifest entry raises
        CollisionError — reusing or patching it would modify an object another
        administrator may own."""
        if not exists:
            return "create"
        if (kind, name) in self._entries:
            return "reuse"
        raise CollisionError(
            f"{kind} '{name}' already exists in the tenant but is not in the "
            "migration manifest — refusing to modify or silently adopt an "
            "object this tool did not create. Adopt it explicitly (Step 3 "
            "lists colliding objects with an adopt option), rename it, or "
            "delete it in Central.", kind=kind, name=name)


def manifest_path(customer_name: str, tenant_fingerprint: str) -> Path:
    """One manifest per customer+tenant — a different tenant must never
    inherit ownership claims from another tenant's run."""
    slug = hashlib.sha1(
        f"{tenant_fingerprint}|{customer_name}".encode()).hexdigest()[:16]
    return MANIFEST_ROOT / f"{slug}.json"
