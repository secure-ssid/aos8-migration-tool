# Source — Mobility Controller / Conductor

The `controller` [[Migration Paths|source path]]. APs terminate on MM/MD; conversion is a
CLI operation (`ap convert`) on the MC. Code: `lib/aos8_client.py` (REST),
`lib/aos8_parser.py` (CLI paste), `lib/runbook.py` (runbook). See [[Tool Internals]].

## Discovery via REST API

`AOS8Client` (`lib/aos8_client.py`) talks to the AOS 8 REST API on **port 4343**:

- **Login** — `POST https://<mc-ip>:4343/v1/api/login` (form-encoded
  username/password). The response carries a **[[Glossary|UIDARUBA]]** session token in
  `_global_result`. `status` comes back as `0`/`"0"` depending on build.
- **Reads** — every request sends `UIDARUBA` (query param + cookie) plus a
  `config_path`:
  - `GET /v1/configuration/object/<name>` — config objects (`ap_group`,
    `ssid_prof`, `virtual_ap` (legacy fallback `wlan_virtual_ap`), `vlan_id`, `rad_server`,
    `server_group_prof`).
  - `GET /v1/configuration/showcommand?command=...` — show commands.
- **`config_path`** — on a **Mobility Conductor** use `/md` (default); on a
  **standalone controller** use `/mm/mynode` (set under Advanced in Step 1).
- TLS is self-signed → `verify=False`.

### What's pulled (`pull_config`)
firmware (`show version`), controller IP + VLAN (`show controller-ip`), AP
groups + their **virtual-ap bindings**, [[Glossary|ssid-profile]] (essid/opmode/passphrase) joined
onto `virtual_ap`, VLANs, RADIUS servers, server-groups, AP inventory
(`show ap database long` → serial/model/MAC/group), and cluster membership.
APs whose group isn't in the configured list get a synthetic group so none are
dropped. If virtual-ap bindings are missing for a group, **all** SSIDs are
assigned to it and `ssid_mapping_incomplete` is set → [[Preflight Checks|SSID→AP-group WARN]].

## Discovery via CLI paste (fallback)

If 4343 is firewalled or the API is off, use **Paste CLI output** mode
(`lib/aos8_parser.py`). Recommended commands (Step 1 lists them):

```
show running-config            # SSIDs, VLANs, ap-group→virtual-ap bindings, RADIUS
show ap database long          # AP inventory: Name, Group, Serial #, Wired MAC
show version                   # exact firmware build (for the ap convert check)
show lc-cluster group-membership   # cluster members + L2/L3 (empty = single MC)
show controller-ip             # controller IP + VLAN (RADIUS NAD reference)
show aaa authentication-server all # RADIUS summary (optional if running-config pasted)
show ap active                 # fallback AP list — NO serial column
```

Tables are sliced at the dash-separator row so columns are exact. `running-config`
+ `ap database long` carry most of the data. Note `show ap active` has **no
serial** → those APs are flagged in [[Preflight Checks|serial coverage]] and can't be claimed in
[[GreenLake Onboarding]] or matched at validation.

### AOS 8 → broadcast SSID mapping
A `wlan virtual-ap` is the binding object (vlan, forward-mode, refs to
ssid-profile + aaa-profile). The **`wlan ssid-profile`** carries the real
broadcast `essid`, `opmode` (→ [[Glossary|auth type]]), and passphrase. `SSID.display_name`
is the essid, falling back to the profile name. This essid-vs-profile-name
distinction matters because [[Destination - New Central|Central keys WLANs by ESSID]] — see
[[Preflight Checks|duplicate ESSID]] checks.

## `ap convert` mechanics

**NOTE — auto-conversion:** assigning the APs to their AOS 10 device group
(Step 4 "Move APs into groups") makes Central push the conversion
automatically — the APs reboot into AOS 10 without you running `ap convert`.
The commands below are the controller-driven path for APs that don't
auto-convert (e.g. unreachable from Central) — do one or the other, inside a
maintenance window (the generated runbook carries the same note).

Syntax (AOS-W 8.x CLI ref; see `lib/runbook.py`):

```
ap convert add {ap-group <grp> | ap-name <name>}
ap convert pre-validate {all-aps | specific-aps}   # 8.10+
ap convert cancel
ap convert active {all-aps | specific-aps} {activate | local-flash | server ...}
```

Runbook flow per group:
1. `ap convert add ap-group <grp>` for each discovered group (canary tip:
   `ap convert clear-all` then `ap convert add ap-name <test-ap>` first).
2. `ap convert pre-validate specific-aps` → `show ap convert-status` until
   **"Pre Validate Success"** → `ap convert cancel`.
3. `ap convert active specific-aps activate` — **primary path**: APs fetch
   their own AOS 10 image from **Aruba Activate** (no image names needed).
4. Alternative (air-gapped Activate): `ap convert active ... server http ...`
   with per-model image families (`MODEL_FAMILIES`, from the Instant
   release-notes image classes: 303→Scorpio, 318/37x→Gemini,
   344/345 + 50x/51x/518/57x→Draco, 53x/55x/58x→Lupus, 635/655→Norma;
   unknown models get an explicit "do NOT
   guess" placeholder).
5. `show ap convert-status` — **10–20 min per AP**.

Prereqs the runbook enforces: firmware on a [[Preflight Checks|supported train]], DHCP+DNS+Internet
reachability for APs, [[RADIUS and NAD Changes|NAD updates]] done, and Steps 1–4 complete (Central
provisioned + APs claimed/subscribed in [[GreenLake Onboarding]]). Convert APs only
**after** [[Destination - New Central|provisioning]] — converted APs pull config from Central; if it
isn't there they come up with nothing to broadcast.

## Cluster sequencing (L2 vs L3)

Detected from `show lc-cluster group-membership` (≥2 members =
[[Glossary|cluster]]). See [[Preflight Checks|cluster check]] and the runbook generator.

- **L2 cluster** — members share client state; converting both at once strands
  APs. Sequence: (1) `apmove all target-v4 <mc1>` move all APs to MC1; (2)
  convert MC2 to gateway (or, if [[Gateway Strategy|retiring]], leave MC2 idle as the rollback
  target); (3) run `ap convert` from MC1; (4) convert/decommission MC1 after
  APs are online.
- **L3 cluster** — members are independent; migrate one at a time: move its APs
  to a peer, `ap convert` from the peer, then convert/decommission the emptied
  MC. Repeat per member.

## Gateway migration (keep path)

When [[Gateway Strategy|keeping gateways]], the MC hardware becomes the AOS 10 gateway. Two
paths (Step 5 UI):
- **ZTP (preferred)** — remove MC from its Activate folder; if previously
  upgraded, load AOS 10 image then `write erase` → `reload`; plug a GW port
  (NOT GE 0/0/1) into a DHCP+Internet port; GW contacts Activate, upgrades,
  registers; assign to the cluster in Central → Devices → Gateways.
- **Static Activate** — `static-activate` on the GW console, enter IP/mask/gw.

## Rollback

Per AP, on the **AP console**:

```
convert-aos-ap cap <mc-ip>
```

The AP downloads the AOS 8 image from the MC and reboots. This needs a **live
AOS 8 MC** — which is exactly why the L2 sequence keeps MC2 online as the
rollback target even when retiring gateways. See [[Gateway Strategy|rollback target]].

## Related
[[Migration Paths]] · [[Preflight Checks]] · [[RADIUS and NAD Changes]] ·
[[Destination - New Central]] · [[Destination - Classic Central]] · [[Glossary]]
