#!/usr/bin/env python3
"""Weekly re-login watchdog for a hosted gateway. Run every 5 minutes.

    .venv/bin/python services/ibkr-data/watchdog.py

Decides "logged in" from the real API handshake (gateway.api_session, the same
check `gateway status` uses), never from the login page's own state. When the
session is gone:

  a. A TOTP_SECRET in any profile (deploy/profiles/<name>.env, the active one
     first): ask the login page (webui.py) to log in as that profile, as its
     own "Log in" button does. At most one automatic attempt per 6 hours
     and 3 per 7 days, persisted, and any failure ends automation until the
     session is back. Never loops: the page's worker makes exactly one try.
  b. Otherwise, or after a failed automatic attempt: email once per outage,
     re-reminding at most every 12 hours, with a link to the login page, to
     everyone who can log in: whoever is free does it. One more email to all
     of them when the session comes back.

Mail settings live outside the repo in ~/.config/ibkr-watchdog/smtp.env
(SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS ALERT_FROM ALERT_TO LOGIN_URL), where
ALERT_TO is a comma-separated recipient list.
State lives in ~/.local/state/ibkr-watchdog/state.json.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

AUTO_MIN_GAP = 6 * 3600
AUTO_WEEK = 7 * 86400
AUTO_PER_WEEK = 3
AUTO_GRACE = 10 * 60        # an attempt that has not produced a session by now failed
REMIND_EVERY = 12 * 3600

STATE_FILE = Path.home() / ".local/state/ibkr-watchdog/state.json"
MAIL_ENV = Path.home() / ".config/ibkr-watchdog/smtp.env"
WEBUI = "http://127.0.0.1:8642"
WEBUI_HOST = "127.0.0.1:8642"

SUBJECT_DOWN = ("[IBKR] market data signed out: whoever is free, "
                "30-second re-login")
SUBJECT_UP = "[IBKR] signed back in"


def fresh_state() -> dict:
    return {"status": "ok", "outage_since": None, "last_email": None,
            "auto_attempts": [], "auto_pending": None, "auto_disabled": False}


def can_auto(attempts: list[float], now: float) -> bool:
    """True when one more automatic login fits both limits."""
    if any(now - t < AUTO_MIN_GAP for t in attempts):
        return False
    return sum(1 for t in attempts if now - t < AUTO_WEEK) < AUTO_PER_WEEK


def decide(state: dict, now: float, logged_in: bool,
           totp: bool) -> tuple[list[str], dict]:
    """Pure transition. Returns (actions, new_state); actions are drawn from
    auto_login, email_down, email_up."""
    s = {**fresh_state(), **state}
    s["auto_attempts"] = [t for t in s["auto_attempts"] if now - t < AUTO_WEEK]
    actions: list[str] = []

    if logged_in:
        if s["status"] == "signed_out" and s["last_email"] is not None:
            actions.append("email_up")
        return actions, {**fresh_state(), "auto_attempts": s["auto_attempts"]}

    if s["status"] != "signed_out":
        s.update(status="signed_out", outage_since=now, last_email=None,
                 auto_pending=None, auto_disabled=False)

    if s["auto_pending"] is not None:
        if now - s["auto_pending"] < AUTO_GRACE:
            return actions, s          # give the attempt time to finish
        s.update(auto_pending=None, auto_disabled=True)   # it failed

    if totp and not s["auto_disabled"] and can_auto(s["auto_attempts"], now):
        s["auto_attempts"].append(now)
        s["auto_pending"] = now
        actions.append("auto_login")
        return actions, s

    if s["last_email"] is None or now - s["last_email"] >= REMIND_EVERY:
        actions.append("email_down")
    return actions, s


# ---- side effects -------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return fresh_state()


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


def mail_conf() -> dict:
    conf = {}
    for line in MAIL_ENV.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip().strip('"').strip("'")
    return conf


def recipients(conf: dict) -> list[str]:
    return [a.strip() for a in conf.get("ALERT_TO", "nic@atab.ai").split(",")
            if a.strip()]


def totp_profile(gw) -> str | None:
    """The profile an automatic login should use: the active one if it has a
    TOTP secret, else any other that does."""
    import webui

    active = gw.active_profile()
    names = sorted(webui.PROFILES, key=lambda n: n != active)
    for name in names:
        p = gw.profile_read(name)
        if p.get("TOTP_SECRET") and p.get("TWS_USERID") and p.get("TWS_PASSWORD"):
            return name
    return None


def down_body(url: str, since: datetime) -> str:
    return ("The IBKR gateway on goober3 has no session "
            f"(signed out since {since:%Y-%m-%d %H:%M} UTC). Whoever is free:\n\n"
            f"Open {url}\n"
            "Password: (the page password Nic sent you)\n"
            "Then pick your name, type your code from your authenticator "
            "app.\n\n"
            "Cached bars still serve; live requests fail until someone does. "
            "Everyone on this list gets one more email when it is back.\n")


def send_mail(subject: str, text: str) -> None:
    c = mail_conf()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = c["ALERT_FROM"]
    msg["To"] = ", ".join(recipients(c))
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(text)
    host, port = c["SMTP_HOST"], int(c.get("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    if c.get("SMTP_STRICT_TLS", "true").lower() in ("0", "false", "no"):
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=45, context=ctx)
    else:
        server = smtplib.SMTP(host, port, timeout=45)
        server.starttls(context=ctx)
    with server:
        if c.get("SMTP_USER"):
            server.login(c["SMTP_USER"], c["SMTP_PASS"])
        server.send_message(msg)


def auto_login(profile: str) -> None:
    """One press of the page's Log in button: fetch the page for its CSRF
    token, then POST /start exactly as the page's JavaScript does."""
    subprocess.run(["systemctl", "--user", "start", "ibkr-webui.service"],
                   check=False, timeout=30)
    for _ in range(10):
        try:
            req = urllib.request.Request(WEBUI + "/", headers={"Host": WEBUI_HOST})
            page = urllib.request.urlopen(req, timeout=10).read().decode()
            break
        except OSError:
            time.sleep(2)
    else:
        raise RuntimeError("login page not reachable")
    token = re.search(r"const TOKEN='([^']+)'", page).group(1)
    req = urllib.request.Request(
        WEBUI + "/start", data=json.dumps({"profile": profile}).encode(),
        method="POST",
        headers={"Host": WEBUI_HOST, "Content-Type": "application/json",
                 "X-CSRF-Token": token})
    reply = json.loads(urllib.request.urlopen(req, timeout=15).read())
    if not reply.get("ok"):
        raise RuntimeError(f"page refused: {reply.get('error')}")


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gateway as gw

    now = time.time()
    logged_in = gw.api_session() is not None
    auto_profile = totp_profile(gw)
    totp = auto_profile is not None
    state = load_state()
    actions, new = decide(state, now, logged_in, totp)
    log(f"logged_in={logged_in} totp={totp} actions={actions or '-'}")

    if "auto_login" in actions:
        log(f"automatic login attempt as {auto_profile} "
            f"({len(new['auto_attempts'])} this week)")
        try:
            auto_login(auto_profile)
        except Exception as exc:
            log(f"automatic login failed to start: {exc}; falling back to email")
            new.update(auto_pending=None, auto_disabled=True)
            save_state(new)
            actions, new = decide(new, now, logged_in, totp)

    url = mail_conf().get("LOGIN_URL", "")
    if "email_down" in actions:
        since = datetime.fromtimestamp(new["outage_since"], timezone.utc)
        send_mail(SUBJECT_DOWN, down_body(url, since))
        new["last_email"] = now
        log("signed-out email sent")
    if "email_up" in actions:
        send_mail(SUBJECT_UP, "The IBKR gateway on goober3 is logged in and "
                              "serving again.\n")
        log("recovery email sent")
    save_state(new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
