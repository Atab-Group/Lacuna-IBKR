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
  c. The same outage also goes to WhatsApp (the Lacuna Monitors group) as
     exactly two messages: one when it is confirmed, one when it is over. No
     reminders there, and nothing at all for an outage that clears inside the
     grace period or is fixed by an automatic login.

"Logged in" means two things since 20 Nov 2026: the API handshake completes
AND a tiny historical request (two daily EUR.USD bars) returns data. On 18 Nov
the gateway sat on "Existing session detected" for hours while the handshake
still answered, so the old check reported logged in and nobody was told.

Nothing is sent until the outage has lasted ALERT_GRACE (no session) or
DATA_GRACE (handshake fine, no data). The 07:30 SAST nightly restart drops the
session for about five minutes, and on 18 Nov that blip mailed everyone twice.

WhatsApp settings live in ~/.config/ibkr-watchdog/whatsapp.env
(WHATSAPP_URLS comma-separated routes tried in order, WHATSAPP_TOKEN,
WHATSAPP_TO comma-separated recipients). No file, no WhatsApp.

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
import urllib.error
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
ALERT_GRACE = 9 * 60        # no session: two 5-minute passes, so a restart blip stays quiet
DATA_GRACE = 20 * 60        # handshake fine but no data: slower, farms blip more often

STATE_FILE = Path.home() / ".local/state/ibkr-watchdog/state.json"
MAIL_ENV = Path.home() / ".config/ibkr-watchdog/smtp.env"
WA_ENV = Path.home() / ".config/ibkr-watchdog/whatsapp.env"
PROBE_CONID = 12087792      # EUR.USD on IDEALPRO: trades around the clock, needs no subscription
WEBUI = "http://127.0.0.1:8642"
WEBUI_HOST = "127.0.0.1:8642"

SUBJECT_DOWN = ("[IBKR] market data signed out: whoever is free, "
                "30-second re-login")
SUBJECT_UP = "[IBKR] signed back in"


def fresh_state() -> dict:
    return {"status": "ok", "outage_since": None, "last_email": None,
            "auto_attempts": [], "auto_pending": None, "auto_disabled": False,
            "reason": None, "wa_down_sent": False, "wa_up_owed": False}


def can_auto(attempts: list[float], now: float) -> bool:
    """True when one more automatic login fits both limits."""
    if any(now - t < AUTO_MIN_GAP for t in attempts):
        return False
    return sum(1 for t in attempts if now - t < AUTO_WEEK) < AUTO_PER_WEEK


def decide(state: dict, now: float, logged_in: bool, totp: bool,
           reason: str = "no_session") -> tuple[list[str], dict]:
    """Pure transition. Returns (actions, new_state); actions are drawn from
    auto_login, email_down, email_up, wa_down, wa_up.

    reason says why the pass failed: "no_session" (no handshake) or "no_data"
    (handshake fine, the data probe failed). It only picks the grace period.
    wa_down and wa_up are retried on later passes until the caller records
    success (wa_down_sent True, wa_up_owed False)."""
    s = {**fresh_state(), **state}
    s["auto_attempts"] = [t for t in s["auto_attempts"] if now - t < AUTO_WEEK]
    actions: list[str] = []

    if logged_in:
        if s["status"] == "signed_out" and s["last_email"] is not None:
            actions.append("email_up")
        owed = bool(s["wa_down_sent"] or s["wa_up_owed"])
        if owed:
            actions.append("wa_up")
        return actions, {**fresh_state(), "auto_attempts": s["auto_attempts"],
                         "wa_up_owed": owed}

    if s["status"] != "signed_out":
        # A new outage. A "back" message still owed for the last one would
        # now be false, so it is dropped.
        s.update(status="signed_out", outage_since=now, last_email=None,
                 auto_pending=None, auto_disabled=False, reason=reason,
                 wa_down_sent=False, wa_up_owed=False)
    if reason == "no_session":
        s["reason"] = "no_session"     # the worse reading wins for the outage

    if s["auto_pending"] is not None:
        if now - s["auto_pending"] < AUTO_GRACE:
            return actions, s          # give the attempt time to finish
        s.update(auto_pending=None, auto_disabled=True)   # it failed

    if totp and not s["auto_disabled"] and can_auto(s["auto_attempts"], now):
        s["auto_attempts"].append(now)
        s["auto_pending"] = now
        actions.append("auto_login")
        return actions, s

    grace = DATA_GRACE if s["reason"] == "no_data" else ALERT_GRACE
    if now - s["outage_since"] < grace:
        return actions, s              # could still be the nightly restart

    if s["last_email"] is None or now - s["last_email"] >= REMIND_EVERY:
        actions.append("email_down")
    if not s["wa_down_sent"]:
        actions.append("wa_down")
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


def data_ok() -> bool:
    """True when the gateway hands back real data, not just a handshake.

    Two daily EUR.USD midpoint bars: about two seconds, one pacing slot, no
    subscription. A gateway stuck on a dialog answers the handshake and then
    never returns these."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gateway as gw
    try:
        from ib_async import IB, Contract, util

        util.logToConsole("CRITICAL")
        ib = IB()
        ib.client.connect(gw.API_HOST, gw.API_PORT,
                          clientId=int(time.time()) % 900 + 60, timeout=8)
        try:
            c = Contract(conId=PROBE_CONID, secType="CASH", symbol="EUR",
                         currency="USD", exchange="IDEALPRO")
            bars = ib.reqHistoricalData(c, "", "2 D", "1 day", "MIDPOINT",
                                        False, timeout=25)
            return bool(bars)
        finally:
            ib.disconnect()
    except Exception:
        return False


def check(gw) -> str:
    """"ok", "no_session" or "no_data"."""
    if gw.api_session() is None:
        return "no_session"
    return "ok" if data_ok() else "no_data"


# ---- WhatsApp -----------------------------------------------------------

def wa_conf() -> dict:
    conf = {}
    try:
        text = WA_ENV.read_text()
    except OSError:
        return conf
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip().strip('"').strip("'")
    return conf


def wa_split(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def local_times(ts: float) -> str:
    """'07:30 UK (09:30 SAST)': the group reads in both."""
    from zoneinfo import ZoneInfo
    uk = datetime.fromtimestamp(ts, ZoneInfo("Europe/London"))
    za = datetime.fromtimestamp(ts, ZoneInfo("Africa/Johannesburg"))
    day = "" if (time.time() - ts) < 20 * 3600 else f" on {uk:%a %d %b}"
    return f"{uk:%H:%M} UK ({za:%H:%M} SAST){day}"


def wa_down_text(url: str, since: float, reason: str) -> str:
    why = ("IBKR market data is signed out" if reason == "no_session" else
           "IBKR market data has stopped (the gateway may be showing "
           "\"Existing session detected\")")
    return (f"{why}, since {local_times(since)}.\n\n"
            f"Whoever is free, please log back in (30 seconds): {url}\n"
            "Pick your name and type the code from your authenticator app.\n\n"
            "You will get one more message here when it is back.")


WA_UP_TEXT = "IBKR is signed back in and market data is flowing again."


def wa_send(text: str, conf: dict | None = None, opener=None) -> tuple[bool, str]:
    """Send `text` to every WHATSAPP_TO. Each recipient is tried on each
    route in order; a 4xx is the gateway's answer and is not retried on
    another route. True only if every recipient got it."""
    conf = wa_conf() if conf is None else conf
    urls, to = wa_split(conf.get("WHATSAPP_URLS")), wa_split(conf.get("WHATSAPP_TO"))
    if not urls or not to or not conf.get("WHATSAPP_TOKEN"):
        return False, "whatsapp not configured"
    send = opener or urllib.request.urlopen
    details = []
    all_ok = True
    for dest in to:
        ok, why = False, ""
        for url in urls:
            req = urllib.request.Request(
                url, method="POST",
                data=json.dumps({"to": dest, "text": text}).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {conf['WHATSAPP_TOKEN']}"})
            try:
                with send(req, timeout=20) as resp:
                    body = resp.read().decode("utf-8", "replace")
                try:
                    refused = json.loads(body).get("ok") is False
                except (ValueError, AttributeError):
                    refused = False
                if refused:
                    why = f"refused: {body[:160]}"
                    break
                ok = True
                break
            except urllib.error.HTTPError as exc:
                why = f"HTTP {exc.code}"
                if exc.code != 429 and exc.code < 500:
                    break
            except OSError as exc:
                why = f"unreachable: {exc}"
        details.append(f"{dest}: {'sent' if ok else why}")
        all_ok = all_ok and ok
    return all_ok, "; ".join(details)


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
    result = check(gw)
    logged_in = result == "ok"
    auto_profile = totp_profile(gw)
    totp = auto_profile is not None
    state = load_state()
    actions, new = decide(state, now, logged_in, totp,
                          reason=result if not logged_in else "no_session")
    log(f"logged_in={logged_in} check={result} totp={totp} "
        f"actions={actions or '-'}")

    if "auto_login" in actions:
        log(f"automatic login attempt as {auto_profile} "
            f"({len(new['auto_attempts'])} this week)")
        try:
            auto_login(auto_profile)
        except Exception as exc:
            log(f"automatic login failed to start: {exc}; falling back to email")
            new.update(auto_pending=None, auto_disabled=True)
            save_state(new)
            actions, new = decide(new, now, logged_in, totp, reason=result)

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
    # WhatsApp after email: a WhatsApp failure must never cost the email, and
    # each send is recorded before the next, so a crash cannot repeat one.
    if "wa_down" in actions:
        ok, detail = wa_send(wa_down_text(url, new["outage_since"], new["reason"]))
        new["wa_down_sent"] = ok
        log(f"whatsapp down {'sent' if ok else 'FAILED'}: {detail}")
    if "wa_up" in actions:
        ok, detail = wa_send(WA_UP_TEXT)
        new["wa_up_owed"] = not ok
        log(f"whatsapp up {'sent' if ok else 'FAILED'}: {detail}")
    save_state(new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
