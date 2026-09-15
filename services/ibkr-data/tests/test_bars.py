"""Pure transform tests. Nothing here touches IBKR, a clock, or a disk."""

import datetime as dt
from types import SimpleNamespace

import pytest

import bars as barlib

ET = barlib.MARKET_TZ
UTC = dt.timezone.utc


def et(y, m, d, hh=0, mm=0):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


# -- intervals -------------------------------------------------------------

@pytest.mark.parametrize("spelling,expected", [
    ("1 min", "1m"), ("1min", "1m"), ("1M", "1m"), (" 1 Day ", "1d"),
    ("30 secs", "30s"), ("60 mins", "1h"), ("daily", "1d"),
])
def test_normalize_interval(spelling, expected):
    assert barlib.normalize_interval(spelling) == expected


def test_normalize_interval_rejects_unknown():
    with pytest.raises(barlib.IntervalError):
        barlib.normalize_interval("3 min")


def test_bar_size_and_seconds():
    assert barlib.bar_size("1m") == "1 min"
    assert barlib.bar_size("30 secs") == "30 secs"
    assert barlib.bar_seconds("1h") == 3600
    assert barlib.max_duration("1d") == "1 Y"
    assert barlib.is_intraday("1h") is True
    assert barlib.is_intraday("1d") is False


# -- time ------------------------------------------------------------------

def test_to_epoch_reads_naive_values_as_market_time():
    naive = dt.datetime(2026, 6, 25, 9, 30)
    assert barlib.to_epoch(naive) == et(2026, 6, 25, 9, 30)


def test_to_epoch_accepts_aware_iso_and_z():
    assert barlib.to_epoch("2026-06-25T13:30:00Z") == et(2026, 6, 25, 9, 30)
    assert barlib.to_epoch("2026-06-25T13:30:00+00:00") == et(2026, 6, 25, 9, 30)


def test_to_epoch_accepts_ibkr_wire_formats():
    assert barlib.to_epoch("20260625") == et(2026, 6, 25)
    assert barlib.to_epoch("20260625  09:30:00") == et(2026, 6, 25, 9, 30)
    assert barlib.to_epoch("20260625 09:30:00 US/Eastern") == et(2026, 6, 25, 9, 30)


def test_to_epoch_of_a_date_is_market_midnight():
    assert barlib.to_epoch(dt.date(2026, 6, 25)) == et(2026, 6, 25)


def test_to_iso_round_trip():
    assert barlib.to_iso(et(2026, 6, 25, 9, 30)) == "2026-06-25T13:30:00Z"


# -- session tagging -------------------------------------------------------

def test_rth_boundaries_are_half_open():
    assert barlib.session_of(et(2026, 6, 25, 9, 29), "1m") == "eth"
    assert barlib.session_of(et(2026, 6, 25, 9, 30), "1m") == "rth"
    assert barlib.session_of(et(2026, 6, 25, 15, 59), "1m") == "rth"
    assert barlib.session_of(et(2026, 6, 25, 16, 0), "1m") == "eth"


def test_weekend_intraday_is_never_rth():
    assert barlib.session_of(et(2026, 6, 27, 11, 0), "1m") == "eth"


def test_daily_bars_carry_the_requested_session():
    ts = et(2026, 6, 25)
    assert barlib.session_of(ts, "1d", requested="eth") == "eth"
    assert barlib.session_of(ts, "1d", requested="rth") == "rth"


def test_session_tagging_survives_a_dst_change():
    # 2026-01-15 is EST, 2026-07-15 is EDT; 09:30 local is rth in both
    assert barlib.session_of(et(2026, 1, 15, 9, 30), "1m") == "rth"
    assert barlib.session_of(et(2026, 7, 15, 9, 30), "1m") == "rth"
    # 14:00 UTC is 09:00 EST (eth) but 10:00 EDT (rth)
    assert barlib.session_of(int(dt.datetime(2026, 1, 15, 14, 0, tzinfo=UTC).timestamp()),
                             "1m") == "eth"
    assert barlib.session_of(int(dt.datetime(2026, 7, 15, 14, 0, tzinfo=UTC).timestamp()),
                             "1m") == "rth"


def test_partition_key_by_interval():
    ts = et(2026, 6, 25, 9, 30)
    assert barlib.partition_key(ts, "1m") == "2026-06-25"
    assert barlib.partition_key(et(2026, 6, 25), "1d") == "2026"


def test_partition_key_uses_market_date_not_utc_date():
    # 20:00 US/Eastern on 24 June is 00:00 UTC on 25 June
    ts = et(2026, 6, 24, 20, 0)
    assert barlib.partition_key(ts, "1m") == "2026-06-24"


# -- BarData to rows -------------------------------------------------------

def fake_bar(date, o=1.0, h=2.0, l=0.5, c=1.5, v=100.0, wap=1.4, n=7):
    return SimpleNamespace(date=date, open=o, high=h, low=l, close=c,
                           volume=v, average=wap, barCount=n)


def test_bars_to_rows_shape_and_types():
    raw = [fake_bar(dt.datetime(2026, 6, 25, 9, 30)),
           fake_bar(dt.datetime(2026, 6, 25, 9, 31))]
    rows = barlib.bars_to_rows(raw, "msft", "1 min", pulled_ts=1756600000)
    assert [r["ts"] for r in rows] == [et(2026, 6, 25, 9, 30), et(2026, 6, 25, 9, 31)]
    row = rows[0]
    assert row["symbol"] == "MSFT"
    assert row["interval"] == "1m"
    assert row["session"] == "rth"
    assert row["source"] == "ibkr"
    assert row["pulled_ts"] == 1756600000
    assert row["wap"] == 1.4
    assert row["bar_count"] == 7
    assert set(row) == {"ts", "symbol", "interval", "open", "high", "low", "close",
                        "volume", "wap", "bar_count", "session", "source", "pulled_ts"}


def test_bars_to_rows_sorts_and_dedupes():
    raw = [fake_bar(dt.datetime(2026, 6, 25, 9, 31), c=2.0),
           fake_bar(dt.datetime(2026, 6, 25, 9, 30)),
           fake_bar(dt.datetime(2026, 6, 25, 9, 31), c=3.0)]
    rows = barlib.bars_to_rows(raw, "MSFT", "1m", 0)
    assert len(rows) == 2
    assert rows[1]["close"] == 3.0     # the last one seen wins


def test_bars_to_rows_handles_daily_dates_and_dicts():
    rows = barlib.bars_to_rows([{"date": dt.date(2026, 6, 25), "open": 1, "high": 2,
                                 "low": 1, "close": 2, "volume": 5,
                                 "average": 1.5, "barCount": 3}],
                               "MSFT", "1 day", 0, requested_session="rth")
    assert rows[0]["ts"] == et(2026, 6, 25)
    assert rows[0]["session"] == "rth"


def test_bars_to_rows_on_an_empty_answer():
    assert barlib.bars_to_rows([], "MSFT", "1m", 0) == []
    assert barlib.bars_to_rows(None, "MSFT", "1m", 0) == []


def test_missing_bar_count_becomes_minus_one():
    bar = SimpleNamespace(date=dt.datetime(2026, 6, 25, 9, 30), open=1, high=1,
                          low=1, close=1, volume=0, average=float("nan"))
    rows = barlib.bars_to_rows([bar], "MSFT", "1m", 0)
    assert rows[0]["bar_count"] == -1
    assert rows[0]["wap"] == 0.0     # NaN never reaches the parquet file


# -- coverage --------------------------------------------------------------

def test_expected_starts_daily_skips_weekends():
    # Mon 2026-06-22 to Sun 2026-06-28
    got = barlib.expected_starts(et(2026, 6, 22), et(2026, 6, 29), "1d")
    assert len(got) == 5
    assert got[0] == et(2026, 6, 22)
    assert got[-1] == et(2026, 6, 26)


def test_expected_starts_rth_minute_day_is_390():
    got = barlib.expected_starts(et(2026, 6, 25), et(2026, 6, 26), "1m", "rth")
    assert len(got) == 390
    assert got[0] == et(2026, 6, 25, 9, 30)
    assert got[-1] == et(2026, 6, 25, 15, 59)


def test_expected_starts_rth_thirty_second_day_is_780():
    got = barlib.expected_starts(et(2026, 6, 25), et(2026, 6, 26), "30s", "rth")
    assert len(got) == 780


def test_expected_starts_unknowable_for_eth():
    assert barlib.expected_starts(et(2026, 6, 25), et(2026, 6, 26), "1m", "eth") is None


def _minute_rows(start, count, symbol="MSFT"):
    return [{"ts": start + 60 * i, "symbol": symbol, "interval": "1m",
             "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
             "wap": 1.0, "bar_count": 1, "session": "rth", "source": "ibkr",
             "pulled_ts": 0} for i in range(count)]


def test_coverage_complete_rth_day():
    rows = _minute_rows(et(2026, 6, 25, 9, 30), 390)
    cov = barlib.coverage(rows, "MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26))
    assert cov["n_bars"] == 390
    assert cov["expected_bars"] == 390
    assert cov["gaps"] == []
    assert cov["complete"] is True
    assert cov["asked"]["start"] == "2026-06-25T04:00:00Z"
    assert cov["got"]["start"] == "2026-06-25T13:30:00Z"


def test_coverage_reports_a_hole_in_the_middle():
    rows = _minute_rows(et(2026, 6, 25, 9, 30), 60)
    rows += _minute_rows(et(2026, 6, 25, 10, 35), 325)
    cov = barlib.coverage(rows, "MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26))
    assert cov["complete"] is False
    assert len(cov["gaps"]) == 1
    gap = cov["gaps"][0]
    assert gap["start"] == barlib.to_iso(et(2026, 6, 25, 10, 30))
    assert gap["end"] == barlib.to_iso(et(2026, 6, 25, 10, 35))
    assert gap["n_bars"] == 5


def test_coverage_counts_a_short_tail_as_a_gap():
    rows = _minute_rows(et(2026, 6, 25, 9, 30), 40)
    cov = barlib.coverage(rows, "MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26))
    assert cov["n_bars"] == 40
    assert cov["expected_bars"] == 390
    assert cov["complete"] is False
    assert cov["gaps"][-1]["end"] == barlib.to_iso(et(2026, 6, 25, 16, 0))


def test_coverage_on_an_empty_store():
    cov = barlib.coverage([], "MSFT", "1d", et(2026, 6, 22), et(2026, 6, 27))
    assert cov["n_bars"] == 0
    assert cov["got"]["start"] is None
    assert cov["complete"] is False
    assert barlib.missing_spans(cov) == [(et(2026, 6, 22), et(2026, 6, 27))]


def _daily_rows(dates, symbol="MSFT"):
    return [{"ts": et(*d), "symbol": symbol, "interval": "1d", "open": 1.0,
             "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "wap": 1.0,
             "bar_count": 1, "session": "rth", "source": "ibkr", "pulled_ts": 0}
            for d in dates]


def test_daily_coverage_ignores_the_weekend():
    rows = _daily_rows([(2026, 6, 25), (2026, 6, 26), (2026, 6, 29)])
    cov = barlib.coverage(rows, "MSFT", "1d", et(2026, 6, 25), et(2026, 6, 30))
    assert cov["expected_bars"] == 3
    assert cov["gaps"] == []
    assert cov["complete"] is True


def test_daily_coverage_shows_a_holiday_as_a_one_weekday_gap():
    # 2026-07-03 is a Friday and a US market holiday
    rows = _daily_rows([(2026, 7, 2), (2026, 7, 6)])
    cov = barlib.coverage(rows, "MSFT", "1d", et(2026, 7, 2), et(2026, 7, 7))
    assert len(cov["gaps"]) == 1
    assert cov["gaps"][0]["start"] == barlib.to_iso(et(2026, 7, 3))
    assert cov["gaps"][0]["n_bars"] == 1


def test_eth_coverage_reports_no_expected_grid():
    rows = _minute_rows(et(2026, 6, 25, 4, 0), 10)
    for row in rows:
        row["session"] = "eth"
    cov = barlib.coverage(rows, "MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26),
                          session="eth")
    assert cov["expected_bars"] is None
    assert cov["gaps"] == []


def test_eth_coverage_finds_a_same_day_run():
    rows = _minute_rows(et(2026, 6, 25, 4, 0), 5)
    rows += _minute_rows(et(2026, 6, 25, 4, 10), 5)
    cov = barlib.coverage(rows, "MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26),
                          session="eth")
    assert len(cov["gaps"]) == 1
    assert cov["gaps"][0]["n_bars"] == 5


def test_missing_spans_merge_and_sort():
    rows = _minute_rows(et(2026, 6, 25, 9, 30), 60)
    rows += _minute_rows(et(2026, 6, 25, 10, 35), 60)
    cov = barlib.coverage(rows, "MSFT", "1m", et(2026, 6, 25), et(2026, 6, 26))
    spans = barlib.missing_spans(cov)
    assert spans == sorted(spans)
    for left, right in zip(spans, spans[1:]):
        assert left[1] < right[0]
    assert spans[0] == (et(2026, 6, 25, 10, 30), et(2026, 6, 25, 10, 35))
