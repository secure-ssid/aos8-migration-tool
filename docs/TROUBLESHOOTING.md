# Troubleshooting

Common failures by symptom, with the cause and the fix. Most errors are surfaced
verbatim in the step's result log — match the message text below.

## Quick reference

| Symptom | Likely cause | Fix |
|---|---|---|
| `403` on a WLAN/`full_wlan` call (classic) | WLAN config APIs not allowlisted for the tenant | Ask your Aruba SE to enable the classic WLAN config APIs for the account. |
| `401`/access check fails (classic) | Access token expired (~2h) or refresh token spent | Generate a fresh token in API Gateway; re-paste. Provide a (current) refresh token + client id/secret for auto-refresh. |
| AOS 8 login fails / no UIDARUBA | Wrong port, or REST API disabled | Confirm 4343 reachable and the REST API enabled, or use paste mode. (Wrong `config_path` shows up as read errors after login — see the AOS 8 login failures section.) |
| GreenLake (GLP) claim fails: "macAddress is required" | AP discovered without a wired MAC | Re-discover with `show ap database long`; or add the device manually in GreenLake. |
| Blank / stale page in the browser | Old Streamlit server process still bound | Stop and restart `streamlit run app.py`. |
| Preflight blocker won't let you continue | A FAIL check (model/firmware/named VLAN/etc.) | Fix the root cause, or tick the override checkbox (acknowledge the risk). |
| Group created but "Architecture reads back as X, not AOS10" (classic) | Known v3 group-create flaw | Delete the group in Central; confirm the tenant supports AOS10 groups; re-run. |
| Provisioning step failed but others continued | By design — failures are recorded, not fatal | Read the per-step error, fix it, use "Reset & re-run provisioning". |

---

## 403 on WLAN APIs (classic) — allowlist

**Message:**
```
POST /configuration/full_wlan/<group>/<name> → 403: the classic WLAN config APIs
are allowlisted per tenant — ask your Aruba SE to enable them for this account.
```

**Cause:** the classic Central WLAN configuration APIs are gated per tenant. The
client detects a `403` on a `full_wlan` path and raises this specific
message.

**Fix:** open a case with your Aruba SE / TAC to enable the WLAN config APIs for
the tenant. Everything else (groups, sites, device moves, firmware compliance)
provisions normally; only the WLAN-create steps fail. Once enabled, use "Reset &
re-run provisioning" — completed objects are reused.

---

## 401 on classic Central — token expiry and refresh rotation

**Symptoms:**
- Step 3 "Classic Central access check failed" before provisioning.
- A step fails with `... failed 401: ...`.

**Cause:** the API Gateway access token is valid for only **~2 hours**. After
that the bearer is rejected.

**Auto-refresh:** if you supplied a refresh token plus client id/secret, the
client refreshes automatically on a 401 and retries once
(`POST /oauth2/token` with the params in the query string). **The refresh token
is single-use and rotates** — each refresh returns a new one. The tool captures
the new token into the session and shows a banner:

> "The refresh token rotated during this run — the new one is saved in this
> session. Update wherever you store it."

**Fixes:**
- No refresh configured → generate a fresh access token (API Gateway → System
  Apps & Tokens → Generate Token) and re-paste it in Step 1.
- Refresh keeps failing → the stored refresh token was already spent (used
  elsewhere). Generate a fresh token pair and re-enter.
- After a long pause mid-engagement → just re-run; the access check will
  refresh or tell you to regenerate.

---

## AOS 8 login failures

**Symptoms:**
- `AOS 8 API error: Login failed: ...`
- `Login succeeded but no UIDARUBA token returned`
- `Connection error: ...` / timeouts

**Causes and fixes:**

| Cause | Fix |
|---|---|
| Wrong `config_path` | Conductor (MM) uses `/md` (default); standalone controller uses `/mm/mynode`. Set it under Step 1 → Advanced — API options. |
| Port 4343 firewalled or REST API disabled | The REST API is on TCP **4343** with a self-signed cert. Confirm reachability from the machine running the tool; otherwise switch to **Paste CLI output** mode. |
| Bad credentials / non-zero login status | The controller (MC — Mobility Conductor/Controller) returns `status != 0`; check username/password. The status can be int `0` or string `"0"` depending on build (the client handles both). |
| Object/show read errors after login | Usually a `config_path` mismatch on a Conductor. Try the node path, or fall back to paste mode. |

In paste mode, always include `show running-config` and `show ap database long`
(serial + wired MAC). Add `show version` so the firmware train check can run, and
`show lc-cluster group-membership` for clusters.

---

## GreenLake claim failures

**Message:**
```
macAddress is required to claim <serial> — re-discover with
`show ap database long` (Wired MAC column)
```

**Cause:** GLP (the HPE GreenLake Platform, where device inventory lives)
requires the wired MAC to claim a network device. The AP was
discovered without one (e.g. via `show ap active`, which has no MAC/serial
columns, or a fixed-width table that overflowed).

**Fixes:**
- Re-discover with `show ap database long` (or API mode), which carries the
  Wired MAC column. Step 4 buckets APs into Claimable / Missing MAC / Missing
  serial so you can see exactly which APs are affected.
- Or add the missing devices manually in the GreenLake UI / via CSV, then click
  "Check workspace" again — already-present APs show as IN WORKSPACE.

**Other claim issues:**

| Symptom | Cause | Fix |
|---|---|---|
| Claim "succeeds" but serials show as NOT in workspace | Async op reported done, but reconciliation against real inventory didn't find them | The flagged serials must be resolved (wrong serial/MAC, or claim rejected) before those APs are converted. |
| Claim times out | Async operation exceeded 5 min | Re-run "Check workspace" to see what landed; re-claim the remainder. |
| "No active subscriptions found" | Workspace has no active (non-ENDED) subscriptions | Add subscription keys in GreenLake first. AP subscriptions are listed first when present. |

---

## Blank pages — stale server process

**Symptom:** a step renders blank, half-rendered, or shows literal `</div>`
fragments; or the page won't update after editing the code.

**Cause:** a previous `streamlit run` process is still bound to port 8501 and the
browser is attached to a stale server, or a hot-reload left the UI in a bad state.

**Fix:** stop the running Streamlit process and restart it:

```bash
# stop the old process (Ctrl-C in its terminal), or:
pkill -f "streamlit run app.py"
streamlit run app.py
```

Then hard-refresh the browser tab. The app keeps all state in the session, so a
restart clears it — you'll re-enter credentials and re-run discovery (which is
the intended reset between engagements anyway).

> Note: literal `</div>` text is specifically guarded against in `lib/styles.py`
> (HTML is emitted as a single unindented line; newlines in detail panes are
> encoded). If you see it, it's almost always a stale server, not a markup bug.

---

## Preflight blockers explained

Blockers (FAIL) stop you advancing to provisioning unless you tick the override
checkbox ("Override blockers — I understand the risk and will resolve them
before cutover"). Resolve
them properly when you can — they represent things that will break the migration.

| Blocker | What to do |
|---|---|
| AP Model Compatibility | One or more AP models don't support AOS 10. Replace the hardware before migrating those APs. |
| MC Firmware Version | The MC isn't on a supported `ap convert` train. Upgrade to ≥ 8.10.0.12 (8.10) or ≥ 8.12.0.1 (8.12). 8.11 does **not** qualify. After upgrading an MC that had prior upgrades, `write erase` + reload. |
| AP DHCP Requirement (when static IPs found) | AOS 10 requires DHCP for all APs. Re-provision static-IP APs for DHCP first. |
| EAP-Offload / FastConnect | Not supported in AOS 10. Remove AAA FastConnect; use standard 802.1X. |
| Internal Authentication Server | Not supported in AOS 10. Migrate to external RADIUS (ClearPass/NPS). |
| Named VLANs Unresolved | An SSID references a named VLAN pool that didn't resolve to an ID; it would provision onto VLAN 1. Look up the real VLAN id on the MC and fix it before provisioning. |
| Conflicting Duplicate ESSIDs | The same ESSID has different vlan/forward-mode/auth across virtual-aps. Central keys WLANs by ESSID; only the first would provision. Rename or consolidate. |
| ESSID Length | ESSID > 32 characters — Central rejects it. Shorten it. |

Warnings (WARN) don't block but should each be read before cutover — RADIUS NAD
changes, switchport changes for retired gateways, split-tunnel behaviour changes,
missing serials/MACs, incomplete SSID mapping, and PSK/802.1X follow-ups are the
ones that most often cause day-2 surprises.

---

## Provisioning: a step failed

Provisioning is intentionally non-fatal per step. A failed step is recorded with
its error and the run continues, so you get the full picture. After the run:

1. Read each failed step's error (shown as a code block).
2. Fix the cause (allowlist, token, permissions, naming collision, etc.).
3. Click **Reset & re-run provisioning**. Idempotent steps reuse anything that
   already succeeded (sites/groups/VLANs/SSIDs/clusters matched by name).

The one hard stop in New Central is "Resolve global scope" — if that fails,
nothing else can run. Check the API client has Aruba Central (network-config)
access in GreenLake and that the regional base URL is correct.
