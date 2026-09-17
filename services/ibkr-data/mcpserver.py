#!/usr/bin/env python3
"""ibkr-data MCP server: read-only market-data tools over the store and gateway.

Transport is stdio, one JSON-RPC message per line, hand-rolled the same way
``services/notification-hub/mcpserver.py`` hand-rolls Streamable HTTP. The
protocol code is the same shape: a version negotiation that answers both the
handshake era and 2026-07-28, a ``dispatch`` that returns a result, and tool
handlers that take plain dicts so tests can call them without a transport.

What is different from the hub, deliberately, is that this server writes. The
hub's store is an audit log and is read-only; this store is a cache of an
external data source, so ``ibkr_get_bars`` and ``ibkr_event_window`` fetch the
missing bars from IBKR, write them into the parquet store, and then serve them.
Writes are idempotent partition replacement. Toward IBKR everything stays a
read: no orders, no positions, no account values.

Two rules run through every response, both inherited from the plan:

1. Every response describes itself. Bars carry a coverage block, the source,
   and the pacing budget left. A tool that returns 40 bars when 390 were asked
   for is how a wrong number reaches a brief.
2. Errors are instructions. A failure carries a ``next_action`` naming who acts
   and what they do, never a stack trace.

Usage:
    .venv/bin/python services/ibkr-data/mcpserver.py       serve on stdio
    .venv/bin/python services/ibkr-data/mcpserver.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bars as barlib            # noqa: E402
import contracts as contractlib  # noqa: E402
import jobs                      # noqa: E402
import store                     # noqa: E402

SERVER_NAME = "ibkr-data"
SERVER_VERSION = "1.1.0"

# Protocol revisions this server speaks, modern first. Same list as the
# notification-hub server, for the same reason: clients in the wild lag.
MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
SUPPORTED_VERSIONS = [MODERN_VERSION] + LEGACY_VERSIONS
DEFAULT_LEGACY_VERSION = LEGACY_VERSIONS[0]

RESULT_COMPLETE = "complete"
CACHE_TTL_MS = 300_000
CACHE_SCOPE = "public"

# JSON-RPC codes. The first four are JSON-RPC itself.
E_PARSE = -32700
E_INVALID_REQUEST = -32600
E_METHOD_NOT_FOUND = -32601
E_INVALID_PARAMS = -32602
E_INTERNAL = -32603

MAX_REQUEST_BYTES = 1 << 20
MAX_RESPONSE_BYTES = 400_000
SQL_ROW_CAP = 500          # the plan's cap for ibkr_query_sql
DEFAULT_MAX_BARS = 2000    # bars in one response before the tail is trimmed

LOGIN_URL = "http://127.0.0.1:8642"
LOGIN_SENTENCE = (
    "A human must log in at " + LOGIN_URL + ". Two factor authentication means "
    "no tool can do this for you, and a programmatic retry gets the account "
    "throttled and then locked."
)

# This service's own client id band. ibclient reserves 201-210 for the CLI and
# the backfill; the MCP server takes the next ten so a long CLI job and a tool
# call never collide on an id.
CLIENT_IDS = tuple(range(211, 221))

UNIVERSE = "universe.json"

# Verified retention, and one assumption. CONTRACT.md records 30-second bars
# coming back in full at three years, against a documented six-month ceiling.
# Nothing has been measured beyond that, so the minute-bar figure below is a
# guess that fails safely: if IBKR holds less, the fetch returns empty, the
# coverage block says so, and the caller sees the truth rather than a silent
# substitution.
RETENTION_DAYS = (("30s", 1095), ("1m", 2190), ("1d", None))

# The horizons every brief in this workspace quotes. Imported from
# shared/lib/eventstudy.py so the two cannot drift apart; the fallback exists
# only so this server still starts if the shared library moves.
try:
    sys.path.insert(0, str(HERE.parent.parent / "shared"))
    from lib.eventstudy import HORIZONS as _PANDAS_HORIZONS

    HORIZON_SECONDS = {name: int(delta.total_seconds())
                       for name, delta in _PANDAS_HORIZONS.items()}
except Exception:  # pragma: no cover - only when shared/lib is unavailable
    HORIZON_SECONDS = {"30s": 30, "5m": 300, "1h": 3600, "1d": 86400}


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class Ctx:
    """Everything a handler needs that is not an argument.

    ``root_dir`` points a test at a temporary store. ``now`` freezes the clock.
    ``connect`` is the gateway factory, so a test substitutes one that raises
    instead of opening a socket.
    """

    def __init__(self, root_dir=None, now=None, connect=None, probe=None):
        self.root_dir = root_dir
        self._now = now
        # Resolved lazily so the two gateway functions can be defined below,
        # next to the rest of the gateway code.
        self._connect = connect
        self._probe = probe

    def now(self) -> float:
        return float(self._now) if self._now is not None else time.time()

    def connect(self, *a, **kw):
        return (self._connect or connect_gateway)(*a, **kw)

    def probe(self, *a, **kw):
        return (self._probe or probe_socket)(*a, **kw)


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

def fail(code: str, message: str, next_action: str, **extra) -> dict:
    """A structured tool error. Every one names who acts next."""
    error = {"code": code, "message": message, "next_action": next_action}
    error.update(extra)
    return {"error": error}


def gateway_down(detail: str, **extra) -> dict:
    return fail(
        "gateway_down",
        f"IB Gateway is not answering on {ibkr_host()}:{ibkr_port()} ({detail}).",
        LOGIN_SENTENCE,
        login_url=LOGIN_URL,
        retryable=False,
        **extra,
    )


def bad_argument(message: str, next_action: str, **extra) -> dict:
    return fail("bad_argument", message, next_action, **extra)


# --------------------------------------------------------------------------
# the gateway
# --------------------------------------------------------------------------

def ibkr_host() -> str:
    import ibclient

    return ibclient.HOST


def ibkr_port() -> int:
    import ibclient

    return ibclient.PORT


def probe_socket(timeout: float = 1.5) -> tuple[bool, str]:
    """Is anything listening on the gateway's API port?

    The cheap half of the health check. A closed port means the container is
    down, which is a different fix from a container that is up at the login
    screen, so the two are reported separately.
    """
    host, port = ibkr_host(), ibkr_port()
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True, f"{host}:{port} accepts connections"
    except OSError as exc:
        return False, f"{host}:{port} refused the connection ({exc})"
    finally:
        try:
            sock.close()
        except OSError:
            pass


def connect_gateway(timeout: int = 20):
    """A connected Client on this server's client id band.

    Raises ``ibclient.GatewayDown`` when no id in the band is free or the
    session is not answering.
    """
    from ibclient import Client, GatewayDown

    last = None
    for client_id in CLIENT_IDS:
        try:
            return Client(client_id=client_id, timeout=timeout).connect()
        except GatewayDown as exc:
            last = exc
            if "already in use" in str(exc).lower():
                continue
            raise
    raise GatewayDown(f"no free clientId in {CLIENT_IDS}: {last}")


DEFAULT_CTX = Ctx()


def _close(client) -> None:
    if client is None:
        return
    try:
        client.disconnect()
    except Exception:
        pass


# --------------------------------------------------------------------------
# argument reading
# --------------------------------------------------------------------------

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value, default=None) -> int:
    """``30m``, ``2h``, ``90s``, ``3d`` or a plain number of seconds."""
    if value is None or value == "":
        if default is None:
            raise ValueError("no duration given")
        return int(default)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value).strip().lower().replace(" ", "")
    if not text:
        raise ValueError("empty duration")
    if text.isdigit():
        return int(text)
    unit = text[-1]
    if unit not in _DURATION_UNITS:
        raise ValueError(f"unknown duration {value!r}; use 30s, 15m, 2h or 3d")
    try:
        count = float(text[:-1])
    except ValueError:
        raise ValueError(f"unknown duration {value!r}; use 30s, 15m, 2h or 3d")
    return int(count * _DURATION_UNITS[unit])


def need_symbol(args, key="symbol") -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required, as a ticker such as MSFT")
    return value.strip().upper()


def read_session(args) -> str:
    session = str(args.get("session") or "rth").strip().lower()
    if session not in ("rth", "eth"):
        raise ValueError(f"session must be rth or eth, got {args.get('session')!r}")
    return session


def trim_bars(rows, limit) -> tuple[list, int]:
    """Keep the response inside a caller's context.

    The coverage block still reports the true count, so a trimmed response
    cannot be mistaken for a short one.
    """
    limit = int(limit or DEFAULT_MAX_BARS)
    if limit <= 0 or len(rows) <= limit:
        return list(rows), 0
    return list(rows[:limit]), len(rows) - limit


def to_iso(ts) -> str | None:
    return barlib.to_iso(int(ts)) if ts is not None else None


def budget_block(book: dict) -> dict:
    """The pacing budget in the shape every response reports it."""
    return {
        "used": book.get("used"),
        "limit": book.get("limit"),
        "remaining": book.get("remaining"),
        "window_seconds": book.get("window_seconds"),
        "reset_at": to_iso(int(book["reset_at"])) if book.get("reset_at") else None,
    }


def universe_path(root_dir=None) -> Path:
    """Where the watchlist lives.

    The file ships with the code and is version controlled, so the default is the
    service directory. The store root is gitignored and would lose it. An explicit
    ``root_dir`` still wins, which is what points a test at a temporary file, and
    a file dropped in the store root wins over the shipped one so a deployment can
    carry its own list.
    """
    configured = os.environ.get("LACUNA_IBKR_UNIVERSE")
    if configured:
        return Path(configured)
    if root_dir is not None:
        return store.root(root_dir) / UNIVERSE
    in_store = store.root() / UNIVERSE
    if in_store.exists():
        return in_store
    return HERE / UNIVERSE


def load_universe(root_dir=None) -> tuple[list, str | None]:
    """The watchlist, or an empty one with the reason it is empty.

    universe.json is milestone M4 and does not exist yet. A missing file is a
    fact about the deployment rather than a failure, so it comes back as an
    empty universe naming the file, and every tool keeps working: nothing here
    requires a symbol to be pre-registered.
    """
    path = universe_path(root_dir)
    if not path.exists():
        return [], (f"no watchlist at {path}. It is built in milestone M4. Every "
                    "tool works on any symbol without it; use "
                    "ibkr_resolve_contract for a name you do not know.")
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        return [], f"{path} could not be read ({exc}); treating the universe as empty"
    if isinstance(data, dict):
        data = data.get("symbols") or data.get("universe") or []
    return list(data or []), None


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

def tool_status(args, ctx=None) -> dict:
    """Which layer is down, and what the budget and the store hold."""
    ctx = ctx or DEFAULT_CTX
    now = ctx.now()

    listening, socket_detail = ctx.probe()
    session = {"listening": listening, "detail": socket_detail}
    account = None
    if listening:
        client = None
        try:
            client = ctx.connect()
            accounts = client.accounts()
            account = accounts[0] if accounts else None
            session.update({"up": True, "accounts": list(accounts),
                            "client_id": getattr(client, "client_id", None),
                            "detail": "socket open and the API handshake answered"})
        except Exception as exc:
            session.update({"up": False, "detail": f"port open, handshake failed: {exc}"})
        finally:
            _close(client)
    else:
        session["up"] = False

    conn = jobs.open_db(ctx.root_dir)
    try:
        book = jobs.budget(conn, now)
        ledger = jobs.ledger_summary(conn)
    finally:
        conn.close()

    stats = store.store_stats(ctx.root_dir)
    watchlist, universe_note = load_universe(ctx.root_dir)

    payload = {
        "server": SERVER_NAME,
        "version": SERVER_VERSION,
        "checked_at": to_iso(int(now)),
        "gateway": {
            "host": ibkr_host(),
            "port": ibkr_port(),
            "up": bool(session.get("up")),
            "detail": session.get("detail"),
            "accounts": session.get("accounts", []),
            "client_id": session.get("client_id"),
        },
        "account": account,
        "budget": budget_block(book),
        "store": {
            "root": stats["root"],
            "files": stats["files"],
            "bytes": stats["bytes"],
            "symbols": stats["symbols"],
            "intervals": stats["intervals"],
            "ledger": ledger,
        },
        "universe": {"count": len(watchlist), "note": universe_note},
    }
    if not payload["gateway"]["up"]:
        payload["login_url"] = LOGIN_URL
        payload["next_action"] = LOGIN_SENTENCE
        payload["cache_still_works"] = (
            "Bars already in the store still serve. Call ibkr_get_bars with "
            "allow_fetch=false, or ibkr_query_sql, while the session is down."
        )
    return payload


def tool_list_symbols(args, ctx=None) -> dict:
    """The watchlist, each name with what the store holds for it."""
    ctx = ctx or DEFAULT_CTX
    watchlist, note = load_universe(ctx.root_dir)
    cache = contractlib.load(ctx.root_dir)

    out = []
    for entry in watchlist:
        if isinstance(entry, str):
            entry = {"symbol": entry}
        symbol = str(entry.get("symbol") or entry.get("ticker") or "").upper()
        contract = cache.get(symbol) or {}
        held = {}
        for interval in sorted(barlib.INTERVALS):
            rows = store.read_bars(symbol, interval, root_dir=ctx.root_dir)
            if rows:
                held[interval] = {
                    "n_bars": len(rows),
                    "first": to_iso(rows[0]["ts"]),
                    "last": to_iso(rows[-1]["ts"]),
                }
        out.append({
            "symbol": symbol,
            "con_id": contract.get("con_id") or entry.get("con_id"),
            "primary_exchange": contract.get("primary_exchange") or entry.get("primary_exchange"),
            "long_name": contract.get("long_name") or entry.get("long_name"),
            "case": entry.get("case") or entry.get("case_slug"),
            "coverage": held,
        })

    payload = {"universe": out, "count": len(out), "source": str(universe_path(ctx.root_dir))}
    if note:
        payload["note"] = note
    return payload


def tool_resolve_contract(args, ctx=None) -> dict:
    """Ticker to conId, from the cache when it can and the gateway when it must."""
    ctx = ctx or DEFAULT_CTX
    try:
        spec = _selector_from_args(args)
        query = str(spec.get("symbol") or spec.get("con_id") or "")
        if not query:
            raise ValueError("query, symbol or con_id is required")
    except ValueError as exc:
        return bad_argument(str(exc), "Pass a ticker or exact contract selector.")
    refresh = bool(args.get("refresh"))

    cache = contractlib.load(ctx.root_dir)
    key = contractlib.cache_key(spec)
    if not refresh and cache.get(key, {}).get("con_id"):
        return {"contract": cache[key], "source": "cache"}

    client = None
    try:
        client = ctx.connect()
    except Exception as exc:
        cached = cache.get(key)
        if cached:
            return {"contract": cached, "source": "cache",
                    "note": f"refresh asked for, but the gateway is down ({exc}); "
                            "this is the cached resolution"}
        return gateway_down(str(exc), symbol=query)

    try:
        contract = contractlib.resolve_selector(spec, client=client, root_dir=ctx.root_dir,
                                                refresh=refresh, now=int(ctx.now()))
    except LookupError as exc:
        return fail("unresolvable_symbol", str(exc),
                    "Ask the user which listing they mean, then call this tool "
                    "again with that ticker.", symbol=query)
    except Exception as exc:
        return gateway_down(str(exc), symbol=query)
    finally:
        _close(client)

    return {"contract": contract, "source": "gateway"}


def _selector_from_args(args, default_sec_type="STK"):
    keys = ("con_id", "symbol", "query", "sec_type", "exchange", "currency",
            "primary_exchange", "expiry", "strike", "right", "multiplier",
            "trading_class", "local_symbol")
    values = {k: args.get(k) for k in keys if args.get(k) not in (None, "")}
    if not values.get("con_id"):
        values.setdefault("sec_type", default_sec_type)
    return contractlib.selector(**values)


def _resolve_selector(args, client, ctx, default_sec_type="STK"):
    return contractlib.resolve_selector(_selector_from_args(args, default_sec_type),
                                        client=client, root_dir=ctx.root_dir,
                                        refresh=bool(args.get("refresh")),
                                        now=int(ctx.now()))


def _serve_bars(symbol, interval, start_ts, end_ts, session, allow_fetch, ctx,
                contract=None, data_type="TRADES"):
    """One ensure_bars call, with the gateway opened only when it is needed."""
    client = None
    gateway_note = None
    if allow_fetch:
        try:
            client = ctx.connect()
        except Exception as exc:
            gateway_note = (f"gateway unavailable ({exc}), serving from the store "
                            f"only. {LOGIN_SENTENCE}")
    try:
        result = jobs.ensure_bars(symbol, interval, start_ts, end_ts, session=session,
                                  allow_fetch=allow_fetch and client is not None,
                                  client=client, root_dir=ctx.root_dir,
                                  now=ctx.now(), contract=contract, what=data_type)
    finally:
        _close(client)
    if gateway_note:
        result["fetch_denied"] = gateway_note
        result["login_url"] = LOGIN_URL
    return result


def tool_get_bars(args, ctx=None) -> dict:
    """Bars for a window, fetching what the store is missing."""
    ctx = ctx or DEFAULT_CTX
    try:
        symbol = str(args.get("symbol") or args.get("query") or "").strip().upper()
        if not symbol and not args.get("con_id"):
            raise ValueError("symbol or con_id is required")
        interval = barlib.normalize_interval(args.get("interval") or "1d")
        session = read_session(args)
        if not args.get("start"):
            raise ValueError("start is required, as an ISO 8601 UTC timestamp")
        start_ts = barlib.to_epoch(args["start"])
        end_ts = (barlib.to_epoch(args["end"]) if args.get("end")
                  else int(ctx.now()))
        data_type = str(args.get("data_type") or "TRADES").upper()
        allowed = {"TRADES", "BID", "ASK", "MIDPOINT", "BID_ASK", "ADJUSTED_LAST",
                   "HISTORICAL_VOLATILITY", "OPTION_IMPLIED_VOLATILITY"}
        if data_type not in allowed:
            raise ValueError(f"unsupported data_type {data_type!r}")
    except (ValueError, TypeError, barlib.IntervalError) as exc:
        return bad_argument(str(exc),
                            "Fix the argument and call again. Timestamps are ISO "
                            "8601 UTC; intervals are 30s, 1m, 5m, 15m, 30m, 1h, 1d.")
    if end_ts <= start_ts:
        return bad_argument(f"end {args.get('end')!r} is not after start {args['start']!r}",
                            "Widen the window so end is later than start.")

    allow_fetch = args.get("allow_fetch")
    allow_fetch = True if allow_fetch is None else bool(allow_fetch)

    contract = None
    exact_keys = ("con_id", "sec_type", "exchange", "currency", "primary_exchange",
                  "expiry", "strike", "right", "multiplier", "trading_class", "local_symbol")
    if any(args.get(k) not in (None, "") for k in exact_keys) or data_type != "TRADES":
        client = None
        try:
            if allow_fetch:
                client = ctx.connect()
            contract = contractlib.resolve_selector(_selector_from_args(args), client=client,
                                                    root_dir=ctx.root_dir,
                                                    now=int(ctx.now()))
            symbol = contract["symbol"]
        except LookupError as exc:
            return fail("contract_not_cached", str(exc),
                        "Resolve this contract with the gateway available, then retry cache-only.")
        except Exception as exc:
            return gateway_down(str(exc), selector=_selector_from_args(args))
        finally:
            _close(client)
    result = _serve_bars(symbol, interval, start_ts, end_ts, session, allow_fetch, ctx,
                         contract=contract, data_type=data_type)
    rows, dropped = trim_bars(result["rows"], args.get("max_bars"))

    payload = {
        "symbol": result["symbol"],
        "interval": result["interval"],
        "session": result["session"],
        "contract": result.get("contract"),
        "data_type": result.get("data_type", data_type),
        "bars": rows,
        "n_returned": len(rows),
        "coverage": result["coverage"],
        "source": result["source"],
        "fetched": result["fetched"],
        "fetch_denied": result["fetch_denied"],
        "budget": budget_block(result["budget"]),
    }
    if dropped:
        payload["truncated"] = (
            f"{dropped} bars past the first {len(rows)} were dropped from this "
            "response. The coverage block counts all of them; narrow the window "
            "or raise max_bars to see the rest.")
    if result.get("login_url"):
        payload["login_url"] = result["login_url"]
    return payload


def pick_interval(event_ts: int, now: float) -> tuple[str, str]:
    """The finest interval retention plausibly reaches for this event date."""
    age_days = max(0.0, (float(now) - int(event_ts)) / 86400.0)
    for interval, limit in RETENTION_DAYS:
        if limit is None:
            return interval, (f"the event is {age_days:.0f} days old, past every "
                              "intraday retention figure this service trusts, so "
                              "daily bars are the finest that will answer")
        if age_days <= limit:
            verified = " (verified live at three years)" if interval == "30s" else ""
            return interval, (f"the event is {age_days:.0f} days old, inside the "
                              f"{limit} day retention this service assumes for "
                              f"{interval} bars{verified}, so {interval} is the "
                              "finest interval likely to answer")
    return "1d", "no interval matched, falling back to daily bars"


def horizon_moves(rows, event_ts: int, interval: str) -> tuple[dict | None, list]:
    """Percentage move at each eventstudy horizon. Pure.

    The base is the OPEN of the bar containing the event, never its close: a
    bar is stamped with its start, so its close is a price from up to one whole
    bar after the event and using it swallows the reaction being measured. The
    comparison is the close of the first bar that has actually closed at or
    after the horizon. Both rules are lifted from shared/lib/eventstudy.py so
    the two produce the same number for the same event.
    """
    step = barlib.bar_seconds(interval)
    before = [r for r in rows if int(r["ts"]) <= int(event_ts)]
    if not before:
        return None, []
    base_row = before[-1]
    base = float(base_row["open"])
    base_at = int(base_row["ts"])

    moves = []
    for label, delta in sorted(HORIZON_SECONDS.items(), key=lambda kv: kv[1]):
        if delta < step:
            moves.append({
                "horizon": label, "served_by": None, "pct": None,
                "status": "unavailable",
                "why": f"{label} is finer than the {interval} bars serving this "
                       "window, so no honest number exists at this resolution",
            })
            continue
        after = [r for r in rows if int(r["ts"]) + step >= int(event_ts) + delta]
        if not after:
            moves.append({
                "horizon": label, "served_by": interval, "pct": None,
                "status": "no_bar",
                "why": f"the window ends before {label} after the event; widen "
                       "after= to measure this horizon",
            })
            continue
        row = after[0]
        close = float(row["close"])
        pct = (close / base - 1) * 100 if base else None
        moves.append({
            "horizon": label, "served_by": interval,
            "pct": round(pct, 2) if pct is not None else None,
            "status": "measured",
            "why": f"base {base:.4f} (open) at {to_iso(base_at)}, compare "
                   f"{close:.4f} at {to_iso(int(row['ts']) + step)}",
        })
    return {"price": base, "at": to_iso(base_at), "from": "open of the bar containing the event"}, moves


def _event_window(args, ctx, symbol):
    """The shared body of ibkr_event_window and ibkr_compare_symbols."""
    event_ts = barlib.to_epoch(args["event_at"])
    before = parse_duration(args.get("before"), 30 * 60)
    after = parse_duration(args.get("after"), 2 * 3600)
    session = read_session(args)

    if args.get("interval"):
        interval = barlib.normalize_interval(args["interval"])
        why = "the caller pinned this interval"
    else:
        interval, why = pick_interval(event_ts, ctx.now())

    start_ts = event_ts - before
    end_ts = event_ts + after
    allow_fetch = args.get("allow_fetch")
    allow_fetch = True if allow_fetch is None else bool(allow_fetch)

    result = _serve_bars(symbol, interval, start_ts, end_ts, session, allow_fetch, ctx)
    rows = result["rows"]
    base, moves = horizon_moves(rows, event_ts, interval)
    kept, dropped = trim_bars(rows, args.get("max_bars"))

    payload = {
        "symbol": result["symbol"],
        "event_at": to_iso(event_ts),
        "interval": result["interval"],
        "interval_choice": {"chosen": interval, "requested": args.get("interval"),
                            "why": why},
        "session": result["session"],
        "window": {"start": to_iso(start_ts), "end": to_iso(end_ts),
                   "before_seconds": before, "after_seconds": after},
        "base": base,
        "moves": moves,
        "bars": kept,
        "n_returned": len(kept),
        "coverage": result["coverage"],
        "source": result["source"],
        "fetched": result["fetched"],
        "fetch_denied": result["fetch_denied"],
        "budget": budget_block(result["budget"]),
    }
    if base is None:
        payload["note"] = (
            f"no bar at or before {to_iso(event_ts)} in this window, so no move "
            "could be measured. Check the coverage block: either the market was "
            "shut at that moment, or the fetch was denied.")
    if dropped:
        payload["truncated"] = (
            f"{dropped} bars past the first {len(kept)} were dropped from this "
            "response; the coverage block and the moves use all of them.")
    if result.get("login_url"):
        payload["login_url"] = result["login_url"]
    return payload


def tool_event_window(args, ctx=None) -> dict:
    """Bars around a moment, plus the move at each standard horizon."""
    ctx = ctx or DEFAULT_CTX
    try:
        symbol = need_symbol(args)
        if not args.get("event_at"):
            raise ValueError("event_at is required, as an ISO 8601 UTC timestamp")
        return _event_window(args, ctx, symbol)
    except (ValueError, TypeError, barlib.IntervalError) as exc:
        return bad_argument(str(exc),
                            "Fix the argument and call again. event_at is ISO 8601 "
                            "UTC; before and after are durations such as 30m or 2h.")


def tool_compare_symbols(args, ctx=None) -> dict:
    """The same event window across several names, on one interval."""
    ctx = ctx or DEFAULT_CTX
    raw = args.get("symbols")
    if isinstance(raw, str):
        raw = [part for part in raw.replace(",", " ").split() if part]
    if not isinstance(raw, list) or not raw:
        return bad_argument("symbols is required, as a list of tickers",
                            "Pass symbols as [\"MSFT\", \"SPY\"].")
    symbols = [str(s).strip().upper() for s in raw if str(s).strip()]
    if len(symbols) > 8:
        return bad_argument(f"{len(symbols)} symbols asked for, the cap is 8",
                            "Split the comparison into two calls; each symbol "
                            "can cost a pacing slot.")
    if not args.get("event_at"):
        return bad_argument("event_at is required, as an ISO 8601 UTC timestamp",
                            "Pass event_at, such as 2026-06-25T14:00:00Z.")

    # One interval across every name, so the numbers are comparable. When the
    # caller left it open, the first symbol's retention decision fixes it for
    # all of them and the response says so.
    shared = dict(args)
    if not shared.get("interval"):
        try:
            chosen, why = pick_interval(barlib.to_epoch(args["event_at"]), ctx.now())
        except (ValueError, TypeError) as exc:
            return bad_argument(str(exc), "event_at must be an ISO 8601 timestamp.")
        shared["interval"] = chosen
        note = f"interval {chosen} chosen for every symbol: {why}"
    else:
        note = f"interval {shared['interval']} pinned by the caller for every symbol"

    # Bars are heavy and this tool returns N windows, so the rows are dropped by
    # default and the moves plus coverage carry the answer.
    shared.setdefault("max_bars", 0 if args.get("include_bars") is not True else None)

    results = {}
    for symbol in symbols:
        try:
            window = _event_window(shared, ctx, symbol)
        except (ValueError, TypeError, barlib.IntervalError) as exc:
            window = bad_argument(f"{symbol}: {exc}", "Fix the argument and call again.")
        if not args.get("include_bars"):
            window.pop("bars", None)
            window.pop("truncated", None)
        results[symbol] = window

    return {"event_at": to_iso(barlib.to_epoch(args["event_at"])),
            "interval": shared["interval"], "note": note, "symbols": results}


def tool_data_coverage(args, ctx=None) -> dict:
    """What the store holds for a name, and how far back IBKR would go."""
    ctx = ctx or DEFAULT_CTX
    try:
        symbol = need_symbol(args)
    except ValueError as exc:
        return bad_argument(str(exc), "Pass symbol as a ticker, such as MSFT.")
    try:
        intervals = ([barlib.normalize_interval(args["interval"])]
                     if args.get("interval") else sorted(barlib.INTERVALS))
    except barlib.IntervalError as exc:
        return bad_argument(str(exc), "Use one of 30s, 1m, 5m, 15m, 30m, 1h, 1d.")

    conn = jobs.open_db(ctx.root_dir)
    try:
        held = []
        for interval in intervals:
            rows = store.read_bars(symbol, interval, root_dir=ctx.root_dir)
            ledger = jobs.get_ledger(conn, symbol, interval)
            if not rows and not ledger:
                continue
            cov = (barlib.coverage(rows, symbol, interval, rows[0]["ts"],
                                   int(rows[-1]["ts"]) + barlib.bar_seconds(interval),
                                   rows[0].get("session", "rth"))
                   if rows else None)
            statuses = {}
            for row in ledger.values():
                statuses[row["status"]] = statuses.get(row["status"], 0) + 1
            last_pull = max((int(r["updated_ts"]) for r in ledger.values()), default=None)
            held.append({
                "interval": interval,
                "n_bars": len(rows),
                "first": to_iso(rows[0]["ts"]) if rows else None,
                "last": to_iso(rows[-1]["ts"]) if rows else None,
                "gaps": cov["gaps"] if cov else [],
                "complete_over_held_span": bool(cov["complete"]) if cov else None,
                "ledger": statuses,
                "last_pull": to_iso(last_pull),
                "partitions": len(store.partitions(symbol, interval, ctx.root_dir)),
            })
        book = jobs.budget(conn, ctx.now())
    finally:
        conn.close()

    payload = {
        "symbol": symbol,
        "intervals": held,
        "budget": budget_block(book),
        "note": ("gaps are judged over the span actually held, not over any "
                 "window you plan to ask for. Call ibkr_get_bars for the real "
                 "coverage of a specific window."),
    }

    if args.get("include_head") is False:
        return payload
    cache = contractlib.load(ctx.root_dir)
    contract = cache.get(symbol)
    if contract is None:
        payload["earliest_ibkr_holds"] = None
        payload["head_note"] = (f"{symbol} is not in the contract cache, so no head "
                                "timestamp was asked for. Call ibkr_resolve_contract "
                                "first.")
        return payload
    client = None
    try:
        client = ctx.connect()
        payload["earliest_ibkr_holds"] = client.head_timestamp(contract)
    except Exception as exc:
        payload["earliest_ibkr_holds"] = None
        payload["head_note"] = (f"the gateway did not answer ({exc}), so the head "
                                f"timestamp is unknown. {LOGIN_SENTENCE}")
    finally:
        _close(client)
    return payload


def tool_query_sql(args, ctx=None) -> dict:
    """One SELECT over the DuckDB view of the parquet store."""
    ctx = ctx or DEFAULT_CTX
    sql = args.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return bad_argument("sql is required",
                            "Pass one SELECT over the bars view, such as "
                            "SELECT symbol, count(*) FROM bars GROUP BY 1.")
    text = sql.strip().rstrip(";").strip()
    if ";" in text:
        return fail("query_rejected", "one statement per query",
                    "Split the query and call this tool once per statement.")
    head = text.split(None, 1)[0].lower()
    if head not in ("select", "with"):
        return fail("query_rejected",
                    f"only SELECT is allowed, got {head!r}",
                    "This store is written by ibkr_get_bars and ibkr_event_window "
                    "only. Rewrite the statement as a SELECT.")

    limit = args.get("limit")
    try:
        limit = min(int(limit), SQL_ROW_CAP) if limit else SQL_ROW_CAP
    except (TypeError, ValueError):
        limit = SQL_ROW_CAP
    limit = max(1, limit)

    try:
        columns, rows = store.query(text, root_dir=ctx.root_dir, limit=limit)
    except ValueError as exc:
        return fail("query_rejected", str(exc),
                    "Rewrite it as a single SELECT or WITH over the bars view.")
    except Exception as exc:
        return fail("query_failed", f"{type(exc).__name__}: {exc}",
                    "Check the column names against the bars view: ts, symbol, "
                    "interval, open, high, low, close, volume, wap, bar_count, "
                    "session, source, pulled_ts.")

    out = [dict(zip(columns, row)) for row in rows]
    payload = {"columns": list(columns), "rows": out, "n_rows": len(out),
               "row_cap": limit,
               "view": "bars (raw ts), bars_dt (adds a dt timestamp column)"}
    if len(out) >= limit:
        payload["truncated"] = (f"the {limit} row cap was reached; aggregate in SQL "
                                "or add a tighter WHERE clause")
    return payload


def tool_get_quote(args, ctx=None) -> dict:
    """A bounded delayed quote, preserving partial fields and warnings."""
    ctx = ctx or DEFAULT_CTX
    if not (args.get("symbol") or args.get("query") or args.get("con_id")):
        return bad_argument("symbol, query or con_id is required",
                            "Pass a ticker or an exact contract selector.")
    try:
        timeout = float(args.get("timeout_seconds") or 8)
        if timeout < 0.25 or timeout > 20:
            raise ValueError("timeout_seconds must be between 0.25 and 20")
    except (TypeError, ValueError) as exc:
        return bad_argument(str(exc), "Fix timeout_seconds and retry.")
    client = None
    try:
        client = ctx.connect()
        contract = _resolve_selector(args, client, ctx)
        rows = client.market_snapshot([contract], timeout=timeout)
        return {"contract": contract, "quote": rows[0] if rows else None,
                "status": rows[0]["status"] if rows else "unavailable",
                "source": "ibkr", "delayed_requested": True}
    except (LookupError, ValueError) as exc:
        return bad_argument(str(exc), "Pass an exact contract selector or con_id.")
    except Exception as exc:
        return gateway_down(str(exc), selector=_selector_from_args(args))
    finally:
        _close(client)


def _option_contracts(args, client, ctx):
    underlying_args = {"symbol": args.get("underlying") or args.get("symbol") or args.get("query"),
                       "sec_type": args.get("underlying_sec_type") or "STK",
                       "exchange": args.get("underlying_exchange") or "SMART",
                       "currency": args.get("currency") or "USD"}
    underlying = contractlib.resolve_selector(underlying_args, client=client,
                                              root_dir=ctx.root_dir, now=int(ctx.now()))
    params = client.option_parameters(underlying)
    preferred = next((p for p in params if p["exchange"] == "SMART"), params[0] if params else None)
    if not preferred:
        return underlying, params, []
    expiries = [args["expiry"]] if args.get("expiry") else preferred["expirations"]
    strikes = preferred["strikes"]
    if args.get("strikes"):
        strikes = [float(x) for x in args["strikes"]]
    lo, hi = args.get("strike_min"), args.get("strike_max")
    if lo is not None: strikes = [x for x in strikes if x >= float(lo)]
    if hi is not None: strikes = [x for x in strikes if x <= float(hi)]
    rights = [str(args.get("right")).upper()] if args.get("right") else ["C", "P"]
    rights = [{"CALL": "C", "PUT": "P"}.get(x, x) for x in rights]
    cap = min(max(int(args.get("max_contracts") or 100), 1), 500)
    candidates = []
    for expiry in expiries:
        for strike in strikes:
            for right in rights:
                candidates.append({"symbol": underlying["symbol"], "sec_type": "OPT",
                    "exchange": args.get("exchange") or "SMART", "currency": underlying["currency"],
                    "expiry": expiry, "strike": strike, "right": right,
                    "multiplier": preferred["multiplier"],
                    "trading_class": preferred["trading_class"]})
                if len(candidates) >= cap: break
            if len(candidates) >= cap: break
        if len(candidates) >= cap: break
    qualified = []
    if args.get("qualify") is not False:
        for candidate in candidates:
            rows = client.resolve_contract(candidate)
            if rows:
                item = rows[0]
                item["resolved_ts"] = int(ctx.now())
                qualified.append(item)
    else:
        qualified = candidates
    return underlying, params, qualified


def tool_option_chain(args, ctx=None) -> dict:
    ctx = ctx or DEFAULT_CTX
    if not (args.get("underlying") or args.get("symbol") or args.get("query")):
        return bad_argument("underlying is required", "Pass underlying such as AAPL.")
    client = None
    try:
        client = ctx.connect()
        underlying, params, contracts = _option_contracts(args, client, ctx)
        return {"underlying": underlying, "parameters": params, "contracts": contracts,
                "n_contracts": len(contracts), "qualified": args.get("qualify") is not False,
                "bounded": True}
    except Exception as exc:
        return gateway_down(str(exc), underlying=args.get("underlying"))
    finally:
        _close(client)


def _snapshot_contracts(args, client, ctx):
    raw = args.get("contracts")
    if raw:
        cap = min(max(int(args.get("max_contracts") or 20), 1), 100)
        return [contractlib.resolve_selector(x, client=client, root_dir=ctx.root_dir,
                                             now=int(ctx.now())) for x in raw[:cap]]
    chain_args = dict(args)
    chain_args["max_contracts"] = min(int(args.get("max_contracts") or 20), 100)
    _underlying, _params, contracts = _option_contracts(chain_args, client, ctx)
    return contracts


def tool_option_snapshot(args, ctx=None, capture=False) -> dict:
    ctx = ctx or DEFAULT_CTX
    try:
        cap = int(args.get("max_contracts") or 20)
        timeout = float(args.get("timeout_seconds") or 8)
        if cap < 1 or cap > 100:
            raise ValueError("max_contracts must be between 1 and 100")
        if timeout < 0.25 or timeout > 20:
            raise ValueError("timeout_seconds must be between 0.25 and 20")
        if not args.get("contracts") and not (args.get("underlying") or args.get("symbol")):
            raise ValueError("contracts or underlying is required")
    except (TypeError, ValueError) as exc:
        return bad_argument(str(exc), "Fix the bounded snapshot arguments and retry.")
    client = None
    try:
        client = ctx.connect()
        contracts = _snapshot_contracts(args, client, ctx)
        rows = client.market_snapshot(contracts, timeout=timeout)
        written = store.write_snapshots(rows, ctx.root_dir) if capture and rows else {}
        return {"snapshots": rows, "n_snapshots": len(rows), "source": "ibkr",
                "delayed_requested": True, "written": written}
    except (LookupError, ValueError) as exc:
        return bad_argument(str(exc), "Pass exact option contracts or a bounded chain selector.")
    except Exception as exc:
        return gateway_down(str(exc))
    finally:
        _close(client)


def tool_capture_option_snapshots(args, ctx=None) -> dict:
    return tool_option_snapshot(args, ctx, capture=True)


def tool_get_option_snapshots(args, ctx=None) -> dict:
    """Read previously captured option snapshots without touching the gateway."""
    ctx = ctx or DEFAULT_CTX
    try:
        con_id = int(args["con_id"])
        limit = min(max(int(args.get("limit") or 1000), 1), 5000)
        rows = store.read_snapshots(con_id, args.get("start"), args.get("end"), ctx.root_dir)
    except (KeyError, TypeError, ValueError) as exc:
        return bad_argument(str(exc), "Pass con_id and optional ISO start/end with limit 1-5000.")
    dropped = max(0, len(rows) - limit)
    payload = {"con_id": con_id, "snapshots": rows[:limit],
               "n_returned": min(len(rows), limit), "source": "cache"}
    if dropped:
        payload["truncated"] = dropped
    return payload


def tool_market_data_capabilities(args, ctx=None) -> dict:
    ctx = ctx or DEFAULT_CTX
    payload = {"server_version": SERVER_VERSION, "read_only": True,
        "asset_types": ["STK", "OPT", "IND", "CASH", "FUT", "CONTFUT", "FOP"],
        "historical_data_types": ["TRADES", "BID", "ASK", "MIDPOINT", "BID_ASK",
          "ADJUSTED_LAST", "HISTORICAL_VOLATILITY", "OPTION_IMPLIED_VOLATILITY"],
        "delayed_snapshots": True, "option_chain": True, "option_daily_bars": False,
        "option_daily_note": "IBKR rejects direct option EOD bars; request intraday bars.",
        "continuous_future_note": "CONTFUT history is limited to one current-window chunk because IBKR rejects explicit end times.",
        "adjusted_last_note": "ADJUSTED_LAST fetches one bounded window from now through the requested start because IBKR requires an empty end time, then filters to the requested dates.",
        "snapshot_history": True, "trading": False}
    if args.get("probe"):
        client = None
        try:
            client = ctx.connect(timeout=5)
            payload["gateway"] = {"up": True, "server_version": client.server_version(),
                                  "accounts": client.accounts()}
        except Exception as exc:
            payload["gateway"] = {"up": False, "detail": str(exc)}
        finally:
            _close(client)
    return payload


TOOL_HANDLERS = {
    "ibkr_status": tool_status,
    "ibkr_list_symbols": tool_list_symbols,
    "ibkr_resolve_contract": tool_resolve_contract,
    "ibkr_get_bars": tool_get_bars,
    "ibkr_event_window": tool_event_window,
    "ibkr_compare_symbols": tool_compare_symbols,
    "ibkr_data_coverage": tool_data_coverage,
    "ibkr_query_sql": tool_query_sql,
    "ibkr_get_quote": tool_get_quote,
    "ibkr_option_chain": tool_option_chain,
    "ibkr_option_snapshot": tool_option_snapshot,
    "ibkr_capture_option_snapshots": tool_capture_option_snapshots,
    "ibkr_get_option_snapshots": tool_get_option_snapshots,
    "ibkr_market_data_capabilities": tool_market_data_capabilities,
}


# --------------------------------------------------------------------------
# the tool table
# --------------------------------------------------------------------------

_INTERVAL_ENUM = ["30s", "1m", "5m", "15m", "30m", "1h", "1d"]
_TS_DESC = "ISO 8601 UTC, such as 2026-06-25T14:00:00Z."
_SESSION_DESC = ("For legacy US equities, rth is 09:30-16:00 US/Eastern and eth "
                 "selects bars outside it. For generalized instruments this is the "
                 "IBKR useRTH dataset: rth requests regular hours; eth requests all "
                 "hours and can include regular-hour bars.")
_DURATION_DESC = "A duration such as 30s, 15m, 2h or 3d, or plain seconds."
_DATA_TYPES = ["TRADES", "BID", "ASK", "MIDPOINT", "BID_ASK", "ADJUSTED_LAST",
               "HISTORICAL_VOLATILITY", "OPTION_IMPLIED_VOLATILITY"]
_SELECTOR_PROPERTIES = {
    "symbol": {"type": "string"}, "query": {"type": "string"},
    "con_id": {"type": "integer"},
    "sec_type": {"type": "string", "enum": ["STK", "OPT", "IND", "CASH", "FUT", "CONTFUT", "FOP"]},
    "exchange": {"type": "string"}, "currency": {"type": "string"},
    "primary_exchange": {"type": "string"}, "expiry": {"type": "string"},
    "strike": {"type": "number"}, "right": {"type": "string", "enum": ["C", "P", "CALL", "PUT"]},
    "multiplier": {"type": "string"}, "trading_class": {"type": "string"},
}

TOOLS = [
    {
        "name": "ibkr_status",
        "title": "IBKR status",
        "description": (
            "Whether the IB Gateway session is up, which account it holds, how "
            "much of the 60-request pacing budget is left and when it resets, "
            "what the local bar store holds, and how many names are in the "
            "watchlist; when the session is down it returns the login URL and "
            "says a human must do the login. Call this first when anything else "
            "misbehaves."),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "ibkr_list_symbols",
        "title": "List watchlist symbols",
        "description": (
            "The watchlist from universe.json, each name with its conId, "
            "primary exchange, case slug and a per-interval summary of what the "
            "local store already holds; the watchlist is not built yet, so "
            "today this returns an empty universe naming the file, and no tool "
            "requires a symbol to appear here."),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "ibkr_resolve_contract",
        "title": "Resolve a contract",
        "description": (
            "Turn a ticker into IBKR's conId with its exchange, primary "
            "exchange, currency and long name, reading the local cache first "
            "and the gateway only on a miss; this is the ADR disambiguator, so "
            "run it before pulling bars for a name like NSRGY, and an ambiguous "
            "ticker comes back as candidates rather than a silent guess."),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_SELECTOR_PROPERTIES,
                "refresh": {"type": "boolean", "description":
                            "Ignore the cache and ask the gateway again."},
            },
            "required": [],
        },
    },
    {
        "name": "ibkr_get_bars",
        "title": "Get bars",
        "description": (
            "Bars for one symbol over one window, served from the local parquet "
            "store and fetching whatever is missing from IBKR inside the pacing "
            "budget; read the coverage block rather than the bar count, because "
            "it states what was asked, what came back, every gap and whether "
            "the window is complete, and a denied fetch still returns the cache "
            "with the reason attached."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker, such as MSFT."},
                **_SELECTOR_PROPERTIES,
                "data_type": {"type": "string", "enum": _DATA_TYPES},
                "interval": {"type": "string", "enum": _INTERVAL_ENUM,
                             "description": "Bar size. Default 1d."},
                "start": {"type": "string", "description": "Window start. " + _TS_DESC},
                "end": {"type": "string", "description":
                        "Window end, exclusive. " + _TS_DESC + " Defaults to now."},
                "session": {"type": "string", "enum": ["rth", "eth"],
                            "description": _SESSION_DESC},
                "allow_fetch": {"type": "boolean", "description":
                                "False serves the store only and never touches "
                                "IBKR or the pacing budget. Default true."},
                "max_bars": {"type": "integer", "description":
                             f"Bars to return before the tail is trimmed. Default "
                             f"{DEFAULT_MAX_BARS}; coverage always counts all of them."},
            },
            "required": ["start"],
        },
    },
    {
        "name": "ibkr_event_window",
        "title": "Event window",
        "description": (
            "Bars around a dated event plus the percentage move at the 30s, 5m, "
            "1h and 1d horizons the briefs in this workspace quote, measured "
            "from the open of the bar containing the event so the reaction is "
            "not swallowed; leave interval out and it picks the finest bar size "
            "retention allows and says which and why, and every horizon the "
            "data cannot serve is returned as unavailable rather than quietly "
            "coarsened."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker, such as MSFT."},
                "event_at": {"type": "string", "description":
                             "When the event happened. " + _TS_DESC},
                "before": {"type": "string", "description":
                           "How far back the window reaches. " + _DURATION_DESC
                           + " Default 30m."},
                "after": {"type": "string", "description":
                          "How far forward the window reaches; a horizon past "
                          "this end comes back as no_bar. " + _DURATION_DESC
                          + " Default 2h."},
                "interval": {"type": "string", "enum": _INTERVAL_ENUM,
                             "description": "Pin the bar size instead of letting "
                                            "retention choose it."},
                "session": {"type": "string", "enum": ["rth", "eth"],
                            "description": _SESSION_DESC},
                "allow_fetch": {"type": "boolean", "description":
                                "False serves the store only. Default true."},
                "max_bars": {"type": "integer", "description":
                             "Bars to return before the tail is trimmed."},
            },
            "required": ["symbol", "event_at"],
        },
    },
    {
        "name": "ibkr_compare_symbols",
        "title": "Compare symbols around an event",
        "description": (
            "The same event window across up to eight names on one shared "
            "interval, which is how a defendant is read against its sector "
            "proxy; each name returns its own moves, coverage block and fetch "
            "outcome, bars are dropped unless include_bars is true, and one "
            "name failing to resolve never hides the others."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbols": {"type": "array", "items": {"type": "string"},
                            "description": "Tickers, at most 8."},
                "event_at": {"type": "string", "description": _TS_DESC},
                "before": {"type": "string", "description": _DURATION_DESC + " Default 30m."},
                "after": {"type": "string", "description": _DURATION_DESC + " Default 2h."},
                "interval": {"type": "string", "enum": _INTERVAL_ENUM,
                             "description": "Pin the bar size for every name."},
                "session": {"type": "string", "enum": ["rth", "eth"],
                            "description": _SESSION_DESC},
                "include_bars": {"type": "boolean", "description":
                                 "Return the raw bars as well as the moves."},
            },
            "required": ["symbols", "event_at"],
        },
    },
    {
        "name": "ibkr_data_coverage",
        "title": "Data coverage",
        "description": (
            "The health view for one name: first and last bar held per "
            "interval, the gaps inside that span, the ledger counts that "
            "separate a shut market from an empty IBKR answer, when it was last "
            "pulled, and how far back IBKR itself would go; it touches no "
            "historical request, so run it across the watchlist the evening "
            "before an expected announcement."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker, such as MSFT."},
                "interval": {"type": "string", "enum": _INTERVAL_ENUM,
                             "description": "One interval. Default is all of them."},
                "include_head": {"type": "boolean", "description":
                                 "Ask the gateway how far back it holds data. "
                                 "Default true; false keeps the call offline."},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "ibkr_query_sql",
        "title": "Query the bar store",
        "description": (
            "One read-only SELECT or WITH over the DuckDB view of the parquet "
            f"store, capped at {SQL_ROW_CAP} rows, for the questions the shaped "
            "tools cannot ask; the bars view carries ts, symbol, interval, "
            "open, high, low, close, volume, wap, bar_count, session, source "
            "and pulled_ts, bars_dt adds a dt timestamp, and it reads only what "
            "is already local so it never fetches and never spends budget."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description":
                        "One SELECT or WITH statement over bars or bars_dt."},
                "limit": {"type": "integer", "description":
                          f"Rows to return, capped at {SQL_ROW_CAP}."},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "ibkr_get_quote",
        "title": "Get a quote",
        "description": (
            "A bounded delayed quote for an exact stock, option, index, FX or futures "
            "contract. Partial fields and IBKR entitlement warnings are preserved."),
        "inputSchema": {
            "type": "object",
            "properties": {**_SELECTOR_PROPERTIES,
                           "timeout_seconds": {"type": "number", "minimum": 0.25, "maximum": 20}},
            "required": [],
        },
    },
    {
        "name": "ibkr_option_chain", "title": "Discover an option chain",
        "description": "Discover expirations and strikes and optionally qualify a bounded set of exact option contracts.",
        "inputSchema": {"type": "object", "properties": {
            "underlying": {"type": "string"}, "expiry": {"type": "string"},
            "strikes": {"type": "array", "items": {"type": "number"}},
            "strike_min": {"type": "number"}, "strike_max": {"type": "number"},
            "right": {"type": "string", "enum": ["C", "P", "CALL", "PUT"]},
            "max_contracts": {"type": "integer", "minimum": 1, "maximum": 500},
            "qualify": {"type": "boolean"}, "currency": {"type": "string"},
            "exchange": {"type": "string"}}, "required": ["underlying"]},
    },
    {
        "name": "ibkr_option_snapshot", "title": "Get delayed option snapshots",
        "description": "Collect delayed quotes, sizes, model and quote Greeks, volume and open interest for at most 100 exact options.",
        "inputSchema": {"type": "object", "properties": {
            "contracts": {"type": "array", "items": {"type": "object"}},
            "underlying": {"type": "string"}, "expiry": {"type": "string"},
            "strikes": {"type": "array", "items": {"type": "number"}},
            "right": {"type": "string"}, "max_contracts": {"type": "integer", "maximum": 100},
            "timeout_seconds": {"type": "number", "minimum": 0.25, "maximum": 20}}, "required": []},
    },
    {
        "name": "ibkr_capture_option_snapshots", "title": "Capture option snapshots",
        "description": "Collect the same bounded delayed option snapshot and retain it in parquet for future historical research.",
        "inputSchema": {"type": "object", "properties": {
            "contracts": {"type": "array", "items": {"type": "object"}},
            "underlying": {"type": "string"}, "expiry": {"type": "string"},
            "strikes": {"type": "array", "items": {"type": "number"}},
            "right": {"type": "string"}, "max_contracts": {"type": "integer", "maximum": 100},
            "timeout_seconds": {"type": "number", "minimum": 0.25, "maximum": 20}}, "required": []},
    },
    {
        "name": "ibkr_get_option_snapshots", "title": "Read captured option snapshots",
        "description": "Read retained delayed quote, Greek and open-interest snapshots for one option conId without touching IBKR.",
        "inputSchema": {"type": "object", "properties": {
            "con_id": {"type": "integer"}, "start": {"type": "string"},
            "end": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 5000}},
            "required": ["con_id"]},
    },
    {
        "name": "ibkr_market_data_capabilities", "title": "Market data capabilities",
        "description": "Report supported asset and historical data types, option limitations and the read-only safety boundary; optionally probe the gateway.",
        "inputSchema": {"type": "object", "properties": {"probe": {"type": "boolean"}}, "required": []},
    },
]

TOOL_NAMES = [t["name"] for t in TOOLS]

INSTRUCTIONS = (
    "Read-only market data from Interactive Brokers for stocks, options, indexes, "
    "FX, futures and futures options: delayed quotes and option Greeks, option-chain "
    "discovery, retained snapshots, and historical bars cached in parquet and served "
    "with a coverage block that states what was asked for against what actually "
    "exists. Reach for it for contract discovery, semi-live observations, historical "
    "series, or what an instrument did around a moment in time. Nothing here can place an "
    "order.\n\n"
    "Three things to know before the first call. Timestamps in and out are ISO "
    "8601 UTC. A breached pacing limit makes IBKR answer with silence rather "
    "than an error, so trust the coverage block and never the length of the bar "
    "list. And the gateway session needs a human with an authenticator app "
    "roughly weekly: when it is down, ibkr_status returns the login URL, the "
    "cached bars still serve, and a login must never be retried "
    "programmatically.\n\n"
    "Start at ibkr_status or ibkr_market_data_capabilities, use "
    "ibkr_resolve_contract for an exact identity, ibkr_option_chain for bounded "
    "option discovery, ibkr_get_quote or ibkr_option_snapshot for delayed data, and "
    "ibkr_event_window or ibkr_get_bars for historical windows."
)


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

def call_tool(name, args, ctx=None):
    """Run one tool and wrap it in the MCP content envelope."""
    if name not in TOOL_HANDLERS:
        raise RpcError(E_INVALID_PARAMS, f"Unknown tool: {name}", {"available": TOOL_NAMES})
    if not isinstance(args, dict):
        raise RpcError(E_INVALID_PARAMS, "arguments must be an object")

    try:
        payload = TOOL_HANDLERS[name](args, ctx or DEFAULT_CTX)
    except RpcError:
        raise
    except Exception as exc:
        # A failure inside a tool is a tool error, not a protocol error: the
        # connection is fine and the model should see what went wrong.
        log(f"ERROR tool={name} {type(exc).__name__}: {exc}")
        log(traceback.format_exc().rstrip())
        payload = fail(f"{type(exc).__name__}", str(exc),
                       "Call ibkr_status to see which layer is down, then retry "
                       "with a narrower window.")

    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > MAX_RESPONSE_BYTES:
        payload = fail(
            "response_too_large",
            f"{name} produced {len(text)} bytes, over the {MAX_RESPONSE_BYTES} byte cap",
            "Narrow the window, lower max_bars, or aggregate in ibkr_query_sql.")
        text = json.dumps(payload, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": payload,
            "isError": bool(isinstance(payload, dict) and "error" in payload)}


def server_capabilities():
    return {"tools": {"listChanged": False}}


def negotiate_version(requested):
    return requested if requested in SUPPORTED_VERSIONS else DEFAULT_LEGACY_VERSION


def meta_protocol_version(meta):
    if isinstance(meta, dict):
        return meta.get("io.modelcontextprotocol/protocolVersion")
    return None


def complete_result(payload, era, ttl_ms=None):
    """Put a result in the envelope its caller's revision expects.

    Same rule as the notification-hub server. 2026-07-28 requires resultType and
    the caching hints; every earlier revision reads an absent resultType as
    complete and ignores members it does not know.
    """
    if era != MODERN_VERSION:
        return payload
    result = {"resultType": RESULT_COMPLETE}
    result.update(payload)
    if ttl_ms is not None:
        result.setdefault("ttlMs", ttl_ms)
        result.setdefault("cacheScope", CACHE_SCOPE)
    meta = dict(result.get("_meta") or {})
    meta.setdefault("io.modelcontextprotocol/serverInfo",
                    {"name": SERVER_NAME, "version": SERVER_VERSION})
    result["_meta"] = meta
    return result


class Session:
    """What survives between two messages on one stdio pipe."""

    def __init__(self, ctx=None):
        self.ctx = ctx or DEFAULT_CTX
        self.era = MODERN_VERSION

    def request_era(self, params):
        declared = meta_protocol_version(params.get("_meta"))
        if declared in SUPPORTED_VERSIONS:
            return declared
        return self.era


def dispatch(session, method, params):
    params = params if isinstance(params, dict) else {}
    era = session.request_era(params)

    if method == "initialize":
        # A handshake-era method: 2026-07-28 removed it, so its result keeps the
        # handshake shape whatever the caller declared and gets no envelope.
        version = negotiate_version(params.get("protocolVersion"))
        session.era = version
        return {
            "protocolVersion": version,
            "capabilities": server_capabilities(),
            "serverInfo": {"name": SERVER_NAME, "title": "IBKR market data",
                           "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }

    if method == "server/discover":
        return complete_result({"supportedVersions": SUPPORTED_VERSIONS,
                                "capabilities": server_capabilities(),
                                "instructions": INSTRUCTIONS},
                               MODERN_VERSION, ttl_ms=CACHE_TTL_MS)

    if method == "ping":
        return complete_result({}, era)

    if method == "tools/list":
        return complete_result({"tools": TOOLS}, era, ttl_ms=CACHE_TTL_MS)

    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise RpcError(E_INVALID_PARAMS, "tools/call needs a name")
        return complete_result(call_tool(name, params.get("arguments") or {},
                                         session.ctx), era)

    raise RpcError(E_METHOD_NOT_FOUND, f"Method not found: {method}")


# --------------------------------------------------------------------------
# stdio transport
# --------------------------------------------------------------------------

_LOG_PATH = os.environ.get("IBKR_MCP_LOG")


def log(msg):
    """Never write to stdout: that pipe carries JSON-RPC and nothing else."""
    line = f"[{barlib.to_iso(int(time.time()))}] {msg}"
    if _LOG_PATH:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            return
        except OSError:
            pass
    print(line, file=sys.stderr, flush=True)


def rpc_error(req_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def handle_message(session, message):
    """One parsed message to one response, or None for a notification."""
    if isinstance(message, list):
        # Batching was removed from MCP in 2025-06-18 and was never supported
        # here, so it is refused plainly rather than half-handled.
        return rpc_error(None, E_INVALID_REQUEST, "JSON-RPC batches are not supported")
    if not isinstance(message, dict):
        return rpc_error(None, E_INVALID_REQUEST, "message must be a JSON-RPC object")

    method = message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    req_id = message.get("id")

    if not isinstance(method, str) or not method:
        return rpc_error(req_id, E_INVALID_REQUEST, "missing method")
    if "id" not in message:
        # A notification gets no reply at all, per the transport spec.
        return None

    try:
        result = dispatch(session, method, params)
    except RpcError as exc:
        log(f"rpc error method={method} code={exc.code} {exc.message}")
        return rpc_error(req_id, exc.code, exc.message, exc.data)
    except Exception as exc:
        log(f"ERROR method={method} {type(exc).__name__}: {exc}")
        log(traceback.format_exc().rstrip())
        return rpc_error(req_id, E_INTERNAL, f"internal error: {exc}")

    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def serve(stdin=None, stdout=None, ctx=None):
    """Read newline-delimited JSON-RPC until the pipe closes."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    session = Session(ctx)
    log(f"{SERVER_NAME} {SERVER_VERSION} on stdio, store {store.root(session.ctx.root_dir)}")

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        if len(line) > MAX_REQUEST_BYTES:
            response = rpc_error(None, E_INVALID_REQUEST,
                                 f"request over {MAX_REQUEST_BYTES} bytes")
        else:
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                response = rpc_error(None, E_PARSE, f"cannot parse JSON: {exc}")
            else:
                try:
                    response = handle_message(session, message)
                except Exception:
                    # One bad message must never take the server down.
                    log("ERROR unhandled in handle_message")
                    log(traceback.format_exc().rstrip())
                    response = rpc_error(message.get("id") if isinstance(message, dict) else None,
                                         E_INTERNAL, "internal error")
        if response is None:
            continue
        stdout.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
        stdout.flush()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="ibkr-data MCP server (stdio)")
    parser.add_argument("--root", help="store root, overriding LACUNA_IBKR_ROOT")
    parser.add_argument("--selftest", action="store_true",
                        help="print the tool table and the current status, then exit")
    args = parser.parse_args(argv)

    ctx = Ctx(root_dir=args.root)
    if args.selftest:
        print(json.dumps({"tools": TOOL_NAMES,
                          "status": tool_status({}, ctx)},
                         indent=2, default=str))
        return 0
    return serve(ctx=ctx)


if __name__ == "__main__":
    sys.exit(main())
