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


def console_codes_allowed() -> bool:
    """Dev-only escape hatch. Printing a live verification code to the server
    log lets anyone with log access activate a pending registration, so it is
    OFF unless explicitly enabled."""
    return os.environ.get("AOS8_ALLOW_CONSOLE_CODES", "").strip().lower() in (
        "1", "true", "yes", "on")


def _deliver_code(email: str, code: str) -> bool:
    """Email the verification code. Returns True if it was actually emailed.

    Fails CLOSED: when delivery fails the code is not logged, because it stays
    valid and the log is a far wider audience than the mailbox. Set
    AOS8_ALLOW_CONSOLE_CODES=true for local development only."""
    subject = "Your AOS 8 Migration Console verification code"
    body = (f"Your verification code is: {code}\n\n"
            f"It expires in {accounts.CODE_TTL_MINUTES} minutes. "
            f"If you didn't request this, ignore this email.")
    ok, err = mailer.send(email, subject, body)
    if not ok:
        if console_codes_allowed():
            print(f"[auth] verification code for {email}: {code} "
                  f"(email not sent: {err})", flush=True)
        else:
            # No code material in the log — just the delivery failure.
            print(f"[auth] verification email to {email} FAILED: {err}. "
                  f"Configure SMTP (or set AOS8_ALLOW_CONSOLE_CODES=true for "
                  f"local development).", flush=True)
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
        if console_codes_allowed():
            st.warning("Email delivery isn't configured on this server — the code "
                       "was written to the server console (dev mode).")
        else:
            st.error("Email delivery isn't configured on this server, so no code "
                     "could be sent. Ask an administrator to configure SMTP.")
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
            _deliver_code(email, new_code)
            st.info(msg)
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
                if ok:
                    _deliver_code(email, code)
                st.session_state["_pending_email"] = accounts._norm(email)
                st.rerun()
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
            if pw1 != pw2:
                st.error("Passwords don't match.")
            else:
                ok, msg, code = accounts.register(email_r, pw1)
                if ok:
                    _deliver_code(email_r, code)
                    st.session_state["_pending_email"] = accounts._norm(email_r)
                    st.rerun()
                else:
                    st.error(msg)


def _password_screen() -> None:
    pw = st.text_input("Access password", type="password", key="app_pw")
    if st.button("Enter", type="primary", key="app_pw_btn"):
        if identity.check_app_password(pw):
            st.session_state["_authenticated"] = True
            st.session_state["_auth_user"] = identity.SHARED_USER
            st.rerun()
        else:
            st.error("Incorrect password.")


def render_gate() -> bool:
    """Render the auth gate. Returns True if signed in (caller proceeds),
    False if the gate was drawn (caller must st.stop())."""
    if st.session_state.get("_authenticated") and st.session_state.get("_auth_user"):
        return True
    st.markdown("### 🔐 AOS 8 → Central Migration Console")
    if identity.auth_mode() == "password":
        st.caption("Enter the access password to continue.")
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
    """Sign out and clear the ENTIRE session.

    Everything the previous operator touched — destination API secrets, Classic
    access/refresh tokens, the discovered customer config and every derived
    provisioning/onboarding result — lives in session_state. Popping only the
    auth flags left all of it readable by whoever signed in next on the same
    browser session, so wipe the session wholesale and let the next sign-in
    rebuild it from that user's own encrypted credstore.
    """
    for k in list(st.session_state.keys()):
        st.session_state.pop(k, None)
    st.rerun()
