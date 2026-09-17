# Store layout

The default root is `shared/data/ibkr/`. Set `LACUNA_IBKR_ROOT` for every
service and CLI process that must share an existing cache. Set
`LACUNA_IBKR_UNIVERSE` as well when the watchlist lives outside this repository.
Mixing roots makes valid cached data appear missing.

```text
shared/data/ibkr/
  bars/1d/MSFT/2026.parquet                    legacy STK/TRADES cache
  bars-v2/TRADES/rth/1d/265598/2026.parquet   series + session + conId
  bars-v2/BID/rth/5m/922317899/2026.parquet
  snapshots/922317899/2026-09-18.parquet      captured point-in-time fields
  jobs.db                                      pacing and coverage ledger
  contracts.json                               qualified contract cache
```

Every newly qualified stock or non-stock write uses v2. The `bars/` layout is
read compatibility for existing unqualified equity trade files only; new stock
writes do not extend it. The v2 key includes `con_id`, `data_type`, interval,
session and timestamp.
This prevents two option strikes, or trade and bid bars for one contract, from
overwriting one another. Existing stock trade parquet under `bars/` remains
readable for compatibility.

## Bar rows

V2 rows preserve the exact contract and dataset identity alongside OHLC data:

| Field | Meaning |
|---|---|
| `ts` | Unix seconds UTC; bar start |
| `con_id` | Stable IBKR contract ID |
| `symbol`, `local_symbol` | Display and exchange contract symbols; the local symbol carries the option definition |
| `sec_type` | `STK`, `OPT`, `IND`, `FUT`, `CONTFUT`, `CASH`, etc. |
| `exchange`, `currency` | Qualified venue and currency metadata |
| `expiry`, `strike`, `right`, `multiplier`, `trading_class` | Derivative identity; null when inapplicable |
| `underlying_con_id`, `time_zone` | Underlying identity and qualified contract time zone |
| `session_date`, `date_anchor` | Exchange-calendar date and how a daily timestamp was anchored |
| `interval` | Canonical interval such as `1m` or `1d` |
| `data_type` | `TRADES`, `BID`, `ASK`, `MIDPOINT`, `BID_ASK`, `ADJUSTED_LAST`, `HISTORICAL_VOLATILITY`, or `OPTION_IMPLIED_VOLATILITY` |
| `open`, `high`, `low`, `close` | Bar values for that data series |
| `volume`, `wap`, `bar_count` | Nullable series metadata; do not assume it applies to quote or volatility bars |
| `session` | Generalized request mode: `rth` is regular hours only; `eth` is all available hours (`useRTH=false`) |
| `source`, `pulled_ts` | Provenance and collection time |

Intraday requests preserve UTC instants. `eth` is not an after-hours-only filter: all-hours results can contain regular
hours too. Legacy US-equity event rows may instead be clock-classified `rth` or
`eth` using 09:30–16:00 Eastern. Daily timestamps anchor midnight in the
resolved exchange timezone; `session_date` is the calendar date and
`date_anchor` records the anchor or explicit UTC fallback. Session calendars vary
by asset; a US equity 09:30–16:00 expectation must not be imposed on FX, futures
or indexes.

## Captured option snapshots

Snapshot files contain the full qualified contract, point-in-time quotes, sizes,
model and quote-side Greeks, open interest, volume, actual market-data type,
per-field and overall status, warnings/errors and the service collection
timestamp. Missing sentinels are stored as null. Nested fields are JSON on disk
and are decoded by the cache-only snapshot tool.
Collection time is not an exchange timestamp.

Each capture appends/deduplicates safely. The files contain only observations
made after capture began. There is no automatic scheduler and no historical
backfill of strike-level Greeks, IV or open interest.

## Queries and notebooks

Prefer shaped MCP tools because they attach contract and coverage metadata.
`ibkr_query_sql` is SELECT-only and row-capped. When querying parquet directly,
filter on `con_id` and `data_type`, not only `symbol`:

```sql
SELECT *
FROM 'shared/data/ibkr/bars-v2/BID/rth/5m/922317899/*.parquet'
ORDER BY ts;
```

The same glob can be read with DuckDB or pandas. For historical strike-level
research, prefer `ibkr_get_option_snapshots`; for direct parquet queries retain
`collected_ts`, `field_status`, status, warnings and errors. Null means
unavailable in that capture, not zero.
