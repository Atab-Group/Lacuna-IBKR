"""Per-person login profiles, selection, recipients; no IBKR, no mail."""
import stat
from datetime import datetime, timezone

import pytest

import gateway as gw
import watchdog
import webui


@pytest.fixture
def deploy(tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "ENV", tmp_path / ".env")
    monkeypatch.setattr(gw, "PROFILES", tmp_path / "profiles")
    return tmp_path


def test_profile_absent_until_written_then_owner_only(deploy):
    assert not gw.profile_has_creds("ben")
    assert not (deploy / "profiles" / "ben.env").exists()
    gw.profile_write("ben", {"TWS_USERID": "b", "TWS_PASSWORD": "pw"})
    path = deploy / "profiles" / "ben.env"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((deploy / "profiles").stat().st_mode) == 0o700
    assert gw.profile_read("ben") == {"TWS_USERID": "b", "TWS_PASSWORD": "pw"}
    assert gw.profile_has_creds("ben") and not gw.profile_has_creds("nic")
    gw.profile_clear("ben")
    assert not path.exists()


def test_profile_name_cannot_escape_the_directory(deploy):
    for bad in ("../x", "", "Nic", "a/b"):
        with pytest.raises(ValueError):
            gw.profile_path(bad)


def test_activate_writes_env_and_reports_person_switch(deploy):
    gw.profile_write("nic", {"TWS_USERID": "n", "TWS_PASSWORD": "np",
                             "TOTP_SECRET": "JBSWY3DP"})
    gw.profile_write("ben", {"TWS_USERID": "b", "TWS_PASSWORD": "bp"})
    assert gw.activate_profile("nic") is False      # nobody before
    assert gw.env_value("TWS_USERID") == "n" and gw.env_value("TOTP_SECRET")
    assert gw.activate_profile("nic") is False      # same person
    assert gw.activate_profile("ben") is True       # switch
    assert gw.env_value("TWS_USERID") == "b"
    assert gw.env_value("TOTP_SECRET") is None      # nic's secret not carried
    assert gw.active_profile() == "ben"


def test_start_login_needs_a_known_profile_with_creds(deploy, monkeypatch):
    started = []
    monkeypatch.setattr(webui, "login_worker", lambda s=False: started.append(s))
    assert webui.start_login("mallory") == "pick who is logging in"
    assert webui.start_login("ben") == "credentials first"
    assert not (deploy / ".env").exists()


def test_switch_drops_the_jts_volume_only_on_switch(deploy, monkeypatch):
    calls = []
    monkeypatch.setattr(webui.gw, "compose", lambda *a: calls.append(a) or "")
    monkeypatch.setattr(webui.gw, "throttle_age", lambda: 0)   # stop early
    webui.login_worker(switched=False)
    assert ("down", "-v") not in calls
    calls.clear()
    webui.login_worker(switched=True)
    assert calls[0] == ("down", "-v")


def test_recipients_list():
    assert watchdog.recipients({}) == ["nic@atab.ai"]
    assert watchdog.recipients(
        {"ALERT_TO": "nic@atab.ai, client@example.com,"}) == [
        "nic@atab.ai", "client@example.com"]


def test_down_email_wording():
    assert "whoever is free" in watchdog.SUBJECT_DOWN
    body = watchdog.down_body("https://x/ibkr/login/",
                              datetime(2026, 9, 18, tzinfo=timezone.utc))
    assert "https://x/ibkr/login/" in body
    assert "(the page password Nic sent you)" in body
    assert "pick your name, type your code" in body.lower()


def test_auto_login_picks_any_profile_with_totp_active_first(deploy):
    assert watchdog.totp_profile(gw) is None
    gw.profile_write("ben", {"TWS_USERID": "b", "TWS_PASSWORD": "bp",
                             "TOTP_SECRET": "JBSWY3DP"})
    assert watchdog.totp_profile(gw) == "ben"
    gw.profile_write("nic", {"TWS_USERID": "n", "TWS_PASSWORD": "np",
                             "TOTP_SECRET": "JBSWY3DP"})
    gw.activate_profile("nic")
    assert watchdog.totp_profile(gw) == "nic"
