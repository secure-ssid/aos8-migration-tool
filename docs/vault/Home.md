# Home — AOS 8 → Central Migration Console

Field reference for the wizard that migrates customers off **AOS 8** (Mobility
Controller/Conductor or Instant) onto **AOS 10** in [[Destination - New Central|New Central]] or
[[Destination - Classic Central|Classic Central]]. App is a Streamlit wizard (`app.py`); domain logic lives in
`lib/`. See [[Tool Internals]] for the code map.

## The wizard (6 steps)

| Step | Note |
|---|---|
| 1. Connect & Discover | [[Source - Mobility Controller]] · [[Source - Instant IAP]] |
| 2. Preflight | [[Preflight Checks]] |
| 3. Provision | [[Destination - New Central]] · [[Destination - Classic Central]] |
| 4. GreenLake | [[GreenLake Onboarding]] |
| 5. Runbook | [[Source - Mobility Controller]] (`ap convert`) / [[Source - Instant IAP]] (Central-driven) |
| 6. Validate | matches converted APs by serial in Central |

## Decide first

- [[Migration Paths]] — the source × destination matrix and which one to pick.
- [[Gateway Strategy]] — keep the MCs as AOS 10 gateways, or retire them.

## Sources (AOS 8)

- [[Source - Mobility Controller]] — REST/CLI discovery, `ap convert`, cluster sequencing, rollback.
- [[Source - Instant IAP]] — VC paste discovery, Central-driven image push.

## Destinations (AOS 10 / Central)

- [[Destination - New Central]] — GreenLake, scopes + scope-maps, overlay SSID sequence.
- [[Destination - Classic Central]] — apigw, v3 AOS10 groups, `full_wlan`.
- [[GreenLake Onboarding]] — claim + subscribe (required before adoption).

## Cross-cutting

- [[Preflight Checks]] — every compatibility/safety gate.
- [[RADIUS and NAD Changes]] — who the NAD becomes per path.
- [[Glossary]] — UIDARUBA, scope-map, overlay/underlay, persona, swarm, etc.
- [[Tool Internals]] — file-by-file code map.

## One-line mental model

Discover AOS 8 → translate to a Central model → preflight → provision Central →
onboard to [[GreenLake Onboarding|GreenLake]] → convert APs ([[Source - Mobility Controller|ap convert]] or
[[Source - Instant IAP|Central push]]) → validate.
