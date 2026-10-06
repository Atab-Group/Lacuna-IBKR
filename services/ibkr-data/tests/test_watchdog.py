"""Watchdog decision and rate limits; pure, no IBKR, no mail."""
import webui
from watchdog import (AUTO_GRACE, REMIND_EVERY, can_auto, decide,
                      fresh_state)

H = 3600
T0 = 1_800_000_000.0


def test_ok_stays_quiet():
    actions, s = decide(fresh_state(), T0, True, True)
    assert actions == [] and s["status"] == "ok"


def test_signed_out_without_totp_emails_once_then_reminds_after_12h():
    actions, s = decide(fresh_state(), T0, False, False)
    assert actions == ["email_down"]
    s["last_email"] = T0
    actions, s = decide(s, T0 + 300, False, False)
    assert actions == []
    actions, s = decide(s, T0 + REMIND_EVERY - 1, False, False)
    assert actions == []
    actions, s = decide(s, T0 + REMIND_EVERY, False, False)
    assert actions == ["email_down"]


def test_recovery_email_only_after_a_down_email():
    s = {**fresh_state(), "status": "signed_out", "outage_since": T0,
         "last_email": T0}
    actions, s = decide(s, T0 + H, True, False)
    assert actions == ["email_up"] and s["status"] == "ok"
    s = {**fresh_state(), "status": "signed_out", "outage_since": T0,
         "auto_pending": T0}
    actions, _ = decide(s, T0 + 60, True, True)
    assert actions == []   # silent automatic recovery


def test_totp_tries_once_then_waits_then_falls_back_to_email():
    actions, s = decide(fresh_state(), T0, False, True)
    assert actions == ["auto_login"] and s["auto_pending"] == T0
    actions, s = decide(s, T0 + 300, False, True)
    assert actions == []                      # attempt still in its grace
    actions, s = decide(s, T0 + AUTO_GRACE, False, True)
    assert actions == ["email_down"] and s["auto_disabled"]
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
    assert actions == ["email_down"]


def test_login_page_uses_relative_urls_for_the_proxy():
    # Served under /ibkr/login/ behind nginx, so no root-absolute fetches.
    assert "fetch('/" not in webui.PAGE and "post('/" not in webui.PAGE
