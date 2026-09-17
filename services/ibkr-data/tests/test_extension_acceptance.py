"""Cross-boundary acceptance tests for generalized market data.

These tests exercise promises that are easy for unit tests of one module to
miss: an option must never inherit its underlying's old cache, dataset identity
must survive storage and the ledger, and cache-only MCP calls must stay offline.
No test opens an IBKR connection.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from types import SimpleNamespace

import pytest

import bars as barlib
import contracts
import ibclient
import jobs
import mcpserver
import store


ET = barlib.MARKET_TZ


def et(y, m, d, hh=0, mm=0):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


def bar(ts, *, symbol="AAPL", con_id=None, sec_type=None,
        data_type="TRADES", session="rth", close=1.0):
    return {
        "ts": ts, "symbol": symbol, "interval": "5m", "open": close,
        "high": close, "low": close, "close": close, "volume": 1.0,
        "wap": close, "bar_count": 1, "session": session,
        "source": "ibkr", "pulled_ts": ts + 1, "con_id": con_id,
        "sec_type": sec_type, "data_type": data_type,
        "exchange": "SMART", "currency": "USD",
        "local_symbol": "AAPL 260925C00337500" if sec_type == "OPT" else "AAPL",
    }


def option_contract(con_id=922317899):
    return {
        "symbol": "AAPL", "con_id": con_id, "sec_type": "OPT",
        "exchange": "SMART", "currency": "USD",
        "local_symbol": "AAPL 260925C00337500", "expiry": "20260925",
        "strike": 337.5, "right": "C", "multiplier": "100",
        "trading_class": "AAPL", "underlying_con_id": 265598,
        "time_zone": "US/Eastern", "resolved_ts": 1,
    }


def test_option_read_never_falls_back_to_legacy_underlying_bars(tmp_path):
    stock_ts = et(2026, 9, 17, 9, 30)
    option_ts = stock_ts + 300
    store.write_bars([bar(stock_ts, close=337.0)], tmp_path)
    store.write_bars([bar(option_ts, con_id=922317899, sec_type="OPT", close=4.7)], tmp_path)

    rows = store.read_bars("AAPL", "5m", root_dir=tmp_path,
                           con_id=922317899, data_type="TRADES")

    assert [(r["ts"], r["con_id"], r["close"]) for r in rows] == [
        (option_ts, 922317899, 4.7)
    ]


def test_symbol_only_equity_read_sees_legacy_and_v2_cache(tmp_path):
    legacy_ts = et(2026, 9, 17, 9, 30)
    v2_ts = legacy_ts + 300
    store.write_bars([bar(legacy_ts, close=336.0)], tmp_path)
    store.write_bars([bar(v2_ts, con_id=265598, sec_type="STK", close=337.0)], tmp_path)

    rows = store.read_bars("AAPL", "5m", root_dir=tmp_path,
                           data_type="TRADES")

    assert [(r["ts"], r["close"]) for r in rows] == [
        (legacy_ts, 336.0), (v2_ts, 337.0)
    ]


def test_qualified_stock_writes_are_v2_and_listings_stay_isolated(tmp_path):
    ts = et(2026, 9, 17, 9, 30)
    asx = bar(ts, symbol="BHP", con_id=101, sec_type="STK", close=45.0)
    asx.update({"exchange": "ASX", "currency": "AUD", "local_symbol": "BHP"})
    nyse = bar(ts, symbol="BHP", con_id=202, sec_type="STK", close=55.0)
    nyse.update({"exchange": "NYSE", "currency": "USD", "local_symbol": "BHP"})

    written = store.write_bars([asx, nyse], tmp_path)
    asx_rows = store.read_bars("BHP", "5m", root_dir=tmp_path,
                               con_id=101, data_type="TRADES")
    nyse_rows = store.read_bars("BHP", "5m", root_dir=tmp_path,
                                con_id=202, data_type="TRADES")

    assert all("bars-v2" in path for path in written)
    assert [r["close"] for r in asx_rows] == [45.0]
    assert [r["close"] for r in nyse_rows] == [55.0]
    assert not (tmp_path / "bars" / "5m" / "BHP").exists()


def test_symbol_only_equity_read_never_mixes_same_symbol_options(tmp_path):
    stock_ts = et(2026, 9, 17, 9, 30)
    option_ts = stock_ts + 300
    store.write_bars([bar(stock_ts, close=337.0)], tmp_path)
    store.write_bars([
        bar(stock_ts, con_id=922317899, sec_type="OPT", close=4.5),
        bar(option_ts, con_id=922317899, sec_type="OPT", close=4.7),
    ], tmp_path)

    rows = store.read_bars("AAPL", "5m", root_dir=tmp_path, data_type="TRADES")

    assert [(r["ts"], r["close"], r.get("sec_type")) for r in rows] == [
        (stock_ts, 337.0, None)
    ]


def test_duckdb_query_works_with_only_v2_files(tmp_path):
    store.write_bars([
        bar(et(2026, 9, 17, 9, 30), con_id=922317899, sec_type="OPT", close=4.7)
    ], tmp_path)

    columns, rows = store.query(
        "SELECT con_id, sec_type, data_type, session, close FROM bars", tmp_path)

    assert columns == ["con_id", "sec_type", "data_type", "session", "close"]
    assert rows == [(922317899, "OPT", "TRADES", "rth", 4.7)]


def test_coverage_ledger_isolates_contract_data_type_and_session(tmp_path):
    conn = jobs.open_db(tmp_path)
    try:
        jobs.set_ledger(conn, "AAPL", "5m", "2026-09-17", "ok", 10,
                        session="rth", con_id=922317899, data_type="TRADES")
        jobs.set_ledger(conn, "AAPL", "5m", "2026-09-17", "ok", 11,
                        session="rth", con_id=922317899, data_type="BID")
        jobs.set_ledger(conn, "AAPL", "5m", "2026-09-17", "ok", 12,
                        session="eth", con_id=922317899, data_type="TRADES")
        jobs.set_ledger(conn, "AAPL", "5m", "2026-09-17", "ok", 13,
                        session="rth", con_id=922318925, data_type="TRADES")

        rows = conn.execute(
            "SELECT con_id, data_type, session, n_bars FROM coverage "
            "ORDER BY con_id, data_type, session"
        ).fetchall()
    finally:
        conn.close()

    assert [tuple(r) for r in rows] == [
        (922317899, "BID", "rth", 11),
        (922317899, "TRADES", "eth", 12),
        (922317899, "TRADES", "rth", 10),
        (922318925, "TRADES", "rth", 13),
    ]


def _create_legacy_coverage(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE coverage (
          symbol TEXT NOT NULL, interval TEXT NOT NULL, date TEXT NOT NULL,
          status TEXT NOT NULL, n_bars INTEGER NOT NULL DEFAULT 0,
          session TEXT NOT NULL DEFAULT 'rth', note TEXT NOT NULL DEFAULT '',
          updated_ts INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (symbol, interval, date));
        INSERT INTO coverage VALUES
          ('AAPL','5m','2026-09-17','ok',10,'rth','',1),
          ('MSFT','5m','2026-09-17','ok',11,'rth','',1);
    """)
    conn.commit()
    conn.close()


def test_legacy_ledger_migration_preserves_multiple_symbols_on_same_date(tmp_path):
    path = jobs.db_path(tmp_path)
    _create_legacy_coverage(path)

    conn = jobs.open_db(tmp_path)
    try:
        rows = conn.execute(
            "SELECT symbol,n_bars FROM coverage ORDER BY symbol"
        ).fetchall()
        leftover = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND name='coverage_legacy'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert [tuple(r) for r in rows] == [("AAPL", 10), ("MSFT", 11)]
    assert leftover == 0


def test_interrupted_ledger_migration_recovers_legacy_rows(tmp_path):
    path = jobs.db_path(tmp_path)
    _create_legacy_coverage(path)
    conn = sqlite3.connect(path)
    conn.executescript("""
        ALTER TABLE coverage RENAME TO coverage_legacy;
        CREATE TABLE coverage (
          con_id INTEGER NOT NULL DEFAULT 0, symbol TEXT NOT NULL,
          data_type TEXT NOT NULL DEFAULT 'TRADES', interval TEXT NOT NULL,
          date TEXT NOT NULL, status TEXT NOT NULL,
          n_bars INTEGER NOT NULL DEFAULT 0, session TEXT NOT NULL DEFAULT 'rth',
          note TEXT NOT NULL DEFAULT '', updated_ts INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (con_id, symbol, data_type, interval, session, date));
    """)
    conn.commit()
    conn.close()

    recovered = jobs.open_db(tmp_path)
    try:
        rows = recovered.execute(
            "SELECT symbol,n_bars FROM coverage ORDER BY symbol"
        ).fetchall()
        leftover = recovered.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND name='coverage_legacy'"
        ).fetchone()[0]
    finally:
        recovered.close()

    assert [tuple(r) for r in rows] == [("AAPL", 10), ("MSFT", 11)]
    assert leftover == 0


def test_cache_only_option_bars_never_connect(tmp_path):
    contract = option_contract()
    contracts.save({contracts.cache_key(contract): contract,
                    f"CONID:{contract['con_id']}": contract}, tmp_path)
    ts = et(2026, 9, 17, 9, 30)
    store.write_bars([bar(ts, con_id=contract["con_id"], sec_type="OPT", close=4.7)], tmp_path)
    calls = []

    def forbidden_connect(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("cache-only call attempted a gateway connection")

    ctx = mcpserver.Ctx(root_dir=tmp_path, now=ts + 300, connect=forbidden_connect)
    result = mcpserver.tool_get_bars({
        "con_id": contract["con_id"], "sec_type": "OPT", "interval": "5m",
        "start": barlib.to_iso(ts), "end": barlib.to_iso(ts + 300),
        "allow_fetch": False,
    }, ctx)

    assert calls == []
    assert "error" not in result
    assert result["n_returned"] == 1
    assert result["bars"][0]["con_id"] == contract["con_id"]


def test_bid_ask_fetch_spends_two_pacing_slots(tmp_path):
    ts = et(2026, 9, 17, 9, 30)
    contract = option_contract()

    class FakeClient:
        def historical_bars(self, contract_arg, end, duration, bar_size,
                            what="TRADES", use_rth=True):
            assert contract_arg == contract
            assert what == "BID_ASK"
            return [SimpleNamespace(
                date=dt.datetime.fromtimestamp(ts, dt.timezone.utc),
                open=4.5, high=4.7, low=4.4, close=4.6, volume=1,
                average=4.6, barCount=1,
            )]

    result = jobs.ensure_bars(
        "AAPL", "5m", ts, ts + 300, client=FakeClient(), root_dir=tmp_path,
        now=ts + 600, contract=contract, what="BID_ASK",
    )

    assert result["budget"]["used"] == 2
    assert result["rows"][0]["data_type"] == "BID_ASK"


@pytest.mark.parametrize(("contract", "mode"), [
    (option_contract(), "sparse_trade_observations"),
    ({"symbol": "EUR", "con_id": 12087792, "sec_type": "CASH",
      "exchange": "IDEALPRO", "currency": "USD"}, "observations"),
])
def test_non_stock_coverage_does_not_apply_the_us_equity_grid(tmp_path, contract, mode):
    ts = et(2026, 9, 17, 10, 0)
    store.write_bars([bar(ts, symbol=contract["symbol"], con_id=contract["con_id"],
                          sec_type=contract["sec_type"], close=1.2)], tmp_path)

    result = jobs.ensure_bars(
        contract["symbol"], "5m", ts, ts + 3600, allow_fetch=False,
        root_dir=tmp_path, now=ts + 3600, contract=contract, what="TRADES",
    )

    assert result["coverage"]["coverage_mode"] == mode
    assert result["coverage"]["expected_bars"] is None
    assert result["coverage"]["complete"] is None
    assert result["coverage"]["gaps"] == []


def test_option_day_without_trades_is_recorded_as_sparse_not_closed(tmp_path):
    first = et(2026, 9, 17, 9, 30)
    end = et(2026, 9, 19, 0, 0)
    contract = option_contract()

    class SparseClient:
        def historical_bars(self, *args, **kwargs):
            return [SimpleNamespace(
                date=dt.datetime.fromtimestamp(first, dt.timezone.utc),
                open=4.5, high=4.7, low=4.4, close=4.6, volume=1,
                average=4.6, barCount=1,
            )]

    jobs.ensure_bars(
        "AAPL", "5m", first, end, client=SparseClient(), root_dir=tmp_path,
        now=end + 3600, contract=contract, what="TRADES",
    )
    conn = jobs.open_db(tmp_path)
    try:
        rows = conn.execute(
            "SELECT date, status, note FROM coverage "
            "WHERE con_id = ? AND data_type = 'TRADES' ORDER BY date",
            (contract["con_id"],),
        ).fetchall()
    finally:
        conn.close()

    assert [(r["date"], r["status"]) for r in rows] == [
        ("2026-09-17", "ok"), ("2026-09-18", "sparse")
    ]
    assert "market status unknown" in rows[1]["note"]


def test_fx_eth_fetch_keeps_daytime_and_evening_bars_in_one_dataset(tmp_path):
    daytime = et(2026, 9, 17, 10, 0)
    evening = et(2026, 9, 17, 18, 0)
    contract = {"symbol": "EUR", "con_id": 12087792, "sec_type": "CASH",
                "exchange": "IDEALPRO", "currency": "USD"}

    class FxClient:
        def historical_bars(self, *args, **kwargs):
            return [
                SimpleNamespace(date=dt.datetime.fromtimestamp(daytime, dt.timezone.utc),
                                open=1.1, high=1.2, low=1.0, close=1.15,
                                volume=1, average=1.15, barCount=1),
                SimpleNamespace(date=dt.datetime.fromtimestamp(evening, dt.timezone.utc),
                                open=1.15, high=1.2, low=1.1, close=1.16,
                                volume=1, average=1.16, barCount=1),
            ]

    jobs.ensure_bars(
        "EUR", "5m", daytime, evening + 300, session="eth", client=FxClient(),
        root_dir=tmp_path, now=evening + 600, contract=contract, what="TRADES",
    )
    rows = store.read_bars("EUR", "5m", root_dir=tmp_path,
                           con_id=contract["con_id"], data_type="TRADES",
                           session="eth")

    assert [r["ts"] for r in rows] == [daytime, evening]
    assert {r["session"] for r in rows} == {"eth"}


def test_bad_bar_arguments_do_not_connect_or_emit_login_instructions(tmp_path):
    calls = []

    def forbidden_connect(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("validation should precede gateway access")

    ctx = mcpserver.Ctx(root_dir=tmp_path, connect=forbidden_connect)
    result = mcpserver.tool_get_bars({
        "symbol": "AAPL", "interval": "nonsense", "start": "not-a-time",
    }, ctx)

    assert calls == []
    assert result["error"]["code"] == "bad_argument"
    assert "login_url" not in result
    assert "log in" not in result["error"]["message"].lower()


def snapshot(ts, greeks=None):
    row = option_contract()
    row.update({
        "ts": ts, "collected_ts": ts, "market_data_type": 3,
        "status": "partial", "bid": 4.5, "ask": 4.65, "last": 4.7,
        "close": 4.6, "bid_size": 10.0, "ask_size": 11.0,
        "last_size": 2.0, "volume": 3914.0,
        "call_open_interest": 581.0, "put_open_interest": None,
        "model_greeks": greeks, "bid_greeks": None, "ask_greeks": None,
        "last_greeks": None, "errors": [], "warnings": [],
    })
    return row


def test_snapshot_schema_accepts_greeks_arriving_after_initial_null(tmp_path):
    first_ts = et(2026, 9, 17, 16, 0)
    second_ts = first_ts + 30
    greeks = {"implied_volatility": 0.231, "delta": 0.505, "gamma": 0.02,
              "vega": 0.12, "theta": -0.08, "option_price": 4.7,
              "underlying_price": 337.0}

    store.write_snapshots([snapshot(first_ts)], tmp_path)
    store.write_snapshots([snapshot(second_ts, greeks)], tmp_path)
    rows = store.read_snapshots(922317899, root_dir=tmp_path)

    assert len(rows) == 2
    assert rows[0]["model_greeks"] is None
    assert rows[1]["model_greeks"]["delta"] == pytest.approx(0.505)


def test_snapshot_history_tool_is_cache_only_and_bounded(tmp_path):
    first_ts = et(2026, 9, 17, 16, 0)
    store.write_snapshots([
        snapshot(first_ts), snapshot(first_ts + 30), snapshot(first_ts + 60)
    ], tmp_path)
    calls = []

    def forbidden_connect(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("snapshot history must not connect")

    ctx = mcpserver.Ctx(root_dir=tmp_path, connect=forbidden_connect)
    result = mcpserver.tool_get_option_snapshots({
        "con_id": 922317899, "start": barlib.to_iso(first_ts), "limit": 2,
    }, ctx)

    assert calls == []
    assert result["source"] == "cache"
    assert result["n_returned"] == 2
    assert result["truncated"] == 1
    assert [r["collected_ts"] for r in result["snapshots"]] == [
        first_ts, first_ts + 30
    ]


def test_exact_option_resolution_preserves_trade_identity(tmp_path):
    expected = option_contract()

    class FakeClient:
        def resolve_contract(self, selector):
            assert selector["con_id"] == expected["con_id"]
            return [expected]

    resolved = contracts.resolve_selector({"con_id": expected["con_id"], "sec_type": "OPT"},
                                          client=FakeClient(), root_dir=tmp_path, now=2)

    for field in ("con_id", "local_symbol", "expiry", "strike", "right",
                  "multiplier", "trading_class", "underlying_con_id", "time_zone"):
        assert resolved[field] == expected[field]


def test_snapshot_number_cleanup_does_not_erase_valid_negative_greeks():
    raw = SimpleNamespace(impliedVol=1.7976931348623157e308, delta=-1.0,
                          gamma=-2.0, vega=-2.0, theta=-0.08,
                          optPrice=-1.0, undPrice=337.0)

    cleaned = ibclient.Client._greeks(raw)

    assert cleaned == {
        "implied_volatility": None, "delta": -1.0, "gamma": None,
        "vega": None, "theta": -0.08, "option_price": None,
        "underlying_price": 337.0,
    }

    raw.theta = -2.0
    assert ibclient.Client._greeks(raw)["theta"] is None


def test_delayed_snapshot_reports_actual_mode_withholds_bad_volume_and_cancels(monkeypatch):
    class Event:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

        def __isub__(self, handler):
            self.handlers.remove(handler)
            return self

    ticker = SimpleNamespace(
        contract=SimpleNamespace(conId=265598), marketDataType=3,
        bid=336.9, ask=337.1, last=337.0, close=335.0,
        bidSize=20, askSize=15, lastSize=2, volume=36_600_000_000_000,
        callOpenInterest=float("nan"), putOpenInterest=-1,
        modelGreeks=None, bidGreeks=None, askGreeks=None, lastGreeks=None,
    )

    class FakeIB:
        def __init__(self):
            self.errorEvent = Event()
            self.cancelled = []

        def reqMktData(self, contract, *args, **kwargs):
            return ticker

        def sleep(self, seconds):
            return None

        def cancelMktData(self, contract):
            self.cancelled.append(contract)

    fake_ib = FakeIB()
    client = ibclient.Client()
    client.ib = fake_ib
    stock = {"symbol": "AAPL", "con_id": 265598, "sec_type": "STK",
             "exchange": "SMART", "currency": "USD"}
    monkeypatch.setattr(client, "qualify", lambda item: SimpleNamespace(conId=item["con_id"]))

    row = client.market_snapshot([stock], timeout=0.25)[0]

    assert row["market_data_type"] == 3
    assert row["collected_ts"] == row["ts"]
    assert row["volume"] is None
    assert any("volume" in warning for warning in row["warnings"])
    assert len(fake_ib.cancelled) == 1
    assert fake_ib.errorEvent.handlers == []


def test_read_only_surface_has_no_order_or_account_mutation_methods():
    forbidden = ("placeorder", "cancelorder", "reqpositions", "reqaccountupdates")
    public = [name.lower() for name in dir(ibclient.Client) if not name.startswith("_")]

    assert not any(any(word in name for word in forbidden) for name in public)
    assert not any("order" in name or "position" in name for name in mcpserver.TOOL_NAMES)
    assert mcpserver.tool_market_data_capabilities({})["trading"] is False
