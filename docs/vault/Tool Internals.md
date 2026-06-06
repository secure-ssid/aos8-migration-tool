# Tool Internals — Code Map

Concept → file. Streamlit wizard; logic in `lib/`, UI in `views/`. Paths
relative to repo root (`aos8-migration-tool/`).

## Entry + UI

- **`app.py`** — page config, 6-step router (`STEPS`), `reset_downstream_state`
  (clears derived state when a new discovery happens), brand header + progress +
  sidebar.
- **`lib/styles.py`** — Streamlit CSS/HTML helpers (badges, chips, cards). No
  domain logic.
- **`views/p1_connect.py`** — [[Migration Paths|source/destination choice]], discovery (API/paste),
  [[Gateway Strategy]] radio, calls `translate()` → `CentralConfig`. Lists the
  paste commands for [[Source - Mobility Controller|MC]] and [[Source - Instant IAP|Instant]]. Redacts PSKs/secrets in the JSON export.
- **`views/p2_preflight.py`** — runs [[Preflight Checks]] (`compatibility.run_all`),
  blocker override gate.
- **`views/p3_provision.py`** — calls `CentralClient.provision` ([[Destination - New Central|New]]) or
  `ClassicCentralClient.provision` ([[Destination - Classic Central|Classic]]); per-step result log; rotates
  the classic [[Glossary|refresh token]] in session.
- **`views/p4_greenlake.py`** — [[GreenLake Onboarding]] (claim + subscribe + workspace
  reconcile).
- **`views/p5_runbook.py`** — renders `runbook.generate(...)`; GW migration
  tabs (ZTP / Static Activate).
- **`views/p6_validate.py`** — matches converted APs by serial via
  `list_all_aps`; post-migration checklist.

## Domain logic (`lib/`)

- **`lib/models.py`** — dataclasses: `SSID` (with `display_name`=[[Glossary|essid]]),
  `APGroup`, `AP`, `VLAN`, `RadiusServer`, `ClusterInfo`, **`CustomerConfig`**
  (`source_type`), **`CentralConfig`** (`destination`, `gateways_retired`,
  `gw_cluster_name`), `CentralGroupConfig`. `ForwardMode`, `AuthType` enums.
- **`lib/aos8_client.py`** — `AOS8Client` ([[Source - Mobility Controller|REST/UIDARUBA discovery]]);
  `INCOMPATIBLE_MODELS` + `is_model_compatible`; opmode/vlan/model helpers
  shared with the parser.
- **`lib/aos8_parser.py`** — CLI-paste fallback: `parse_customer_config` ([[Source - Mobility Controller|MC]])
  and `parse_instant_config` ([[Source - Instant IAP|Instant]] — zones→groups, ssid-profiles ARE WLANs).
  Dash-anchored table parser.
- **`lib/translator.py`** — `translate()` maps `CustomerConfig` →
  `CentralConfig` per [[Gateway Strategy|gateway_mode]] (retire rewrites SSIDs to bridge; keep
  sets `gw_cluster_name`).
- **`lib/compatibility.py`** — [[Preflight Checks]] (`run_all` + per-check
  functions); `SUPPORTED_TRAINS`, `_fw_ok`.
- **`lib/central_client.py`** — `CentralClient` ([[Destination - New Central]]): scopes,
  scope-maps, device groups, overlay/underlay SSIDs, gateway cluster, auth
  servers, firmware compliance, `provision()`.
- **`lib/classic_central_client.py`** — `ClassicCentralClient`
  ([[Destination - Classic Central]]): v3 AOS10 groups + readback, `full_wlan` value-wrapper,
  rotating refresh, manual follow-ups.
- **`lib/glp_client.py`** — `GLPClient` ([[GreenLake Onboarding]]): claim (async poll),
  workspace reconcile, subscription resolve/assign.
- **`lib/runbook.py`** — `generate()`: [[Source - Mobility Controller|ap convert]] runbook (single MC / L2 / L3
  sequencing, rollback) and `_generate_instant` ([[Source - Instant IAP|Central-driven]]). `MODEL_FAMILIES`
  for the manual image-server path.

## State flow

`customer_config` (discovery) → `central_config` (translate) → `preflight_
results` → `provision_results` → `glp_*` → `validation_results`. A new
discovery clears everything downstream (`reset_downstream_state` in `app.py`).
Credentials live in session only; the discovery JSON export redacts PSKs and
RADIUS secrets.

## Related
[[Home]] · [[Glossary]] · [[Migration Paths]]
