"""
Verification-email sender. Two modes via ``AOS8_SMTP_MODE``:

  relay  (default) — hand the message to an SMTP server you point it at
                     (AOS8_SMTP_HOST). Works with any provider's SMTP: a
                     transactional service (SendGrid/Mailgun/Brevo/Resend free
                     tier) or an authenticated mailbox (Gmail/O365 app password).
  direct          — NO relay needed: look up the recipient domain's MX records
                    and deliver straight to them on port 25 (STARTTLS if
                    offered). This is the "send it ourselves" path.

  ⚠ Direct-mode deliverability: a receiving mail security gateway (e.g. hpe.com
  fronts Proofpoint) will usually REJECT or quarantine mail that arrives from an
  IP not authorized to send for the From domain. So direct mode is reliable only
  when the app's egress IP is a sanctioned sender for AOS8_SMTP_FROM's domain
  (typically: running inside the org network). Otherwise prefer relay mode with
  a transactional provider that gives you a verified sender (SPF/DKIM).

Config (all optional except where noted):
  AOS8_SMTP_MODE   relay | direct           (default relay)
  AOS8_SMTP_FROM   From address             (default no-reply@<recipient domain>)
  relay mode:      AOS8_SMTP_HOST (required), _PORT (587), _USER, _PASS,
                   _STARTTLS (true), _SSL (false)
  direct mode:     needs dnspython for MX lookup; delivers on port 25

When sending isn't possible (no relay host / MX failure), send() returns
(False, reason) and the caller logs the code to the server console (dev only).
"""
import os
import smtplib
import socket
import ssl
from email.message import EmailMessage


def mode() -> str:
    return os.environ.get("AOS8_SMTP_MODE", "relay").strip().lower()


def _default_from() -> str:
    """A From address when AOS8_SMTP_FROM isn't set. Deliberately NOT the
    recipient's domain — sending 'from' the recipient's own domain (e.g.
    no-reply@hpe.com) from an unauthorized IP is treated as spoofing and
    rejected by gateways like Proofpoint. Use the sending host instead; real
    deployments should set AOS8_SMTP_FROM to a domain they control / a
    provider's verified sender."""
    host = socket.getfqdn() or "localhost"
    if "." not in host or host.startswith("localhost"):
        host = "migration-console.invalid"
    return f"no-reply@{host}"


def configured() -> bool:
    """True when a delivery path is set up (so the UI can stop warning)."""
    if mode() == "direct":
        return True
    return bool(os.environ.get("AOS8_SMTP_HOST"))


def _message(sender: str, to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def send(to: str, subject: str, body: str):
    """Send a plaintext email. Returns (ok, error_message)."""
    if mode() == "direct":
        return _send_direct(to, subject, body)
    return _send_relay(to, subject, body)


def _send_relay(to, subject, body):
    host = os.environ.get("AOS8_SMTP_HOST")
    if not host:
        return False, "smtp-unconfigured"
    port = int(os.environ.get("AOS8_SMTP_PORT", "587"))
    user = os.environ.get("AOS8_SMTP_USER")
    password = os.environ.get("AOS8_SMTP_PASS")
    sender = os.environ.get("AOS8_SMTP_FROM") or user or _default_from()
    msg = _message(sender, to, subject, body)
    try:
        if os.environ.get("AOS8_SMTP_SSL", "false").strip().lower() == "true":
            with smtplib.SMTP_SSL(host, port, timeout=15,
                                  context=ssl.create_default_context()) as s:
                if user:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                if os.environ.get("AOS8_SMTP_STARTTLS", "true").strip().lower() == "true":
                    s.starttls(context=ssl.create_default_context())
                if user:
                    s.login(user, password)
                s.send_message(msg)
        return True, ""
    except Exception as e:
        return False, str(e)


def _mx_hosts(domain: str):
    """MX hostnames for a domain, best (lowest preference) first."""
    import dns.resolver  # dnspython
    answers = dns.resolver.resolve(domain, "MX")
    ranked = sorted((r.preference, str(r.exchange).rstrip(".")) for r in answers)
    return [host for _pref, host in ranked]


def _send_direct(to, subject, body):
    domain = to.rsplit("@", 1)[-1]
    sender = os.environ.get("AOS8_SMTP_FROM") or _default_from()
    try:
        hosts = _mx_hosts(domain)
    except ImportError:
        return False, "direct mode requires dnspython (pip install dnspython)"
    except Exception as e:
        return False, f"MX lookup failed for {domain}: {e}"
    if not hosts:
        return False, f"no MX records for {domain}"
    msg = _message(sender, to, subject, body)
    last = "no MX host reachable"
    for host in hosts:
        try:
            with smtplib.SMTP(host, 25, timeout=20) as s:
                s.ehlo()
                if s.has_extn("starttls"):
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                s.send_message(msg)
            return True, ""
        except Exception as e:
            last = f"{host}: {e}"
            continue
    return False, f"direct delivery failed ({last})"
