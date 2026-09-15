# Event studies

`ibkr_event_window` answers one question: what did a name do around a dated
event. It exists because free Yahoo-backed data runs out of 1-minute bars at
30 days and has no 30-second bars at any date.

## `event_window` semantics

Arguments: `symbol`, `event_at`, `before`, `after`, `interval` (optional).

`event_at` is an ISO 8601 UTC timestamp: the moment the event happened, not
the moment it was noticed. `before` and `after` are durations (e.g. `30m`,
`2h`, `1d`) defining the window pulled around it. The response returns:

- The bars themselves, at the interval used.
- A `coverage` block, same contract as `ibkr_get_bars` (see `pacing.md`).
- Moves computed at the four standard horizons, so two names measured around
  the same moment are directly comparable.

If `interval` is left unset, the tool picks the finest interval retention
actually allows for that symbol and date, rather than defaulting to something
coarse. That is a live decision made per request, since
retention depends on how far back the event is.

## Horizons

The event window defines four horizons:

```python
HORIZONS = {"30s": pd.Timedelta(seconds=30),
            "5m": pd.Timedelta(minutes=5),
            "1h": pd.Timedelta(hours=1),
            "1d": pd.Timedelta(days=1)}
```

`ibkr_event_window` reports moves at these same four horizons. Yahoo's
module could reach `30s` at no date and `1m` only inside the last 30 days;
IBKR's tool reaches `30s` at any date subject to actual data coverage, which
is the entire reason it exists. When both a Yahoo-backed number and an
IBKR-backed number exist for the same event, prefer the IBKR one and say so,
since it is measured rather than coarsened.

## `rth` versus `eth`

Every bar carries a `session` field, `rth` (regular trading hours) or `eth`
(extended, meaning pre-market and after-hours). News that lands before
the 09:30 US Eastern open or after the 16:00 close needs `eth` bars to see
the market's first reaction; `rth`-only bars would show nothing until the
next open, hours late. Pass `session` explicitly when the event time is
outside 09:30 to 16:00 US Eastern. `ibkr_event_window` defaults to including
both, tagged, so the caller decides which to read rather than the tool
deciding for them.

## Verified retention facts

Live testing established, against a read-only secondary username:

- **30-second bars go back at least 3 years**, far beyond the 6 months IBKR
  documents as the ceiling for sub-minute data. Verified with full 780-bar
  sessions at 2 months, 6 months, 14 months, and 3 years back.
- **1-minute bars returned full 390-bar sessions at every one of those same
  distances.**
- Daily history reaches back decades. AAPL goes to 1980-12-12.

None of this is a promise for every symbol. IBKR's true per-contract limit is
`reqHeadTimeStamp`, the earliest bar it will ever serve for that contract,
and it varies by name. **Verify per contract with `ibkr_data_coverage`
before promising a date range to a person**, especially at 30-second
resolution and especially for a name not yet tested. Treat the numbers above
as evidence the documented ceiling is wrong rather than as a new ceiling to trust
blindly in its place.
