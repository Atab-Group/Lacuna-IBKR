---
name: market-data
description: Use for any question about what a listed stock did at or around a moment in time, event studies on a dated announcement or filing, price checks on a name, building bar datasets, or comparing a name against a sector proxy, via the ibkr_ MCP tools; also covers IBKR gateway session state when a tool call fails.
---

# IBKR market data

Nine read-only tools, all prefixed `ibkr_`. They read historical and (v2) live
bars from IB Gateway through a local cache. Nothing here places an order,
reads a position, or reads an account value. That refusal is permanent and
deliberate.

## When to use this, and when not to

Use it for: a stock's move around a dated announcement, an event study on a
filing or a court date, a plain price check, building a bar dataset for a
notebook, a name-versus-sector comparison, checking whether IBKR actually
holds data for a symbol and interval.

Do not use it for: portfolio state, account value, or positions (out of scope and
unimplemented); anything that places, modifies, or cancels an order (refused
permanently, at three layers: the tool surface, the MCP server, and the
gateway's own read-only API key).

## Decision table

| Question shape | Tool |
|---|---|
| "Is the session even up?" / any tool call errors | `ibkr_status` |
| "What symbols do we track?" | `ibkr_list_symbols` |
| "What's the conId / exchange for this ADR or unlisted name?" | `ibkr_resolve_contract` |
| "Give me bars for X between two dates" | `ibkr_get_bars` |
| "What did X do around this timestamp?" (the common case) | `ibkr_event_window` |
| "Same window, several names" | `ibkr_compare_symbols` |
| "How far back does data for X actually go, and are there gaps?" | `ibkr_data_coverage` |
| A question the shaped tools can't ask | `ibkr_query_sql` |
| "What's X trading at right now?" | `ibkr_get_quote` (v2; returns "not collected" until built) |

`ibkr_event_window` is the tool to reach for first for most
questions about a dated event: pass `symbol`, `event_at`, `before`, `after`, and it returns bars
around the timestamp plus moves at the four standard horizons the service
uses. Leave `interval` unset and it picks the finest one retention allows.

## Session model, compressed

The gateway needs a human once a week rather than once a session. A prior login's
token survives the gateway's own daily restart, so tools keep working for
days without anyone touching a keyboard. It stops working when the weekly
Sunday ~01:00 US Eastern token invalidation hits, or when the container
crashes or is brought down.

When a tool call fails on session state: call `ibkr_status`. It returns the
login URL. A human opens it and types a code from their authenticator app.
**Never retry a login programmatically.** IBKR's throttle sits directly in
front of a lockout: repeated automated attempts are read as the same failure
IBKR treats as an attack, and the lockout that follows is worse than the
outage that caused you to retry. Report the down state and the URL, then
stop.

## The five traps

- **Pacing failures are silent.** A violated 60-requests/10-minute budget
  returns an empty response rather than an error. Trust the `coverage` block on
  every response rather than the row count: it says whether data was `fetched` or
  came from `cache`, and whether a gap is a real market gap or a denied
  fetch.
- **Sub-minute retention beats the documented limit, but verify per name.**
  IBKR's docs cap 30-second bars at six months; live testing found full
  sessions 3+ years back. Call `ibkr_data_coverage` before promising a date
  range at that resolution instead of assuming either number.
- **ADRs and unlisted names need `ibkr_resolve_contract` first.** Passing a
  bare ticker to `ibkr_get_bars` for a name outside the tracked universe can
  resolve to the wrong exchange or currency.
- **Live quotes are delayed; historical bars are not.** A fresh account gets
  delayed-only streaming data, so `ibkr_get_quote` can be stale by minutes.
  Historical bars carry no such discount regardless of subscription level.
- **Pre-market and after-hours prints exist and matter.** News that lands
  before the open needs `session=eth` bars. Every bar row carries
  its own `session` field so a pre-market print is never compared against a
  regular-hours close.

## References

Read only the one you need:

- `references/pacing.md`: the budget numbers, what the `coverage` block
  means, how `ibkr_get_bars` chunks a fetch.
- `references/store.md`: the parquet layout on disk, bar columns, DuckDB
  glob examples, and direct pandas access for notebooks.
- `references/event-studies.md`: `event_window` semantics, the horizons
  the service defines, `rth` versus `eth`, and the
  verified retention facts.


## Gateway recovery fixes (0.1.1)

A running container, an open API port, or a successful API handshake does
not prove the upstream market-data connection works. Verify recovery with
one small historical-bar request and inspect its returned data and coverage.
Errors 1100/2110 indicate lost upstream connectivity; 2103/2157 identify
broken data/security-definition connections. Do not diagnose every empty
response as a login failure: check contract resolution, permissions, session
filtering and the fetch metadata first.

The data client and login probe use the low-level API handshake, skipping
`IB.connect` account/order synchronization. Never enable write access to
resolve an "API client needs write access" prompt for this service.

IBC has a separate "Re-login is required" dialog handler which can retry
even when the second-factor retry setting is disabled. The web UI stops the
container after failed attempts. If an unattended re-login loop is observed,
stop the container and leave the next login to the human; never automate a
retry. This cleanup is containment, not a change to IBC's internal handler.
