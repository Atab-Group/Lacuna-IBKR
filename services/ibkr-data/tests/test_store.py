"""Parquet round trip and the DuckDB surface, all in a tmp directory."""

import datetime as dt

import pytest

import bars as barlib
import store

ET = barlib.MARKET_TZ


def et(y, m, d, hh=0, mm=0):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


def row(ts, symbol="MSFT", interval="1m", close=1.5, session="rth", pulled=100):
    return {"ts": ts, "symbol": symbol, "interval": interval, "open": 1.0,
            "high": 2.0, "low": 0.5, "close": close, "volume": 10.0, "wap": 1.4,
            "bar_count": 3, "session": session, "source": "ibkr",
            "pulled_ts": pulled}


@pytest.fixture
def root(tmp_path):
    return tmp_path / "ibkr"


def test_partition_paths_follow_the_layout(root):
    minute = store.partition_path("msft", "1 min", "2026-06-25", root)
    assert minute == root / "bars" / "1m" / "MSFT" / "2026-06-25.parquet"
    daily = store.partition_path("MSFT", "1d", "2026", root)
    assert daily == root / "bars" / "1d" / "MSFT" / "2026.parquet"


def test_write_then_read_round_trip(root):
    rows = [row(et(2026, 6, 25, 9, 30)), row(et(2026, 6, 25, 9, 31), close=1.7)]
    written = store.write_bars(rows, root)
    assert len(written) == 1
    back = store.read_bars("MSFT", "1m", root_dir=root)
    assert [{k: item[k] for k in rows[0]} for item in back] == rows


def test_intraday_writes_one_file_per_day_daily_one_per_year(root):
    store.write_bars([row(et(2026, 6, 25, 9, 30)), row(et(2026, 6, 26, 9, 30))], root)
    assert len(store.partitions("MSFT", "1m", root)) == 2

    store.write_bars([row(et(2026, 3, 2), interval="1d"),
                      row(et(2026, 9, 2), interval="1d"),
                      row(et(2025, 9, 2), interval="1d")], root)
    daily = store.partitions("MSFT", "1d", root)
    assert [p.stem for p in daily] == ["2025", "2026"]


def test_rewriting_the_same_rows_is_idempotent(root):
    rows = [row(et(2026, 6, 25, 9, 30) + 60 * i) for i in range(5)]
    store.write_bars(rows, root)
    first = store.read_bars("MSFT", "1m", root_dir=root)
    store.write_bars(rows, root)
    store.write_bars(rows, root)
    assert store.read_bars("MSFT", "1m", root_dir=root) == first
    assert len(store.partitions("MSFT", "1m", root)) == 1


def test_a_repull_replaces_by_timestamp_and_keeps_neighbours(root):
    base = et(2026, 6, 25, 9, 30)
    store.write_bars([row(base), row(base + 60), row(base + 120)], root)
    store.write_bars([row(base + 60, close=99.0, pulled=200)], root)
    back = store.read_bars("MSFT", "1m", root_dir=root)
    assert len(back) == 3
    assert back[1]["close"] == 99.0
    assert back[1]["pulled_ts"] == 200
    assert back[0]["close"] == 1.5


def test_writing_ten_days_keeps_the_rest_of_the_year(root):
    store.write_bars([row(et(2026, 1, 5) + 86400 * i, interval="1d")
                      for i in range(200)], root)
    store.write_bars([row(et(2026, 1, 5), interval="1d", close=42.0)], root)
    back = store.read_bars("MSFT", "1d", root_dir=root)
    assert len(back) == 200
    assert back[0]["close"] == 42.0


def test_read_filters_by_range_and_session(root):
    base = et(2026, 6, 25, 9, 30)
    rows = [row(base), row(base + 60), row(base + 120, session="eth")]
    store.write_bars(rows, root)
    assert len(store.read_bars("MSFT", "1m", base, base + 60, root_dir=root)) == 1
    assert len(store.read_bars("MSFT", "1m", session="eth", root_dir=root)) == 1
    assert store.read_bars("MSFT", "1m", root_dir=root)[0]["ts"] == base


def test_read_of_an_unknown_symbol_is_empty_not_an_error(root):
    assert store.read_bars("NOPE", "1d", root_dir=root) == []
    assert store.partitions("NOPE", "1d", root) == []


def test_symbols_do_not_bleed_into_each_other(root):
    store.write_bars([row(et(2026, 6, 25, 9, 30)),
                      row(et(2026, 6, 25, 9, 30), symbol="KO", close=9.0)], root)
    msft = store.read_bars("MSFT", "1m", root_dir=root)
    assert len(msft) == 1 and msft[0]["close"] == 1.5


def test_store_stats(root):
    store.write_bars([row(et(2026, 6, 25, 9, 30)),
                      row(et(2026, 6, 25), interval="1d")], root)
    stats = store.store_stats(root)
    assert stats["files"] == 2
    assert stats["symbols"] == ["MSFT"]
    assert stats["intervals"] == ["1d", "1m"]
    assert stats["bytes"] > 0


def test_read_frame_adds_a_utc_column(root):
    store.write_bars([row(et(2026, 6, 25, 9, 30))], root)
    frame = store.read_frame("MSFT", "1m", root_dir=root)
    assert list(frame.columns)[:3] == ["ts", "dt", "symbol"]
    assert str(frame["dt"].iloc[0]) == "2026-06-25 13:30:00+00:00"


# -- DuckDB ----------------------------------------------------------------

def test_duckdb_reads_the_glob(root):
    store.write_bars([row(et(2026, 6, 25, 9, 30) + 60 * i) for i in range(3)], root)
    conn = store.connect(root)
    try:
        assert conn.execute("SELECT count(*) FROM bars").fetchone()[0] == 3
        assert conn.execute(
            "SELECT symbol, interval FROM bars LIMIT 1").fetchone() == ("MSFT", "1m")
    finally:
        conn.close()


def test_duckdb_on_a_cold_store_returns_zero_rows(root):
    conn = store.connect(root)
    try:
        assert conn.execute("SELECT count(*) FROM bars").fetchone()[0] == 0
    finally:
        conn.close()


def test_query_helper_caps_rows(root):
    store.write_bars([row(et(2026, 6, 25, 9, 30) + 60 * i) for i in range(50)], root)
    columns, rows = store.query("SELECT ts, close FROM bars ORDER BY ts",
                                root, limit=10)
    assert columns == ["ts", "close"]
    assert len(rows) == 10


@pytest.mark.parametrize("sql", [
    "DELETE FROM bars",
    "SELECT 1; DROP TABLE bars",
    "CREATE TABLE t AS SELECT 1",
    "",
])
def test_query_refuses_anything_that_is_not_a_select(root, sql):
    with pytest.raises(ValueError):
        store.query(sql, root)


def test_query_allows_a_with_clause(root):
    store.write_bars([row(et(2026, 6, 25, 9, 30))], root)
    _cols, rows = store.query(
        "WITH x AS (SELECT * FROM bars) SELECT count(*) FROM x", root)
    assert rows[0][0] == 1
