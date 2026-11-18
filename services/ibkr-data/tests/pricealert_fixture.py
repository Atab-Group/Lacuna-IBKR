"""A price-alert engine's files, written into a temporary directory.

Shared by test_pricealerts.py and the price-alert cases in test_mcpserver.py and
test_httpserver.py. The SQLite schema is the engine contract's DDL, copied
verbatim. Every value is a literal so a test can assert the exact answer.
Nothing here opens a socket.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pricealerts

NOW = 1795100000                 # 2026-11-19T14:53:20Z
NEWEST = 1795099920              # 2026-11-19T14:52:00Z, the newest tape minute
OLD_MINUTE = 1795096320          # 2026-11-19T13:52:00Z, an hour before the window

# Recipient values the tools must never return. Reserved test domain and a
# fictional 555 number.
EMAIL_ONE = "recipient-one@example.invalid"
EMAIL_TWO = "recipient-two@example.invalid"
PHONE = "15550001111"
SECRETS = (EMAIL_ONE, EMAIL_TWO, PHONE)

DDL = """
CREATE TABLE IF NOT EXISTS minutes (
  instrument   TEXT    NOT NULL,   -- watch id, or 'ref:<SYMBOL>' for a reference instrument
  minute       INTEGER NOT NULL,
  last         REAL,               -- last trade price at the close of the minute
  bid          REAL,
  ask          REAL,
  mid          REAL,               -- (bid+ask)/2 when both present and bid <= ask
  spread_pct   REAL,               -- (ask-bid)/mid*100
  volume       REAL,               -- shares traded in the minute; NULL when unknown
  trades       INTEGER,            -- trade prints in the minute; NULL when unknown
  dollars      REAL,               -- volume * last
  day_volume   REAL,               -- cumulative day volume at the close of the minute
  data_type    INTEGER,            -- IBKR market data type: 1 live, 3 delayed; 0 = not IBKR
  source       TEXT    NOT NULL,   -- 'stream', 'warmup' or 'poll'
  PRIMARY KEY (instrument, minute)
);

CREATE TABLE IF NOT EXISTS readings (
  watch_id      TEXT    NOT NULL,
  minute        INTEGER NOT NULL,
  rule          TEXT    NOT NULL,  -- 'move' or 'gap'
  verdict       TEXT    NOT NULL,  -- 'none', 'early', 'firm', 'gap', or 'skip'
  reason        TEXT,              -- why 'skip' or 'none': e.g. 'no_price', 'wide_spread', 'baseline_short'
  held          INTEGER NOT NULL DEFAULT 0,  -- 1: a verdict the cooldown held back
  move_pct      REAL,              -- signed 3-minute move of the watched instrument, percent
  volume_x      REAL,              -- volume multiple
  window_volume REAL,
  window_trades INTEGER,
  window_dollars REAL,
  market_pct    REAL,              -- signed market reference move, percent; NULL when not fresh
  relative_pct  REAL,              -- move_pct - market_pct
  market_fresh  INTEGER,           -- 1 fresh, 0 stale, NULL no market configured
  gap_pct       REAL,              -- gap rule only
  price_pass    INTEGER, volume_pass INTEGER, market_pass INTEGER,
  PRIMARY KEY (watch_id, minute, rule)
);

CREATE TABLE IF NOT EXISTS alerts (
  key        TEXT PRIMARY KEY,     -- watch_id|rule|tier|direction|minute
  watch_id   TEXT    NOT NULL,
  rule       TEXT    NOT NULL,     -- 'move' or 'gap'
  tier       TEXT    NOT NULL,     -- 'early', 'firm', or 'gap-<level>' e.g. 'gap-1.5'
  direction  TEXT    NOT NULL,     -- 'up' or 'down'
  minute     INTEGER NOT NULL,
  created_ts INTEGER NOT NULL,
  subject    TEXT    NOT NULL,     -- the email subject line
  figures    TEXT    NOT NULL,     -- JSON object: the reading that fired, plus prices
  status     TEXT    NOT NULL,     -- 'pending', 'sent', 'dry_run', 'failed'
  email_ok   INTEGER, whatsapp_ok INTEGER,
  sent_ts    INTEGER,
  market_check TEXT   NOT NULL     -- 'on', 'off_stale', 'not_required'
);
CREATE INDEX IF NOT EXISTS alerts_by_watch ON alerts (watch_id, minute);
"""

MINUTE_ROWS = [
    # instrument, minute, last, bid, ask, mid, spread_pct, volume, trades, dollars,
    # day_volume, data_type, source
    ("example-watch", OLD_MINUTE, 99.0, 98.99, 99.01, 99.0, 0.0202, 1000.0, 40, 99000.0,
     50000.0, 1, "stream"),
    ("example-watch", 1795099800, 100.0, 99.99, 100.01, 100.0, 0.02, 1200.0, 50, 120000.0,
     80000.0, 1, "stream"),
    ("example-watch", 1795099860, 100.4, 100.39, 100.41, 100.4, 0.0199, 3000.0, 90, 301200.0,
     83000.0, 1, "stream"),
    ("example-watch", NEWEST, 100.9, 100.89, 100.91, 100.9, 0.0198, 6000.0, 150, 605400.0,
     89000.0, 1, "stream"),
    ("ref:SPY", NEWEST, 600.0, 599.99, 600.01, 600.0, 0.0033, None, None, None, None, 0, "poll"),
]

READING_ROWS = [
    # watch_id, minute, rule, verdict, reason, held, move_pct, volume_x, window_volume,
    # window_trades, window_dollars, market_pct, relative_pct, market_fresh, gap_pct,
    # price_pass, volume_pass, market_pass
    ("example-watch", NEWEST, "move", "early", None, 0, 0.9, 4.5, 10200.0, 290, 1026600.0,
     0.1, 0.8, 1, None, 1, 1, 1),
    ("example-watch", NEWEST, "gap", "none", "no_reference", 0, None, None, None, None, None,
     None, None, None, None, None, None, None),
]

FIGURES_A1 = {"direction": "up", "move_pct": 0.9, "volume_x": 4.5, "price": 100.9,
              "price_before": 100.0}

ALERT_ROWS = [
    # key, watch_id, rule, tier, direction, minute, created_ts, subject, figures, status,
    # email_ok, whatsapp_ok, sent_ts, market_check
    (f"example-watch|move|early|up|{NEWEST}", "example-watch", "move", "early", "up", NEWEST,
     1795099985, "[PRICE] EARLY: AAPL +0.9% in 3 min, volume 4x (example)",
     json.dumps(FIGURES_A1), "dry_run", None, None, None, "on"),
    ("example-watch|move|firm|up|1795099800", "example-watch", "move", "firm", "up",
     1795099800, 1795099865,
     f"[PRICE] FIRM: AAPL +2.0% in 3 min, volume 9x (copy for {EMAIL_ONE})",
     json.dumps({"direction": "up", "note": f"also to {PHONE}"}), "sent", 1, 1, 1795099866,
     "on"),
    ("example-adr|move|firm|down|1794499980", "example-adr", "move", "firm", "down",
     1794499980, 1794500041, "[PRICE] FIRM: MSFT -2.1% in 3 min, volume 8x (example)",
     "not json", "failed", 0, None, None, "off_stale"),
    ("example-adr|move|early|down|1794000000", "example-adr", "move", "early", "down",
     1794000000, 1794000061, "[PRICE] EARLY: MSFT -1.1% in 3 min, volume 5x (example)",
     json.dumps({"direction": "down"}), "sent", 1, 1, 1794000062, "on"),
]

WATCH_ONE = {
    "id": "example-watch",
    "label": "Example stock",
    "subject": "example-subject",
    "enabled": True,
    "context": "An example stock on Nasdaq.",
    "contract": {"symbol": "AAPL", "sec_type": "STK", "exchange": "SMART", "currency": "USD"},
    "hours": {"tz": "America/New_York", "open": "09:30", "close": "16:00"},
    "price": {"source": "last", "lookback_min": 3},
    "volume": {"window_min": 1, "baseline": "median", "baseline_min": 30},
    "market": {"kind": "nasdaq_quote", "symbol": "SPY"},
    "tiers": {
        "early": {"move_pct": 0.75, "volume_x": 4, "require": ["price", "volume", "market"]},
        "firm": {"move_pct": 1.5, "volume_x": 8, "require": ["price", "volume", "market"]},
    },
    "x_note": "an unknown key the writer must keep",
}

WATCH_TWO = {
    "id": "example-adr",
    "label": "Example second listing",
    "subject": "example-subject",
    "enabled": True,
    "context": "A second listing of an example stock.",
    "contract": {"con_id": 272093, "symbol": "MSFT", "sec_type": "STK", "exchange": "SMART",
                 "currency": "USD"},
    "hours": {"tz": "America/New_York", "open": "09:30", "close": "16:00"},
    "price": {"source": "mid", "lookback_min": 3, "max_spread_pct": 1.0},
    "volume": {"window_min": 3, "baseline": "mean", "baseline_min": 60,
               "min_trades": 10, "min_dollars": 250000},
    "market": {"kind": "ibkr_basket", "exchange": "SMART", "currency": "USD",
               "symbols": ["AAPL", "NVDA"], "min_fresh": 1},
    "tiers": {
        "early": {"move_pct": 1.0, "volume_x": 4, "require": ["price", "volume", "market"]},
        "firm": {"move_pct": 2.0, "volume_x": 8, "require": ["price", "volume", "market"]},
    },
    "gap": {"against": "example-watch", "ratio": 1,
            "fx": {"symbol": "EUR", "sec_type": "CASH", "exchange": "IDEALPRO",
                   "currency": "USD"},
            "move_pct": 1.5, "step_pct": 1.0, "rearm_pct": 1.0},
}

RULES = {
    "v": 1,
    "cooldown_min": 15,
    "recipients": {"email": [EMAIL_ONE, EMAIL_TWO], "whatsapp": [PHONE]},
    "watches": [WATCH_ONE, WATCH_TWO],
    "x_top": {"kept": True},
}

HEARTBEAT = {
    "v": 1,
    "ts": 1795099990,
    "pid": 1234,
    "deployed": "abc1234",
    "dry_run": True,
    "gateway_connected": True,
    "rules_ok": True,
    "rules_error": f"an example error naming {EMAIL_TWO}",
    "rules_mtime": 1795090000,
    "instruments": {
        "example-watch": {"last_tick_ts": 1795099950, "data_type": 1, "in_hours": True,
                          "ticks_today": 5120},
        "example-adr": {"last_tick_ts": 1795099000, "data_type": 1, "in_hours": True,
                        "ticks_today": 300},
    },
    "references": {
        "example-watch": {"kind": "nasdaq_quote", "fresh": True, "fresh_count": None,
                          "newest_ts": 1795099995},
        "example-adr": {"kind": "ibkr_basket", "fresh": False, "fresh_count": 0,
                        "newest_ts": 1795080000},
    },
    "fx": {"last_tick_ts": 1795099999, "data_type": 1},
}

BACKTEST_SCRIPT = """\
import json, os, sys
print(json.dumps({"date": sys.argv[sys.argv.index("--date") + 1],
                  "argv": sys.argv[1:],
                  "rules": os.environ.get("PRICE_ALERTS_RULES"),
                  "watches": []}))
"""


def build(tmp_path: Path, rules=None, heartbeat=None) -> pricealerts.Config:
    """Write the three files and a backtest script. Returns the Config pointing at them."""
    state = tmp_path / "state"
    state.mkdir()
    conn = sqlite3.connect(state / "tape.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    conn.executemany("INSERT INTO minutes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", MINUTE_ROWS)
    conn.executemany("INSERT INTO readings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     READING_ROWS)
    conn.executemany("INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ALERT_ROWS)
    conn.commit()
    conn.close()
    (state / "heartbeat.json").write_text(json.dumps(HEARTBEAT if heartbeat is None
                                                     else heartbeat))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    rules_path = config_dir / "price-alerts.json"
    rules_path.write_text(json.dumps(RULES if rules is None else rules, indent=2))
    rules_path.chmod(0o640)
    backtest = tmp_path / "backtest.py"
    backtest.write_text(BACKTEST_SCRIPT)
    return pricealerts.Config(state_dir=state, rules_path=rules_path, backtest_path=backtest,
                              python="/opt/example/bin/python", ibkr_root="/example/store",
                              ibkr_dir="/example/ibkr-data")


def env_for(cfg: pricealerts.Config) -> dict:
    """The environment that makes the server find the files ``build`` wrote."""
    return {"LACUNA_PRICE_ALERTS_DIR": str(cfg.state_dir),
            "LACUNA_PRICE_ALERTS_RULES": str(cfg.rules_path),
            "LACUNA_PRICE_ALERTS_BACKTEST": str(cfg.backtest_path),
            "LACUNA_PRICE_ALERTS_PYTHON": cfg.python}


def assert_no_recipient(payload) -> None:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} leaked into a tool result"
