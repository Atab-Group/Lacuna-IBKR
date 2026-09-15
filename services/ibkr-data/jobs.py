"""Pacing budget, coverage ledger, and the fetch orchestrator.

Two things live here because they are the same problem. IBKR allows 60 historical
requests per rolling 10 minutes and answers a breach with silence, so a caller
that cannot see its own spend will corrupt its own dataset. The budget is
persisted in ``shared/data/ibkr/jobs.db`` so it survives a process restart: a
backfill killed at second 30 of its window does not get a fresh 60 requests.

The ledger records what happened per ``(symbol, interval, session date)``.
``empty`` and ``no_data`` are different outcomes and conflating them destroys the
ledger's value. See CONTRACT.md.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import sqlite3
import time

import bars as barlib
import store

DB = "jobs.db"

PACING_LIMIT = 60          # historical requests per window
PACING_WINDOW = 600        # seconds
IDENTICAL_COOLDOWN = 15    # seconds between two identical requests
MAX_OPEN_REQUESTS = 50     # this service never runs concurrent requests
BID_ASK_WEIGHT = 2         # a BID_ASK request spends two slots

REQUEST_RETENTION = 7 * 86400   # how long spent-request rows are kept

STATUSES = ("ok", "empty", "no_data", "error", "pending")

# Longest span one request may cover, per interval, in seconds. Mirrors the max
# duration table in CONTRACT.md.
CHUNK_SECONDS = {
    "30s": 28800,
    "1m": 86400,
    "5m": 7 * 86400,
    "15m": 7 * 86400,
    "30m": 30 * 86400,
    "1h": 30 * 86400,
    "1d": 365 * 86400,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL    NOT NULL,
  key     TEXT    NOT NULL,
  weight  INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS requests_ts  ON requests(ts);
CREATE INDEX IF NOT EXISTS requests_key ON requests(key, ts);

CREATE TABLE IF NOT EXISTS coverage (
  symbol     TEXT    NOT NULL,
  interval   TEXT    NOT NULL,
  date       TEXT    NOT NULL,
  status     TEXT    NOT NULL,
  n_bars     INTEGER NOT NULL DEFAULT 0,
  session    TEXT    NOT NULL DEFAULT 'rth',
  note       TEXT    NOT NULL DEFAULT '',
  updated_ts INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (symbol, interval, date)
);
"""


def db_path(root_dir=None):
    return store.root(root_dir) / DB


def open_db(root_dir=None) -> sqlite3.Connection:
    path = db_path(root_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# --------------------------------------------------------------------------
# pacing
# --------------------------------------------------------------------------

def request_key(symbol: str, interval: str, end, duration: str,
                what: str = "TRADES", use_rth: bool = True) -> str:
    """Stable id for one historical request. Pure.

    Two requests with the same key are the ones IBKR refuses inside 15 seconds.
    """
    raw = "|".join([str(symbol).upper(), str(interval), str(end), str(duration),
                    str(what).upper(), "rth" if use_rth else "eth"])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def weight_for(what: str = "TRADES") -> int:
    return BID_ASK_WEIGHT if str(what).upper() == "BID_ASK" else 1


def budget_from(timestamps, now: float, limit: int = PACING_LIMIT,
                window: int = PACING_WINDOW) -> dict:
    """Budget arithmetic over ``[(ts, weight), ...]``. Pure, no clock read.

    ``reset_at`` is when the oldest request inside the window falls out of it,
    which is the first moment a blocked caller gains a slot. With nothing in the
    window it is ``now``.
    """
    cutoff = now - window
    inside = [(float(ts), int(weight)) for ts, weight in timestamps
              if float(ts) > cutoff]
    used = sum(weight for _ts, weight in inside)
    oldest = min((ts for ts, _w in inside), default=None)
    return {
        "used": used,
        "limit": int(limit),
        "remaining": max(0, int(limit) - used),
        "window_seconds": int(window),
        "oldest_in_window": oldest,
        "reset_at": (oldest + window) if oldest is not None else now,
    }


def budget(conn: sqlite3.Connection, now: float | None = None,
           limit: int = PACING_LIMIT, window: int = PACING_WINDOW) -> dict:
    now = float(now if now is not None else time.time())
    rows = conn.execute(
        "SELECT ts, weight FROM requests WHERE ts > ?", (now - window,)
    ).fetchall()
    return budget_from([(r["ts"], r["weight"]) for r in rows], now, limit, window)


def spend(conn: sqlite3.Connection, now: float, key: str, weight: int = 1) -> dict:
    """Record one request against the budget and return the budget after it."""
    conn.execute("INSERT INTO requests (ts, key, weight) VALUES (?, ?, ?)",
                 (float(now), str(key), int(weight)))
    conn.execute("DELETE FROM requests WHERE ts < ?", (float(now) - REQUEST_RETENTION,))
    conn.commit()
    return budget(conn, now)


def cooldown_remaining(conn: sqlite3.Connection, now: float, key: str,
                       cooldown: int = IDENTICAL_COOLDOWN) -> float:
    """Seconds until an identical request is allowed again. Zero when it is."""
    row = conn.execute("SELECT MAX(ts) AS last FROM requests WHERE key = ?",
                       (str(key),)).fetchone()
    last = row["last"] if row else None
    if last is None:
        return 0.0
    return max(0.0, float(last) + cooldown - float(now))


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------

def set_ledger(conn: sqlite3.Connection, symbol: str, interval: str, date: str,
               status: str, n_bars: int = 0, session: str = "rth",
               note: str = "", now: float | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown ledger status {status!r}; use one of {STATUSES}")
    now = int(now if now is not None else time.time())
    conn.execute(
        "INSERT INTO coverage (symbol, interval, date, status, n_bars, session,"
        " note, updated_ts) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(symbol, interval, date) DO UPDATE SET"
        " status=excluded.status, n_bars=excluded.n_bars,"
        " session=excluded.session, note=excluded.note,"
        " updated_ts=excluded.updated_ts",
        (str(symbol).upper(), barlib.normalize_interval(interval), str(date),
         status, int(n_bars), session, str(note), now),
    )
    conn.commit()


def get_ledger(conn: sqlite3.Connection, symbol: str, interval: str,
               dates=None) -> dict:
    sql = "SELECT * FROM coverage WHERE symbol = ? AND interval = ?"
    args = [str(symbol).upper(), barlib.normalize_interval(interval)]
    if dates:
        marks = ",".join("?" * len(dates))
        sql += f" AND date IN ({marks})"
        args.extend(list(dates))
    return {r["date"]: dict(r) for r in conn.execute(sql, args).fetchall()}


def ledger_summary(conn: sqlite3.Connection, symbol: str | None = None,
                   interval: str | None = None) -> dict:
    sql = "SELECT status, COUNT(*) AS n FROM coverage WHERE 1=1"
    args: list = []
    if symbol:
        sql += " AND symbol = ?"
        args.append(str(symbol).upper())
    if interval:
        sql += " AND interval = ?"
        args.append(barlib.normalize_interval(interval))
    sql += " GROUP BY status"
    return {r["status"]: r["n"] for r in conn.execute(sql, args).fetchall()}


# --------------------------------------------------------------------------
# request planning (pure)
# --------------------------------------------------------------------------

def duration_for(seconds: int, interval: str) -> str:
    """An IBKR ``durationStr`` covering ``seconds`` at this bar size.

    Intraday spans up to a day are expressed in seconds, which is the unit IBKR
    is happiest with. Anything longer, and every daily request, is expressed in
    days or years, because the seconds unit caps at 86400.
    """
    canon = barlib.normalize_interval(interval)
    seconds = max(int(math.ceil(seconds)), 1)
    if barlib.is_intraday(canon):
        if seconds <= 86400:
            return f"{max(seconds, 60)} S"
        days = int(math.ceil(seconds / 86400))
        return f"{days} D"
    days = int(math.ceil(seconds / 86400))
    if days <= 365:
        return f"{max(days, 1)} D"
    return f"{int(math.ceil(days / 365))} Y"


def chunk_requests(spans, interval: str, now: int) -> list[dict]:
    """Turn missing spans into IBKR requests. Pure.

    Each request covers at most ``CHUNK_SECONDS[interval]``, which is the max
    duration IBKR accepts for that bar size. Nothing is ever asked for beyond
    ``now``. Newest chunk first, because a fresh event is the reason this
    service exists and a budget that runs out should run out on the old data.
    """
    canon = barlib.normalize_interval(interval)
    chunk = CHUNK_SECONDS[canon]
    out: list[dict] = []
    for raw_start, raw_end in spans:
        start = int(raw_start)
        end = min(int(raw_end), int(now))
        while end > start:
            seg_start = max(start, end - chunk)
            out.append({
                "start_ts": seg_start,
                "end_ts": end,
                "duration": duration_for(end - seg_start, canon),
                "interval": canon,
            })
            end = seg_start
    out.sort(key=lambda r: r["end_ts"], reverse=True)
    return out


def weekdays_between(start_ts: int, end_ts: int) -> list[str]:
    """US/Eastern weekday dates touched by a span, as ``YYYY-MM-DD``. Pure."""
    first = barlib.market_time(start_ts).date()
    last = barlib.market_time(max(end_ts - 1, start_ts)).date()
    out = []
    day = first
    while day <= last:
        if day.weekday() < 5:
            out.append(day.strftime("%Y-%m-%d"))
        day += dt.timedelta(days=1)
    return out


# --------------------------------------------------------------------------
# the orchestrator
# --------------------------------------------------------------------------

def ensure_bars(symbol: str, interval: str, start, end, session: str = "rth",
                allow_fetch: bool = True, client=None, root_dir=None,
                now: float | None = None, contract: dict | None = None,
                what: str = "TRADES", conn: sqlite3.Connection | None = None) -> dict:
    """Serve bars from the store, fetching what is missing inside the budget.

    Never sleeps, never retries, never raises on a fetch failure. When the budget
    runs out it returns what the cache holds and says so in ``fetch_denied``.
    """
    canon = barlib.normalize_interval(interval)
    sym = str(symbol).upper()
    start_ts, end_ts = barlib.to_epoch(start), barlib.to_epoch(end)
    now = float(now if now is not None else time.time())

    owns_conn = conn is None
    conn = conn if conn is not None else open_db(root_dir)

    try:
        rows = store.read_bars(sym, canon, start_ts, end_ts, session, root_dir)
        cov = barlib.coverage(rows, sym, canon, start_ts, end_ts, session)

        result = {
            "symbol": sym, "interval": canon, "session": session,
            "rows": rows, "coverage": cov, "source": "cache",
            "fetched": [], "fetch_denied": None,
            "budget": budget(conn, now),
        }
        if cov["complete"] or not allow_fetch:
            if not cov["complete"] and not allow_fetch:
                result["fetch_denied"] = "allow_fetch is false, serving cache only"
            return result

        if client is None:
            result["fetch_denied"] = (
                "no gateway client given, serving cache only. Start the gateway "
                "and pass a client to fetch the gaps."
            )
            return result

        if contract is None:
            import contracts as contractlib
            contract = contractlib.resolve(sym, client=client, root_dir=root_dir,
                                           now=int(now))

        use_rth = session == "rth"
        plan = chunk_requests(barlib.missing_spans(cov), canon, int(now))
        fetched_any = False

        for request in plan:
            key = request_key(sym, canon, request["end_ts"], request["duration"],
                              what, use_rth)
            book = budget(conn, now)
            cost = weight_for(what)
            if book["remaining"] < cost:
                result["fetch_denied"] = (
                    f"pacing budget exhausted ({book['used']}/{book['limit']} in "
                    f"{book['window_seconds']}s), first slot frees at "
                    f"{barlib.to_iso(int(book['reset_at']))}"
                )
                break
            wait = cooldown_remaining(conn, now, key)
            if wait > 0:
                result["fetched"].append({
                    "end": barlib.to_iso(request["end_ts"]),
                    "duration": request["duration"], "bars": 0,
                    "status": "skipped",
                    "note": f"identical request {wait:.0f}s ago, cooldown is "
                            f"{IDENTICAL_COOLDOWN}s",
                })
                continue

            end_dt = dt.datetime.fromtimestamp(request["end_ts"], dt.timezone.utc)
            spend(conn, now, key, cost)
            try:
                raw = client.historical_bars(contract, end_dt, request["duration"],
                                             barlib.bar_size(canon), what=what,
                                             use_rth=use_rth)
            except Exception as exc:
                for date in weekdays_between(request["start_ts"], request["end_ts"]):
                    set_ledger(conn, sym, canon, date, "error", 0, session,
                               str(exc)[:300], now)
                result["fetched"].append({
                    "end": barlib.to_iso(request["end_ts"]),
                    "duration": request["duration"], "bars": 0,
                    "status": "error", "note": str(exc)[:300],
                })
                continue

            new_rows = barlib.bars_to_rows(raw, sym, canon, int(now), session)
            if new_rows:
                store.write_bars(new_rows, root_dir)
                fetched_any = True
                per_date: dict[str, int] = {}
                for row in new_rows:
                    date = barlib.session_date(int(row["ts"]), canon)
                    per_date[date] = per_date.get(date, 0) + 1
                spanned = weekdays_between(request["start_ts"], request["end_ts"])
                for date in sorted(set(spanned) | set(per_date)):
                    count = per_date.get(date, 0)
                    # the request answered, so a weekday with no bars inside it
                    # is a shut market rather than a pacing breach
                    set_ledger(conn, sym, canon, date,
                               "ok" if count else "no_data", count, session, "", now)
            else:
                for date in weekdays_between(request["start_ts"], request["end_ts"]):
                    set_ledger(conn, sym, canon, date, "empty", 0, session,
                               "IBKR returned no bars; pacing or entitlement", now)
            result["fetched"].append({
                "end": barlib.to_iso(request["end_ts"]),
                "duration": request["duration"], "bars": len(new_rows),
                "status": "ok" if new_rows else "empty",
            })

        if fetched_any:
            rows = store.read_bars(sym, canon, start_ts, end_ts, session, root_dir)
            result["rows"] = rows
            result["coverage"] = barlib.coverage(rows, sym, canon, start_ts,
                                                 end_ts, session)
            result["source"] = "mixed" if cov["n_bars"] else "fetched"
        result["budget"] = budget(conn, now)
        return result
    finally:
        if owns_conn:
            conn.close()
