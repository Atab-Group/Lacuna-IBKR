#!/usr/bin/env python3
"""Command line over the ibkr-data core.

    .venv/bin/python services/ibkr-data/cli.py resolve MSFT
    .venv/bin/python services/ibkr-data/cli.py bars MSFT --interval 1d --start 2026-08-01
    .venv/bin/python services/ibkr-data/cli.py pull MSFT --interval 1m --date 2026-06-25
    .venv/bin/python services/ibkr-data/cli.py coverage MSFT --interval 1d
    .venv/bin/python services/ibkr-data/cli.py universe --resolve
    .venv/bin/python services/ibkr-data/cli.py backfill --start 2019-01-01
    .venv/bin/python services/ibkr-data/cli.py update

``bars`` and ``pull`` serve from the parquet store and fetch what is missing,
inside the pacing budget. ``coverage`` reads the ledger and the store and touches
no network. ``backfill`` and ``update`` walk ``universe.json``; both decide what
to ask for from the coverage ledger, so a killed run resumes where it stopped and
a finished run costs no requests at all.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bars as barlib      # noqa: E402
import contracts as contractlib  # noqa: E402
import jobs                # noqa: E402
import store               # noqa: E402


def _client(args):
    """A connected client, or None when the caller asked to stay offline."""
    if getattr(args, "no_fetch", False):
        return None
    from ibclient import Client, GatewayDown
    try:
        return Client(client_id=args.client_id).connect()
    except GatewayDown as exc:
        print(f"gateway unavailable: {exc}\n", file=sys.stderr)
        return None


def _print_rows(rows, limit=20, as_csv=False):
    if as_csv:
        print(",".join(store.COLUMNS))
        for row in rows:
            print(",".join(str(row.get(c, "")) for c in store.COLUMNS))
        return
    if not rows:
        print("  (no bars)")
        return
    head = rows[:limit]
    print(f"  {'time (UTC)':<20} {'open':>9} {'high':>9} {'low':>9} "
          f"{'close':>9} {'volume':>12} {'sess':>4}")
    for row in head:
        print(f"  {barlib.to_iso(row['ts']):<20} {row['open']:>9.4f} "
              f"{row['high']:>9.4f} {row['low']:>9.4f} {row['close']:>9.4f} "
              f"{row['volume']:>12.0f} {row['session']:>4}")
    if len(rows) > limit:
        print(f"  ... {len(rows) - limit} more rows")


def _print_result(result, limit=20, as_csv=False):
    cov = result["coverage"]
    print(f"{result['symbol']} {result['interval']} {result['session']}  "
          f"source={result['source']}")
    print(f"  asked {cov['asked']['start']} to {cov['asked']['end']}")
    print(f"  got   {cov['got']['start']} to {cov['got']['end']}  "
          f"bars={cov['n_bars']} expected={cov['expected_bars']} "
          f"complete={cov['complete']}")
    if cov["gaps"]:
        print(f"  gaps  {len(cov['gaps'])}, first "
              f"{cov['gaps'][0]['start']} to {cov['gaps'][0]['end']}")
    for fetch in result["fetched"]:
        print(f"  fetch {fetch['end']} {fetch['duration']:>8} -> "
              f"{fetch['status']} ({fetch['bars']} bars)"
              + (f"  {fetch.get('note','')}" if fetch.get("note") else ""))
    if result["fetch_denied"]:
        print(f"  denied: {result['fetch_denied']}")
    book = result["budget"]
    print(f"  budget {book['used']}/{book['limit']} used in "
          f"{book['window_seconds']}s, {book['remaining']} left")
    if not as_csv:
        _print_rows(result["rows"], limit)
    else:
        _print_rows(result["rows"], as_csv=True)


def cmd_resolve(args) -> int:
    client = _client(args)
    try:
        contract = contractlib.resolve_selector(_selector_args(args), client=client,
                                                refresh=args.refresh)
    except Exception as exc:   # LookupError and GatewayDown both land here
        print(f"could not resolve {args.symbol}: {exc}", file=sys.stderr)
        return 1
    finally:
        if client:
            client.disconnect()
    print(json.dumps(contract, indent=2, sort_keys=True))
    return 0


def cmd_bars(args) -> int:
    end = args.end or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    client = _client(args)
    try:
        contract = None
        if args.con_id or args.sec_type != "STK" or args.data_type != "TRADES":
            contract = contractlib.resolve_selector(_selector_args(args), client=client)
        result = jobs.ensure_bars(args.symbol or contract["symbol"], args.interval, args.start, end,
                                  session=args.session,
                                  allow_fetch=not args.no_fetch,
                                  client=client, contract=contract, what=args.data_type)
    finally:
        if client:
            client.disconnect()
    _print_result(result, limit=args.limit, as_csv=args.csv)
    return 0


def cmd_pull(args) -> int:
    """One session date, which is the call typed when an event lands."""
    day = dt.date.fromisoformat(args.date)
    start = barlib.day_start_epoch(day)
    end = barlib.day_start_epoch(day + dt.timedelta(days=1))
    client = _client(args)
    try:
        result = jobs.ensure_bars(args.symbol, args.interval, start, end,
                                  session=args.session,
                                  allow_fetch=not args.no_fetch,
                                  client=client)
    finally:
        if client:
            client.disconnect()
    _print_result(result, limit=args.limit, as_csv=args.csv)
    return 0


def cmd_coverage(args) -> int:
    conn = jobs.open_db()
    try:
        intervals = [args.interval] if args.interval else sorted(barlib.INTERVALS)
        stats = store.store_stats()
        print(f"store {stats['root']}: {stats['files']} files, "
              f"{stats['bytes'] / 1e6:.1f} MB, symbols {', '.join(stats['symbols']) or '(none)'}")
        print(f"budget {json.dumps(jobs.budget(conn))}")
        for interval in intervals:
            rows = store.read_bars(args.symbol, interval)
            if not rows:
                continue
            summary = jobs.ledger_summary(conn, args.symbol, interval)
            print(f"\n{args.symbol} {interval}: {len(rows)} bars, "
                  f"{barlib.to_iso(rows[0]['ts'])} to {barlib.to_iso(rows[-1]['ts'])}")
            print(f"  ledger {summary or '(empty)'}")
            cov = barlib.coverage(rows, args.symbol, interval,
                                  rows[0]["ts"], rows[-1]["ts"] + 1,
                                  args.session)
            print(f"  gaps inside that range: {len(cov['gaps'])}")
            for gap in cov["gaps"][:5]:
                print(f"    {gap['start']} to {gap['end']} ({gap['n_bars']} bars)")
    finally:
        conn.close()
    return 0


# --------------------------------------------------------------------------
# the watchlist (pure, apart from the one file read)
# --------------------------------------------------------------------------

UNIVERSE_PATH = Path(os.environ.get("LACUNA_IBKR_UNIVERSE") or
                     Path(__file__).resolve().parent / "universe.json")


def _selector_args(args):
    keys = ("symbol", "con_id", "sec_type", "exchange", "currency", "expiry",
            "strike", "right", "multiplier", "trading_class")
    return {k: getattr(args, k) for k in keys if hasattr(args, k) and
            getattr(args, k) not in (None, "")}


def cmd_chain(args) -> int:
    import mcpserver
    result = mcpserver.tool_option_chain(vars(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if "error" in result else 0


def cmd_quote(args) -> int:
    import mcpserver
    result = mcpserver.tool_get_quote(vars(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if "error" in result else 0


def cmd_snapshot(args) -> int:
    import mcpserver
    fn = mcpserver.tool_capture_option_snapshots if args.capture else mcpserver.tool_option_snapshot
    result = fn(vars(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if "error" in result else 0

# 221 to 230 is the watchlist band. 201 to 210 is ibclient's default for ad hoc
# calls and 211 to 220 belongs to the MCP server.
WATCHLIST_CLIENT_ID = 221

# A ledger status that will never change on a re-ask. `ok` means bars landed,
# `no_data` means IBKR answered and the market was shut. Everything else is
# worth asking again, which is what makes a backfill resumable.
SETTLED = ("ok", "no_data")


def parse_universe(data) -> list[dict]:
    """The watchlist entries out of whatever ``universe.json`` holds. Pure.

    Accepts a bare list or an object carrying ``symbols`` or ``universe``, which
    is the same pair of shapes ``mcpserver.load_universe`` accepts. A bare string
    entry becomes ``{"symbol": ...}``. Symbols are upper cased. Keys starting
    with an underscore are comments and are ignored.
    """
    if isinstance(data, dict):
        data = data.get("symbols") or data.get("universe") or []
    out = []
    for entry in data or []:
        if isinstance(entry, str):
            entry = {"symbol": entry}
        if not isinstance(entry, dict):
            continue
        entry = {k: v for k, v in entry.items() if not k.startswith("_")}
        entry["symbol"] = str(entry.get("symbol") or entry.get("ticker") or "").upper()
        entry.setdefault("case", entry.get("case_slug"))
        entry.setdefault("intervals", ["1d"])
        out.append(entry)
    return out


def validate_universe(entries) -> list[str]:
    """Everything wrong with a parsed watchlist, as sentences. Pure.

    An empty list means the file is usable. A universe with problems is still
    returned by ``parse_universe``, because a single bad row should not cost the
    other thirty their backfill.
    """
    problems = []
    seen = set()
    for i, entry in enumerate(entries):
        where = entry.get("symbol") or f"entry {i}"
        if not entry.get("symbol"):
            problems.append(f"entry {i} has no symbol")
        elif entry["symbol"] in seen:
            problems.append(f"{where} appears more than once")
        else:
            seen.add(entry["symbol"])
        intervals = entry.get("intervals")
        if not isinstance(intervals, list) or not intervals:
            problems.append(f"{where} has no intervals list")
            continue
        for interval in intervals:
            try:
                barlib.normalize_interval(interval)
            except ValueError:
                problems.append(f"{where} asks for an unknown interval {interval!r}")
    return problems


def read_universe(path=None) -> tuple[list[dict], dict]:
    """The watchlist and the raw document, so a writer can keep the file shape."""
    path = Path(path or UNIVERSE_PATH)
    raw = json.loads(path.read_text())
    return parse_universe(raw), raw


def write_universe(raw, entries, path=None) -> Path:
    """Put resolved contract fields back into the file, shape untouched."""
    path = Path(path or UNIVERSE_PATH)
    by_symbol = {e["symbol"]: e for e in entries}
    rows = raw["symbols"] if isinstance(raw, dict) else raw
    for row in rows:
        merged = by_symbol.get(str(row.get("symbol", "")).upper())
        if merged:
            for key in ("con_id", "primary_exchange", "exchange", "currency",
                        "long_name", "note"):
                if key in merged:
                    row[key] = merged[key]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2, sort_keys=False) + "\n")
    os.replace(tmp, path)
    return path


def pending_dates(ledger: dict, dates) -> list[str]:
    """Session dates still worth asking IBKR for. Pure.

    ``ledger`` is what ``jobs.get_ledger`` returns. A date with no row has never
    been asked; a date sitting on ``empty`` or ``error`` was asked and did not
    settle, so both come back.
    """
    return [d for d in dates if (ledger.get(d) or {}).get("status") not in SETTLED]


def _yesterday_ny() -> dt.date:
    return barlib.market_time(time.time()).date() - dt.timedelta(days=1)


# --------------------------------------------------------------------------
# watchlist commands
# --------------------------------------------------------------------------

def cmd_universe(args) -> int:
    entries, raw = read_universe(args.file)
    problems = validate_universe(entries)
    for problem in problems:
        print(f"  problem: {problem}", file=sys.stderr)

    if args.resolve:
        client = _client(args)
        if client is None:
            print("cannot resolve without a gateway client", file=sys.stderr)
            return 1
        try:
            for entry in entries:
                try:
                    contract = contractlib.resolve(entry["symbol"], client=client,
                                                   refresh=args.refresh)
                except Exception as exc:
                    entry["con_id"] = None
                    entry["note"] = f"unresolved: {str(exc)[:200]}"
                    print(f"{entry['symbol']:<6} FAILED  {str(exc)[:120]}")
                    continue
                entry["con_id"] = contract["con_id"]
                entry["primary_exchange"] = contract["primary_exchange"]
                entry["exchange"] = contract["exchange"]
                entry["currency"] = contract["currency"]
                entry["long_name"] = contract["long_name"]
                if str(entry.get("note", "")).startswith("unresolved"):
                    entry.pop("note")
                print(f"{entry['symbol']:<6} {contract['con_id']:<10} "
                      f"{contract['primary_exchange'] or '?':<8} {contract['long_name']}")
        finally:
            client.disconnect()
        write_universe(raw, entries, args.file)
        print(f"\nwrote {args.file or UNIVERSE_PATH}")
        return 0

    for entry in entries:
        print(f"{entry['symbol']:<6} {str(entry.get('con_id') or '-'):<10} "
              f"{str(entry.get('primary_exchange') or '-'):<8} "
              f"{entry.get('case') or '(no case)'}"
              + (f"  [{entry['note']}]" if entry.get("note") else ""))
    unresolved = [e["symbol"] for e in entries if not e.get("con_id")]
    print(f"\n{len(entries)} names, {len(unresolved)} unresolved"
          + (f": {', '.join(unresolved)}" if unresolved else ""))
    return 2 if problems else 0


def _fill_symbol(entry, interval, start_ts, end_ts, client, conn, wait, rounds):
    """Fetch every unsettled session date for one symbol.

    Returns ``(outcome, settled, still_pending, note)``. It re-reads the ledger
    every round, so it never asks twice for a date that landed, and a kill at any
    point leaves the next run exactly this much less to do.
    """
    symbol = entry["symbol"]
    dates = jobs.weekdays_between(start_ts, end_ts)
    note = ""
    for _ in range(rounds):
        ledger = jobs.get_ledger(conn, symbol, interval)
        pending = pending_dates(ledger, dates)
        if not pending:
            return "complete", len(dates), 0, note
        span_start = barlib.day_start_epoch(dt.date.fromisoformat(pending[0]))
        result = jobs.ensure_bars(symbol, interval, span_start, end_ts,
                                  session="rth", client=client, conn=conn)
        denied = result["fetch_denied"] or ""
        after = pending_dates(jobs.get_ledger(conn, symbol, interval), dates)

        if "budget" in denied and wait:
            reset = float(result["budget"]["reset_at"])
            nap = max(reset - time.time(), 0) + 3
            print(f"  budget spent, sleeping {nap:.0f}s until "
                  f"{barlib.to_iso(int(reset))}", flush=True)
            time.sleep(nap)
            continue
        if denied:
            return "denied", len(dates) - len(after), len(after), denied
        if len(after) >= len(pending):
            statuses = jobs.ledger_summary(conn, symbol, interval)
            return ("stalled", len(dates) - len(after), len(after),
                    f"no progress in a round; ledger {statuses}")
    left = pending_dates(jobs.get_ledger(conn, symbol, interval), dates)
    return ("rounds_exhausted", len(dates) - len(left), len(left),
            f"stopped after {rounds} rounds; run it again")


def _walk_universe(args, start_ts, end_ts, label) -> int:
    entries, _ = read_universe(args.file)
    problems = validate_universe(entries)
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    if args.symbols:
        wanted = {s.upper() for s in args.symbols}
        entries = [e for e in entries if e["symbol"] in wanted]

    print(f"{label}: {len(entries)} names, {barlib.to_iso(int(start_ts))} to "
          f"{barlib.to_iso(int(end_ts))}, store {store.root()}", flush=True)
    # the watchlist walkers hold a connection for an hour, so they sit on their
    # own clientId band and never contend with an ad hoc CLI call or the MCP
    # server on 211-220
    if args.client_id is None:
        args.client_id = WATCHLIST_CLIENT_ID
    client = _client(args)
    conn = jobs.open_db()
    counts: dict[str, int] = {}
    try:
        for i, entry in enumerate(entries, 1):
            if client is None:
                print(f"[{i}/{len(entries)}] {entry['symbol']:<6} no gateway, skipped",
                      flush=True)
                counts["no_gateway"] = counts.get("no_gateway", 0) + 1
                continue
            started = time.time()
            try:
                outcome, settled, left, note = _fill_symbol(
                    entry, args.interval, start_ts, end_ts, client, conn,
                    wait=not args.no_wait, rounds=args.max_rounds)
            except Exception as exc:
                # an unresolvable ticker, or a gateway that went away mid walk.
                # One bad name must not cost the other thirty their run.
                outcome, settled, left, note = "failed", 0, 0, str(exc)[:200]
            counts[outcome] = counts.get(outcome, 0) + 1
            book = jobs.budget(conn)
            print(f"[{i}/{len(entries)}] {entry['symbol']:<6} {outcome:<10} "
                  f"settled={settled:<5} pending={left:<5} "
                  f"budget={book['remaining']}/{book['limit']} "
                  f"{time.time() - started:.0f}s"
                  + (f"  {note}" if note else ""), flush=True)
    finally:
        conn.close()
        if client:
            client.disconnect()

    print(f"\n{label} finished: "
          + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())), flush=True)
    return 0 if counts.get("complete", 0) == len(entries) else 1


def cmd_backfill(args) -> int:
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end) if args.end else _yesterday_ny()
    start_ts = barlib.day_start_epoch(start)
    end_ts = barlib.day_start_epoch(end + dt.timedelta(days=1))
    return _walk_universe(args, start_ts, end_ts, "backfill")


def cmd_update(args) -> int:
    """Yesterday and today for every name. What the nightly timer runs."""
    end = barlib.market_time(time.time()).date()
    start = end - dt.timedelta(days=args.days)
    start_ts = barlib.day_start_epoch(start)
    end_ts = barlib.day_start_epoch(end + dt.timedelta(days=1))
    return _walk_universe(args, start_ts, end_ts, "update")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lacuna-ibkr", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", type=int, default=None,
                        help="gateway clientId; defaults to the first free of 201-210")
    sub = parser.add_subparsers(dest="command", required=True)

    resolve = sub.add_parser("resolve", help="symbol to conId, cached")
    resolve.add_argument("symbol")
    resolve.add_argument("--refresh", action="store_true")
    resolve.add_argument("--no-fetch", action="store_true",
                         help="cache only, never open the gateway")
    resolve.set_defaults(func=cmd_resolve)

    def selector_flags(command, positional=False):
        if not positional:
            command.add_argument("--symbol", default=None)
        command.add_argument("--con-id", type=int, default=None)
        command.add_argument("--sec-type", default="STK",
                             choices=("STK", "OPT", "IND", "CASH", "FUT", "CONTFUT", "FOP"))
        command.add_argument("--exchange", default="SMART")
        command.add_argument("--currency", default="USD")
        command.add_argument("--expiry", default=None)
        command.add_argument("--strike", type=float, default=None)
        command.add_argument("--right", choices=("C", "P", "CALL", "PUT"), default=None)
        command.add_argument("--multiplier", default=None)
        command.add_argument("--trading-class", default=None)

    selector_flags(resolve, positional=True)

    bars_cmd = sub.add_parser("bars", help="serve a range, fetching what is missing")
    bars_cmd.add_argument("symbol", nargs="?", default=None)
    selector_flags(bars_cmd, positional=True)
    bars_cmd.add_argument("--interval", default="1d")
    bars_cmd.add_argument("--start", required=True)
    bars_cmd.add_argument("--end", default=None)
    bars_cmd.add_argument("--session", default="rth", choices=("rth", "eth"))
    bars_cmd.add_argument("--no-fetch", action="store_true")
    bars_cmd.add_argument("--limit", type=int, default=20)
    bars_cmd.add_argument("--csv", action="store_true")
    bars_cmd.add_argument("--data-type", default="TRADES", choices=("TRADES", "BID", "ASK", "MIDPOINT", "BID_ASK", "ADJUSTED_LAST", "HISTORICAL_VOLATILITY", "OPTION_IMPLIED_VOLATILITY"))
    bars_cmd.set_defaults(func=cmd_bars)

    chain = sub.add_parser("chain", help="discover and qualify a bounded option chain")
    chain.add_argument("underlying")
    chain.add_argument("--expiry", default=None)
    chain.add_argument("--strike-min", type=float, default=None)
    chain.add_argument("--strike-max", type=float, default=None)
    chain.add_argument("--strikes", nargs="*", type=float, default=None)
    chain.add_argument("--right", choices=("C", "P", "CALL", "PUT"), default=None)
    chain.add_argument("--max-contracts", type=int, default=100)
    chain.add_argument("--no-qualify", dest="qualify", action="store_false")
    chain.set_defaults(func=cmd_chain, qualify=True)

    quote = sub.add_parser("quote", help="bounded delayed quote")
    selector_flags(quote)
    quote.add_argument("--timeout-seconds", type=float, default=8)
    quote.set_defaults(func=cmd_quote)

    snap = sub.add_parser("snapshot", help="bounded delayed option snapshots")
    snap.add_argument("underlying")
    snap.add_argument("--expiry", default=None)
    snap.add_argument("--strikes", nargs="*", type=float, default=None)
    snap.add_argument("--right", choices=("C", "P", "CALL", "PUT"), default=None)
    snap.add_argument("--max-contracts", type=int, default=20)
    snap.add_argument("--timeout-seconds", type=float, default=8)
    snap.add_argument("--capture", action="store_true",
                      help="retain snapshots in parquet for historical reuse")
    snap.set_defaults(func=cmd_snapshot)

    pull = sub.add_parser("pull", help="one session date, the event-day call")
    pull.add_argument("symbol")
    pull.add_argument("--interval", default="1m")
    pull.add_argument("--date", required=True, help="YYYY-MM-DD, US/Eastern")
    pull.add_argument("--session", default="rth", choices=("rth", "eth"))
    pull.add_argument("--no-fetch", action="store_true")
    pull.add_argument("--limit", type=int, default=20)
    pull.add_argument("--csv", action="store_true")
    pull.set_defaults(func=cmd_pull)

    cov = sub.add_parser("coverage", help="what the store and ledger hold")
    cov.add_argument("symbol")
    cov.add_argument("--interval", default=None)
    cov.add_argument("--session", default="rth", choices=("rth", "eth"))
    cov.set_defaults(func=cmd_coverage)

    uni = sub.add_parser("universe", help="the watchlist, and its contract resolution")
    uni.add_argument("--file", default=None, help=f"defaults to {UNIVERSE_PATH}")
    uni.add_argument("--resolve", action="store_true",
                     help="resolve every symbol and write the conIds back")
    uni.add_argument("--refresh", action="store_true",
                     help="re-ask the gateway even for a cached contract")
    uni.add_argument("--no-fetch", action="store_true")
    uni.set_defaults(func=cmd_universe)

    back = sub.add_parser("backfill", help="daily history for the whole watchlist")
    back.add_argument("--start", default="2019-01-01")
    back.add_argument("--end", default=None, help="defaults to yesterday, US/Eastern")
    back.add_argument("--interval", default="1d")
    back.add_argument("--file", default=None)
    back.add_argument("--symbols", nargs="*", default=None,
                      help="only these tickers, for a re-run of one name")
    back.add_argument("--no-wait", action="store_true",
                      help="stop on an exhausted budget instead of sleeping to the reset")
    back.add_argument("--max-rounds", type=int, default=40)
    back.add_argument("--no-fetch", action="store_true")
    back.set_defaults(func=cmd_backfill)

    upd = sub.add_parser("update", help="the last session or two, for the nightly timer")
    upd.add_argument("--days", type=int, default=1,
                     help="calendar days back from today, US/Eastern")
    upd.add_argument("--interval", default="1d")
    upd.add_argument("--file", default=None)
    upd.add_argument("--symbols", nargs="*", default=None)
    upd.add_argument("--no-wait", action="store_true")
    upd.add_argument("--max-rounds", type=int, default=4)
    upd.add_argument("--no-fetch", action="store_true")
    upd.set_defaults(func=cmd_update)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
