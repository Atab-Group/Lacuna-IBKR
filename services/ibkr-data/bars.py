"""Pure transforms for IBKR bars.

No network, no filesystem, no clock reads. Every function that needs the current
time takes it as an argument. That is what makes the tests deterministic, and it
is the rule every pure function in here follows.

See CONTRACT.md for the ROW and COVERAGE shapes this module produces.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("US/Eastern")
UTC = dt.timezone.utc

RTH_OPEN = (9, 30)
RTH_CLOSE = (16, 0)

# canonical short name -> (IBKR bar size string, seconds, max duration per request)
INTERVALS = {
    "30s": ("30 secs", 30, "28800 S"),
    "1m": ("1 min", 60, "1 D"),
    "5m": ("5 mins", 300, "1 W"),
    "15m": ("15 mins", 900, "1 W"),
    "30m": ("30 mins", 1800, "1 M"),
    "1h": ("1 hour", 3600, "1 M"),
    "1d": ("1 day", 86400, "1 Y"),
}

# every spelling we accept on the way in
_ALIASES = {
    "30s": "30s", "30 secs": "30s", "30sec": "30s", "30secs": "30s", "30 sec": "30s",
    "1m": "1m", "1 min": "1m", "1min": "1m", "1 minute": "1m",
    "5m": "5m", "5 mins": "5m", "5min": "5m", "5 min": "5m",
    "15m": "15m", "15 mins": "15m", "15min": "15m", "15 min": "15m",
    "30m": "30m", "30 mins": "30m", "30min": "30m", "30 min": "30m",
    "1h": "1h", "1 hour": "1h", "1hour": "1h", "60m": "1h", "60 mins": "1h",
    "1d": "1d", "1 day": "1d", "1day": "1d", "d": "1d", "daily": "1d",
}


class IntervalError(ValueError):
    """An interval this service does not serve."""


def normalize_interval(name: str) -> str:
    """Any accepted spelling of a bar size to its canonical short name."""
    key = str(name).strip().lower()
    if key in _ALIASES:
        return _ALIASES[key]
    raise IntervalError(
        f"unknown interval {name!r}. Serve one of: {', '.join(sorted(INTERVALS))}"
    )


def bar_size(interval: str) -> str:
    """Canonical short name to the string IBKR's API wants."""
    return INTERVALS[normalize_interval(interval)][0]


def bar_seconds(interval: str) -> int:
    return INTERVALS[normalize_interval(interval)][1]


def max_duration(interval: str) -> str:
    """Longest ``durationStr`` IBKR accepts for one request at this bar size."""
    return INTERVALS[normalize_interval(interval)][2]


def is_intraday(interval: str) -> bool:
    return bar_seconds(interval) < 86400


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------

def to_epoch(value, assume_tz: ZoneInfo = MARKET_TZ) -> int:
    """Anything that names a moment to unix seconds UTC.

    Accepts an int, a ``date``, a ``datetime`` (naive or aware), or a string in
    ISO 8601 or ``YYYYMMDD  HH:MM:SS`` form. A naive value is read in
    ``assume_tz``, never in UTC, because that is what IB Gateway sends for US
    equities with ``formatDate=1``.
    """
    if isinstance(value, bool):
        raise TypeError("bool is not a timestamp")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=assume_tz)
        return int(value.astimezone(UTC).timestamp())
    if isinstance(value, dt.date):
        return int(dt.datetime(value.year, value.month, value.day,
                               tzinfo=assume_tz).timestamp())
    text = str(value).strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.isdigit() and len(text) == 8:  # 20260625
        parsed = dt.datetime.strptime(text, "%Y%m%d").replace(tzinfo=assume_tz)
        return int(parsed.astimezone(UTC).timestamp())
    cleaned = text.replace("Z", "+00:00")
    # IBKR's "20260625  13:30:00" and "20260625 13:30:00 US/Eastern"
    if cleaned[:8].isdigit() and len(cleaned) > 8 and not cleaned[8].isdigit():
        head = cleaned[:8]
        rest = cleaned[8:].strip()
        zone = assume_tz
        parts = rest.split()
        if len(parts) > 1:
            try:
                zone = ZoneInfo(parts[-1])
                rest = " ".join(parts[:-1])
            except Exception:
                rest = " ".join(parts)
        clock = rest or "00:00:00"
        parsed = dt.datetime.strptime(f"{head} {clock}", "%Y%m%d %H:%M:%S")
        return int(parsed.replace(tzinfo=zone).astimezone(UTC).timestamp())
    parsed = dt.datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=assume_tz)
    return int(parsed.astimezone(UTC).timestamp())


def to_iso(ts: int | None) -> str | None:
    """Unix seconds to the ISO 8601 UTC string every response uses."""
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def market_time(ts: int) -> dt.datetime:
    """Unix seconds to a US/Eastern aware datetime."""
    return dt.datetime.fromtimestamp(int(ts), UTC).astimezone(MARKET_TZ)


def session_date(ts: int, interval: str = "1m") -> str:
    """The US/Eastern calendar date a bar belongs to, as ``YYYY-MM-DD``."""
    return market_time(ts).strftime("%Y-%m-%d")


def session_of(ts: int, interval: str, requested: str = "rth") -> str:
    """``rth`` or ``eth`` for one bar START.

    Daily bars carry the session that was requested, because ``useRTH`` on the
    request is the only thing that distinguishes them. Intraday bars are judged
    on the clock: 09:30 up to but not including 16:00 US/Eastern on a weekday.
    """
    if not is_intraday(interval):
        return requested
    local = market_time(ts)
    if local.weekday() >= 5:
        return "eth"
    minutes = local.hour * 60 + local.minute
    open_m = RTH_OPEN[0] * 60 + RTH_OPEN[1]
    close_m = RTH_CLOSE[0] * 60 + RTH_CLOSE[1]
    return "rth" if open_m <= minutes < close_m else "eth"


def partition_key(ts: int, interval: str) -> str:
    """The parquet partition a bar lands in: a year for 1d, a date intraday."""
    local = market_time(ts)
    return local.strftime("%Y-%m-%d") if is_intraday(interval) else local.strftime("%Y")


def day_start_epoch(day: dt.date) -> int:
    """00:00 US/Eastern on a calendar date, as unix seconds."""
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=MARKET_TZ).timestamp())


# --------------------------------------------------------------------------
# BarData -> rows
# --------------------------------------------------------------------------

def _num(value, default=0.0):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:  # NaN
        return default
    return out


def bars_to_rows(bars, symbol: str, interval: str, pulled_ts: int,
                 requested_session: str = "rth", source: str = "ibkr",
                 instrument_timezone: str | None = None,
                 preserve_requested_session: bool = False) -> list[dict]:
    """IBKR ``BarData`` objects (or plain dicts) to canonical ROW dicts.

    Only attribute reads happen here, so nothing imports ``ib_async``. Rows come
    back sorted by ``ts`` with duplicate timestamps collapsed to the last one
    seen, which is what a retried chunk produces.
    """
    canon = normalize_interval(interval)
    sym = str(symbol).upper()
    zone = MARKET_TZ
    if instrument_timezone:
        try:
            zone = ZoneInfo(instrument_timezone)
        except Exception:
            zone = UTC
    out: dict[int, dict] = {}
    for bar in bars or []:
        get = bar.get if isinstance(bar, dict) else (lambda k, d=None, b=bar: getattr(b, k, d))
        raw_date = get("date", None)
        if raw_date is None:
            continue
        ts = to_epoch(raw_date, assume_tz=zone)
        out[ts] = {
            "ts": ts,
            "symbol": sym,
            "interval": canon,
            "open": _num(get("open", 0.0)),
            "high": _num(get("high", 0.0)),
            "low": _num(get("low", 0.0)),
            "close": _num(get("close", 0.0)),
            "volume": _num(get("volume", 0.0)),
            "wap": _num(get("average", get("wap", 0.0))),
            "bar_count": int(_num(get("barCount", get("bar_count", -1)), -1)),
            "session": (requested_session if preserve_requested_session else
                        session_of(ts, canon, requested_session)),
            "source": str(source),
            "pulled_ts": int(pulled_ts),
        }
        if instrument_timezone:
            out[ts]["session_date"] = dt.datetime.fromtimestamp(ts, UTC).astimezone(zone).date().isoformat()
            out[ts]["date_anchor"] = (f"exchange_midnight:{getattr(zone, 'key', str(zone))}"
                                      if not is_intraday(canon) else "bar_start_utc")
    return [out[k] for k in sorted(out)]


# --------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------

def expected_starts(start: int, end: int, interval: str,
                    session: str = "rth") -> list[int] | None:
    """Every bar start the asked range should contain, or None when unknowable.

    Daily: every weekday in range, stamped 00:00 US/Eastern. A US market holiday
    therefore appears here and shows up later as a one-weekday gap. That is
    deliberate; the ledger is where a holiday gets labelled ``no_data``.

    Intraday ``rth``: the 09:30 to 16:00 US/Eastern grid on each weekday.

    Intraday ``eth``: returns None. The extended grid depends on the instrument
    and is not knowable from here.
    """
    canon = normalize_interval(interval)
    if is_intraday(canon) and session != "rth":
        return None

    step = bar_seconds(canon)
    first = market_time(start).date()
    last = market_time(max(end - 1, start)).date()
    out: list[int] = []
    day = first
    one = dt.timedelta(days=1)
    while day <= last:
        if day.weekday() < 5:
            if not is_intraday(canon):
                ts = day_start_epoch(day)
                if start <= ts < end:
                    out.append(ts)
            else:
                open_ts = int(dt.datetime(day.year, day.month, day.day,
                                          RTH_OPEN[0], RTH_OPEN[1],
                                          tzinfo=MARKET_TZ).timestamp())
                close_ts = int(dt.datetime(day.year, day.month, day.day,
                                           RTH_CLOSE[0], RTH_CLOSE[1],
                                           tzinfo=MARKET_TZ).timestamp())
                ts = open_ts
                while ts < close_ts:
                    if start <= ts < end:
                        out.append(ts)
                    ts += step
        day += one
    return out


def _group_runs(missing: list[int], step_for) -> list[tuple[int, int]]:
    """Consecutive missing slots into half-open spans."""
    spans: list[tuple[int, int]] = []
    for ts in missing:
        step = step_for(ts)
        if spans and spans[-1][1] == ts:
            spans[-1] = (spans[-1][0], ts + step)
        else:
            spans.append((ts, ts + step))
    return spans


def coverage(rows: list[dict], symbol: str, interval: str, start, end,
             session: str = "rth") -> dict:
    """What was asked for against what is actually held. See CONTRACT.md."""
    canon = normalize_interval(interval)
    start_ts, end_ts = to_epoch(start), to_epoch(end)
    have = sorted({int(r["ts"]) for r in rows
                   if start_ts <= int(r["ts"]) < end_ts})

    expect = expected_starts(start_ts, end_ts, canon, session)
    step = bar_seconds(canon)

    if expect is not None:
        have_set = set(have)
        missing = [ts for ts in expect if ts not in have_set]
        if not is_intraday(canon):
            # daily slots are one weekday apart, so a run is contiguous in the
            # expected sequence rather than in wall-clock seconds
            spans = []
            index = {ts: i for i, ts in enumerate(expect)}
            for ts in missing:
                if spans and spans[-1][2] == index[ts] - 1:
                    spans[-1] = (spans[-1][0], ts + 86400, index[ts], spans[-1][3] + 1)
                else:
                    spans.append((ts, ts + 86400, index[ts], 1))
            gaps = [{"start": to_iso(a), "end": to_iso(b), "n_bars": n}
                    for a, b, _i, n in spans]
        else:
            spans2 = _group_runs(missing, lambda _ts: step)
            gaps = [{"start": to_iso(a), "end": to_iso(b),
                     "n_bars": max(1, (b - a) // step)} for a, b in spans2]
        expected_bars = len(expect)
    else:
        # eth intraday: judge only the runs between bars we actually hold, on the
        # same US/Eastern date, since the extended grid is unknown
        gaps = []
        for prev, nxt in zip(have, have[1:]):
            if nxt - prev <= step:
                continue
            if session_date(prev, canon) != session_date(nxt, canon):
                continue
            gaps.append({"start": to_iso(prev + step), "end": to_iso(nxt),
                         "n_bars": (nxt - prev - step) // step})
        expected_bars = None

    complete = bool(have) and not gaps
    if expect is not None:
        complete = len(have) >= len(expect) and not gaps

    return {
        "symbol": str(symbol).upper(),
        "interval": canon,
        "session": session,
        "asked": {"start": to_iso(start_ts), "end": to_iso(end_ts)},
        "got": {"start": to_iso(have[0]) if have else None,
                "end": to_iso(have[-1]) if have else None},
        "n_bars": len(have),
        "expected_bars": expected_bars,
        "gaps": gaps,
        "complete": complete,
    }


def missing_spans(cov: dict) -> list[tuple[int, int]]:
    """The gaps in a COVERAGE block as ``(start, end)`` unix second pairs.

    When nothing is held at all the whole asked range comes back as one span, so
    a cold store turns into a single fetch plan rather than a thousand.
    """
    asked_start = to_epoch(cov["asked"]["start"])
    asked_end = to_epoch(cov["asked"]["end"])
    if cov["n_bars"] == 0:
        return [(asked_start, asked_end)]

    spans = [(to_epoch(g["start"]), to_epoch(g["end"])) for g in cov["gaps"]]
    step = bar_seconds(cov["interval"])
    got_start = to_epoch(cov["got"]["start"])
    got_end = to_epoch(cov["got"]["end"]) + step
    if cov["expected_bars"] is None:
        # eth: the expected grid is unknown, so only the head and tail shortfall
        # against the asked range can be judged
        if got_start > asked_start:
            spans.append((asked_start, got_start))
        if got_end < asked_end:
            spans.append((got_end, asked_end))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for span in spans:
        if merged and span[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span[1]))
        else:
            merged.append(span)
    return merged
