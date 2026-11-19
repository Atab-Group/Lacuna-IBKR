"""Tests for the optional price-alert tools in pricealerts.py.

Each test writes an engine's files into a temporary directory (see
pricealert_fixture.py) and calls ``pricealerts.call`` the way the MCP server does.
The backtest runs through a fake runner, except one case that runs a tiny local
script. Nothing opens a gateway connection or the network, and this file needs
only the standard library and pytest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

import pricealerts as pa
from pricealert_fixture import (ALERT_ROWS, EMAIL_ONE, FIGURES_A1, NEWEST, NOW, OLD_MINUTE,
                                PHONE, RULES, WATCH_ONE, WATCH_TWO, assert_no_recipient, build)


@pytest.fixture()
def cfg(tmp_path):
    return build(tmp_path)


def call(cfg, name, args=None, **kw):
    return pa.call(name, args or {}, cfg, NOW, **kw)


def error_of(cfg, name, args=None, **kw):
    with pytest.raises(pa.ToolError) as caught:
        call(cfg, name, args, **kw)
    return caught.value


def rules_on_disk(cfg):
    return json.loads(cfg.rules_path.read_text())


# ------------------------------------------------------------ the switch ----

def test_enabled_only_when_the_state_directory_is_named():
    assert pa.enabled({}) is False
    assert pa.enabled({"LACUNA_PRICE_ALERTS_DIR": ""}) is False
    assert pa.enabled({"LACUNA_PRICE_ALERTS_DIR": "   "}) is False
    assert pa.enabled({"LACUNA_PRICE_ALERTS_DIR": "/srv/example/state"}) is True


def test_config_reads_every_env_name():
    cfg = pa.Config.from_env({"LACUNA_PRICE_ALERTS_DIR": "/srv/example/state",
                              "LACUNA_PRICE_ALERTS_RULES": "/srv/example/rules.json",
                              "LACUNA_PRICE_ALERTS_BACKTEST": "/srv/example/backtest.py",
                              "LACUNA_PRICE_ALERTS_PYTHON": "/srv/example/python"},
                             ibkr_root="/store", ibkr_dir="/code")
    assert str(cfg.tape_path) == "/srv/example/state/tape.db"
    assert str(cfg.heartbeat_path) == "/srv/example/state/heartbeat.json"
    assert str(cfg.rules_path) == "/srv/example/rules.json"
    assert str(cfg.lock_path) == "/srv/example/rules.json.lock"
    assert str(cfg.backtest_path) == "/srv/example/backtest.py"
    assert cfg.python == "/srv/example/python"
    assert (cfg.ibkr_root, cfg.ibkr_dir) == ("/store", "/code")


def test_config_defaults_follow_the_engine_contract():
    cfg = pa.Config.from_env({"LACUNA_PRICE_ALERTS_DIR": "/srv/example/state"})
    home = os.path.expanduser("~")
    assert str(cfg.rules_path) == f"{home}/.config/lacuna-ibkr/price-alerts.json"
    assert str(cfg.backtest_path) == f"{home}/price-alerts/services/price-alerts/backtest.py"
    assert cfg.python == sys.executable


# -------------------------------------------------------------- the tape ----

def test_live_tape_returns_the_window_with_the_move_reading(cfg):
    out = call(cfg, pa.TOOL_LIVE_TAPE, {"watch": "example-watch", "minutes": 3})

    assert out["watch"] == "example-watch"
    assert out["newest_minute"] == NEWEST
    assert out["newest_minute_utc"] == "2026-11-19T14:52:00Z"
    assert out["age_seconds"] == 20             # NOW - (NEWEST + 60)
    assert out["heartbeat_age_seconds"] == 10
    assert out["live"] is True
    assert out["market_fresh"] is True
    assert out["n_returned"] == 3
    assert out["truncated"] is True              # the 13:52 row lies before the window
    assert out["cap"] == 390
    assert [r["minute"] for r in out["rows"]] == [1795099800, 1795099860, NEWEST]
    assert out["rows"][0] == {
        "minute": 1795099800, "last": 100.0, "bid": 99.99, "ask": 100.01, "mid": 100.0,
        "spread_pct": 0.02, "volume": 1200.0, "trades": 50, "dollars": 120000.0,
        "day_volume": 80000.0, "data_type": 1, "source": "stream",
        "time_utc": "2026-11-19T14:50:00Z", "move": None}
    assert out["rows"][2]["move"] == {
        "verdict": "early", "reason": None, "held": 0, "move_pct": 0.9, "volume_x": 4.5,
        "window_volume": 10200.0, "window_trades": 290, "window_dollars": 1026600.0,
        "market_pct": 0.1, "relative_pct": 0.8, "market_fresh": 1,
        "price_pass": 1, "volume_pass": 1, "market_pass": 1}


def test_live_tape_default_window_is_thirty_minutes(cfg):
    out = call(cfg, pa.TOOL_LIVE_TAPE, {"watch": "example-watch"})
    assert out["n_returned"] == 3
    assert out["truncated"] is True


def test_live_tape_widest_window_reaches_the_older_row(cfg):
    out = call(cfg, pa.TOOL_LIVE_TAPE, {"watch": "example-watch", "minutes": 390})
    assert [r["minute"] for r in out["rows"]] == [OLD_MINUTE, 1795099800, 1795099860, NEWEST]
    assert out["truncated"] is False


@pytest.mark.parametrize("minutes", [0, 391, -5, "30", True, 2.5])
def test_live_tape_refuses_a_bad_minutes(cfg, minutes):
    err = error_of(cfg, pa.TOOL_LIVE_TAPE, {"watch": "example-watch", "minutes": minutes})
    assert err.code == "bad_argument"
    assert err.extra["path"] == "minutes"


@pytest.mark.parametrize("watch", [None, "", "Example-Watch", "ref:SPY", 7])
def test_live_tape_refuses_a_bad_watch_id(cfg, watch):
    err = error_of(cfg, pa.TOOL_LIVE_TAPE, {"watch": watch})
    assert err.code == "bad_argument"
    assert err.extra["path"] == "watch"


def test_live_tape_names_an_unknown_watch(cfg):
    err = error_of(cfg, pa.TOOL_LIVE_TAPE, {"watch": "no-such-watch"})
    assert err.code == "bad_argument"
    assert err.extra["known_watches"] == ["example-watch", "example-adr"]


def test_live_tape_for_a_known_watch_with_no_rows(cfg):
    out = call(cfg, pa.TOOL_LIVE_TAPE, {"watch": "example-adr"})
    assert out["rows"] == []
    assert out["newest_minute"] is None
    assert out["n_returned"] == 0
    assert out["truncated"] is False
    assert out["live"] is False                  # its last tick is 1000 seconds old


# ---------------------------------------------------------------- health ----

def heartbeat_with(**instrument):
    base = {"last_tick_ts": NOW - 50, "data_type": 1, "in_hours": True}
    base.update(instrument)
    return {"instruments": {"w": base}, "references": {"w": {"fresh": False}}}


def test_live_needs_a_live_feed_in_hours_with_a_recent_tick():
    assert pa.watch_health(heartbeat_with(), "w", NOW) == {
        "live": True, "data_type": 1, "last_tick_age_seconds": 50, "in_hours": True,
        "market_fresh": False}


@pytest.mark.parametrize("change", [{"data_type": 3}, {"in_hours": False},
                                    {"last_tick_ts": NOW - 181}, {"last_tick_ts": None}])
def test_live_is_false_otherwise(change):
    assert pa.watch_health(heartbeat_with(**change), "w", NOW)["live"] is False


def test_live_at_exactly_180_seconds():
    assert pa.watch_health(heartbeat_with(last_tick_ts=NOW - 180), "w", NOW)["live"] is True


def test_a_watch_missing_from_the_heartbeat_is_not_live():
    assert pa.watch_health({}, "w", NOW) == {
        "live": False, "data_type": None, "last_tick_age_seconds": None, "in_hours": None,
        "market_fresh": None}


# --------------------------------------------------------------- listing ----

def test_list_returns_each_watch_with_health_and_last_alert(cfg):
    out = call(cfg, pa.TOOL_LIST)

    assert out["recipients"] == {"email": 2, "whatsapp": 1}
    assert out["cooldown_min"] == 15
    assert out["engine"] == {"heartbeat_age_seconds": 10, "dry_run": True,
                             "gateway_connected": True, "rules_ok": True,
                             "deployed": "abc1234"}
    first, second = out["watches"]
    assert first == {
        "id": "example-watch", "label": "Example stock", "subject": "example-subject",
        "enabled": True, "context": "An example stock on Nasdaq.",
        "contract": WATCH_ONE["contract"], "hours": WATCH_ONE["hours"],
        "tiers": WATCH_ONE["tiers"], "volume": WATCH_ONE["volume"],
        "price": WATCH_ONE["price"], "gap": None, "market_kind": "nasdaq_quote",
        "last_alert": {"minute": NEWEST, "time_utc": "2026-11-19T14:52:00Z", "tier": "early",
                       "direction": "up",
                       "subject": "[PRICE] EARLY: AAPL +0.9% in 3 min, volume 4x (example)"},
        "health": {"live": True, "data_type": 1, "last_tick_age_seconds": 50,
                   "in_hours": True, "market_fresh": True},
    }
    assert second["id"] == "example-adr"
    assert second["gap"] == WATCH_TWO["gap"]
    assert second["market_kind"] == "ibkr_basket"
    assert second["last_alert"]["minute"] == 1794499980
    assert second["health"] == {"live": False, "data_type": 1, "last_tick_age_seconds": 1000,
                                "in_hours": True, "market_fresh": False}


def test_list_never_returns_a_recipient(cfg):
    out = call(cfg, pa.TOOL_LIST)
    assert_no_recipient(out)
    assert "rules_error" not in json.dumps(out)


# --------------------------------------------------------------- history ----

def test_history_defaults_to_the_last_seven_days_newest_first(cfg):
    out = call(cfg, pa.TOOL_HISTORY)
    assert [a["key"] for a in out["alerts"]] == [row[0] for row in ALERT_ROWS[:3]]
    assert out["n_returned"] == 3
    assert out["truncated"] is False
    assert out["cap"] == 200
    assert out["limit"] == 50
    assert out["window"] == {"start": "2026-11-12T14:53:20Z", "end": "2026-11-19T14:53:20Z",
                             "watch": None}
    first = out["alerts"][0]
    assert first["figures"] == FIGURES_A1
    assert first["status"] == "dry_run"
    assert first["time_utc"] == "2026-11-19T14:52:00Z"
    assert first["market_check"] == "on"


def test_history_keeps_an_unparseable_figures_string_and_says_so(cfg):
    out = call(cfg, pa.TOOL_HISTORY, {"watch": "example-adr"})
    assert [a["minute"] for a in out["alerts"]] == [1794499980]
    assert out["alerts"][0]["figures"] == "not json"
    assert out["alerts"][0]["figures_unparsed"] is True


def test_history_by_iso_dates_covers_whole_days(cfg):
    out = call(cfg, pa.TOOL_HISTORY, {"start": "2026-11-19", "end": "2026-11-19"})
    assert [a["minute"] for a in out["alerts"]] == [NEWEST, 1795099800]
    assert out["window"]["end"] == "2026-11-20T00:00:00Z"


def test_history_by_epoch_and_timestamp(cfg):
    out = call(cfg, pa.TOOL_HISTORY, {"start": 1793000000, "end": "2026-11-12T00:00:00Z"})
    assert [a["minute"] for a in out["alerts"]] == [1794000000]


def test_history_states_the_cut(cfg):
    out = call(cfg, pa.TOOL_HISTORY, {"limit": 2})
    assert out["n_returned"] == 2
    assert out["truncated"] is True
    assert out["cap"] == 200


@pytest.mark.parametrize("args,path", [
    ({"limit": 0}, "limit"), ({"limit": 201}, "limit"), ({"limit": "5"}, "limit"),
    ({"start": "yesterday"}, "start"), ({"end": "2026-02-30"}, "end"),
    ({"start": "2026-11-19", "end": "2026-11-18"}, "end"), ({"watch": "Bad Id"}, "watch"),
])
def test_history_refuses_bad_arguments(cfg, args, path):
    err = error_of(cfg, pa.TOOL_HISTORY, args)
    assert err.code == "bad_argument"
    assert err.extra["path"] == path


def test_history_scrubs_a_recipient_inside_a_subject_or_figures(cfg):
    out = call(cfg, pa.TOOL_HISTORY)
    second = out["alerts"][1]
    assert second["subject"] == "[PRICE] FIRM: AAPL +2.0% in 3 min, volume 9x (copy for [recipient])"
    assert second["figures"] == {"direction": "up", "note": "also to [recipient]"}
    assert_no_recipient(out)


# ------------------------------------------------------- missing files ----

@pytest.mark.parametrize("tool,args", [
    (pa.TOOL_LIVE_TAPE, {"watch": "example-watch"}), (pa.TOOL_LIST, {}),
    (pa.TOOL_HISTORY, {}),
])
def test_a_missing_tape_database_is_unavailable(cfg, tool, args):
    cfg.tape_path.unlink()
    err = error_of(cfg, tool, args)
    assert err.code == "price_alerts_unavailable"
    assert "tape.db" in err.message
    assert "operator" in err.next_action


def test_a_corrupt_tape_database_is_unavailable(cfg):
    for suffix in ("", "-wal", "-shm"):
        path = cfg.tape_path.with_name("tape.db" + suffix)
        if path.exists():
            path.unlink()
    cfg.tape_path.write_bytes(b"this is not a sqlite file" * 100)
    err = error_of(cfg, pa.TOOL_HISTORY)
    assert err.code == "price_alerts_unavailable"


@pytest.mark.parametrize("tool,args", [
    (pa.TOOL_LIVE_TAPE, {"watch": "example-watch"}), (pa.TOOL_LIST, {}),
])
def test_a_missing_heartbeat_is_unavailable(cfg, tool, args):
    cfg.heartbeat_path.unlink()
    err = error_of(cfg, tool, args)
    assert err.code == "price_alerts_unavailable"
    assert "heartbeat" in err.message


@pytest.mark.parametrize("text", ["{not json", "[]", '{"watches": [{"label": "no id"}]}'])
def test_an_unreadable_rules_file_is_unavailable(cfg, text):
    cfg.rules_path.write_text(text)
    for tool, args in [(pa.TOOL_LIST, {}),
                       (pa.TOOL_UPDATE, {"watch": "example-watch",
                                         "changes": {"enabled": False}}),
                       (pa.TOOL_TEST, {"date": "2026-03-04"})]:
        err = error_of(cfg, tool, args, can_write=True,
                       runner=lambda *a: pytest.fail("the runner must not start"))
        assert err.code == "price_alerts_unavailable"


def test_a_missing_rules_file_is_unavailable(cfg):
    cfg.rules_path.unlink()
    err = error_of(cfg, pa.TOOL_UPDATE, {"watch": "example-watch",
                                         "changes": {"enabled": False}}, can_write=True)
    assert err.code == "price_alerts_unavailable"


# ---------------------------------------------------------------- update ----

def test_update_changes_the_allowed_fields_and_returns_before_and_after(cfg):
    out = call(cfg, pa.TOOL_UPDATE,
               {"watch": "example-watch",
                "changes": {"tiers": {"early": {"move_pct": 1.0, "require": ["price"]}},
                            "enabled": False}},
               can_write=True)

    assert out["watch"] == "example-watch"
    assert out["changed"] == ["tiers.early.move_pct", "tiers.early.require", "enabled"]
    assert out["before"] == WATCH_ONE
    assert out["after"]["tiers"]["early"] == {"move_pct": 1.0, "volume_x": 4,
                                              "require": ["price"]}
    assert out["after"]["enabled"] is False
    assert out["after"]["tiers"]["firm"] == WATCH_ONE["tiers"]["firm"]

    on_disk = rules_on_disk(cfg)
    assert on_disk["watches"][0] == out["after"]
    assert on_disk["watches"][1] == WATCH_TWO
    assert on_disk["recipients"] == RULES["recipients"]
    assert on_disk["x_top"] == {"kept": True}
    assert on_disk["watches"][0]["x_note"] == "an unknown key the writer must keep"
    assert list(on_disk) == list(RULES)
    assert list(on_disk["watches"][0]) == list(WATCH_ONE)
    assert oct(os.stat(cfg.rules_path).st_mode & 0o777) == "0o640"
    assert_no_recipient(out)


def test_update_adds_an_optional_floor_the_watch_did_not_have(cfg):
    out = call(cfg, pa.TOOL_UPDATE,
               {"watch": "example-watch", "changes": {"volume": {"min_trades": 12}}},
               can_write=True)
    assert out["changed"] == ["volume.min_trades"]
    assert rules_on_disk(cfg)["watches"][0]["volume"] == {
        "window_min": 1, "baseline": "median", "baseline_min": 30, "min_trades": 12}


def test_update_moves_both_tiers_in_one_call(cfg):
    out = call(cfg, pa.TOOL_UPDATE,
               {"watch": "example-adr",
                "changes": {"tiers": {"early": {"move_pct": 3.0}, "firm": {"move_pct": 4.0}},
                            "gap": {"step_pct": 0.5}}},
               can_write=True)
    assert out["changed"] == ["tiers.early.move_pct", "tiers.firm.move_pct", "gap.step_pct"]


def test_a_no_op_update_leaves_the_file_untouched(cfg):
    before = os.stat(cfg.rules_path)
    text = cfg.rules_path.read_text()
    out = call(cfg, pa.TOOL_UPDATE,
               {"watch": "example-watch",
                "changes": {"tiers": {"early": {"move_pct": 0.75, "volume_x": 4.0}},
                            "enabled": True}},
               can_write=True)
    assert out["changed"] == []
    assert out["before"] == out["after"] == WATCH_ONE
    after = os.stat(cfg.rules_path)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert cfg.rules_path.read_text() == text


@pytest.mark.parametrize("watch,changes,path", [
    ("example-watch", {"recipients": {"email": ["x"]}}, "recipients"),
    ("example-watch", {"cooldown_min": 5}, "cooldown_min"),
    ("example-watch", {"contract": {"symbol": "MSFT"}}, "contract"),
    ("example-watch", {"hours": {"open": "10:00"}}, "hours"),
    ("example-watch", {"subject": "other"}, "subject"),
    ("example-watch", {"id": "renamed"}, "id"),
    ("example-watch", {"tiers": "loose"}, "tiers"),
    ("example-watch", {"tiers": {"middle": {"move_pct": 1.0}}}, "tiers.middle"),
    ("example-watch", {"tiers": {"early": {"lookback": 3}}}, "tiers.early.lookback"),
    ("example-watch", {"price": {"source": "mid"}}, "price.source"),
    ("example-watch", {"volume": {"window_min": 3}}, "volume.window_min"),
    ("example-adr", {"gap": {"against": "example-watch"}}, "gap.against"),
    ("example-watch", {"tiers": {"early": {"move_pct": 0.01}}}, "tiers.early.move_pct"),
    ("example-watch", {"tiers": {"firm": {"move_pct": 20.5}}}, "tiers.firm.move_pct"),
    ("example-watch", {"tiers": {"early": {"volume_x": 0.5}}}, "tiers.early.volume_x"),
    ("example-watch", {"tiers": {"firm": {"volume_x": 101}}}, "tiers.firm.volume_x"),
    ("example-watch", {"volume": {"min_trades": 1001}}, "volume.min_trades"),
    ("example-watch", {"volume": {"min_trades": 2.5}}, "volume.min_trades"),
    ("example-watch", {"volume": {"min_dollars": -1}}, "volume.min_dollars"),
    ("example-watch", {"volume": {"min_dollars": 100000001}}, "volume.min_dollars"),
    ("example-watch", {"price": {"max_spread_pct": 0.01}}, "price.max_spread_pct"),
    ("example-watch", {"price": {"max_spread_pct": 11}}, "price.max_spread_pct"),
    ("example-adr", {"gap": {"move_pct": 25}}, "gap.move_pct"),
    ("example-adr", {"gap": {"step_pct": 0.05}}, "gap.step_pct"),
    ("example-adr", {"gap": {"rearm_pct": 10.5}}, "gap.rearm_pct"),
    ("example-watch", {"tiers": {"early": {"move_pct": "1.0"}}}, "tiers.early.move_pct"),
    ("example-watch", {"tiers": {"early": {"move_pct": True}}}, "tiers.early.move_pct"),
    ("example-watch", {"tiers": {"early": {"move_pct": None}}}, "tiers.early.move_pct"),
    ("example-watch", {"tiers": {"early": {"move_pct": float("nan")}}}, "tiers.early.move_pct"),
    ("example-watch", {"tiers": {"early": {"require": []}}}, "tiers.early.require"),
    ("example-watch", {"tiers": {"early": {"require": ["price", "news"]}}},
     "tiers.early.require"),
    ("example-watch", {"tiers": {"early": {"require": ["price", "price"]}}},
     "tiers.early.require"),
    ("example-watch", {"tiers": {"firm": {"require": "price"}}}, "tiers.firm.require"),
    ("example-watch", {"enabled": "yes"}, "enabled"),
    ("example-watch", {"enabled": 0}, "enabled"),
    ("example-watch", {"tiers": {"early": {"move_pct": 1.6}}}, "tiers.early.move_pct"),
    ("example-watch", {"tiers": {"firm": {"volume_x": 3}}}, "tiers.early.volume_x"),
    ("example-watch", {"gap": {"move_pct": 1.0}}, "gap.move_pct"),
    ("example-watch", {}, "changes"),
    ("example-watch", None, "changes"),
    ("example-watch", ["enabled"], "changes"),
    ("no-such-watch", {"enabled": False}, "watch"),
])
def test_update_rejects_and_names_the_path(cfg, watch, changes, path):
    text = cfg.rules_path.read_text()
    err = error_of(cfg, pa.TOOL_UPDATE, {"watch": watch, "changes": changes}, can_write=True)
    assert err.code == "bad_argument"
    assert err.extra["path"] == path
    assert path in err.message
    assert cfg.rules_path.read_text() == text


# ----------------------------------------------------------- write access ----

def test_a_read_connection_cannot_update(cfg):
    text = cfg.rules_path.read_text()
    err = error_of(cfg, pa.TOOL_UPDATE,
                   {"watch": "example-watch", "changes": {"enabled": False}}, can_write=False)
    assert err.code == "write_not_allowed"
    assert "write token" in err.next_action
    assert "operator" in err.next_action
    assert cfg.rules_path.read_text() == text


def test_write_is_refused_before_the_arguments_are_read(cfg):
    err = error_of(cfg, pa.TOOL_UPDATE, {"watch": "Not A Watch"}, can_write=False)
    assert err.code == "write_not_allowed"


def test_stdio_write_needs_the_operator_switch(cfg):
    args = {"watch": "example-watch", "changes": {"enabled": False}}
    assert error_of(cfg, pa.TOOL_UPDATE, args, can_write=None, env={}).code == "write_not_allowed"
    assert error_of(cfg, pa.TOOL_UPDATE, args, can_write=None,
                    env={"LACUNA_PRICE_ALERTS_STDIO_WRITE": "true"}).code == "write_not_allowed"
    out = call(cfg, pa.TOOL_UPDATE, args, can_write=None,
               env={"LACUNA_PRICE_ALERTS_STDIO_WRITE": "1"})
    assert out["changed"] == ["enabled"]


def test_a_token_flag_wins_over_the_stdio_switch(cfg):
    args = {"watch": "example-watch", "changes": {"enabled": False}}
    err = error_of(cfg, pa.TOOL_UPDATE, args, can_write=False,
                   env={"LACUNA_PRICE_ALERTS_STDIO_WRITE": "1"})
    assert err.code == "write_not_allowed"


# --------------------------------------------------- concurrent and atomic ----

def test_concurrent_updates_through_the_lock_all_land(cfg, monkeypatch):
    """Eight writers at once, each changing a different field. Every change survives.

    The write is slowed down so each writer would read the file before the
    others wrote, were the lock missing.
    """
    real_write = pa._atomic_write

    def slow_write(path, text, **kw):
        time.sleep(0.05)
        real_write(path, text, **kw)

    monkeypatch.setattr(pa, "_atomic_write", slow_write)
    changes = [
        {"tiers": {"early": {"move_pct": 0.8}}}, {"tiers": {"early": {"volume_x": 5}}},
        {"tiers": {"firm": {"move_pct": 1.9}}}, {"tiers": {"firm": {"volume_x": 9}}},
        {"volume": {"min_trades": 7}}, {"volume": {"min_dollars": 5000}},
        {"price": {"max_spread_pct": 0.5}}, {"enabled": False},
    ]
    start = threading.Barrier(len(changes))
    errors = []

    def writer(change):
        start.wait()
        try:
            call(cfg, pa.TOOL_UPDATE, {"watch": "example-watch", "changes": change},
                 can_write=True)
        except Exception as exc:     # surfaced below, so a failure names itself
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(c,)) for c in changes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert errors == []
    watch = rules_on_disk(cfg)["watches"][0]
    assert watch["tiers"]["early"]["move_pct"] == 0.8
    assert watch["tiers"]["early"]["volume_x"] == 5
    assert watch["tiers"]["firm"]["move_pct"] == 1.9
    assert watch["tiers"]["firm"]["volume_x"] == 9
    assert watch["volume"]["min_trades"] == 7
    assert watch["volume"]["min_dollars"] == 5000
    assert watch["price"]["max_spread_pct"] == 0.5
    assert watch["enabled"] is False


def test_a_failed_rename_leaves_the_old_file_and_no_temp_file(cfg, monkeypatch):
    text = cfg.rules_path.read_text()

    def broken_replace(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pa.os, "replace", broken_replace)
    err = error_of(cfg, pa.TOOL_UPDATE,
                   {"watch": "example-watch", "changes": {"enabled": False}}, can_write=True)
    assert err.code == "write_failed"
    assert cfg.rules_path.read_text() == text
    leftovers = sorted(p.name for p in cfg.rules_path.parent.iterdir())
    assert leftovers == ["price-alerts.json", "price-alerts.json.lock"]


def test_a_successful_write_leaves_one_valid_file(cfg):
    call(cfg, pa.TOOL_UPDATE, {"watch": "example-watch", "changes": {"enabled": False}},
         can_write=True)
    leftovers = sorted(p.name for p in cfg.rules_path.parent.iterdir())
    assert leftovers == ["price-alerts.json", "price-alerts.json.lock"]
    assert rules_on_disk(cfg)["watches"][0]["enabled"] is False


# -------------------------------------------------------------- backtest ----

class FakeRunner:
    def __init__(self, code=0, stdout='{"date": "2026-03-04", "watches": []}', stderr="",
                 raises=None):
        self.code, self.stdout, self.stderr, self.raises = code, stdout, stderr, raises
        self.calls = []

    def __call__(self, argv, env, timeout):
        self.calls.append((argv, env, timeout))
        if self.raises:
            raise self.raises
        return self.code, self.stdout, self.stderr


def test_backtest_argument_vector_environment_and_timeout(cfg):
    runner = FakeRunner(stdout=json.dumps({
        "date": "2026-03-04",
        "watches": [{"id": "example-watch", "bars": 390, "market_bars": 390,
                     "market_available": True, "notes": [], "alerts": []}]}))
    overrides = {"example-watch": {"tiers": {"early": {"move_pct": 1.0}}}}
    out = call(cfg, pa.TOOL_TEST,
               {"date": "2026-03-04", "watch": "example-watch", "overrides": overrides},
               runner=runner)

    assert out == {"date": "2026-03-04",
                   "watches": [{"id": "example-watch", "bars": 390, "market_bars": 390,
                                "market_available": True, "notes": [], "alerts": []}]}
    [(argv, env, timeout)] = runner.calls
    assert argv == ["/opt/example/bin/python", str(cfg.backtest_path), "--json",
                    "--date", "2026-03-04", "--watch", "example-watch",
                    "--overrides", '{"example-watch":{"tiers":{"early":{"move_pct":1.0}}}}']
    assert timeout == 30
    assert env["PRICE_ALERTS_RULES"] == str(cfg.rules_path)
    assert env["PRICE_ALERTS_STATE_DIR"] == str(cfg.state_dir)
    assert env["LACUNA_IBKR_ROOT"] == "/example/store"
    assert env["LACUNA_IBKR_DIR"] == "/example/ibkr-data"
    assert env["PATH"] == os.environ["PATH"]


def test_backtest_without_watch_or_overrides(cfg):
    runner = FakeRunner()
    call(cfg, pa.TOOL_TEST, {"date": "2026-03-04"}, runner=runner)
    assert runner.calls[0][0] == ["/opt/example/bin/python", str(cfg.backtest_path),
                                  "--json", "--date", "2026-03-04"]


def test_backtest_exit_two_carries_the_scripts_message(cfg):
    runner = FakeRunner(code=2, stdout='{"error": "no bar store at /example/store"}')
    err = error_of(cfg, pa.TOOL_TEST, {"date": "2026-03-04"}, runner=runner)
    assert err.code == "backtest_failed"
    assert "no bar store at /example/store" in err.message


def test_backtest_timeout_is_an_error(cfg):
    runner = FakeRunner(raises=subprocess.TimeoutExpired(["python"], 30))
    err = error_of(cfg, pa.TOOL_TEST, {"date": "2026-03-04"}, runner=runner)
    assert err.code == "backtest_timeout"
    assert err.extra["timeout_seconds"] == 30


@pytest.mark.parametrize("code,stdout", [(0, "not json"), (0, "[1, 2]"), (1, ""),
                                         (2, "Traceback")])
def test_backtest_unreadable_output_is_an_error(cfg, code, stdout):
    runner = FakeRunner(code=code, stdout=stdout, stderr="boom")
    err = error_of(cfg, pa.TOOL_TEST, {"date": "2026-03-04"}, runner=runner)
    assert err.code == "backtest_failed"
    assert err.extra["exit_code"] == code
    assert err.extra["stderr_tail"] == "boom"


@pytest.mark.parametrize("day", ["2026-3-4", "2026-02-30", 20260304, "2026-03-04T00:00",
                                 None, " 2026-03-04"])
def test_backtest_refuses_a_bad_date_before_spawning(cfg, day):
    runner = FakeRunner()
    err = error_of(cfg, pa.TOOL_TEST, {"date": day}, runner=runner)
    assert err.code == "bad_argument"
    assert err.extra["path"] == "date"
    assert runner.calls == []


@pytest.mark.parametrize("overrides,path", [
    ({"example-watch": {"tiers": {"early": {"move_pct": 0.01}}}},
     "overrides.example-watch.tiers.early.move_pct"),
    ({"example-watch": {"contract": {"symbol": "MSFT"}}}, "overrides.example-watch.contract"),
    ({"example-watch": {"tiers": {"early": {"volume_x": 9}}}},
     "overrides.example-watch.tiers.early.volume_x"),
    ({"no-such-watch": {"enabled": False}}, "overrides.no-such-watch.watch"),
    ({}, "overrides"),
    ("tight", "overrides"),
])
def test_backtest_validates_overrides_before_spawning(cfg, overrides, path):
    runner = FakeRunner()
    err = error_of(cfg, pa.TOOL_TEST, {"date": "2026-03-04", "overrides": overrides},
                   runner=runner)
    assert err.code == "bad_argument"
    assert err.extra["path"] == path
    assert runner.calls == []


def test_backtest_names_an_unknown_watch(cfg):
    runner = FakeRunner()
    err = error_of(cfg, pa.TOOL_TEST, {"date": "2026-03-04", "watch": "no-such-watch"},
                   runner=runner)
    assert err.code == "bad_argument"
    assert runner.calls == []


def test_backtest_missing_script_is_unavailable(cfg):
    cfg.backtest_path.unlink()
    err = error_of(cfg, pa.TOOL_TEST, {"date": "2026-03-04"}, runner=FakeRunner())
    assert err.code == "price_alerts_unavailable"


def test_backtest_result_is_scrubbed(cfg):
    runner = FakeRunner(stdout=json.dumps({"date": "2026-03-04",
                                           "watches": [{"notes": [f"sent to {PHONE}"]}]}))
    out = call(cfg, pa.TOOL_TEST, {"date": "2026-03-04"}, runner=runner)
    assert out["watches"][0]["notes"] == ["sent to [recipient]"]


def test_the_real_runner_starts_the_script_without_a_shell(tmp_path):
    cfg = build(tmp_path)
    cfg = pa.Config(state_dir=cfg.state_dir, rules_path=cfg.rules_path,
                    backtest_path=cfg.backtest_path, python=sys.executable,
                    ibkr_root="/example/store", ibkr_dir="/example/ibkr-data")
    out = pa.call(pa.TOOL_TEST, {"date": "2026-03-04", "watch": "example-watch"}, cfg, NOW)
    assert out == {"date": "2026-03-04",
                   "argv": ["--json", "--date", "2026-03-04", "--watch", "example-watch"],
                   "rules": str(cfg.rules_path), "watches": []}


# --------------------------------------------------- no recipient anywhere ----

def test_no_tool_result_carries_a_recipient(cfg):
    runner = FakeRunner(stdout=json.dumps({"date": "2026-03-04",
                                           "watches": [{"notes": [EMAIL_ONE]}]}))
    results = [
        call(cfg, pa.TOOL_LIVE_TAPE, {"watch": "example-watch", "minutes": 390}),
        call(cfg, pa.TOOL_LIST),
        call(cfg, pa.TOOL_HISTORY, {"start": 0, "limit": 200}),
        call(cfg, pa.TOOL_TEST, {"date": "2026-03-04"}, runner=runner),
        call(cfg, pa.TOOL_UPDATE, {"watch": "example-watch",
                                   "changes": {"tiers": {"firm": {"move_pct": 1.6}}}},
             can_write=True),
    ]
    for result in results:
        assert_no_recipient(result)


# ================================================================ review fixes ====
# Each block below names the finding it covers. Every case was seen red on the code
# before the fix, except the finding 6 checks, which confirm behaviour that held.

import copy
import dataclasses
import sqlite3

from pricealert_fixture import EMAIL_TWO, HEARTBEAT

WATCH_PLAIN = {
    "id": "example-plain",
    "label": "Example stock with no market check",
    "subject": "example-subject",
    "enabled": True,
    "contract": {"symbol": "NVDA", "sec_type": "STK", "exchange": "SMART", "currency": "USD"},
    "hours": {"tz": "America/New_York", "open": "09:30", "close": "16:00"},
    "price": {"source": "last", "lookback_min": 3},
    "volume": {"window_min": 1, "baseline": "median", "baseline_min": 30},
    "tiers": {
        "early": {"move_pct": 0.75, "volume_x": 4, "require": ["price", "volume"]},
        "firm": {"move_pct": 1.5, "volume_x": 8, "require": ["price", "volume"]},
    },
}


def rules_with(*watches, **top):
    rules = copy.deepcopy(RULES)
    rules["watches"] = [copy.deepcopy(w) for w in watches] or rules["watches"]
    rules.update(top)
    return rules


@pytest.fixture()
def cfg3(tmp_path):
    """The two fixture watches plus one with no market object."""
    return build(tmp_path, rules=rules_with(WATCH_ONE, WATCH_TWO, WATCH_PLAIN))


def leftovers(cfg):
    return sorted(p.name for p in cfg.rules_path.parent.iterdir())


# ------------------------------------------- finding 1: cross-field rules ----

@pytest.mark.parametrize("watch,changes,path", [
    ("example-adr", {"gap": {"rearm_pct": 2.0}}, "gap.rearm_pct"),
    ("example-adr", {"gap": {"move_pct": 0.5}}, "gap.rearm_pct"),
    ("example-watch", {"tiers": {"early": {"require": ["market"]}}}, "tiers.early.require"),
    ("example-watch", {"tiers": {"firm": {"require": ["market"]}}}, "tiers.firm.require"),
    ("example-plain", {"tiers": {"early": {"require": ["price", "market"]}}},
     "tiers.early.require"),
    ("example-plain", {"tiers": {"firm": {"require": ["market"]}}}, "tiers.firm.require"),
    ("example-plain", {"gap": {"step_pct": 0.5}}, "gap.step_pct"),
])
def test_update_refuses_a_cross_field_break(cfg3, watch, changes, path):
    text = cfg3.rules_path.read_text()
    err = error_of(cfg3, pa.TOOL_UPDATE, {"watch": watch, "changes": changes}, can_write=True)
    assert err.code == "bad_argument"
    assert err.extra["path"] == path
    assert cfg3.rules_path.read_text() == text


def test_update_refuses_when_an_untouched_tier_already_breaks_a_rule(tmp_path):
    plain = copy.deepcopy(WATCH_PLAIN)
    plain["tiers"]["firm"]["require"] = ["market"]
    cfg = build(tmp_path, rules=rules_with(WATCH_ONE, plain))
    text = cfg.rules_path.read_text()
    err = error_of(cfg, pa.TOOL_UPDATE,
                   {"watch": "example-plain", "changes": {"enabled": False}}, can_write=True)
    assert err.code == "bad_argument"
    assert err.extra["path"] == "tiers.firm.require"
    assert cfg.rules_path.read_text() == text


@pytest.mark.parametrize("changes,expected", [
    ({"price": {"max_spread_pct": 0.5}},
     {"source": "last", "lookback_min": 3, "max_spread_pct": 0.5}),
    ({"volume": {"min_dollars": 1000}},
     {"window_min": 1, "baseline": "median", "baseline_min": 30, "min_dollars": 1000}),
])
def test_update_adds_a_floor_or_filter_that_was_absent(cfg3, changes, expected):
    out = call(cfg3, pa.TOOL_UPDATE, {"watch": "example-plain", "changes": changes},
               can_write=True)
    section = next(iter(changes))
    assert out["after"][section] == expected
    assert rules_on_disk(cfg3)["watches"][2][section] == expected


def test_update_accepts_require_with_market_when_the_watch_has_a_market(cfg3):
    out = call(cfg3, pa.TOOL_UPDATE,
               {"watch": "example-watch",
                "changes": {"tiers": {"early": {"require": ["volume", "market"]}}}},
               can_write=True)
    assert out["changed"] == ["tiers.early.require"]


@pytest.mark.parametrize("overrides,path", [
    ({"example-adr": {"gap": {"rearm_pct": 2.0}}}, "overrides.example-adr.gap.rearm_pct"),
    ({"example-plain": {"tiers": {"early": {"require": ["market"]}}}},
     "overrides.example-plain.tiers.early.require"),
    ({"example-watch": {"tiers": {"firm": {"require": ["market"]}}}},
     "overrides.example-watch.tiers.firm.require"),
])
def test_backtest_overrides_obey_the_cross_field_rules(cfg3, overrides, path):
    runner = FakeRunner()
    err = error_of(cfg3, pa.TOOL_TEST, {"date": "2026-03-04", "overrides": overrides},
                   runner=runner)
    assert err.code == "bad_argument"
    assert err.extra["path"] == path
    assert runner.calls == []


# ---------------------------- finding 1: the whole-file safety net, local ----

def test_the_fixture_rules_pass_the_local_check():
    pa.check_rules(copy.deepcopy(RULES))
    pa.check_rules(rules_with(WATCH_ONE, WATCH_TWO, WATCH_PLAIN))


def _set(rules, dotted, value):
    node = rules
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    if value is _DROP:
        del node[parts[-1]]
    else:
        node[parts[-1]] = value


_DROP = object()


@pytest.mark.parametrize("dotted,value,field", [
    ("v", 2, "v"),
    ("cooldown_min", 0, "cooldown_min"),
    ("cooldown_min", _DROP, "cooldown_min"),
    ("recipients.email", ["not-an-address"], "recipients.email"),
    ("recipients.whatsapp", ["+1 555"], "recipients.whatsapp"),
    ("watches.0.id", "Bad Id", "watches.0.id"),
    ("watches.1.id", "example-watch", "watches.1.id"),
    ("watches.0.subject", "", "watches.example-watch.subject"),
    ("watches.0.enabled", "yes", "watches.example-watch.enabled"),
    ("watches.0.no_tick_alarm_min", 121, "watches.example-watch.no_tick_alarm_min"),
    ("watches.0.contract", {"exchange": "SMART"}, "watches.example-watch.contract"),
    ("watches.0.hours.tz", "Mars/Base", "watches.example-watch.hours.tz"),
    ("watches.0.hours.open", "9.30", "watches.example-watch.hours.open"),
    ("watches.0.hours.close", "09:00", "watches.example-watch.hours"),
    ("watches.0.price.source", "close", "watches.example-watch.price.source"),
    ("watches.0.price.lookback_min", 31, "watches.example-watch.price.lookback_min"),
    ("watches.0.volume.window_min", 2, "watches.example-watch.volume.window_min"),
    ("watches.0.volume.baseline", "mode", "watches.example-watch.volume.baseline"),
    ("watches.0.volume.baseline_min", 91, "watches.example-watch.volume.baseline_min"),
    ("watches.1.volume.baseline_min", 31, "watches.example-adr.volume.baseline_min"),
    ("watches.0.market.kind", "index", "watches.example-watch.market.kind"),
    ("watches.0.market.symbol", "", "watches.example-watch.market.symbol"),
    ("watches.1.market.min_fresh", 3, "watches.example-adr.market.min_fresh"),
    ("watches.1.market.symbols", [], "watches.example-adr.market.symbols"),
    ("watches.0.tiers.middle", {"move_pct": 1, "volume_x": 4},
     "watches.example-watch.tiers"),
    ("watches.0.tiers.early.move_pct", _DROP, "watches.example-watch.tiers.early.move_pct"),
    ("watches.0.tiers.firm.volume_x", 101, "watches.example-watch.tiers.firm.volume_x"),
    ("watches.1.gap.against", "no-such-watch", "watches.example-adr.gap.against"),
    ("watches.1.gap.against", "example-adr", "watches.example-adr.gap.against"),
    ("watches.1.gap.ratio", 0, "watches.example-adr.gap.ratio"),
    ("watches.1.gap.fx", {}, "watches.example-adr.gap.fx"),
    ("watches.1.price.source", "last", "watches.example-adr.gap"),
])
def test_the_local_check_names_the_field_at_fault(dotted, value, field):
    rules = copy.deepcopy(RULES)
    _set(rules, dotted, value)
    with pytest.raises(pa.ToolError) as caught:
        pa.check_rules(rules)
    assert caught.value.code == "bad_argument"
    assert caught.value.extra["path"] == "rules"
    assert caught.value.extra["field"] == field


@pytest.mark.parametrize("dotted,value", [
    ("watches.0.hours.tz", "Mars/Base"),
    ("watches.1.gap.against", "no-such-watch"),
    ("cooldown_min", 0),
])
def test_update_refuses_to_write_a_file_the_engine_would_ignore(tmp_path, dotted, value):
    rules = copy.deepcopy(RULES)
    _set(rules, dotted, value)
    cfg = build(tmp_path, rules=rules)
    text = cfg.rules_path.read_text()
    err = error_of(cfg, pa.TOOL_UPDATE,
                   {"watch": "example-watch", "changes": {"enabled": False}}, can_write=True)
    assert err.code == "bad_argument"
    assert err.extra["path"] == "rules"
    assert cfg.rules_path.read_text() == text
    assert leftovers(cfg) == ["price-alerts.json", "price-alerts.json.lock"]


# ------------------------- finding 1: the engine's own check, when named ----

class ValidatorRunner(FakeRunner):
    """Records what the temp file held when the validator was asked about it."""

    def __call__(self, argv, env, timeout):
        self.seen = json.loads(open(argv[-1]).read())
        return super().__call__(argv, env, timeout)


@pytest.fixture()
def vcfg(cfg, tmp_path):
    script = tmp_path / "config.py"
    script.write_text("raise SystemExit(0)\n")
    return dataclasses.replace(cfg, validator_path=script)


def test_config_reads_the_validator_env_name():
    cfg = pa.Config.from_env({"LACUNA_PRICE_ALERTS_DIR": "/srv/example/state",
                              "LACUNA_PRICE_ALERTS_VALIDATOR": "/srv/example/config.py"})
    assert str(cfg.validator_path) == "/srv/example/config.py"
    assert pa.Config.from_env({"LACUNA_PRICE_ALERTS_DIR": "/s"}).validator_path is None


def test_the_engine_check_runs_on_the_temp_file_before_the_rename(vcfg):
    runner = ValidatorRunner(code=0, stdout="")
    out = call(vcfg, pa.TOOL_UPDATE,
               {"watch": "example-watch", "changes": {"enabled": False}},
               can_write=True, runner=runner)
    assert out["changed"] == ["enabled"]
    [(argv, env, timeout)] = runner.calls
    assert argv[:3] == ["/opt/example/bin/python", str(vcfg.validator_path), "--check"]
    assert os.path.dirname(argv[3]) == str(vcfg.rules_path.parent)
    assert argv[3] != str(vcfg.rules_path)
    assert timeout == 10
    assert runner.seen["watches"][0]["enabled"] is False
    assert rules_on_disk(vcfg)["watches"][0]["enabled"] is False
    assert leftovers(vcfg) == ["price-alerts.json", "price-alerts.json.lock"]


def test_an_engine_check_refusal_writes_nothing_and_is_scrubbed(vcfg):
    text = vcfg.rules_path.read_text()
    runner = ValidatorRunner(
        code=2, stdout=f"watch example-watch.tiers: bad, sent to {EMAIL_ONE}\nsecond line\n")
    err = error_of(vcfg, pa.TOOL_UPDATE,
                   {"watch": "example-watch", "changes": {"enabled": False}},
                   can_write=True, runner=runner)
    assert err.code == "bad_argument"
    assert err.extra["path"] == "rules"
    assert err.message == ("the engine's own check refused the merged rules: "
                           "watch example-watch.tiers: bad, sent to [recipient]")
    assert vcfg.rules_path.read_text() == text
    assert leftovers(vcfg) == ["price-alerts.json", "price-alerts.json.lock"]


@pytest.mark.parametrize("runner", [
    FakeRunner(code=1, stdout="", stderr="Traceback"),
    FakeRunner(raises=subprocess.TimeoutExpired(["python"], 10)),
    FakeRunner(raises=OSError(2, "No such file")),
])
def test_an_engine_check_that_cannot_answer_writes_nothing(vcfg, runner):
    text = vcfg.rules_path.read_text()
    err = error_of(vcfg, pa.TOOL_UPDATE,
                   {"watch": "example-watch", "changes": {"enabled": False}},
                   can_write=True, runner=runner)
    assert err.code == "validator_failed"
    assert vcfg.rules_path.read_text() == text
    assert leftovers(vcfg) == ["price-alerts.json", "price-alerts.json.lock"]


def test_a_named_validator_that_is_missing_writes_nothing(vcfg):
    vcfg.validator_path.unlink()
    text = vcfg.rules_path.read_text()
    err = error_of(vcfg, pa.TOOL_UPDATE,
                   {"watch": "example-watch", "changes": {"enabled": False}},
                   can_write=True, runner=lambda *a: pytest.fail("the runner must not start"))
    assert err.code == "price_alerts_unavailable"
    assert vcfg.rules_path.read_text() == text


def test_no_validator_named_means_no_subprocess(cfg):
    out = call(cfg, pa.TOOL_UPDATE, {"watch": "example-watch", "changes": {"enabled": False}},
               can_write=True, runner=lambda *a: pytest.fail("the runner must not start"))
    assert out["changed"] == ["enabled"]


def test_the_real_runner_runs_a_validator_script(tmp_path):
    cfg = build(tmp_path)
    script = tmp_path / "check.py"
    script.write_text("import json, sys\n"
                      "rules = json.load(open(sys.argv[2]))\n"
                      "if rules['watches'][0]['enabled'] is False:\n"
                      "    print('watch example-watch.enabled: refused by the example check')\n"
                      "    raise SystemExit(2)\n")
    cfg = dataclasses.replace(cfg, python=sys.executable, validator_path=script)
    err = error_of(cfg, pa.TOOL_UPDATE,
                   {"watch": "example-watch", "changes": {"enabled": False}}, can_write=True)
    assert err.code == "bad_argument"
    assert err.message.endswith("watch example-watch.enabled: refused by the example check")
    out = call(cfg, pa.TOOL_UPDATE,
               {"watch": "example-watch", "changes": {"tiers": {"early": {"move_pct": 0.8}}}},
               can_write=True)
    assert out["changed"] == ["tiers.early.move_pct"]


# --------------------------------------- finding 2: errors are scrubbed too ----

def test_a_backtest_refusal_is_scrubbed(cfg):
    runner = FakeRunner(code=2, stdout=json.dumps({"error": f"cannot send to {EMAIL_ONE}"}))
    err = error_of(cfg, pa.TOOL_TEST, {"date": "2026-03-04"}, runner=runner)
    assert err.code == "backtest_failed"
    assert err.message == "the backtest refused: cannot send to [recipient]"


def test_a_backtest_stderr_tail_is_scrubbed(cfg):
    runner = FakeRunner(code=1, stdout="", stderr=f"Traceback: bad number {PHONE}")
    err = error_of(cfg, pa.TOOL_TEST, {"date": "2026-03-04"}, runner=runner)
    assert err.extra["stderr_tail"] == "Traceback: bad number [recipient]"


def test_a_backtest_start_failure_is_scrubbed(cfg):
    runner = FakeRunner(raises=OSError(2, f"No such file: /x/{EMAIL_TWO}"))
    err = error_of(cfg, pa.TOOL_TEST, {"date": "2026-03-04"}, runner=runner)
    assert err.code == "price_alerts_unavailable"
    assert EMAIL_TWO not in err.message
    assert "[recipient]" in err.message


def test_an_unexpected_exception_leaves_as_a_scrubbed_tool_error(cfg):
    runner = FakeRunner(raises=ValueError(f"odd value for {PHONE}"))
    err = error_of(cfg, pa.TOOL_TEST, {"date": "2026-03-04"}, runner=runner)
    assert err.code == "price_alert_tool_failed"
    assert err.message == "ValueError: odd value for [recipient]"


def test_with_the_rules_unreadable_an_error_carries_no_free_text(cfg):
    cfg.rules_path.write_text(f'{{"recipients": {{"email": ["{EMAIL_ONE}"]}}, "watches": 3')
    err = error_of(cfg, pa.TOOL_LIST)
    assert err.code == "price_alerts_unavailable"
    assert err.message == ("the price-alert engine's files could not be read, and the rules "
                           "file could not be read to check the details for recipient values")
    assert err.extra == {}
    assert "operator" in err.next_action


def test_with_the_rules_unreadable_a_tape_error_carries_no_free_text(cfg):
    cfg.rules_path.unlink()
    cfg.tape_path.unlink()
    err = error_of(cfg, pa.TOOL_HISTORY)
    assert err.code == "price_alerts_unavailable"
    assert "tape.db" not in err.message
    assert err.extra == {}


# ----------------------------------- finding 3: dotted keys are normalised ----

def test_backtest_overrides_with_dotted_keys_are_passed_on_nested(cfg):
    runner = FakeRunner()
    call(cfg, pa.TOOL_TEST,
         {"date": "2026-03-04",
          "overrides": {"example-watch": {"tiers.early.move_pct": 0.5,
                                          "tiers.firm": {"volume_x": 9}}}},
         runner=runner)
    argv = runner.calls[0][0]
    assert argv[argv.index("--overrides") + 1] == (
        '{"example-watch":{"tiers":{"early":{"move_pct":0.5},"firm":{"volume_x":9}}}}')


def test_update_with_dotted_keys_writes_nested_fields(cfg):
    out = call(cfg, pa.TOOL_UPDATE,
               {"watch": "example-watch", "changes": {"tiers.early.move_pct": 0.5}},
               can_write=True)
    assert out["changed"] == ["tiers.early.move_pct"]
    watch = rules_on_disk(cfg)["watches"][0]
    assert watch["tiers"]["early"] == {"move_pct": 0.5, "volume_x": 4,
                                       "require": ["price", "volume", "market"]}
    assert "tiers.early.move_pct" not in watch


@pytest.mark.parametrize("changes", [
    {"tiers.early.move_pct": 0.5, "tiers": {"early": {"move_pct": 0.6}}},
    {"tiers": {"early": {"move_pct": 0.5}}, "tiers.early": {"move_pct": 0.5}},
])
def test_a_dotted_and_a_nested_key_for_one_path_are_refused(cfg, changes):
    text = cfg.rules_path.read_text()
    err = error_of(cfg, pa.TOOL_UPDATE, {"watch": "example-watch", "changes": changes},
                   can_write=True)
    assert err.code == "bad_argument"
    assert err.extra["path"] == "tiers.early.move_pct"
    assert cfg.rules_path.read_text() == text
    runner = FakeRunner()
    err = error_of(cfg, pa.TOOL_TEST,
                   {"date": "2026-03-04", "overrides": {"example-watch": changes}},
                   runner=runner)
    assert err.extra["path"] == "overrides.example-watch.tiers.early.move_pct"
    assert runner.calls == []


# ------------------------------------------ finding 4: a huge integer ----

@pytest.mark.parametrize("changes,path", [
    ({"tiers": {"early": {"move_pct": 10 ** 400}}}, "tiers.early.move_pct"),
    ({"volume": {"min_trades": 10 ** 400}}, "volume.min_trades"),
    ({"volume": {"min_dollars": -(10 ** 400)}}, "volume.min_dollars"),
])
def test_update_refuses_a_huge_integer_by_path(cfg, changes, path):
    err = error_of(cfg, pa.TOOL_UPDATE, {"watch": "example-watch", "changes": changes},
                   can_write=True)
    assert err.code == "bad_argument"
    assert err.extra["path"] == path


@pytest.mark.parametrize("args,path", [
    ({"start": 10 ** 400}, "start"), ({"end": 10 ** 400}, "end"),
    ({"start": float("inf")}, "start"), ({"start": "9" * 400}, "start"),
])
def test_history_refuses_a_huge_epoch_by_path(cfg, args, path):
    err = error_of(cfg, pa.TOOL_HISTORY, args)
    assert err.code == "bad_argument"
    assert err.extra["path"] == path


# ---------------------------------------- finding 5: a symlinked rules file ----

def test_update_through_a_symlink_keeps_the_link_and_changes_the_target(cfg, tmp_path):
    target_dir = tmp_path / "real"
    target_dir.mkdir()
    target = target_dir / "rules.json"
    target.write_text(cfg.rules_path.read_text())
    target.chmod(0o640)
    cfg.rules_path.unlink()
    cfg.rules_path.symlink_to(target)

    call(cfg, pa.TOOL_UPDATE, {"watch": "example-watch", "changes": {"enabled": False}},
         can_write=True)
    assert cfg.rules_path.is_symlink()
    assert os.readlink(cfg.rules_path) == str(target)
    assert json.loads(target.read_text())["watches"][0]["enabled"] is False
    assert oct(os.stat(target).st_mode & 0o777) == "0o640"
    assert sorted(p.name for p in target_dir.iterdir()) == ["rules.json"]


# ------------------------------------ finding 6: the engine adds fields ----

def test_extra_heartbeat_fields_and_tape_columns_are_tolerated(tmp_path):
    heartbeat = copy.deepcopy(HEARTBEAT)
    heartbeat["started_ts"] = 1795000000
    heartbeat["instruments"]["example-watch"]["x_new"] = {"nested": True}
    heartbeat["references"]["example-watch"]["x_new"] = 3
    cfg = build(tmp_path, heartbeat=heartbeat)
    conn = sqlite3.connect(cfg.tape_path)
    conn.execute("ALTER TABLE minutes ADD COLUMN x_extra TEXT")
    conn.execute("ALTER TABLE readings ADD COLUMN x_extra TEXT")
    conn.execute("ALTER TABLE alerts ADD COLUMN x_extra TEXT")
    conn.execute("INSERT INTO minutes (instrument, minute, last, source) "
                 "VALUES ('ref:EXAMPLE', ?, 1.0, 'poll')", (NEWEST,))
    conn.commit()
    conn.close()

    tape = call(cfg, pa.TOOL_LIVE_TAPE, {"watch": "example-watch", "minutes": 3})
    assert [r["minute"] for r in tape["rows"]] == [1795099800, 1795099860, NEWEST]
    assert "x_extra" not in tape["rows"][0]
    listed = call(cfg, pa.TOOL_LIST)
    assert [w["id"] for w in listed["watches"]] == ["example-watch", "example-adr"]
    assert listed["engine"]["heartbeat_age_seconds"] == 10
    history = call(cfg, pa.TOOL_HISTORY)
    assert history["n_returned"] == 3


def test_list_works_when_the_engine_reports_bad_rules(tmp_path):
    heartbeat = copy.deepcopy(HEARTBEAT)
    heartbeat["rules_ok"] = False
    cfg = build(tmp_path, heartbeat=heartbeat)
    out = call(cfg, pa.TOOL_LIST)
    assert out["engine"]["rules_ok"] is False
    assert len(out["watches"]) == 2
    assert_no_recipient(out)
