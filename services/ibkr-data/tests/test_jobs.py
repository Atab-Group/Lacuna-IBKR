"""Pacing arithmetic, the ledger, request planning, and ensure_bars.

The clock is always an argument, so nothing here sleeps and nothing here is
flaky. The only client is a fake that counts calls.
"""

import datetime as dt
import sqlite3
from types import SimpleNamespace

import pytest

import bars as barlib
import jobs
import store

ET = barlib.MARKET_TZ


def et(y, m, d, hh=0, mm=0):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


def test_interrupted_legacy_migration_preserves_symbols_and_pacing(root):
    root.mkdir(parents=True)
    raw = sqlite3.connect(root / "jobs.db")
    raw.executescript("""
      CREATE TABLE requests (id INTEGER PRIMARY KEY, ts REAL, key TEXT, weight INTEGER);
      INSERT INTO requests VALUES (1, 100, 'kept', 1);
      CREATE TABLE coverage_legacy (
        symbol TEXT, interval TEXT, date TEXT, status TEXT, n_bars INTEGER,
        session TEXT, note TEXT, updated_ts INTEGER,
        PRIMARY KEY(symbol,interval,date));
      INSERT INTO coverage_legacy VALUES ('AAPL','1d','2026-09-17','ok',1,'rth','',100);
      INSERT INTO coverage_legacy VALUES ('MSFT','1d','2026-09-17','ok',1,'rth','',100);
      CREATE TABLE coverage (
        con_id INTEGER, symbol TEXT, data_type TEXT, interval TEXT, session TEXT,
        date TEXT, status TEXT, n_bars INTEGER, note TEXT, updated_ts INTEGER,
        PRIMARY KEY(con_id,data_type,interval,session,date));
    """)
    raw.commit(); raw.close()

    conn = jobs.open_db(root)
    try:
        rows = conn.execute("SELECT symbol FROM coverage ORDER BY symbol").fetchall()
        assert [r["symbol"] for r in rows] == ["AAPL", "MSFT"]
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='coverage_legacy'").fetchone()
    finally:
        conn.close()


@pytest.fixture
def root(tmp_path):
    return tmp_path / "ibkr"


@pytest.fixture
def conn(root):
    connection = jobs.open_db(root)
    yield connection
    connection.close()


# -- pure budget arithmetic -------------------------------------------------

def test_budget_of_an_empty_window():
    book = jobs.budget_from([], now=1000.0)
    assert book == {"used": 0, "limit": 60, "remaining": 60,
                    "window_seconds": 600, "oldest_in_window": None,
                    "reset_at": 1000.0}


def test_budget_counts_only_the_rolling_window():
    stamps = [(400.0, 1), (401.0, 1), (999.0, 1)]   # the first two are 600s old
    book = jobs.budget_from(stamps, now=1001.0)
    assert book["used"] == 1
    assert book["remaining"] == 59
    assert book["oldest_in_window"] == 999.0


def test_the_window_edge_is_exclusive():
    assert jobs.budget_from([(400.0, 1)], now=1000.0)["used"] == 0
    assert jobs.budget_from([(400.1, 1)], now=1000.0)["used"] == 1


def test_bid_ask_spends_two_slots():
    assert jobs.weight_for("BID_ASK") == 2
    assert jobs.weight_for("TRADES") == 1
    book = jobs.budget_from([(999.0, 2)] * 30, now=1000.0)
    assert book["used"] == 60
    assert book["remaining"] == 0


def test_reset_at_is_when_the_oldest_request_falls_out():
    book = jobs.budget_from([(950.0, 1), (990.0, 1)], now=1000.0)
    assert book["reset_at"] == 950.0 + 600


def test_sixty_requests_exhaust_the_budget():
    stamps = [(900.0 + i, 1) for i in range(60)]
    assert jobs.budget_from(stamps, now=1000.0)["remaining"] == 0


# -- the persisted bucket ---------------------------------------------------

def test_spending_survives_a_reopen(root):
    first = jobs.open_db(root)
    for i in range(5):
        jobs.spend(first, 1000.0 + i, key=f"k{i}")
    first.close()

    second = jobs.open_db(root)
    try:
        assert jobs.budget(second, now=1010.0)["used"] == 5
    finally:
        second.close()


def test_old_requests_leave_the_window_without_being_deleted(conn):
    jobs.spend(conn, 1000.0, "k")
    assert jobs.budget(conn, now=1000.0)["used"] == 1
    assert jobs.budget(conn, now=1000.0 + 601)["used"] == 0


def test_identical_request_cooldown(conn):
    key = jobs.request_key("MSFT", "1m", 1750000000, "1 D")
    assert jobs.cooldown_remaining(conn, 1000.0, key) == 0.0
    jobs.spend(conn, 1000.0, key)
    assert jobs.cooldown_remaining(conn, 1005.0, key) == 10.0
    assert jobs.cooldown_remaining(conn, 1015.0, key) == 0.0
    other = jobs.request_key("MSFT", "1m", 1750000000, "2 D")
    assert jobs.cooldown_remaining(conn, 1005.0, other) == 0.0


def test_request_key_is_stable_and_discriminating():
    args = ("MSFT", "1m", 1750000000, "1 D")
    assert jobs.request_key(*args) == jobs.request_key(*args)
    assert jobs.request_key("msft", "1m", 1750000000, "1 D") == jobs.request_key(*args)
    assert jobs.request_key(*args, use_rth=False) != jobs.request_key(*args)
    assert jobs.request_key(*args, what="BID_ASK") != jobs.request_key(*args)


# -- ledger -----------------------------------------------------------------

def test_ledger_upserts_on_the_key(conn):
    jobs.set_ledger(conn, "MSFT", "1m", "2026-06-25", "pending", now=1)
    jobs.set_ledger(conn, "MSFT", "1m", "2026-06-25", "ok", n_bars=390, now=2)
    row = jobs.get_ledger(conn, "MSFT", "1m")["2026-06-25"]
    assert row["status"] == "ok"
    assert row["n_bars"] == 390
    assert row["updated_ts"] == 2


def test_empty_and_no_data_are_separate_outcomes(conn):
    jobs.set_ledger(conn, "MSFT", "1d", "2026-07-03", "no_data", now=1)
    jobs.set_ledger(conn, "MSFT", "1d", "2026-07-06", "empty", now=1)
    assert jobs.ledger_summary(conn, "MSFT", "1d") == {"empty": 1, "no_data": 1}


def test_ledger_rejects_an_invented_status(conn):
    with pytest.raises(ValueError):
        jobs.set_ledger(conn, "MSFT", "1d", "2026-07-03", "maybe", now=1)


def test_ledger_normalizes_the_interval(conn):
    jobs.set_ledger(conn, "msft", "1 min", "2026-06-25", "ok", now=1)
    assert "2026-06-25" in jobs.get_ledger(conn, "MSFT", "1m")


# -- request planning -------------------------------------------------------

def test_duration_for_intraday_and_daily():
    assert jobs.duration_for(3600, "1m") == "3600 S"
    assert jobs.duration_for(86400, "1m") == "86400 S"
    assert jobs.duration_for(10, "30s") == "60 S"        # floored
    assert jobs.duration_for(7 * 86400, "5m") == "7 D"
    assert jobs.duration_for(30 * 86400, "1d") == "30 D"
    assert jobs.duration_for(800 * 86400, "1d") == "3 Y"


def test_chunking_respects_the_max_duration_per_bar_size():
    span = [(et(2026, 6, 1), et(2026, 6, 8))]
    plan = jobs.chunk_requests(span, "1m", now=et(2026, 6, 8))
    assert len(plan) == 7                       # 1 min caps at one day per request
    assert all(p["duration"] == "86400 S" for p in plan)
    assert plan[0]["end_ts"] > plan[-1]["end_ts"]   # newest first


def test_chunking_never_asks_beyond_now():
    plan = jobs.chunk_requests([(et(2026, 6, 1), et(2026, 6, 30))], "1d",
                               now=et(2026, 6, 10))
    assert plan[0]["end_ts"] == et(2026, 6, 10)
    assert len(plan) == 1
    assert plan[0]["duration"] == "9 D"


def test_chunking_a_short_span_is_one_request():
    plan = jobs.chunk_requests([(et(2026, 6, 25, 9, 30), et(2026, 6, 25, 16, 0))],
                               "1m", now=et(2026, 7, 1))
    assert len(plan) == 1
    assert plan[0]["duration"] == "23400 S"


def test_chunking_thirty_second_bars_at_eight_hours():
    plan = jobs.chunk_requests([(et(2026, 6, 25), et(2026, 6, 26))], "30s",
                               now=et(2026, 7, 1))
    assert len(plan) == 3           # 24 hours in 8 hour chunks
    assert plan[0]["duration"] == "28800 S"


def test_weekdays_between_skips_the_weekend():
    got = jobs.weekdays_between(et(2026, 6, 26), et(2026, 6, 30))
    assert got == ["2026-06-26", "2026-06-29"]


# -- ensure_bars ------------------------------------------------------------

class FakeClient:
    """Answers historical_bars from a canned per-day map. Counts every call."""

    def __init__(self, per_day=None, raise_on=None):
        self.per_day = per_day or {}
        self.calls = []
        self.raise_on = raise_on

    def historical_bars(self, contract, end, duration, bar_size,
                        what="TRADES", use_rth=True):
        self.calls.append({"end": end, "duration": duration, "bar_size": bar_size})
        if self.raise_on and len(self.calls) in self.raise_on:
            raise RuntimeError("connection reset by peer")
        out = []
        for day, count in self.per_day.items():
            start = et(*day, 9, 30)
            for i in range(count):
                stamp = dt.datetime.fromtimestamp(start + 60 * i, dt.timezone.utc)
                out.append(SimpleNamespace(date=stamp, open=1.0, high=1.0, low=1.0,
                                           close=1.0 + i / 100, volume=5.0,
                                           average=1.0, barCount=2))
        return out


CONTRACT = {"symbol": "MSFT", "con_id": 1, "sec_type": "STK", "exchange": "SMART",
            "primary_exchange": "NYSE", "currency": "USD", "long_name": "",
            "resolved_ts": 0}


def test_adjusted_last_fetches_from_now_and_filters_a_past_window(root):
    client = FakeClient()
    result = jobs.ensure_bars(
        "MSFT", "1d", et(2026, 6, 1), et(2026, 6, 5), client=client,
        root_dir=root, now=et(2026, 7, 1), contract=CONTRACT,
        what="ADJUSTED_LAST")

    assert len(client.calls) == 1
    assert client.calls[0]["end"] == dt.datetime.fromtimestamp(et(2026, 7, 1), dt.timezone.utc)
    assert result["rows"] == []
    assert result["fetch_denied"] is None


def test_ensure_bars_serves_a_complete_cache_without_calling_the_gateway(root):
    rows = barlib.bars_to_rows(
        [SimpleNamespace(date=dt.datetime.fromtimestamp(et(2026, 6, 25, 9, 30) + 60 * i,
                                                        dt.timezone.utc),
                         open=1, high=1, low=1, close=1, volume=1, average=1,
                         barCount=1) for i in range(390)],
        "MSFT", "1m", 0)
    store.write_bars(rows, root)
    client = FakeClient()
    result = jobs.ensure_bars("MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26),
                              client=client, root_dir=root, now=et(2026, 7, 1),
                              contract=CONTRACT)
    assert client.calls == []
    assert result["source"] == "cache"
    assert result["coverage"]["complete"] is True
    assert len(result["rows"]) == 390


def test_ensure_bars_fetches_when_the_store_is_cold(root):
    client = FakeClient(per_day={(2026, 6, 25): 390})
    result = jobs.ensure_bars("MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26),
                              client=client, root_dir=root, now=et(2026, 7, 1),
                              contract=CONTRACT)
    assert len(client.calls) == 1
    assert result["source"] == "fetched"
    assert result["coverage"]["complete"] is True
    assert result["budget"]["used"] == 1
    # and it landed on disk, in the right partition
    assert store.dataset_partition_path(CONTRACT["con_id"], "1m", "2026-06-25",
                                        "TRADES", "rth", root).exists()


def test_a_second_call_is_served_from_the_cache(root):
    client = FakeClient(per_day={(2026, 6, 25): 390})
    args = dict(client=client, root_dir=root, now=et(2026, 7, 1), contract=CONTRACT)
    jobs.ensure_bars("MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26), **args)
    again = jobs.ensure_bars("MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26), **args)
    assert len(client.calls) == 1
    assert again["source"] == "cache"


def test_allow_fetch_false_never_touches_the_gateway(root):
    client = FakeClient(per_day={(2026, 6, 25): 390})
    result = jobs.ensure_bars("MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26),
                              allow_fetch=False, client=client, root_dir=root,
                              now=et(2026, 7, 1), contract=CONTRACT)
    assert client.calls == []
    assert result["rows"] == []
    assert "allow_fetch is false" in result["fetch_denied"]


def test_an_exhausted_budget_denies_the_fetch_and_serves_the_cache(root):
    conn = jobs.open_db(root)
    now = float(et(2026, 7, 1))
    for i in range(60):
        jobs.spend(conn, now - i, key=f"other{i}")
    client = FakeClient(per_day={(2026, 6, 25): 390})
    try:
        result = jobs.ensure_bars("MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26),
                                  client=client, root_dir=root, now=now,
                                  contract=CONTRACT, conn=conn)
    finally:
        conn.close()
    assert client.calls == []
    assert "pacing budget exhausted" in result["fetch_denied"]
    assert result["source"] == "cache"
    assert result["rows"] == []


def test_an_empty_answer_is_recorded_as_empty_not_no_data(root):
    conn = jobs.open_db(root)
    client = FakeClient(per_day={})
    try:
        result = jobs.ensure_bars("MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26),
                                  client=client, root_dir=root,
                                  now=et(2026, 7, 1), contract=CONTRACT, conn=conn)
        ledger = jobs.get_ledger(conn, "MSFT", "1m")
    finally:
        conn.close()
    assert result["fetched"][0]["status"] == "empty"
    assert ledger["2026-06-25"]["status"] == "empty"
    assert result["rows"] == []


def test_a_shut_market_inside_an_answered_request_is_no_data(root):
    conn = jobs.open_db(root)
    # ask for two days, the fake answers with bars for only the first
    client = FakeClient(per_day={(2026, 6, 25): 390})
    try:
        jobs.ensure_bars("MSFT", "1m", et(2026, 6, 25), et(2026, 6, 27),
                         client=client, root_dir=root, now=et(2026, 7, 1),
                         contract=CONTRACT, conn=conn)
        ledger = jobs.get_ledger(conn, "MSFT", "1m")
    finally:
        conn.close()
    assert ledger["2026-06-25"]["status"] == "ok"
    assert ledger["2026-06-25"]["n_bars"] == 390
    assert ledger["2026-06-26"]["status"] == "no_data"


def test_a_raising_request_lands_in_the_ledger_as_error(root):
    conn = jobs.open_db(root)
    client = FakeClient(per_day={(2026, 6, 25): 390}, raise_on={1})
    try:
        result = jobs.ensure_bars("MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26),
                                  client=client, root_dir=root,
                                  now=et(2026, 7, 1), contract=CONTRACT, conn=conn)
        ledger = jobs.get_ledger(conn, "MSFT", "1m")
    finally:
        conn.close()
    assert result["fetched"][0]["status"] == "error"
    assert ledger["2026-06-25"]["status"] == "error"
    assert "connection reset" in ledger["2026-06-25"]["note"]


def test_a_failed_request_still_spends_budget(root):
    conn = jobs.open_db(root)
    client = FakeClient(raise_on={1})
    try:
        result = jobs.ensure_bars("MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26),
                                  client=client, root_dir=root,
                                  now=et(2026, 7, 1), contract=CONTRACT, conn=conn)
    finally:
        conn.close()
    assert result["budget"]["used"] == 1


def test_a_partial_cache_becomes_a_mixed_answer(root):
    have = barlib.bars_to_rows(
        [SimpleNamespace(date=dt.datetime.fromtimestamp(et(2026, 6, 25, 9, 30) + 60 * i,
                                                        dt.timezone.utc),
                         open=1, high=1, low=1, close=1, volume=1, average=1,
                         barCount=1) for i in range(100)],
        "MSFT", "1m", 0)
    store.write_bars(have, root)
    client = FakeClient(per_day={(2026, 6, 25): 390})
    result = jobs.ensure_bars("MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26),
                              client=client, root_dir=root, now=et(2026, 7, 1),
                              contract=CONTRACT)
    assert result["source"] == "mixed"
    assert len(result["rows"]) == 390
    assert result["coverage"]["complete"] is True


def test_no_client_means_cache_only_with_a_reason(root):
    result = jobs.ensure_bars("MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26),
                              client=None, root_dir=root, now=et(2026, 7, 1))
    assert result["source"] == "cache"
    assert "no gateway client" in result["fetch_denied"]
