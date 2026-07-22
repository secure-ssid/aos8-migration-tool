"""
Login / self-service registration gate for the multi-user (accounts) mode.

Rendered by app.py before the wizard whenever AOS8_AUTH_MODE=accounts and the
session isn't signed in yet. Three sub-states, driven by session_state:
  - default          -> Sign in / Register tabs
  - _pending_email   -> "enter the code we emailed you" verification screen
  - _authenticated   -> gate passes, the wizard renders

Identity established here flows into the per-user encrypted credstore and audit
log via lib.identity.current_user() (which reads _auth_user in accounts mode).
"""
import os

import streamlit as st

from lib import accounts, identity, mailer


def _console_codes_allowed() -> bool:
    """Explicit opt-in for the dev fallback that prints verification codes
    to the server console. Fail closed by default: with no SMTP configured
    and no opt-in, registration is blocked instead of leaking codes into
    (potentially centralized) logs."""
    return os.environ.get("AOS8_ALLOW_CONSOLE_CODES", "").strip().lower() == "true"


def _delivery_available() -> bool:
    return mailer.configured() or _console_codes_allowed()


def _deliver_code(email: str, code: str) -> bool:
    """Email the verification code. With NO SMTP configured (dev mode) the
    code is logged to the server console instead. Returns True if it was
    actually emailed."""
    subject = "Your AOS 8 Migration Console verification code"
    body = (f"Your verification code is: {code}\n\n"
            f"It expires in {accounts.CODE_TTL_MINUTES} minutes. "
            f"If you didn't request this, ignore this email.")
    ok, err = mailer.send(email, subject, body)
    if not ok:
        if mailer.configured():
            # SMTP exists but the send failed — do NOT drop the secret into
            # the (production) logs; the user can hit Resend.
            print(f"[auth] verification email to {email} FAILED: {err}",
                  flush=True)
        elif _console_codes_allowed():
            # Dev fallback only — requires AOS8_ALLOW_CONSOLE_CODES=true.
            print(f"[auth] verification code for {email}: {code} "
                  f"(no SMTP configured)", flush=True)
            return True
        else:
            print(f"[auth] verification code for {email} NOT delivered — no "
                  "SMTP configured and AOS8_ALLOW_CONSOLE_CODES is not set",
                  flush=True)
    return ok


def _signed_in(email: str) -> None:
    st.session_state["_authenticated"] = True
    st.session_state["_auth_user"] = accounts._norm(email)
    st.session_state.pop("_pending_email", None)
    st.rerun()


def _verify_screen(email: str) -> None:
    st.markdown("#### Verify your email")
    st.caption(f"Enter the 6-digit code we sent to **{email}**.")
    if not mailer.configured():
        if _console_codes_allowed():
            st.warning("Email delivery isn't configured on this server — the "
                       "code was written to the server console (dev mode).")
        else:
            st.error("Email delivery isn't configured on this server — codes "
                     "cannot be delivered. Configure SMTP (or set "
                     "AOS8_ALLOW_CONSOLE_CODES=true for local dev).")
    code = st.text_input("Verification code", max_chars=6, key="verify_code_input")
    c1, c2, c3 = st.columns([1, 1, 1])
    if c1.button("Verify", type="primary", use_container_width=True):
        ok, msg = accounts.verify_code(email, code)
        if ok:
            _signed_in(email)
        else:
            st.error(msg)
    if c2.button("Resend code", use_container_width=True):
        ok, msg, new_code = accounts.resend_code(email)
        if ok:
            if _deliver_code(email, new_code):
                st.info(msg)
            else:
                st.error("Could not send the verification email — check the "
                         "server's SMTP settings and try again.")
        else:
            st.error(msg)
    if c3.button("Use a different email", use_container_width=True):
        st.session_state.pop("_pending_email", None)
        st.rerun()


def _login_register() -> None:
    tab_login, tab_register = st.tabs(["Sign in", "Register"])

    with tab_login:
        email = st.text_input("Email", key="login_email")
        pw = st.text_input("Password", type="password", key="login_pw")
        if st.button("Sign in", type="primary", key="login_btn"):
            status = accounts.verify_password(email, pw)
            if status == "ok":
                _signed_in(email)
            elif status == "unverified":
                # account exists but never verified — re-arm a code and divert
                ok, _msg, code = accounts.resend_code(email)
                delivered = _deliver_code(email, code) if ok else False
                if ok and not delivered and mailer.configured():
                    st.error("Your account isn't verified yet and the "
                             "verification email could not be sent — check "
                             "the server's SMTP settings and try again.")
                elif not _delivery_available():
                    st.error("Your account isn't verified yet and this server "
                             "has no email delivery configured — ask the "
                             "administrator to set up SMTP.")
                else:
                    st.session_state["_pending_email"] = accounts._norm(email)
                    st.rerun()
            elif status == "locked":
                st.error(f"Too many failed attempts — this account is locked "
                         f"for {accounts.LOGIN_LOCK_MINUTES} minutes. "
                         f"Try again later.")
            else:
                st.error("Invalid email or password.")

    with tab_register:
        domain_hint = f"**@{accounts.ALLOWED_DOMAIN}** addresses" if accounts.ALLOWED_DOMAIN else "any email address"
        st.caption(f"Registration is open to {domain_hint}. We'll email a code to confirm it's yours.")
        email_label = f"Email (@{accounts.ALLOWED_DOMAIN})" if accounts.ALLOWED_DOMAIN else "Email"
        email_r = st.text_input(email_label, key="reg_email")
        pw1 = st.text_input(f"Password (min {accounts.MIN_PASSWORD_LEN} chars)",
                            type="password", key="reg_pw1")
        pw2 = st.text_input("Confirm password", type="password", key="reg_pw2")
        if st.button("Create account", type="primary", key="reg_btn"):
            if not _delivery_available():
                st.error("Registration is unavailable: this server has no "
                         "email delivery configured (and console codes are "
                         "not enabled). Configure SMTP — see the Deployment "
                         "guide — or set AOS8_ALLOW_CONSOLE_CODES=true for "
                         "local development.")
            elif pw1 != pw2:
                st.error("Passwords don't match.")
            else:
                ok, msg, code = accounts.register(email_r, pw1)
                if ok:
                    if _deliver_code(email_r, code):
                        st.session_state["_pending_email"] = accounts._norm(email_r)
                        st.rerun()
                    else:
                        st.error("Account created, but the verification email "
                                 "could not be sent — check the server's SMTP "
                                 "settings, then sign in to resend the code.")
                else:
                    st.error(msg)


def _password_screen() -> None:
    error_slot = st.empty()
    pw = st.text_input("Access password", type="password",
                       placeholder="Enter password and press Enter",
                       key="app_pw")
    if st.button("Unlock", type="primary", key="app_pw_btn"):
        wait = identity.app_password_retry_after()
        if wait:
            error_slot.error(f"Too many failed attempts — wait {wait}s "
                             "before trying again.")
        elif identity.check_app_password(pw):
            st.session_state["_authenticated"] = True
            st.session_state["_auth_user"] = identity.SHARED_USER
            st.rerun()
        else:
            identity.record_app_password_failure()
            error_slot.error("Incorrect password — try again.")


def render_gate() -> bool:
    """Render the auth gate. Returns True if signed in (caller proceeds),
    False if the gate was drawn (caller must st.stop())."""
    if st.session_state.get("_authenticated") and st.session_state.get("_auth_user"):
        return True
    st.markdown("### 🔐 AOS 8 → Central Migration Console")
    if identity.auth_mode() == "password":
        _password_screen()
        return False
    st.caption("Sign in to continue.")
    pending = st.session_state.get("_pending_email")
    if pending:
        _verify_screen(pending)
    else:
        _login_register()
    return False


def logout() -> None:
    # Clear the WHOLE session, not just the auth flags: the sign-out button
    # exists for shared workstations, and the previous operator's decrypted
    # API secrets, tokens and discovered customer data must not survive into
    # the next sign-in on this browser session.
    st.session_state.clear()
    st.rerun()
