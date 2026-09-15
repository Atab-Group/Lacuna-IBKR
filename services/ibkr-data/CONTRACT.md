# ibkr-data: internal module contract

Authoritative interface spec. Every module is written against this. Do not change a
signature or a dict key without changing this file.

## Dependency override

This service runs on the machine the person is sitting at, against a local IB Gateway on
127.0.0.1:4001, because one API session exists per IBKR username. It takes four
third-party dependencies:

| Package | Version tested | Why |
|---|---|---|
| `ib_async` | 2.1.0 | the only maintained TWS API client; `ib_insync` is archived |
| `pyarrow` | 25.0.1 | parquet read and write for the bar store |
| `duckdb` | 1.5.5 | analytical query surface over the parquet glob |
| `pandas` | 3.0.3 | frame surface for notebooks and event-study horizons |

Every one of them is installed by `setup.sh` into a local virtualenv.

## Modules

| File | Owns | Does IO? |
|---|---|---|
| `ibclient.py` | the socket to IB Gateway, and nothing else | yes, network only |
| `contracts.py` | symbol to qualified contract, cached in `contracts.json` | yes, gateway plus one JSON file |
| `bars.py` | interval names, session tagging, BarData to rows, coverage maths | no, pure functions |
| `store.py` | parquet partitions, DuckDB query surface | yes, filesystem only |
| `jobs.py` | pacing bucket, coverage ledger, `ensure_bars` orchestration | yes, sqlite plus store plus client |
| `cli.py` | argparse front end over the above | yes |

Never name a module in here `ibapi.py`. That shadows IBKR's own package import and the
failure is silent.

`bars.py` reads no clock and touches no disk. Every function that needs the current time
takes `now` as an argument rather than reading the clock. That is
what makes the tests deterministic.

## Storage layout

Root is `shared/data/ibkr/`, gitignored at `.gitignore:67`.

```
shared/data/ibkr/
  bars/1d/MSFT/2026.parquet          one file per symbol-year, daily and coarser
  bars/1m/MSFT/2026-06-25.parquet    one file per symbol-day, intraday
  bars/30s/MSFT/2026-06-25.parquet
  jobs.db                           sqlite: requests, coverage
  contracts.json                    symbol -> qualified contract cache
```

Partition rule: `1d` and anything coarser partitions by year, every intraday interval
partitions by the session date in US/Eastern. A re-pull of one event day rewrites exactly
one file.

## Data structure: ROW

One bar. Produced by `bars.bars_to_rows`, written by `store.write_bars`.

```python
{
  "ts": 1750857000,        # int, unix seconds UTC, the bar's START
  "symbol": "MSFT",         # str, upper case
  "interval": "1m",        # str, canonical short name from bars.INTERVALS
  "open": 6.71,            # float
  "high": 6.74,
  "low": 6.70,
  "close": 6.73,
  "volume": 12400.0,       # float; IBKR reports round lots for some feeds
  "wap": 6.7218,           # float, volume weighted average price
  "bar_count": 63,         # int, trades in the bar; -1 when IBKR omits it
  "session": "rth",        # "rth" | "eth", see below
  "source": "ibkr",        # str, provenance
  "pulled_ts": 1756600000, # int, unix seconds UTC, when the fetch happened
}
```

Daily bars carry no time of day. Their `ts` is 00:00 US/Eastern on the session date, so
the calendar date survives a round trip and ordering stays correct. Do not read a daily
`ts` as a UTC midnight.

`session` on an intraday bar is computed from the bar start in US/Eastern: `rth` when the
start falls on a weekday inside 09:30 to 16:00, `eth` otherwise. A bar starting at 15:59
is `rth`, one starting at 16:00 is `eth`. Daily bars carry the session that was requested,
because the request flag `useRTH` is the only thing that distinguishes them.

## Data structure: COVERAGE

Produced by `bars.coverage`. Every response that carries bars carries one of these. A tool
that returns 40 bars when 390 were asked for is how a wrong number reaches a brief.

```python
{
  "symbol": "MSFT",
  "interval": "1m",
  "session": "rth",
  "asked": {"start": "2026-06-25T00:00:00Z", "end": "2026-06-26T00:00:00Z"},
  "got":   {"start": "2026-06-25T13:30:00Z", "end": "2026-06-25T19:59:00Z"},
  "n_bars": 390,
  "expected_bars": 390,        # int|None; None when the grid is unknowable (eth)
  "gaps": [                    # ordered, non-overlapping, half-open [start, end)
     {"start": "2026-06-25T15:00:00Z", "end": "2026-06-25T15:05:00Z", "n_bars": 5},
  ],
  "complete": True,            # no gaps and nothing missing at either end
}
```

Gap rules, stated so nobody has to guess:

- `1d`: the expected grid is every weekday in the asked range. A US market holiday
  therefore shows up as a one-weekday gap. That is deliberate. The ledger, not the
  coverage block, is where a holiday gets labelled `no_data`.
- Intraday with `session="rth"`: the expected grid is every bar start from 09:30 to 16:00
  US/Eastern on each weekday in range.
- Intraday with `session="eth"`: the extended grid depends on the instrument and is not
  knowable from here, so `expected_bars` is `None` and gaps are runs where two consecutive
  bars on the same US/Eastern date sit more than one bar apart.

## Data structure: CONTRACT

Cached in `shared/data/ibkr/contracts.json`, keyed by upper case symbol.

```python
{
  "symbol": "AAPL",
  "con_id": 265598,            # int, the only stable identifier IBKR has
  "sec_type": "STK",
  "exchange": "SMART",
  "primary_exchange": "NASDAQ",  # what disambiguates an ADR from a same-named listing
  "currency": "USD",
  "long_name": "APPLE INC",
  "resolved_ts": 1756600000,   # int, unix seconds UTC
}
```

ADRs are ordinary SMART/USD stocks with an unusual `primary_exchange` (PINK, ARCA,
VALUE). Resolution never guesses: `contracts.pick` sorts the `reqContractDetails` results
and picks a USD STK, preferring the primary exchanges in `contracts.EXCHANGE_PREFERENCE`.
Ambiguity is returned to the caller as candidates instead of being resolved silently.

## Data structure: LEDGER ROW

Table `coverage` in `jobs.db`, primary key `(symbol, interval, date)` where `date` is the
US/Eastern session date as `YYYY-MM-DD`.

```python
{
  "symbol": "MSFT", "interval": "1m", "date": "2026-06-25",
  "status": "ok",          # ok | empty | no_data | error | pending
  "n_bars": 390,
  "session": "rth",
  "note": "",              # error text, or the pacing reason
  "updated_ts": 1756600000,
}
```

`empty` and `no_data` are different outcomes and conflating them destroys the ledger's
value. A violated pacing limit returns an empty response rather than an error, so an
`empty` row means IBKR answered with nothing while the request looked valid, and it should
be retried later. `no_data` means the market was shut, which is final. `error` means the
request raised. `pending` means a job claimed the slot and has not finished.

## Pacing limits

Verified numbers. They drive every decision in `jobs.py`.

| Limit | Value | Consequence of breaching it |
|---|---|---|
| historical requests per rolling window | 60 | silent empty responses |
| the window | 600 seconds | |
| BID_ASK request weight | 2 | one BID_ASK call spends two slots |
| every other `whatToShow` weight | 1 | |
| identical request cooldown | 15 seconds | silent empty responses |
| open requests at once | 50 | requests dropped |

`jobs.py` persists one row per spent request in `jobs.db`, so the budget survives a
process restart. A backfill killed and restarted at second 30 of its window does not get a
fresh 60 requests.

Max duration per request, by bar size. Asking for more than this returns an error, so
`ensure_bars` chunks against this table.

| Interval | IBKR bar size | Max duration per request |
|---|---|---|
| `30s` | `30 secs` | `28800 S` |
| `1m` | `1 min` | `1 D` |
| `5m` | `5 mins` | `1 W` |
| `15m` | `15 mins` | `1 W` |
| `30m` | `30 mins` | `1 M` |
| `1h` | `1 hour` | `1 M` |
| `1d` | `1 day` | `1 Y` |

## Function signatures

### ibclient.py

The only module that imports `ib_async`. Everything else takes a client object.

```python
HOST = "127.0.0.1"
PORT = 4001              # live gateway; the image maps host 4001 to container 4003
CLIENT_IDS = range(201, 211)   # this service's reserved band; other clients use 0, 1, 77
DELAYED = 3              # reqMarketDataType

class GatewayDown(RuntimeError): ...
    # str() is an instruction, never a stack trace: which layer is down and who acts.

class Client:
    def __init__(self, host=HOST, port=PORT, client_id=None, timeout=20, delayed=True): ...
    def connect(self): ...          # raises GatewayDown
    def disconnect(self): ...
    def __enter__(self) / __exit__(self, *exc): ...
    def accounts(self): ...                      # -> [str]
    def contract_details(self, symbol, sec_type="STK", exchange="SMART",
                         currency="USD", primary_exchange=None): ...   # -> [dict]
    def head_timestamp(self, contract, what="TRADES", use_rth=False): ...  # -> str|None
    def historical_bars(self, contract, end, duration, bar_size,
                        what="TRADES", use_rth=True): ...              # -> [BarData]
```

`contract_details` returns plain dicts in the CONTRACT shape above, so no `ib_async`
object escapes this module. `historical_bars` is the one exception: it hands raw `BarData`
to `bars.bars_to_rows`, which only reads attributes and never imports the package.

`client_id=None` picks the first free id in `CLIENT_IDS`. Never connect on 0, 1 or 77:
those belong to a human's TWS and to `check_connection.py`.

### contracts.py

```python
CACHE = "contracts.json"                 # under the store root
EXCHANGE_PREFERENCE = ("NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "PINK", "VALUE")

def load(root=None): ...                             # -> {symbol: CONTRACT}
def save(cache, root=None): ...                      # atomic write
def pick(details, currency="USD", sec_type="STK"): ...  # pure; -> (CONTRACT|None, [candidates])
def resolve(symbol, client=None, root=None, refresh=False, now=None): ...  # -> CONTRACT
def qualified(contract_dict): ...   # -> ib_async Stock with conId set, for a fetch
```

`resolve` reads the cache first and only touches the gateway on a miss or `refresh=True`.
A resolution failure raises `LookupError` whose message lists the near-miss candidates.

### bars.py

Pure. No network, no filesystem, no clock reads except the `now` argument.

```python
INTERVALS = {"30s": ..., "1m": ..., "5m": ..., "15m": ..., "30m": ..., "1h": ..., "1d": ...}
MARKET_TZ = "US/Eastern"
RTH_OPEN = (9, 30)
RTH_CLOSE = (16, 0)

def normalize_interval(name): ...      # "1 min", "1min", "1m", "1 MIN" -> "1m"; raises ValueError
def bar_size(interval): ...            # "1m" -> "1 min", the string IBKR wants
def bar_seconds(interval): ...         # "1m" -> 60; "1d" -> 86400
def max_duration(interval): ...        # "1m" -> "1 D"
def is_intraday(interval): ...         # -> bool

def session_of(ts, interval, requested="rth"): ...   # -> "rth" | "eth"
def session_date(ts, interval): ...                  # -> "YYYY-MM-DD", US/Eastern
def partition_key(ts, interval): ...                 # -> "2026" or "2026-06-25"

def bars_to_rows(bars, symbol, interval, pulled_ts,
                 requested_session="rth", source="ibkr"): ...   # -> [ROW], sorted by ts
def expected_starts(start, end, interval, session="rth"): ...   # -> [int]|None
def coverage(rows, symbol, interval, start, end, session="rth"): ...  # -> COVERAGE
def missing_spans(cov): ...            # -> [(start_ts, end_ts)] worth asking IBKR for
```

`bars_to_rows` accepts whatever `BarData.date` happens to be: a `date` for daily bars, a
naive `datetime` in market time, a timezone aware `datetime`, an epoch int, or a string.
It never guesses UTC for a naive value, it assumes US/Eastern, because that is what the
gateway sends for US equities with `formatDate=1`.

### store.py

```python
ROOT = repo/shared/data/ibkr            # override with LACUNA_IBKR_ROOT
COLUMNS = ["ts","symbol","interval","open","high","low","close","volume",
           "wap","bar_count","session","source","pulled_ts"]

def root(override=None): ...                       # -> Path
def partition_path(symbol, interval, key, root=None): ...   # -> Path
def write_bars(rows, root=None): ...               # -> {path: n_rows_written}
def read_bars(symbol, interval, start=None, end=None, session=None, root=None): ...  # -> [ROW]
def read_frame(...): ...                           # same args, -> pandas.DataFrame
def glob_pattern(symbol=None, interval=None, root=None): ...  # -> str
def connect(root=None): ...                        # -> duckdb connection with a `bars` view
def query(sql, root=None, limit=10000): ...        # -> (columns, rows); SELECT only
def store_stats(root=None): ...                    # -> {"files": n, "bytes": n, "symbols": [...]}
```

Writes are idempotent partition replacement. `write_bars` reads the target partition,
drops every existing row whose `(ts, symbol, interval)` a new row repeats, concatenates,
sorts by `ts`, and rewrites the whole file. Writing the same rows twice leaves the file
byte-identical except for nothing at all. Writing ten days into a year file keeps the
other 240.

`connect` opens DuckDB read-only over the parquet glob and creates a `bars` view. When the
store holds no files it creates an empty view with the right column types, so a query on a
cold store returns zero rows instead of raising.

`query` refuses anything that is not a single SELECT or WITH statement, and caps rows.

### jobs.py

```python
DB = "jobs.db"                  # under the store root
PACING_LIMIT = 60
PACING_WINDOW = 600
IDENTICAL_COOLDOWN = 15
BID_ASK_WEIGHT = 2

def open_db(root=None): ...     # -> sqlite3.Connection, schema applied

# pure arithmetic, tested with a fake clock
def budget_from(timestamps, now, limit=PACING_LIMIT, window=PACING_WINDOW): ...
    # timestamps is [(ts, weight)]; -> {"used","limit","remaining","window_seconds",
    #                                   "reset_at","oldest_in_window"}

def budget(conn, now): ...                       # the same, read out of jobs.db
def spend(conn, now, key, weight=1): ...         # record one request; -> budget after
def cooldown_remaining(conn, now, key): ...      # -> float seconds until an identical
                                                 #    request is allowed again
def request_key(symbol, interval, end, duration, what, use_rth): ...  # -> str, pure

def set_ledger(conn, symbol, interval, date, status, n_bars, session, note, now): ...
def get_ledger(conn, symbol, interval, dates=None): ...   # -> {date: LEDGER ROW}
def ledger_summary(conn, symbol=None, interval=None): ... # -> counts per status

def ensure_bars(symbol, interval, start, end, session="rth", allow_fetch=True,
                client=None, root=None, now=None, contract=None): ...
```

`ensure_bars` is the whole point of the module. In order:

1. Normalize the interval and the range. Read the store.
2. Compute coverage against what was asked.
3. When coverage is complete, or `allow_fetch` is false, return with `source` set to
   `cache` and `fetched` set to `[]`.
4. Otherwise turn the gaps into IBKR requests, chunked against the max duration table,
   newest chunk first.
5. Before each request, check the budget and the 15 second identical-request cooldown.
   Out of budget stops the loop, sets `fetched_denied`, and returns whatever the cache
   holds. It never sleeps and never retries.
6. Write every returned bar through `store.write_bars`, record the ledger row per session
   date, and record `empty` when IBKR answers with nothing.
7. Re-read the store and return.

Its return value is the shape the MCP layer will serve directly:

```python
{
  "symbol": "MSFT", "interval": "1m", "session": "rth",
  "rows": [ROW, ...],
  "coverage": COVERAGE,
  "source": "cache" | "fetched" | "mixed",
  "fetched": [{"end": iso, "duration": "1 D", "bars": 390, "status": "ok"}, ...],
  "fetch_denied": None | "budget exhausted, resets at 2026-08-31T12:41:00Z",
  "budget": {"used": 4, "limit": 60, "remaining": 56, "reset_at": iso},
}
```

### cli.py

```
lacuna-ibkr resolve MSFT [--refresh]
lacuna-ibkr bars MSFT --interval 1d --start 2026-08-01 [--end ...] [--session rth]
                     [--no-fetch] [--csv]
lacuna-ibkr pull MSFT --interval 1m --date 2026-06-25 [--session eth]
lacuna-ibkr coverage MSFT [--interval 1d]
```

`bars` serves from the store and fetches what is missing. `pull` is the same call pinned
to one session date, which is what gets typed when news lands. `coverage` reads the
ledger and the store and touches no network.

## Facts established by live testing

Tested against a read-only secondary username with no market data subscriptions.

1. Historical bars need no market data subscription. Every symbol tried, ordinary US
   listings and ADRs alike, returned daily and intraday bars on a bare account.
2. Retention beats the documentation. 30-second bars came back as full 780-bar sessions at
   2 months, 6 months, 14 months and 3 years back, against a documented six-month ceiling
   for sub-minute data. Daily history reaches 1980-12-12 for AAPL.
3. ADR resolution works. An ADR comes back as an ordinary SMART/USD stock with a primary
   exchange such as PINK.
4. Host port 4001 maps to container port 4003, because the image runs socat in front of
   the gateway. Live is 4001/4003 and paper is 4002/4004.
5. A violated pacing limit returns an empty bar list rather than raising. `empty` in the
   ledger exists for exactly this reason.

Added by the M1 core smoke run on the same day, against the live session:

6. MSFT is conId 29110391, primary exchange NYSE, "GENWORTH FINANCIAL INC".
7. A `durationStr` in seconds counts **trading** seconds when `useRTH=True`, not wall
   clock seconds. Asking for `86400 S` of 1-minute bars ending at 2026-06-26 00:00
   US/Eastern returned 1440 bars covering four sessions, 2026-06-22 09:30 through
   2026-06-25 15:59. The store absorbs this because it partitions on the session date of
   each bar, so the extra days simply land in their own files. Nothing may read the
   returned span as the asked span.
8. `ib_async` logs `open orders request timed out` and `completed orders request timed
   out` at ERROR on every connect. That is the read-only username refusing the order
   subscriptions the client asks for by default. It is harmless and it is not a
   connection failure.
9. Two historical requests, one contract lookup, and the whole core round trips: resolve,
   fetch, parquet write, `store.read_bars`, and a DuckDB `GROUP BY` over the glob.

## MCP server

`mcpserver.py` is the tool surface a model uses. Transport is stdio, one
JSON-RPC message per line, hand-rolled the way
`services/notification-hub/mcpserver.py` hand-rolls Streamable HTTP. It is
registered in the repo's `.mcp.json` as `ibkr`, running under `.venv/bin/python`.

It reserves client ids 211 to 220. That band sits above `ibclient.CLIENT_IDS`,
so a long CLI backfill and a tool call never collide on an id.

The server writes to the store and only reads from IBKR. `ibkr_get_bars` and
`ibkr_event_window` call `jobs.ensure_bars`, which fetches the gaps inside the
pacing budget and writes them through `store.write_bars`. Orders, positions and
account values are absent from the tool table entirely.

### The nine tools

| Tool | Arguments | Returns |
|---|---|---|
| `ibkr_status` | none | gateway up/down, account, budget, store stats, ledger counts, universe count; `login_url` when the session is down |
| `ibkr_list_symbols` | none | the `universe.json` watchlist with per-interval store coverage |
| `ibkr_resolve_contract` | `query`, `refresh` | one CONTRACT, from the cache or the gateway |
| `ibkr_get_bars` | `symbol`, `interval`, `start`, `end`, `session`, `allow_fetch`, `max_bars` | ROWs plus COVERAGE, `source`, `fetched`, `fetch_denied`, `budget` |
| `ibkr_event_window` | `symbol`, `event_at`, `before`, `after`, `interval`, `session`, `allow_fetch`, `max_bars` | the same, plus `interval_choice`, `base` and `moves` |
| `ibkr_compare_symbols` | `symbols` (max 8), `event_at`, `before`, `after`, `interval`, `session`, `include_bars` | one event window per symbol on one shared interval |
| `ibkr_data_coverage` | `symbol`, `interval`, `include_head` | first and last bar held, gaps, ledger counts, last pull, IBKR head timestamp |
| `ibkr_query_sql` | `sql`, `limit` | columns and rows from the DuckDB `bars` view, SELECT only, capped at 500 rows |
| `ibkr_get_quote` | `symbol` | a `not_collected` record. This is a v2 stub and it never returns an error |

Conventions every tool holds to. Timestamps in and out are ISO 8601 UTC.
Intervals accept any spelling `bars.normalize_interval` takes. Every response
carrying bars carries a COVERAGE block, and the coverage counts all the bars
even when the response trims the tail at `max_bars`. Any call that could spend
pacing budget reports the budget left and when it resets.

`ibkr_event_window` measures the move the way the event-study horizons below do,
at the same four horizons: 30s, 5m, 1h, 1d. The base is the OPEN of the bar
containing the event. Its close would be a price from up to one whole bar later,
which swallows the reaction being measured. The comparison is the close of the
first bar that has actually closed at or after the horizon. A horizon finer than
the bar size comes back `unavailable`, and one past the end of the window comes
back `no_bar`. Neither is silently coarsened.

With `interval` omitted, `ibkr_event_window` picks the finest bar size retention
is expected to reach and says which and why in `interval_choice`. The table is
30s within 1095 days, 1m within 2190 days, 1d beyond that. Only the 30s figure
is measured (fact 2 above). The 1m figure is an assumption, and it fails safely:
if IBKR holds less, the fetch returns empty and the coverage block says so.

### The error contract

A failure returns a normal result whose payload is one `error` object. The
`isError` flag is set as well, so a client sees it either way.

```python
{"error": {"code": "gateway_down",
           "message": "IB Gateway is not answering on 127.0.0.1:4001 (...)",
           "next_action": "A human must log in at http://127.0.0.1:8642. ...",
           "login_url": "http://127.0.0.1:8642",
           "retryable": False}}
```

`next_action` is mandatory and it names who acts. Codes in use:

| Code | Raised when | Who acts |
|---|---|---|
| `gateway_down` | the socket or the API handshake failed | a human, at the login page |
| `unresolvable_symbol` | `contracts.resolve` raised `LookupError` | the model, by asking the user which listing |
| `bad_argument` | an interval, timestamp, duration or window that cannot be read | the model, by fixing the argument |
| `query_rejected` | `ibkr_query_sql` got something other than one SELECT | the model, by rewriting it |
| `query_failed` | DuckDB raised on a valid-looking SELECT | the model, by checking the column names |
| `response_too_large` | the JSON payload passed 400 KB | the model, by narrowing the window |

Three failures are deliberately not errors. A down gateway on a bars call still
serves the cache, with the reason in `fetch_denied` and the login URL attached.
An exhausted budget does the same, and `fetch_denied` carries the reset time. A
missing `universe.json` returns an empty universe naming the file, and no tool
requires a symbol to be registered.

## The watchlist

`services/ibkr-data/universe.json`, version controlled. The symbols the nightly
backfill maintains. It ships with two generic large caps and is meant to be
edited.

The file lives beside the code rather than in the store root, because the store
root is gitignored and the watchlist is an asset worth keeping. `mcpserver.universe_path`
resolves it in this order: an explicit `root_dir` wins, which is what points a
test at a temporary file; then `shared/data/ibkr/universe.json` if a deployment
dropped its own list there; then the shipped file.

```json
{
  "_comment": "keys starting with an underscore are comments and are ignored",
  "symbols": [
    {"symbol": "MSFT",
     "case": "big-tech",               // optional free-text grouping label
     "exchange": "SMART",
     "currency": "USD",
     "primary_exchange": "NYSE",       // what separates an ADR from a same-named listing
     "con_id": 29110391,               // null when resolution failed; see note
     "long_name": "GENWORTH FINANCIAL INC",
     "intervals": ["1d"],              // what the backfill maintains
     "note": ""}                       // optional; carries the reason a con_id is null
  ]
}
```

A bare list, and a `universe` key instead of `symbols`, both parse. A string
entry becomes `{"symbol": ...}`. `cli.parse_universe` and
`mcpserver.load_universe` accept the same shapes.

A name that will not resolve keeps its row with `con_id` null and the failure in
`note`. Dropping it would hide the fact that a case has no tradeable identity,
which is a thing a human needs to see.

`intervals` is what the nightly walkers maintain, and it is `["1d"]` for every
name. Intraday bars stay fetch on demand through `ibkr_event_window` and
`ibkr_get_bars`, because a dated event is the only reason to want them and the pacing
budget will not carry a minute-bar backfill across thirty names.

### cli commands over the watchlist

```
lacuna-ibkr universe [--resolve] [--refresh] [--file ...]
lacuna-ibkr backfill [--start 2019-01-01] [--end ...] [--symbols MSFT KO]
                     [--no-wait] [--max-rounds 40]
lacuna-ibkr update [--days 1] [--symbols ...]
```

`universe` with no flags prints the list and validates it, exit code 2 when
something is wrong. `--resolve` walks every symbol through `contracts.resolve`
and writes `con_id`, `primary_exchange` and `long_name` back into the file,
preserving its shape and its comments.

`backfill` and `update` both use clientId 221 unless told otherwise. That band,
221 to 230, is theirs: 201 to 210 is `ibclient`'s default for ad hoc calls and
211 to 220 belongs to the MCP server, so an hour-long backfill never contends
with a tool call for an id.

### Resume semantics

Neither walker trusts the coverage block to decide what to fetch, and this is
the one design decision in M4 worth stating twice. `bars.coverage` counts a US
market holiday as a one-weekday gap, by design, so a range that has been fully
fetched still reports `complete: false` forever. A walker driven by coverage
would therefore re-request seven years of history on every single run.

So both walkers ask the ledger. `cli.pending_dates` returns the weekday session
dates whose ledger status is not `ok` and not `no_data`. Those two are settled:
bars landed, or IBKR answered and the market was shut. `empty`, `error` and a
date with no row at all come back as pending, which is exactly the set that is
worth spending a request on.

The consequences:

- A killed backfill resumes from the first unsettled date. Nothing already held
  is asked for twice.
- A finished backfill re-run costs zero requests and prints `complete` per name.
- `update` is the same walk over a two-day window, so the nightly run is at most
  one request per name and usually zero on a holiday it has already recorded.
- A name whose history starts after `--start`, an IPO in the middle of the
  range, settles its pre-listing weekdays as `no_data` inside the request that
  answered, and stalls on the chunks entirely before its first bar. `stalled`
  in the per-symbol line is that outcome, and the note carries the ledger
  counts.

Budget exhaustion inside a walk is not an error. `ensure_bars` never sleeps, so
the CLI does: it reads `budget.reset_at`, sleeps until three seconds past it,
and asks again for the same symbol. `--no-wait` turns that into a clean stop
that reports `denied`, which is what a run under a timer wants.

### The nightly timer

systemd user units, not system units, because the gateway runs as the desktop
user.

| Unit | What |
|---|---|
| `~/.config/systemd/user/lacuna-ibkr-update.service` | oneshot, `cli.py update`, appends to `services/ibkr-data/update.log` |
| `~/.config/systemd/user/lacuna-ibkr-update.timer` | `Mon..Fri 23:00 Africa/Johannesburg`, `Persistent=true` |

23:00 SAST is 17:00 US/Eastern in summer and 16:00 in winter. In summer the
session is comfortably closed. In winter the run lands on the close itself and
may catch an unfinished daily bar; the day is picked up the following night,
because `update` always asks for yesterday as well as today.

The hour before the gateway's own 23:59 restart is the reason for that slot, and
`TimeoutStartSec=40m` on the service is the guard: a run that overruns is
abandoned rather than left holding a clientId across the restart.

`Persistent=true` means a laptop asleep at 23:00 runs the update when it wakes.
That is safe precisely because the walk is ledger driven, so a late run asks only
for what is genuinely missing.
