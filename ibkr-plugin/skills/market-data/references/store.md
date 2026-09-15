# Store layout

`shared/data/ibkr/` holds the whole cache: parquet bar files plus a SQLite
ledger. Everything under it is gitignored.

```
shared/data/ibkr/
  bars/1d/MSFT/2026.parquet          one file per symbol-year
  bars/1m/MSFT/2026-06-25.parquet    one file per symbol-day (event pulls)
  bars/5m/MSFT/2026.parquet
  quotes/2026-08-31.parquet         v2 live-quote snapshots, appended
  jobs.db                           SQLite: coverage ledger, requests, errors
  contracts.json                    symbol -> conId cache
```

The path encodes interval and symbol, so a re-run of one symbol-day or
symbol-year overwrites exactly that file. No merge step, no partial-write
risk against a file another process is reading.

## Bar columns

Every parquet file, regardless of interval, carries the same columns:

| Column | Meaning |
|---|---|
| `ts` | Unix seconds, UTC |
| `symbol` | Ticker as tracked in `universe.json` |
| `interval` | IBKR's own interval name, e.g. `1 day`, `1 min`, `30 secs` |
| `open`, `high`, `low`, `close` | Bar OHLC |
| `volume` | Bar volume |
| `wap` | Volume-weighted average price for the bar |
| `bar_count` | Number of trades IBKR aggregated into the bar |
| `session` | `rth` or `eth`. Never compare a `eth` print against a `rth` close without checking this column. |
| `source` | Where the row came from (`fetched`, backfill, etc.) |
| `pulled_ts` | When this row was written to the store |

## DuckDB glob examples

DuckDB reads the parquet files directly with no import step, globbing across
symbol-year or symbol-day files as needed:

```sql
-- all 2026 daily bars for one symbol
SELECT * FROM 'shared/data/ibkr/bars/1d/MSFT/*.parquet'
ORDER BY ts;

-- every symbol's daily bars in one query
SELECT * FROM 'shared/data/ibkr/bars/1d/*/*.parquet'
WHERE ts >= epoch('2026-01-01') 
ORDER BY symbol, ts;

-- a single event day of 1-minute bars
SELECT * FROM 'shared/data/ibkr/bars/1m/MSFT/2026-06-25.parquet'
WHERE session = 'rth'
ORDER BY ts;
```

`ibkr_query_sql` runs exactly this kind of query server-side: SELECT only,
row-capped, over the same glob patterns. Reach for it when the shaped tools
(`ibkr_get_bars`, `ibkr_event_window`, `ibkr_compare_symbols`) cannot phrase
the question, for example a cross-symbol aggregate or a filter on `wap`.

## Direct pandas / notebook access

The same globs work from pandas via DuckDB, without going through the MCP
server at all:

```python
import duckdb

df = duckdb.sql("""
    SELECT * FROM 'shared/data/ibkr/bars/1d/MSFT/*.parquet'
    ORDER BY ts
""").df()
```

Or, for a single known file, `pd.read_parquet(path)` directly. Both routes
read the same files the MCP tools read, so a coverage check done via
`ibkr_data_coverage` before a notebook session tells you what is actually on
disk to read.
