"""Watchdog decision and rate limits; pure, no IBKR, no mail."""
import webui
import json
import urllib.error

from watchdog import (ALERT_GRACE, AUTO_GRACE, DATA_GRACE, REMIND_EVERY,
                      can_auto, decide, fresh_state, wa_down_text, wa_send)

H = 3600
T0 = 1_800_000_000.0


def test_ok_stays_quiet():
    actions, s = decide(fresh_state(), T0, True, True)
    assert actions == [] and s["status"] == "ok"


def test_signed_out_without_totp_emails_once_then_reminds_after_12h():
    actions, s = decide(fresh_state(), T0, False, False)
    assert actions == []                      # inside the grace period
    actions, s = decide(s, T0 + ALERT_GRACE, False, False)
    assert actions == ["email_down", "wa_down"]
    s["last_email"] = T0 + ALERT_GRACE
    s["wa_down_sent"] = True
    actions, s = decide(s, T0 + ALERT_GRACE + 300, False, False)
    assert actions == []
    actions, s = decide(s, T0 + ALERT_GRACE + REMIND_EVERY - 1, False, False)
    assert actions == []
    actions, s = decide(s, T0 + ALERT_GRACE + REMIND_EVERY, False, False)
    assert actions == ["email_down"]          # reminder: email only, never WhatsApp


def test_nightly_restart_blip_sends_nothing():
    # 18 Nov 2026: down 05:30:13Z, back 05:35:36Z; it mailed everyone twice.
    actions, s = decide(fresh_state(), T0, False, False)
    assert actions == []
    actions, s = decide(s, T0 + 323, True, False)
    assert actions == [] and s["status"] == "ok"


def test_data_stall_waits_longer_and_says_why():
    actions, s = decide(fresh_state(), T0, False, False, reason="no_data")
    actions, s = decide(s, T0 + ALERT_GRACE, False, False, reason="no_data")
    assert actions == []
    actions, s = decide(s, T0 + DATA_GRACE, False, False, reason="no_data")
    assert actions == ["email_down", "wa_down"] and s["reason"] == "no_data"


def test_no_session_mid_stall_switches_to_the_short_grace():
    actions, s = decide(fresh_state(), T0, False, False, reason="no_data")
    actions, s = decide(s, T0 + ALERT_GRACE, False, False, reason="no_session")
    assert actions == ["email_down", "wa_down"]


def test_whatsapp_down_retried_until_sent_then_up_owed_until_sent():
    actions, s = decide(fresh_state(), T0, False, False)
    actions, s = decide(s, T0 + ALERT_GRACE, False, False)
    s["last_email"] = T0 + ALERT_GRACE        # email went, WhatsApp failed
    actions, s = decide(s, T0 + ALERT_GRACE + 300, False, False)
    assert actions == ["wa_down"]
    s["wa_down_sent"] = True
    actions, s = decide(s, T0 + H, True, False)
    assert actions == ["email_up", "wa_up"] and s["wa_up_owed"]
    # the up message failed: retried on the next pass, email not repeated
    actions, s = decide(s, T0 + H + 300, True, False)
    assert actions == ["wa_up"]
    s["wa_up_owed"] = False
    actions, s = decide(s, T0 + H + 600, True, False)
    assert actions == []


def test_owed_up_message_dropped_when_it_goes_down_again():
    s = {**fresh_state(), "wa_up_owed": True}
    actions, s = decide(s, T0, False, False)
    assert not s["wa_up_owed"] and actions == []


def test_recovery_email_only_after_a_down_email():
    s = {**fresh_state(), "status": "signed_out", "outage_since": T0,
         "last_email": T0}
    actions, s = decide(s, T0 + H, True, False)
    assert actions == ["email_up"] and s["status"] == "ok"
    s = {**fresh_state(), "status": "signed_out", "outage_since": T0,
         "auto_pending": T0}
    actions, _ = decide(s, T0 + 60, True, True)
    assert actions == []   # silent automatic recovery, WhatsApp included


def test_totp_tries_once_then_waits_then_falls_back_to_email():
    actions, s = decide(fresh_state(), T0, False, True)
    assert actions == ["auto_login"] and s["auto_pending"] == T0
    actions, s = decide(s, T0 + 300, False, True)
    assert actions == []                      # attempt still in its grace
    actions, s = decide(s, T0 + AUTO_GRACE, False, True)
    assert actions == ["email_down", "wa_down"] and s["auto_disabled"]
    s["last_email"] = T0 + AUTO_GRACE
    # well past the 6h gap: still no second attempt in this outage
    actions, s = decide(s, T0 + 7 * H, False, True)
    assert "auto_login" not in actions


def test_auto_attempts_limited_per_6h_and_per_week():
    assert can_auto([], T0)
    assert not can_auto([T0 - 5 * H], T0)
    assert can_auto([T0 - 6 * H], T0)
    three = [T0 - 5 * 86400, T0 - 3 * 86400, T0 - 1 * 86400]
    assert not can_auto(three, T0)
    assert can_auto(three[1:], T0)
    assert can_auto(three, T0 + 2 * 86400 + 1)   # oldest aged out


def test_new_outage_respects_attempts_from_earlier_outage():
    actions, s = decide(fresh_state(), T0, False, True)
    actions, s = decide(s, T0 + 60, True, True)          # recovered
    assert s["auto_attempts"] == [T0]
    actions, s = decide(s, T0 + 2 * H, False, True)      # down again, <6h
    assert actions == []                                 # grace first
    actions, s = decide(s, T0 + 2 * H + ALERT_GRACE, False, True)
    assert actions == ["email_down", "wa_down"]


def test_login_page_uses_relative_urls_for_the_proxy():
    # Served under /ibkr/login/ behind nginx, so no root-absolute fetches.
    assert "fetch('/" not in webui.PAGE and "post('/" not in webui.PAGE


class _Resp:
    def __init__(self, body): self.body = body
    def read(self): return self.body.encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


CONF = {"WHATSAPP_URLS": "https://direct/v1/send,https://relay/v1/send",
        "WHATSAPP_TOKEN": "t", "WHATSAPP_TO": "group@g.us"}


def test_wa_send_falls_back_on_5xx_but_not_on_4xx():
    calls = []

    def flaky(req, timeout):
        calls.append(req.full_url)
        if "direct" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 502, "bad", {}, None)
        assert json.loads(req.data) == {"to": "group@g.us", "text": "hi"}
        assert req.headers["Authorization"] == "Bearer t"
        return _Resp('{"ok": true}')

    ok, _ = wa_send("hi", CONF, opener=flaky)
    assert ok and calls == ["https://direct/v1/send", "https://relay/v1/send"]

    calls.clear()

    def forbidden(req, timeout):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 403, "no", {}, None)

    ok, detail = wa_send("hi", CONF, opener=forbidden)
    assert not ok and calls == ["https://direct/v1/send"] and "403" in detail


def test_wa_send_treats_ok_false_as_failure_and_needs_config():
    ok, _ = wa_send("hi", CONF, opener=lambda r, timeout: _Resp('{"ok": false}'))
    assert not ok
    assert wa_send("hi", {}) == (False, "whatsapp not configured")


def test_down_text_names_the_page_and_no_secret():
    text = wa_down_text("https://example/ibkr/login/", T0, "no_session")
    assert "https://example/ibkr/login/" in text and "signed out" in text
    assert "password" not in text.lower()
    assert "Existing session" in wa_down_text("u", T0, "no_data")
    assert "\u2014" not in text
