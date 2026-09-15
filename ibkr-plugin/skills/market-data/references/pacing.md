# Pacing budget

IBKR's historical data API is rate-limited, and the limit is enforced by
`jobs.py`'s shared token bucket, which every caller shares.

## The numbers

- **60 requests maximum in any rolling 10 minutes.** That is an 8,640/day
  ceiling if spread evenly, but the rolling window matters more than the
  daily total: 60 requests inside one 10-minute stretch exhausts the budget
  even if nothing else was pulled that day.
- **`BID_ASK` requests count double** against the same 60/10-minute budget.
  Ordinary trade bars (open/high/low/close/volume) count once.
- **No identical request within 15 seconds.** Same symbol, interval, date
  range, and session, requested twice inside 15 seconds: the second one is
  refused or silently degraded rather than served fresh.
- **Max 50 open requests at once.** Relevant to bulk backfills rather than a
  single tool call.
- **A violated limit degrades into a silent empty response** instead of an error.
  This is the single most important fact about the whole pacing system: "no
  bars" and "budget exhausted" look identical unless you read `coverage`.

## What the `coverage` block means

Every response from `ibkr_get_bars` and `ibkr_event_window` carries a
`coverage` block. Read it before trusting the row count:

- `requested`: the range and interval actually asked for.
- `source`: `cache` (served from the parquet store, no budget spent) or
  `fetched` (a live IBKR request was made).
- `fetched: denied`: the budget was exhausted, so the tool served whatever
  the cache already had and did not attempt a live pull. This is distinct
  from `empty`, which means IBKR was asked and genuinely returned nothing
  (a market holiday, a pre-listing date, a delisted contract).
- `gaps`: date or time ranges inside the requested window with no bars,
  each tagged with why (`empty`, `no_data`, `pending`, `error`).
- `budget_remaining`: requests left in the current 10-minute window, so a
  model can decide whether to ask for more right now or wait.

A tool that returns 40 bars when 390 were asked for is only honest because
`coverage` says so. Never infer completeness from row count alone.

## How `ibkr_get_bars` chunks a fetch

A single tool call can span more calendar time than one IBKR request is
allowed to cover in one go (finer intervals have shorter maximum durations
per request). `ibkr_get_bars` splits a wide request into multiple IBKR calls
internally, spending budget for each one, and reports the combined result
under one `coverage` block. If the budget runs out partway through a chunked
fetch, the response returns what was pulled so far plus a `gaps` entry for
the remainder instead of a partial-request error.

Because each chunk is a separate historical request, a wide finest-interval
pull (say, 30-second bars over several months) can consume a large fraction
of the 60/10-minute budget in one tool call. Ask `ibkr_data_coverage` first
if you are about to request a large range you have not pulled before, so the
budget spend is a choice rather than a surprise.
