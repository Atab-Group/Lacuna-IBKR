"""Tests for the MCP tool surface.

Every test calls a handler directly with a dict of arguments and a Ctx pointed
at a temporary store, so nothing here opens a socket to IB Gateway. The one
place a gateway would be reached is stubbed with a factory that raises, which is
also how the session-down responses are exercised.
"""

from __future__ import annotations

import json

import pytest

import bars as barlib
import jobs
import mcpserver as mcp
import store


class Down(RuntimeError):
    """Stands in for ibclient.GatewayDown without importing ib_async."""


def down_ctx(root, now=None):
    """A Ctx whose gateway is unreachable and whose port is closed."""
    return mcp.Ctx(
        root_dir=str(root),
        now=now,
        connect=lambda *a, **kw: (_ for _ in ()).throw(Down("connection refused")),
        probe=lambda *a, **kw: (False, "127.0.0.1:4001 refused the connection"),
    )


def seed(root, symbol="MSFT", interval="1m", start="2026-06-25T13:30:00Z",
         n=60, price=6.00, step_pct=0.0):
    """Write n synthetic bars into a temporary store and return them."""
    start_ts = barlib.to_epoch(start)
    size = barlib.bar_seconds(interval)
    rows = []
    for i in range(n):
        close = price * (1 + step_pct * i)
        rows.append({
            "ts": start_ts + i * size, "symbol": symbol, "interval": interval,
            "open": price * (1 + step_pct * (i - 1)) if i else price,
            "high": close, "low": close, "close": close,
            "volume": 100.0, "wap": close, "bar_count": 5,
            "session": "rth", "source": "test", "pulled_ts": start_ts,
        })
    store.write_bars(rows, str(root))
    return rows


# ---------------------------------------------------------------- status ----

def test_status_when_the_gateway_is_unreachable(tmp_path):
    payload = mcp.tool_status({}, down_ctx(tmp_path, now=1_756_600_000))

    assert payload["gateway"]["up"] is False
    assert payload["gateway"]["host"] and payload["gateway"]["port"]
    assert payload["account"] is None
    assert payload["login_url"] == "http://127.0.0.1:8642"
    assert "human must log in" in payload["next_action"]
    assert "allow_fetch=false" in payload["cache_still_works"]

    # the rest of the health view still answers off the local store
    assert payload["budget"]["remaining"] == jobs.PACING_LIMIT
    assert payload["budget"]["limit"] == jobs.PACING_LIMIT
    assert payload["store"]["files"] == 0
    assert payload["universe"]["count"] == 0
    assert "universe.json" in payload["universe"]["note"]
    assert payload["checked_at"].endswith("Z")


def test_status_counts_what_the_store_holds(tmp_path):
    seed(tmp_path, n=10)
    payload = mcp.tool_status({}, down_ctx(tmp_path))

    assert payload["store"]["files"] == 1
    assert payload["store"]["bytes"] > 0
    assert payload["store"]["symbols"] == ["MSFT"]


# -------------------------------------------------------------- get_bars ----

def test_get_bars_serves_the_cache_without_a_gateway(tmp_path):
    seed(tmp_path, n=390, start="2026-06-25T13:30:00Z")
    ctx = down_ctx(tmp_path)

    payload = mcp.tool_get_bars({
        "symbol": "MSFT", "interval": "1m",
        "start": "2026-06-25T13:30:00Z", "end": "2026-06-25T20:00:00Z",
        "allow_fetch": False,
    }, ctx)

    assert payload["source"] == "cache"
    assert payload["n_returned"] == 390
    assert payload["coverage"]["n_bars"] == 390
    assert payload["coverage"]["complete"] is True
    assert payload["fetched"] == []
    assert payload["budget"]["remaining"] == jobs.PACING_LIMIT


def test_get_bars_reports_the_gap_it_could_not_fetch(tmp_path):
    seed(tmp_path, n=60, start="2026-06-25T13:30:00Z")
    ctx = down_ctx(tmp_path)

    payload = mcp.tool_get_bars({
        "symbol": "MSFT", "interval": "1m",
        "start": "2026-06-25T13:30:00Z", "end": "2026-06-25T20:00:00Z",
    }, ctx)

    assert payload["coverage"]["complete"] is False
    assert payload["coverage"]["n_bars"] == 60
    assert payload["coverage"]["expected_bars"] == 390
    assert payload["coverage"]["gaps"]
    # the gateway was down, so the response says so and still serves the cache
    assert "gateway unavailable" in payload["fetch_denied"]
    assert payload["login_url"] == "http://127.0.0.1:8642"


def test_get_bars_trims_the_tail_but_not_the_coverage(tmp_path):
    seed(tmp_path, n=200)
    payload = mcp.tool_get_bars({
        "symbol": "MSFT", "interval": "1m", "start": "2026-06-25T13:30:00Z",
        "end": "2026-06-25T17:00:00Z", "allow_fetch": False, "max_bars": 20,
    }, down_ctx(tmp_path))

    assert payload["n_returned"] == 20
    assert payload["coverage"]["n_bars"] == 200
    assert "180 bars" in payload["truncated"]


def test_get_bars_rejects_a_bad_interval(tmp_path):
    payload = mcp.tool_get_bars({"symbol": "MSFT", "interval": "7m",
                                 "start": "2026-06-25T13:30:00Z"},
                                down_ctx(tmp_path))
    assert payload["error"]["code"] == "bad_argument"
    assert payload["error"]["next_action"]


def test_get_bars_needs_a_start(tmp_path):
    payload = mcp.tool_get_bars({"symbol": "MSFT"}, down_ctx(tmp_path))
    assert payload["error"]["code"] == "bad_argument"
    assert "start" in payload["error"]["message"]


# ------------------------------------------------------------- query_sql ----

@pytest.mark.parametrize("sql", [
    "DELETE FROM bars",
    "DROP VIEW bars",
    "INSERT INTO bars VALUES (1)",
    "UPDATE bars SET close = 0",
    "PRAGMA database_list",
    "SELECT 1; DROP VIEW bars",
])
def test_query_sql_refuses_anything_that_is_not_a_select(tmp_path, sql):
    payload = mcp.tool_query_sql({"sql": sql}, down_ctx(tmp_path))
    assert payload["error"]["code"] == "query_rejected"
    assert payload["error"]["next_action"]


def test_query_sql_runs_a_select_over_the_store(tmp_path):
    seed(tmp_path, n=30)
    payload = mcp.tool_query_sql(
        {"sql": "SELECT symbol, count(*) AS n FROM bars GROUP BY 1"},
        down_ctx(tmp_path))

    assert payload["rows"] == [{"symbol": "MSFT", "n": 30}]
    assert payload["columns"] == ["symbol", "n"]
    assert payload["row_cap"] == mcp.SQL_ROW_CAP


def test_query_sql_caps_rows_at_500(tmp_path):
    seed(tmp_path, n=600)
    payload = mcp.tool_query_sql({"sql": "SELECT ts FROM bars", "limit": 10_000},
                                 down_ctx(tmp_path))

    assert payload["n_rows"] == mcp.SQL_ROW_CAP
    assert "cap was reached" in payload["truncated"]


def test_query_sql_needs_a_statement(tmp_path):
    payload = mcp.tool_query_sql({"sql": "   "}, down_ctx(tmp_path))
    assert payload["error"]["code"] == "bad_argument"


# ---------------------------------------------------------- event_window ----

def test_event_window_horizon_math_on_synthetic_bars(tmp_path):
    # 1-minute bars rising 1% per bar from 100.00, starting 13:30Z.
    start = "2026-06-25T13:30:00Z"
    seed(tmp_path, interval="1m", start=start, n=200, price=100.0, step_pct=0.01)
    event = "2026-06-25T14:00:00Z"

    payload = mcp.tool_event_window({
        "symbol": "MSFT", "event_at": event, "interval": "1m",
        "before": "30m", "after": "2h", "allow_fetch": False,
    }, down_ctx(tmp_path))

    moves = {m["horizon"]: m for m in payload["moves"]}

    # bar 30 starts at 14:00 and its open is bar 29's close: 100 * (1 + 0.29)
    assert payload["base"]["price"] == pytest.approx(129.0)
    assert payload["base"]["at"] == "2026-06-25T14:00:00Z"
    assert payload["base"]["from"].startswith("open")

    # 30s is finer than a 1m bar, so it is refused rather than coarsened
    assert moves["30s"]["status"] == "unavailable"
    assert moves["30s"]["pct"] is None

    # 5m: the first bar closing at or after 14:05 is the one starting 14:04,
    # whose close is 100 * (1 + 0.34)
    assert moves["5m"]["status"] == "measured"
    assert moves["5m"]["pct"] == pytest.approx((134.0 / 129.0 - 1) * 100, abs=0.01)

    # 1h: the bar starting 14:59, close 100 * (1 + 0.89)
    assert moves["1h"]["status"] == "measured"
    assert moves["1h"]["pct"] == pytest.approx((189.0 / 129.0 - 1) * 100, abs=0.01)

    # 1d runs past the end of the window, and says so rather than guessing
    assert moves["1d"]["status"] == "no_bar"
    assert "widen after" in moves["1d"]["why"]


def test_event_window_horizons_match_eventstudy(tmp_path):
    seed(tmp_path, interval="1m", n=200, price=10.0, step_pct=0.001)
    payload = mcp.tool_event_window({
        "symbol": "MSFT", "event_at": "2026-06-25T14:00:00Z", "interval": "1m",
        "allow_fetch": False,
    }, down_ctx(tmp_path))

    assert [m["horizon"] for m in payload["moves"]] == ["30s", "5m", "1h", "1d"]
    assert set(mcp.HORIZON_SECONDS) == {"30s", "5m", "1h", "1d"}


def test_event_window_picks_and_explains_the_interval(tmp_path):
    ctx = down_ctx(tmp_path, now=barlib.to_epoch("2026-06-25T18:00:00Z"))

    recent = mcp.tool_event_window({"symbol": "MSFT", "allow_fetch": False,
                                    "event_at": "2026-06-25T14:00:00Z"}, ctx)
    assert recent["interval_choice"]["chosen"] == "30s"
    assert recent["interval_choice"]["requested"] is None
    assert "retention" in recent["interval_choice"]["why"]

    old = mcp.tool_event_window({"symbol": "MSFT", "allow_fetch": False,
                                 "event_at": "2015-06-25T14:00:00Z"}, ctx)
    assert old["interval_choice"]["chosen"] == "1d"
    assert "daily bars" in old["interval_choice"]["why"]


def test_event_window_says_when_no_bar_covers_the_event(tmp_path):
    seed(tmp_path, interval="1m", start="2026-06-25T15:00:00Z", n=30)
    payload = mcp.tool_event_window({
        "symbol": "MSFT", "event_at": "2026-06-25T14:00:00Z", "interval": "1m",
        "before": "5m", "after": "10m", "allow_fetch": False,
    }, down_ctx(tmp_path))

    assert payload["base"] is None
    assert payload["moves"] == []
    assert "no bar at or before" in payload["note"]


def test_event_window_needs_an_event_at(tmp_path):
    payload = mcp.tool_event_window({"symbol": "MSFT"}, down_ctx(tmp_path))
    assert payload["error"]["code"] == "bad_argument"


# ------------------------------------------------------- compare_symbols ----

def test_compare_symbols_shares_one_interval(tmp_path):
    seed(tmp_path, symbol="MSFT", interval="1m", n=120, price=10.0, step_pct=0.01)
    seed(tmp_path, symbol="KO", interval="1m", n=120, price=20.0, step_pct=0.002)

    payload = mcp.tool_compare_symbols({
        "symbols": ["MSFT", "KO"], "event_at": "2026-06-25T14:00:00Z",
        "interval": "1m", "allow_fetch": False,
    }, down_ctx(tmp_path))

    assert set(payload["symbols"]) == {"MSFT", "KO"}
    assert payload["interval"] == "1m"
    assert "bars" not in payload["symbols"]["MSFT"]
    msft = {m["horizon"]: m for m in payload["symbols"]["MSFT"]["moves"]}
    ko = {m["horizon"]: m for m in payload["symbols"]["KO"]["moves"]}
    assert msft["5m"]["pct"] > ko["5m"]["pct"]


def test_compare_symbols_caps_the_list(tmp_path):
    payload = mcp.tool_compare_symbols({
        "symbols": [f"S{i}" for i in range(9)],
        "event_at": "2026-06-25T14:00:00Z",
    }, down_ctx(tmp_path))
    assert payload["error"]["code"] == "bad_argument"
    assert "cap is 8" in payload["error"]["message"]


# --------------------------------------------------------- data_coverage ----

def test_data_coverage_reads_the_store_and_the_ledger(tmp_path):
    seed(tmp_path, interval="1d", start="2026-08-03T04:00:00Z", n=5)
    conn = jobs.open_db(str(tmp_path))
    jobs.set_ledger(conn, "MSFT", "1d", "2026-08-03", "ok", 1, "rth", "", 1_756_600_000)
    conn.close()

    payload = mcp.tool_data_coverage({"symbol": "MSFT", "interval": "1d"},
                                     down_ctx(tmp_path))

    assert payload["symbol"] == "MSFT"
    held = payload["intervals"][0]
    assert held["interval"] == "1d"
    assert held["n_bars"] == 5
    assert held["ledger"] == {"ok": 1}
    assert held["last_pull"] == barlib.to_iso(1_756_600_000)
    # no contract cached, so no head timestamp was asked for and it says why
    assert payload["earliest_ibkr_holds"] is None
    assert "contract cache" in payload["head_note"]


def test_data_coverage_stays_offline_when_asked(tmp_path):
    seed(tmp_path, interval="1d", n=3)
    payload = mcp.tool_data_coverage({"symbol": "MSFT", "include_head": False},
                                     down_ctx(tmp_path))
    assert "earliest_ibkr_holds" not in payload
    assert payload["budget"]["limit"] == jobs.PACING_LIMIT


# -------------------------------------------------- resolve and list_symbols --

def test_resolve_contract_returns_the_gateway_down_error(tmp_path):
    payload = mcp.tool_resolve_contract({"query": "NSRGY"}, down_ctx(tmp_path))
    assert payload["error"]["code"] == "gateway_down"
    assert payload["error"]["login_url"] == "http://127.0.0.1:8642"
    assert payload["error"]["retryable"] is False


def test_resolve_contract_serves_the_cache_without_a_gateway(tmp_path):
    import contracts as contractlib

    contractlib.save({"NSRGY": {"symbol": "NSRGY", "con_id": 4458964,
                                "sec_type": "STK", "exchange": "SMART",
                                "primary_exchange": "PINK", "currency": "USD",
                                "long_name": "NESTLE SA-SPONSORED ADR",
                                "resolved_ts": 1_756_600_000}}, str(tmp_path))

    payload = mcp.tool_resolve_contract({"query": "nsrgy"}, down_ctx(tmp_path))
    assert payload["source"] == "cache"
    assert payload["contract"]["con_id"] == 4458964


def test_list_symbols_without_a_universe_file(tmp_path):
    payload = mcp.tool_list_symbols({}, down_ctx(tmp_path))
    assert payload["universe"] == []
    assert payload["count"] == 0
    assert payload["source"].endswith("universe.json")
    assert "M4" in payload["note"]


def test_list_symbols_reads_a_universe_file(tmp_path):
    seed(tmp_path, interval="1d", n=4)
    (tmp_path / "universe.json").write_text(json.dumps(
        [{"symbol": "MSFT", "case": "big-tech"}]))

    payload = mcp.tool_list_symbols({}, down_ctx(tmp_path))
    assert payload["count"] == 1
    entry = payload["universe"][0]
    assert entry["symbol"] == "MSFT"
    assert entry["case"] == "big-tech"
    assert entry["coverage"]["1d"]["n_bars"] == 4
    assert "note" not in payload


# ------------------------------------------------------------- get_quote ----

def test_get_quote_reports_gateway_down(tmp_path):
    payload = mcp.tool_get_quote({"symbol": "msft"}, down_ctx(tmp_path))

    assert payload["error"]["code"] == "gateway_down"
    assert payload["error"]["retryable"] is False


def test_get_quote_without_a_symbol(tmp_path):
    payload = mcp.tool_get_quote({}, down_ctx(tmp_path))
    assert payload["error"]["code"] == "bad_argument"


# -------------------------------------------------------------- protocol ----

def test_every_planned_tool_is_registered_and_described():
    planned = ["ibkr_status", "ibkr_list_symbols", "ibkr_resolve_contract",
               "ibkr_get_bars", "ibkr_event_window", "ibkr_compare_symbols",
               "ibkr_data_coverage", "ibkr_query_sql", "ibkr_get_quote",
               "ibkr_option_chain", "ibkr_option_snapshot",
               "ibkr_capture_option_snapshots", "ibkr_get_option_snapshots",
               "ibkr_market_data_capabilities"]
    assert mcp.TOOL_NAMES == planned
    assert sorted(mcp.TOOL_HANDLERS) == sorted(planned)
    for tool in mcp.TOOLS:
        assert len(tool["description"]) > 40
        assert tool["inputSchema"]["type"] == "object"


def test_call_tool_marks_a_structured_error(tmp_path):
    result = mcp.call_tool("ibkr_query_sql", {"sql": "DROP VIEW bars"},
                           down_ctx(tmp_path))
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "query_rejected"
    assert json.loads(result["content"][0]["text"])["error"]["code"] == "query_rejected"


def test_call_tool_rejects_an_unknown_name(tmp_path):
    with pytest.raises(mcp.RpcError):
        mcp.call_tool("ibkr_place_order", {}, down_ctx(tmp_path))


def test_initialize_then_tools_list(tmp_path):
    session = mcp.Session(down_ctx(tmp_path))

    reply = mcp.handle_message(session, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"}})
    assert reply["result"]["protocolVersion"] == "2025-06-18"
    assert reply["result"]["serverInfo"]["name"] == mcp.SERVER_NAME
    assert "coverage block" in reply["result"]["instructions"]

    listed = mcp.handle_message(session, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in listed["result"]["tools"]] == mcp.TOOL_NAMES
    # a handshake-era caller gets no 2026-07-28 envelope fields
    assert "resultType" not in listed["result"]


def test_a_notification_gets_no_reply(tmp_path):
    session = mcp.Session(down_ctx(tmp_path))
    assert mcp.handle_message(session, {"jsonrpc": "2.0",
                                        "method": "notifications/initialized"}) is None


def test_an_unknown_method_is_a_protocol_error(tmp_path):
    session = mcp.Session(down_ctx(tmp_path))
    reply = mcp.handle_message(session, {"jsonrpc": "2.0", "id": 3, "method": "nope"})
    assert reply["error"]["code"] == mcp.E_METHOD_NOT_FOUND


def test_parse_duration_reads_the_spellings():
    assert mcp.parse_duration("30s") == 30
    assert mcp.parse_duration("30m") == 1800
    assert mcp.parse_duration("2h") == 7200
    assert mcp.parse_duration("3d") == 259200
    assert mcp.parse_duration(90) == 90
    assert mcp.parse_duration(None, 600) == 600
    with pytest.raises(ValueError):
        mcp.parse_duration("2 fortnights")
