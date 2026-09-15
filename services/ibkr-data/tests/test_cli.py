"""Tests for the pure watchlist logic in cli.py.

Nothing here opens a socket or a store. The three functions under test are what
decide which names get walked and which session dates get asked for, so they are
the parts where a mistake costs either a wrong universe or a re-run that spends
the whole pacing budget on data already held.
"""

from __future__ import annotations

import json

import cli


# ------------------------------------------------------------ parsing ----

def test_parse_accepts_the_shipped_file_and_normalizes():
    entries = cli.parse_universe({
        "_comment": "ignored",
        "symbols": [
            {"symbol": "msft", "case": "big-tech", "intervals": ["1d"]},
            {"ticker": "ko", "case_slug": "beverages"},
            "nke",
        ],
    })

    assert [e["symbol"] for e in entries] == ["MSFT", "KO", "NKE"]
    assert entries[1]["case"] == "beverages"
    assert entries[2]["intervals"] == ["1d"]   # the default the backfill walks


def test_parse_accepts_a_bare_list_and_a_universe_key():
    assert cli.parse_universe([{"symbol": "MSFT", "case": "x"}])[0]["symbol"] == "MSFT"
    assert cli.parse_universe({"universe": ["msft"]})[0]["symbol"] == "MSFT"
    assert cli.parse_universe({}) == []


def test_parse_drops_underscore_keys_and_junk_rows():
    entries = cli.parse_universe({"symbols": [{"symbol": "MSFT", "_note": "x"}, 7]})
    assert len(entries) == 1
    assert "_note" not in entries[0]


# --------------------------------------------------------- validation ----

def test_a_good_universe_has_no_problems():
    entries = cli.parse_universe([{"symbol": "MSFT", "intervals": ["1d"]}])
    assert cli.validate_universe(entries) == []


def test_validation_names_every_fault():
    entries = cli.parse_universe([
        {"symbol": "MSFT", "intervals": ["1d"]},
        {"symbol": "MSFT", "intervals": ["1d"]},
        {"symbol": "", "intervals": ["1d"]},
        {"symbol": "NKE", "intervals": []},
        {"symbol": "ORCL", "intervals": ["1 fortnight"]},
    ])
    problems = " | ".join(cli.validate_universe(entries))

    assert "MSFT appears more than once" in problems
    assert "has no symbol" in problems
    assert "NKE has no intervals" in problems
    assert "unknown interval" in problems


def test_the_shipped_universe_parses_and_validates():
    entries, _ = cli.read_universe()
    assert cli.validate_universe(entries) == []
    assert entries, "the shipped watchlist should hold at least one symbol"


def test_an_entry_without_a_case_field_is_valid():
    entries = cli.parse_universe({"symbols": [
        {"symbol": "AAPL", "intervals": ["1d"]},
        {"symbol": "MSFT", "case": "anything", "intervals": ["1d"]},
    ]})
    assert cli.validate_universe(entries) == []
    assert entries[0]["case"] is None


# ------------------------------------------------------------- resume ----

def test_pending_dates_returns_only_what_never_settled():
    ledger = {
        "2026-06-22": {"status": "ok"},
        "2026-06-23": {"status": "no_data"},   # market shut, final
        "2026-06-24": {"status": "empty"},     # IBKR answered with nothing, retry
        "2026-06-25": {"status": "error"},
    }
    dates = ["2026-06-22", "2026-06-23", "2026-06-24", "2026-06-25", "2026-06-26"]

    # 26th has no row at all, so it has never been asked for
    assert cli.pending_dates(ledger, dates) == ["2026-06-24", "2026-06-25", "2026-06-26"]


def test_a_finished_backfill_asks_for_nothing():
    dates = ["2026-06-22", "2026-06-23"]
    ledger = {d: {"status": "ok"} for d in dates}
    assert cli.pending_dates(ledger, dates) == []


# -------------------------------------------------------------- write ----

def test_write_universe_keeps_the_file_shape(tmp_path):
    path = tmp_path / "universe.json"
    path.write_text(json.dumps({"_comment": "keep me",
                                "symbols": [{"symbol": "MSFT", "case": "big-tech",
                                             "con_id": None, "intervals": ["1d"]}]}))
    entries, raw = cli.read_universe(path)
    entries[0]["con_id"] = 29110391
    entries[0]["primary_exchange"] = "NYSE"
    cli.write_universe(raw, entries, path)

    written = json.loads(path.read_text())
    assert written["_comment"] == "keep me"
    assert written["symbols"][0]["con_id"] == 29110391
    assert written["symbols"][0]["primary_exchange"] == "NYSE"
    assert written["symbols"][0]["case"] == "big-tech"
