"""
Minimal SMTP sender for account-verification emails. Pure stdlib (smtplib).

Configured entirely via environment so no secret is baked into the image:
  AOS8_SMTP_HOST       SMTP server host (unset => email disabled; see below)
  AOS8_SMTP_PORT       default 587
  AOS8_SMTP_USER       login user (optional; omit for an open internal relay)
  AOS8_SMTP_PASS       login password
  AOS8_SMTP_FROM       From address (default: AOS8_SMTP_USER or no-reply@hpe.com)
  AOS8_SMTP_STARTTLS   "true" (default) to STARTTLS on a plain connection
  AOS8_SMTP_SSL        "true" to use an implicit-TLS (SMTPS) connection instead

When AOS8_SMTP_HOST is unset, send() returns (False, "smtp-unconfigured") and
the caller falls back to logging the code to the server console (dev only).
"""
import os
import smtplib
import ssl
from email.message import EmailMessage


def configured() -> bool:
    return bool(os.environ.get("AOS8_SMTP_HOST"))


def send(to: str, subject: str, body: str):
    """Send a plaintext email. Returns (ok, error_message)."""
    host = os.environ.get("AOS8_SMTP_HOST")
    if not host:
        return False, "smtp-unconfigured"
    port = int(os.environ.get("AOS8_SMTP_PORT", "587"))
    user = os.environ.get("AOS8_SMTP_USER")
    password = os.environ.get("AOS8_SMTP_PASS")
    sender = os.environ.get("AOS8_SMTP_FROM") or user or "no-reply@hpe.com"

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

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
    except Exception as e:  # network/auth/TLS — surface to the caller, never raise
        return False, str(e)
