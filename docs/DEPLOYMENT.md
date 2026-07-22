# Deployment Guide

How to run the console for yourself, for a team, and safely on a network.
If you just want to try the tool, you don't need any of this — see
[Getting Started](GETTING-STARTED.md).

## Single user (laptop / one engagement)

```bash
# Docker
docker build -t aos8-migration .
docker run -p 8501:8501 aos8-migration

# Or just run locally:
streamlit run app.py
```

In this default (`AOS8_AUTH_MODE=local`) mode there is no app login. Live
credentials stay in the Streamlit session only. The optional **Remember**
toggle persists *destination* API creds (client id/secret + Classic refresh
token, never source-side secrets) to `~/.aos8-migration/<user>/credentials.json`,
**encrypted at rest** with a private auto-generated key. Uncheck to delete.

## Multi-user (Docker farm, concurrent engineers)

Two built-in login options — no OAuth, no IdP:

### Simplest — one shared password (`AOS8_AUTH_MODE=password`, the default in compose)

Set `AOS8_APP_PASSWORD` and everyone uses that one password to get in. No
registration, no email. There's no per-person identity, so saved creds are a
single shared store and audit lines are attributed to a generic `team`.
Repeated wrong guesses are rate-limited process-wide.

```bash
cp .env.example .env        # set AOS8_APP_PASSWORD
docker compose up --build
```

### Per-person — self-service login (`AOS8_AUTH_MODE=accounts`)

Users register with a verified email; a 6-digit code is emailed to confirm the
address, then they set a password. The signed-in email scopes the per-user
encrypted credential store and the audit log.

- **Verified registration.** Open to any valid email by default. Set
  `AOS8_ALLOWED_EMAIL_DOMAIN=example.com` to restrict to one domain. The
  emailed code proves ownership so someone can't register a colleague's
  address. Passwords are stored scrypt-hashed with a per-user salt; codes are
  short-lived, hashed, and resend-throttled; repeated failed logins lock the
  account for a few minutes.
- **Verification email.** The From can be any account that can SMTP-send:
  - **Gmail (easiest + reliable):** `AOS8_SMTP_MODE=relay`,
    `AOS8_SMTP_HOST=smtp.gmail.com`, port `587`, user/from = your Gmail
    address, pass = a Google **App Password** (Security → App passwords).
  - **Transactional provider** (SendGrid/Resend/Brevo free tier) — same
    shape, a verified sender domain.
  - **`AOS8_SMTP_MODE=direct`** — no account at all; the app does the MX
    lookup and delivers itself. May be spam-filtered from an unauthenticated
    IP; set `AOS8_SMTP_FROM` to a domain you control.
  - With nothing set, codes are written to the **container log only** (dev).
- **Per-user credential isolation.** Saved creds are keyed and encrypted per
  signed-in user; one engineer's tenant secrets never load into another's
  session. With no `AOS8_CREDSTORE_KEY`, persistence is disabled entirely
  (session-only) — a fail-safe. Signing out clears the entire session.

## HTTPS via Caddy (recommended for any multi-user mode)

Passwords/codes traverse the connection — never serve plain `:8501` to
users. The shipped compose file publishes `8501` on **all interfaces** so a
Caddy on a separate host can front it; `deploy/Caddyfile` is a ready example
(Caddy handles the websockets Streamlit needs automatically).

- If Caddy runs on the **same** host, change the compose binding to
  `127.0.0.1:8501:8501`.
- Either way, firewall plain `:8501` from users.

## Reverse-proxy header mode (`AOS8_AUTH_MODE=proxy`)

An alternative for shops that already have an authenticating proxy
(oauth2-proxy etc.): identity comes from one trusted header
(`AOS8_IDENTITY_HEADER`, default `X-Forwarded-Email`). The proxy must SET
**and inbound-strip** that header and be the sole ingress.

> ⚠️ **In proxy mode the loopback (or proxy-network-only) binding is
> mandatory** — anyone who can reach `:8501` directly can impersonate any
> user with one header. The built-in `accounts` mode above is the
> recommended path.

Unrecognized `AOS8_AUTH_MODE` values fail closed: the app refuses to serve
rather than fall back to an unauthenticated mode.

## Operations notes

- **Persistence.** The `aos8_state` volume holds `users.json` + the
  encrypted cred files. Without it, accounts and saved creds reset on
  redeploy. Keep `AOS8_CREDSTORE_KEY` stable across deploys.
- **Audit trail.** Sensitive actions (provision, cutover, claim, cleanup)
  are emitted as JSON audit lines to stdout, tagged with the signed-in user.
- **Scaling.** Streamlit sessions are websocket-bound to one replica. If you
  scale `app`, pin each user to one replica (cookie/IP affinity) at the LB
  and share the volume so all replicas see the same accounts.
- **Sessions.** Login lasts for the browser session — a full page refresh
  signs the user out and they log back in (no cookie/JWT persistence yet).

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `AOS8_AUTH_MODE` | `local` | `password` = one shared gate password; `accounts` = per-person verified-email login (any domain unless restricted); `proxy` = reverse-proxy header; `local` = single user. Any other value fails closed (the app refuses to serve). |
| `AOS8_APP_PASSWORD` | _(unset)_ | The shared password for `password` mode (required in that mode; fail-closed if unset) |
| `AOS8_ALLOWED_EMAIL_DOMAIN` | _(unset — any email)_ | Restrict registration to one domain in `accounts` mode (e.g. `example.com`) |
| `AOS8_USERS_FILE` | `~/.aos8-migration/users.json` | Path to the user registry (put on a persistent volume) |
| `AOS8_SMTP_MODE` | `relay` | `direct` = MX-lookup delivery (no relay); `relay` = send via `AOS8_SMTP_HOST` |
| `AOS8_SMTP_FROM` | _(sending host)_ | From address on verification emails — set to your sender (e.g. a gmail address) |
| `AOS8_SMTP_HOST` / `_PORT` / `_USER` / `_PASS` | _(unset)_ / `587` / — / — | `relay` mode SMTP server. No host (and not `direct`) ⇒ codes logged to console (dev only) |
| `AOS8_CREDSTORE_KEY` | _(unset)_ | Fernet key enabling per-user encrypted "Remember". Unset in a multi-user mode = persistence off |
| `AOS8_IDENTITY_HEADER` | `X-Forwarded-Email` | (`proxy` mode only) the single trusted identity header; the proxy must set **and** inbound-strip it |
| `AOS8_LOCAL_USER` | `local@localhost` | Principal used to scope the credstore in `local` mode |
