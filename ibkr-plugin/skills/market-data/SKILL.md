---
name: market-data
description: Use for read-only IBKR market-data work across equities, options, indexes, futures and FX, including contract resolution, option chains, delayed quotes and Greeks, historical bars, captured option snapshots, event studies, coverage and gateway state.
---

# IBKR market data

Use the `ibkr_` tools for read-only contract and market-data questions. They do
not expose orders, positions or account values. Do not attempt to add or infer
trading actions through this skill.

## Choose the operation

| Need | Tool |
|---|---|
| Check the service, gateway, pacing budget or store | `ibkr_status` |
| List the configured equity watchlist | `ibkr_list_symbols` |
| Resolve a stock or a precise instrument | `ibkr_resolve_contract` |
| Discover expirations, strikes and option contracts | `ibkr_option_chain` |
| Read a delayed quote for one instrument | `ibkr_get_quote` |
| Read delayed quotes, Greeks and open interest for options | `ibkr_option_snapshot` |
| Save current option snapshots for later research | `ibkr_capture_option_snapshots` |
| Read option snapshots already saved locally | `ibkr_get_option_snapshots` |
| Explain implemented series and optionally probe gateway connectivity | `ibkr_market_data_capabilities` |
| Get historical bars for an instrument and data series | `ibkr_get_bars` |
| Analyze one stock around an event | `ibkr_event_window` |
| Compare several watched stocks around an event | `ibkr_compare_symbols` |
| Inspect cached coverage | `ibkr_data_coverage` |
| Query stored parquet data when shaped tools cannot express the question | `ibkr_query_sql` |

For options, resolve by `con_id` whenever one is already known. Otherwise give
the full selector: underlying, expiry, strike, right, exchange/currency where
needed, multiplier and trading class when ambiguity remains. A ticker does not
uniquely identify an option.

`ibkr_option_chain` returns listed expirations and strikes plus qualified
contracts when requested. The expiration and strike sets are not a Cartesian
product; use returned qualified contracts rather than inventing combinations.
Bound the expiry/strike/right selection before asking for snapshots. The tools
also impose caps so one request cannot subscribe to an entire large chain.

## Interpret snapshots carefully

Quote and option snapshot responses report the actual market-data type and a
collection time. On accounts without live subscriptions this is commonly
delayed data. The collection time is when this service received the values; it
is not an exchange trade timestamp and does not measure the exact delay.

Snapshot fields can arrive at different times. Read `field_status`, the overall
status, warnings/errors and populated fields, and preserve useful partial
results. A subscription warning can coexist with valid delayed prices or model
Greeks. Missing IBKR sentinels are normalized to null; legitimate negative put
delta and theta remain valid values.

`model_greeks` is generally the most complete current model result. Bid, ask or
last Greeks may be unavailable. Open interest and volume are point-in-time
fields when returned, not historical series. Do not use implausible volume
values without checking them against another source.

`ibkr_capture_option_snapshots` saves the current response. Repeated captures
can build local strike-level IV, Greek, quote and open-interest history from the
time collection begins. It does not backfill past snapshots and no capture
schedule is enabled automatically.

`ibkr_get_option_snapshots` is cache-only: pass `con_id` and optional start/end
times. It never contacts IBKR or spends pacing budget. Null fields remain null;
use their saved `field_status`, warnings and collection time when comparing
captures.

## Historical series

`ibkr_get_bars` accepts an exact contract and a `data_type`: `TRADES`, `BID`,
`ASK`, `MIDPOINT`, `BID_ASK`, `ADJUSTED_LAST`, `HISTORICAL_VOLATILITY`, or
`OPTION_IMPLIED_VOLATILITY`. Support depends on the instrument. Use
`ibkr_market_data_capabilities` when choosing a series.

IBKR serves intraday historical trade and quote bars for active options, but a
direct daily option or futures-option request fails with no end-of-day option
chart data. Do
not present an intraday aggregation as an exchange daily bar unless you derive
and label it, and only do so when coverage is sufficient.

`OPTION_IMPLIED_VOLATILITY` and `HISTORICAL_VOLATILITY` are underlying-level
historical series. They are not a past strike-by-strike IV surface or historical
Greeks. Strike-level history exists only for snapshots captured locally after
collection starts.

Sparse option `TRADES` bars can mean no trade occurred. Compare BID, ASK or
MIDPOINT coverage before calling a missing trade bar a data outage. A returned
head timestamp is the earliest result for that contract and series, not a
universal IBKR retention promise.

For generalized history, `session=rth` requests regular trading hours only;
`session=eth` maps to IBKR `useRTH=false` and therefore includes all available
hours, including regular hours. It does not mean an after-hours-only dataset.
Legacy US-equity event data can still carry row-level `rth`/`eth` labels based
on the 09:30–16:00 Eastern clock.

Continuous futures (`CONTFUT`) accept one current-window chunk because IBKR
rejects an explicit historical end time for them. Treat that as a current
continuous series, not a promise that arbitrary dated windows can be backfilled.

`ADJUSTED_LAST` also requires an empty IBKR end time. The service can fetch one
bounded chunk from now back through the requested start, then filters the stored
result to the exact requested window. If the start lies beyond one permitted
chunk for the interval, it returns `adjusted_last_current_window_limit`; choose
a coarser interval or another series instead of retrying the same request.

## Coverage, pacing and recovery

Read the `coverage` block before using historical results. Its contract and
`data_type` identify the dataset; its gaps and request metadata distinguish
cached data, pacing denial, an empty IBKR response and actual observations.
Trade and quote datasets are stored separately.

`ibkr_market_data_capabilities(probe=true)` checks whether the gateway answers
and reports the static interface matrix. It does not prove that this account is
entitled to every listed asset, exchange, field or historical series. Use a
bounded request for the exact contract to establish actual availability.

IBKR historical pacing is shared by every caller: 60 request units per rolling
10 minutes, with `BID_ASK` spending two. The local ledger enforces this budget.
Never bypass it. Read [references/pacing.md](references/pacing.md) before a wide
or fine-grained pull.

If a call fails on session state, use `ibkr_status`. Give the returned login URL
to the human and stop. Never submit credentials, type a two-factor code or retry
a failed login programmatically. A running local socket alone does not prove the
upstream market-data farms are healthy; a small data request is the useful check.

## References

- Read [references/store.md](references/store.md) before direct parquet,
  DuckDB or notebook access, or when configuring a shared store.
- Read [references/pacing.md](references/pacing.md) before bulk history pulls.
- Read [references/event-studies.md](references/event-studies.md) for stock
  event-window semantics, sessions and retention caveats.
