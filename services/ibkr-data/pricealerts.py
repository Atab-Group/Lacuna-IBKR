"""Optional price-alert tools: read the live tape, list and test alert rules, change thresholds.

A separate price-alert engine runs beside this server on the same machine. It keeps a
one-minute tape in SQLite, reads its rules from a JSON file and writes a heartbeat file once a
minute. This module reads those files and, for one tool, rewrites the rules file. It never
talks to the gateway and never sends an alert. The formats belong to the engine's own contract;
the parts this module depends on are restated in ``CONTRACT.md`` beside this file.

The interface is four names:

- ``enabled(env)``: the tools exist only when ``LACUNA_PRICE_ALERTS_DIR`` is set.
- ``Config.from_env(env, ibkr_root, ibkr_dir)``: where the files are.
- ``call(name, args, cfg, now, can_write, runner)``: run one tool, return its payload.
- ``ToolError``: what ``call`` raises; ``mcpserver`` turns it into its usual error result.

Three rules hold for every tool. Recipient addresses and numbers never leave this module: the
listing returns counts, and every payload, a result or an error, is scrubbed of any recipient
value before it leaves. A result cut to a cap says so, with ``truncated`` and the cap. A
missing or unreadable file is a ``price_alerts_unavailable`` error that names the file and says
who acts next. Before the rules file is rewritten, the whole merged file is checked against the
engine's rules, so the writer never leaves a file the engine would ignore.

Standard library only, like the rest of the service.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ENV_DIR = "LACUNA_PRICE_ALERTS_DIR"
ENV_RULES = "LACUNA_PRICE_ALERTS_RULES"
ENV_BACKTEST = "LACUNA_PRICE_ALERTS_BACKTEST"
ENV_PYTHON = "LACUNA_PRICE_ALERTS_PYTHON"
ENV_STDIO_WRITE = "LACUNA_PRICE_ALERTS_STDIO_WRITE"
ENV_VALIDATOR = "LACUNA_PRICE_ALERTS_VALIDATOR"

DEFAULT_RULES = "~/.config/lacuna-ibkr/price-alerts.json"
DEFAULT_BACKTEST = "~/price-alerts/services/price-alerts/backtest.py"

TAPE_DB = "tape.db"
HEARTBEAT = "heartbeat.json"

TAPE_DEFAULT_MINUTES = 30
TAPE_MAX_MINUTES = 390
HISTORY_DEFAULT_LIMIT = 50
HISTORY_CAP = 200
HISTORY_DEFAULT_DAYS = 7
LIVE_TICK_SECONDS = 180
BACKTEST_TIMEOUT_SECONDS = 30
VALIDATOR_TIMEOUT_SECONDS = 10
EPOCH_MAX = 253402300799         # 9999-12-31T23:59:59Z, the last second a datetime can hold

TOOL_LIVE_TAPE = "ibkr_get_live_tape"
TOOL_LIST = "ibkr_list_price_alerts"
TOOL_HISTORY = "ibkr_list_price_alert_history"
TOOL_TEST = "ibkr_test_price_alert"
TOOL_UPDATE = "ibkr_update_price_alert"
TOOL_NAMES = [TOOL_LIVE_TAPE, TOOL_LIST, TOOL_HISTORY, TOOL_TEST, TOOL_UPDATE]

WATCH_ID = re.compile(r"^[a-z0-9-]+$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REQUIRE_NAMES = ("price", "volume", "market")

MINUTE_COLUMNS = ("minute", "last", "bid", "ask", "mid", "spread_pct", "volume", "trades",
                  "dollars", "day_volume", "data_type", "source")
READING_COLUMNS = ("verdict", "reason", "held", "move_pct", "volume_x", "window_volume",
                   "window_trades", "window_dollars", "market_pct", "relative_pct",
                   "market_fresh", "price_pass", "volume_pass", "market_pass")
ALERT_COLUMNS = ("key", "watch_id", "rule", "tier", "direction", "minute", "created_ts",
                 "subject", "figures", "status", "email_ok", "whatsapp_ok", "sent_ts",
                 "market_check")

# The fields the writer may change, each with its bounds (inclusive) and kind. Everything
# else in a watch is out of reach: contracts, hours, subject, the cooldown, recipients.
_MOVE = ("number", 0.05, 20.0)
_VOLUME_X = ("number", 1.0, 100.0)
_STEP = ("number", 0.1, 10.0)
ALLOWED_FIELDS = {
    "tiers.early.move_pct": _MOVE,
    "tiers.early.volume_x": _VOLUME_X,
    "tiers.early.require": ("require", None, None),
    "tiers.firm.move_pct": _MOVE,
    "tiers.firm.volume_x": _VOLUME_X,
    "tiers.firm.require": ("require", None, None),
    "volume.min_trades": ("integer", 0, 1000),
    "volume.min_dollars": ("number", 0.0, 100_000_000.0),
    "price.max_spread_pct": ("number", 0.05, 10.0),
    "gap.move_pct": _MOVE,
    "gap.step_pct": _STEP,
    "gap.rearm_pct": _STEP,
    "enabled": ("boolean", None, None),
}
_ALLOWED_PREFIXES = {path.rsplit(".", i)[0]
                     for path in ALLOWED_FIELDS for i in range(1, path.count(".") + 1)}

REDACTED = "[recipient]"


class ToolError(Exception):
    """A failure the caller can act on. ``mcpserver`` renders it with ``fail()``."""

    def __init__(self, code, message, next_action, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_action = next_action
        self.extra = extra


def _bad(message, next_action, **extra):
    return ToolError("bad_argument", message, next_action, **extra)


def _unavailable(message, **extra):
    return ToolError(
        "price_alerts_unavailable", message,
        "Tell the operator: the price-alert engine's files are missing or unreadable on the "
        "server, so its state cannot be read. Check that the engine is installed and running "
        "and that this server's price-alert paths point at its files. Do not retry until "
        "they say it is fixed.", **extra)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def enabled(env=None) -> bool:
    """The five tools exist only when the state directory is named."""
    env = os.environ if env is None else env
    return bool((env.get(ENV_DIR) or "").strip())


@dataclass(frozen=True)
class Config:
    state_dir: Path
    rules_path: Path
    backtest_path: Path
    python: str
    ibkr_root: str
    ibkr_dir: str
    validator_path: Path | None = None   # the engine's config.py; None skips that check

    @property
    def tape_path(self) -> Path:
        return self.state_dir / TAPE_DB

    @property
    def heartbeat_path(self) -> Path:
        return self.state_dir / HEARTBEAT

    @property
    def lock_path(self) -> Path:
        return self.rules_path.with_name(self.rules_path.name + ".lock")

    @classmethod
    def from_env(cls, env=None, ibkr_root="", ibkr_dir=""):
        env = os.environ if env is None else env

        def path(name, default):
            return Path(os.path.expanduser((env.get(name) or "").strip() or default))

        return cls(
            state_dir=path(ENV_DIR, ""),
            rules_path=path(ENV_RULES, DEFAULT_RULES),
            backtest_path=path(ENV_BACKTEST, DEFAULT_BACKTEST),
            python=(env.get(ENV_PYTHON) or "").strip() or sys.executable,
            ibkr_root=str(ibkr_root),
            ibkr_dir=str(ibkr_dir),
            validator_path=(path(ENV_VALIDATOR, "") if (env.get(ENV_VALIDATOR) or "").strip()
                            else None),
        )


def write_allowed(can_write, env=None) -> bool:
    """HTTP passes the token's flag. Stdio passes None and the operator's env decides."""
    if can_write is not None:
        return bool(can_write)
    env = os.environ if env is None else env
    return env.get(ENV_STDIO_WRITE) == "1"


# --------------------------------------------------------------------------
# the one entry point
# --------------------------------------------------------------------------

def call(name, args, cfg, now, can_write=None, runner=None, env=None) -> dict:
    """Run one price-alert tool. Raises ToolError for anything the caller must act on."""
    runner = runner or run_subprocess
    handlers = {
        TOOL_LIVE_TAPE: lambda: live_tape(cfg, args, now),
        TOOL_LIST: lambda: list_alerts(cfg, now),
        TOOL_HISTORY: lambda: alert_history(cfg, args, now),
        TOOL_TEST: lambda: backtest_alert(cfg, args, runner),
        TOOL_UPDATE: lambda: update_alert(cfg, args, write_allowed(can_write, env), runner),
    }
    if name not in handlers:
        raise KeyError(name)
    try:
        payload = handlers[name]()
    except ToolError as exc:
        raise _scrubbed_error(exc, _recipient_values(cfg)) from None
    except Exception as exc:
        # Anything else would reach the caller as raw exception text. Give it a code and
        # send it through the same scrub.
        failure = ToolError("price_alert_tool_failed", f"{type(exc).__name__}: {exc}",
                            "Tell the operator this price-alert tool failed on the server. "
                            "Do not retry until they say it is fixed.")
        raise _scrubbed_error(failure, _recipient_values(cfg)) from None
    return redact(payload, _recipient_values(cfg) or [])


UNREADABLE_DETAIL = ("the price-alert engine's files could not be read, and the rules file "
                     "could not be read to check the details for recipient values")


def _scrubbed_error(exc, secrets):
    """The error with every recipient value replaced. When the rules file could not be read,
    nothing tells us what to scrub, so only the code, a fixed message and next_action stay."""
    if secrets is None:
        return ToolError(exc.code, UNREADABLE_DETAIL, exc.next_action)
    return ToolError(exc.code, redact(exc.message, secrets), redact(exc.next_action, secrets),
                     **redact(exc.extra, secrets))


# --------------------------------------------------------------------------
# reading the files
# --------------------------------------------------------------------------

def _open_tape(cfg):
    path = cfg.tape_path
    if not path.is_file():
        raise _unavailable(f"the tape database {path} does not exist")
    try:
        conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM minutes LIMIT 1").fetchall()
    except sqlite3.Error as exc:
        raise _unavailable(f"the tape database {path} could not be read ({exc})")
    return conn


def _query(conn, sql, params, path):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise _unavailable(f"the tape database {path} could not be read ({exc})")


def read_heartbeat(cfg) -> dict:
    path = cfg.heartbeat_path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _unavailable(f"the engine heartbeat {path} does not exist")
    except (OSError, ValueError) as exc:
        raise _unavailable(f"the engine heartbeat {path} could not be read ({exc})")
    if not isinstance(data, dict):
        raise _unavailable(f"the engine heartbeat {path} does not hold a JSON object")
    return data


def _parse_rules(text, path) -> dict:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise _unavailable(f"the rules file {path} is not valid JSON ({exc})")
    if not isinstance(data, dict) or not isinstance(data.get("watches"), list):
        raise _unavailable(f"the rules file {path} has no watches list")
    for watch in data["watches"]:
        if not isinstance(watch, dict) or not isinstance(watch.get("id"), str):
            raise _unavailable(f"the rules file {path} holds a watch without an id")
    return data


def read_rules(cfg) -> dict:
    path = cfg.rules_path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise _unavailable(f"the rules file {path} does not exist")
    except OSError as exc:
        raise _unavailable(f"the rules file {path} could not be read ({exc})")
    return _parse_rules(text, path)


def _recipient_values(cfg) -> list[str] | None:
    """Every recipient value in the rules file, for the scrub. None when unreadable."""
    try:
        rules = read_rules(cfg)
    except ToolError:
        return None
    found = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, (str, int)) and not isinstance(node, bool):
            text = str(node).strip()
            if len(text) >= 5:
                found.append(text)

    walk(rules.get("recipients"))
    return sorted(set(found), key=len, reverse=True)


def redact(node, secrets):
    """Replace any recipient value inside any string of a payload."""
    if not secrets:
        return node
    if isinstance(node, dict):
        return {key: redact(value, secrets) for key, value in node.items()}
    if isinstance(node, list):
        return [redact(value, secrets) for value in node]
    if isinstance(node, str):
        for secret in secrets:
            if secret in node:
                node = node.replace(secret, REDACTED)
        return node
    return node


def _find_watch(rules, watch_id):
    for watch in rules["watches"]:
        if watch.get("id") == watch_id:
            return watch
    known = [w["id"] for w in rules["watches"]]
    raise _bad(f"watch: no watch named {watch_id!r}",
               "Call ibkr_list_price_alerts for the watch ids, then call again with one.",
               path="watch", known_watches=known)


# --------------------------------------------------------------------------
# argument reading
# --------------------------------------------------------------------------

def _need_watch_id(args, key="watch", required=True):
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not WATCH_ID.match(value.strip()):
        raise _bad(f"{key}: a watch id is required, lower case letters, digits and dashes, "
                   "such as example-watch",
                   "Call ibkr_list_price_alerts for the watch ids, then call again with one.",
                   path=key)
    return value.strip()


def _bounded_int(args, key, default, low, high):
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _bad(f"{key}: must be a whole number from {low} to {high}, got {value!r}",
                   f"Call again with {key} between {low} and {high}.", path=key)
    return value


def _as_epoch(value, key, end_of_day=False) -> int:
    """An epoch number, an ISO date or an ISO timestamp. A date as an end is exclusive of the
    day after it, so ``end=2026-03-04`` covers all of the 4th."""
    if isinstance(value, bool):
        raise _bad(f"{key}: must be an ISO date, an ISO timestamp or epoch seconds",
                   "Pass a date such as 2026-03-04.", path=key)
    if isinstance(value, (int, float)):
        return _epoch_in_range(value, key)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return _epoch_in_range(int(text), key)
        if ISO_DATE.match(text):
            try:
                day = date.fromisoformat(text)
            except ValueError:
                raise _bad(f"{key}: {text!r} is not a real date", "Pass a real date.", path=key)
            if end_of_day:
                day += timedelta(days=1)
            return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return int(moment.timestamp())
    raise _bad(f"{key}: {value!r} is not an ISO date, an ISO timestamp or epoch seconds",
               "Pass a date such as 2026-03-04 or a timestamp such as 2026-03-04T14:30:00Z.",
               path=key)


def _epoch_in_range(value, key) -> int:
    if isinstance(value, float) and not math.isfinite(value) or not 0 <= value <= EPOCH_MAX:
        raise _bad(f"{key}: epoch seconds must be from 0 to {EPOCH_MAX}",
                   "Pass a date such as 2026-03-04.", path=key)
    return int(value)


def _iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------

def _age(now, ts):
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    return int(now - ts)


def watch_health(heartbeat, watch_id, now) -> dict:
    """Live means a live feed (data type 1), in hours, with a tick in the last 180 seconds."""
    inst = (heartbeat.get("instruments") or {}).get(watch_id) or {}
    ref = (heartbeat.get("references") or {}).get(watch_id) or {}
    tick_age = _age(now, inst.get("last_tick_ts"))
    in_hours = inst.get("in_hours")
    data_type = inst.get("data_type")
    live = (data_type == 1 and in_hours is True and tick_age is not None
            and tick_age <= LIVE_TICK_SECONDS)
    return {"live": live, "data_type": data_type, "last_tick_age_seconds": tick_age,
            "in_hours": in_hours, "market_fresh": ref.get("fresh")}


def engine_block(heartbeat, now) -> dict:
    return {"heartbeat_age_seconds": _age(now, heartbeat.get("ts")),
            "dry_run": heartbeat.get("dry_run"),
            "gateway_connected": heartbeat.get("gateway_connected"),
            "rules_ok": heartbeat.get("rules_ok"),
            "deployed": heartbeat.get("deployed")}


# --------------------------------------------------------------------------
# ibkr_get_live_tape
# --------------------------------------------------------------------------

def live_tape(cfg, args, now) -> dict:
    watch_id = _need_watch_id(args)
    minutes = _bounded_int(args, "minutes", TAPE_DEFAULT_MINUTES, 1, TAPE_MAX_MINUTES)
    heartbeat = read_heartbeat(cfg)
    conn = _open_tape(cfg)
    try:
        newest = _query(conn, "SELECT max(minute) FROM minutes WHERE instrument = ?",
                        (watch_id,), cfg.tape_path)[0][0]
        if newest is None:
            # No rows at all. Say "unknown watch" when the rules can tell us so.
            try:
                rules = read_rules(cfg)
            except ToolError:
                rules = None
            if rules is not None:
                _find_watch(rules, watch_id)
            health = watch_health(heartbeat, watch_id, now)
            return {"watch": watch_id, "rows": [], "newest_minute": None,
                    "newest_minute_utc": None, "age_seconds": None,
                    "live": health["live"], "market_fresh": health["market_fresh"],
                    "heartbeat_age_seconds": _age(now, heartbeat.get("ts")),
                    "n_returned": 0, "truncated": False, "cap": TAPE_MAX_MINUTES,
                    "note": "the tape holds no rows for this watch yet"}
        first = newest - (minutes - 1) * 60
        cols = ", ".join(f"m.{c}" for c in MINUTE_COLUMNS)
        rcols = ", ".join(f"r.{c} AS r_{c}" for c in READING_COLUMNS)
        rows = _query(conn,
                      f"SELECT {cols}, r.watch_id AS r_present, {rcols} FROM minutes m "
                      "LEFT JOIN readings r ON r.watch_id = m.instrument "
                      "AND r.minute = m.minute AND r.rule = 'move' "
                      "WHERE m.instrument = ? AND m.minute >= ? ORDER BY m.minute",
                      (watch_id, first), cfg.tape_path)
        older = _query(conn, "SELECT 1 FROM minutes WHERE instrument = ? AND minute < ? LIMIT 1",
                       (watch_id, first), cfg.tape_path)
    finally:
        conn.close()

    out = []
    for row in rows:
        item = {c: row[c] for c in MINUTE_COLUMNS}
        item["time_utc"] = _iso(row["minute"])
        item["move"] = ({c: row[f"r_{c}"] for c in READING_COLUMNS}
                        if row["r_present"] is not None else None)
        out.append(item)
    health = watch_health(heartbeat, watch_id, now)
    return {
        "watch": watch_id,
        "rows": out,
        "newest_minute": newest,
        "newest_minute_utc": _iso(newest),
        # A row for minute m is written once the minute closes, at m + 60.
        "age_seconds": int(now - (newest + 60)),
        "live": health["live"],
        "market_fresh": health["market_fresh"],
        "heartbeat_age_seconds": _age(now, heartbeat.get("ts")),
        "n_returned": len(out),
        "truncated": bool(older),
        "cap": TAPE_MAX_MINUTES,
    }


# --------------------------------------------------------------------------
# ibkr_list_price_alerts
# --------------------------------------------------------------------------

def list_alerts(cfg, now) -> dict:
    rules = read_rules(cfg)
    heartbeat = read_heartbeat(cfg)
    conn = _open_tape(cfg)
    try:
        last = {}
        for watch in rules["watches"]:
            found = _query(conn, "SELECT minute, tier, direction, subject FROM alerts "
                                 "WHERE watch_id = ? ORDER BY minute DESC, created_ts DESC "
                                 "LIMIT 1", (watch["id"],), cfg.tape_path)
            last[watch["id"]] = ({"minute": found[0]["minute"],
                                  "time_utc": _iso(found[0]["minute"]),
                                  "tier": found[0]["tier"],
                                  "direction": found[0]["direction"],
                                  "subject": found[0]["subject"]} if found else None)
    finally:
        conn.close()

    watches = []
    for watch in rules["watches"]:
        market = watch.get("market")
        watches.append({
            "id": watch["id"],
            "label": watch.get("label"),
            "subject": watch.get("subject"),
            "enabled": watch.get("enabled"),
            "context": watch.get("context"),
            "contract": watch.get("contract"),
            "hours": watch.get("hours"),
            "tiers": watch.get("tiers"),
            "volume": watch.get("volume"),
            "price": watch.get("price"),
            "gap": watch.get("gap"),
            "market_kind": market.get("kind") if isinstance(market, dict) else None,
            "last_alert": last[watch["id"]],
            "health": watch_health(heartbeat, watch["id"], now),
        })
    recipients = rules.get("recipients") if isinstance(rules.get("recipients"), dict) else {}

    def count(key):
        value = recipients.get(key)
        return len(value) if isinstance(value, list) else 0

    return {"watches": watches,
            "recipients": {"email": count("email"), "whatsapp": count("whatsapp")},
            "cooldown_min": rules.get("cooldown_min"),
            "engine": engine_block(heartbeat, now)}


# --------------------------------------------------------------------------
# ibkr_list_price_alert_history
# --------------------------------------------------------------------------

def alert_history(cfg, args, now) -> dict:
    watch_id = _need_watch_id(args, required=False)
    limit = _bounded_int(args, "limit", HISTORY_DEFAULT_LIMIT, 1, HISTORY_CAP)
    end = (_as_epoch(args["end"], "end", end_of_day=True) if args.get("end") is not None
           else int(now))
    start = (_as_epoch(args["start"], "start") if args.get("start") is not None
             else end - HISTORY_DEFAULT_DAYS * 86400)
    if end <= start:
        raise _bad(f"end: {args.get('end')!r} is not after start {args.get('start')!r}",
                   "Call again with end later than start.", path="end")

    where = "minute >= ? AND minute < ?"
    params = [start, end]
    if watch_id:
        where += " AND watch_id = ?"
        params.append(watch_id)
    conn = _open_tape(cfg)
    try:
        rows = _query(conn, f"SELECT {', '.join(ALERT_COLUMNS)} FROM alerts WHERE {where} "
                            "ORDER BY minute DESC, created_ts DESC LIMIT ?",
                      (*params, limit + 1), cfg.tape_path)
    finally:
        conn.close()

    alerts = []
    for row in rows[:limit]:
        item = {c: row[c] for c in ALERT_COLUMNS}
        item["time_utc"] = _iso(row["minute"])
        try:
            item["figures"] = json.loads(row["figures"])
        except (TypeError, ValueError):
            item["figures_unparsed"] = True
        alerts.append(item)
    return {"alerts": alerts, "n_returned": len(alerts), "truncated": len(rows) > limit,
            "limit": limit, "cap": HISTORY_CAP,
            "window": {"start": _iso(start), "end": _iso(end), "watch": watch_id}}


# --------------------------------------------------------------------------
# validating a change
# --------------------------------------------------------------------------

def _check_value(path, value):
    kind, low, high = ALLOWED_FIELDS[path]
    if kind == "boolean":
        if not isinstance(value, bool):
            raise _bad(f"{path}: must be true or false, got {value!r}",
                       f"Call again with {path} as true or false.", path=path)
        return value
    if kind == "require":
        if (not isinstance(value, list) or not value
                or any(not isinstance(v, str) or v not in REQUIRE_NAMES for v in value)
                or len(set(value)) != len(value)):
            raise _bad(f"{path}: must be a non-empty list drawn from price, volume and market, "
                       f"each at most once, got {value!r}",
                       f"Call again with {path} such as [\"price\", \"volume\"].", path=path)
        return list(value)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or isinstance(value, float) and not math.isfinite(value)):
        raise _bad(f"{path}: must be a number, got {value!r}",
                   f"Call again with {path} as a number from {low} to {high}.", path=path)
    if kind == "integer":
        if isinstance(value, float) and not value.is_integer():
            raise _bad(f"{path}: must be a whole number, got {value!r}",
                       f"Call again with {path} as a whole number from {low} to {high}.",
                       path=path)
        value = int(value)
    if not low <= value <= high:
        raise _bad(f"{path}: {value!r} is outside the allowed range {low} to {high}",
                   f"Call again with {path} from {low} to {high}.", path=path)
    return value


def _flatten_changes(changes, prefix="", flat=None):
    """A partial watch to {dotted path: value}, refusing every path the writer may not touch.

    A key may be dotted (``"tiers.early.move_pct"``) or nested, or a mix of both. Two keys
    that reach the same field are refused, because which one wins would be a guess.
    """
    flat = {} if flat is None else flat
    for key, value in changes.items():
        path = f"{prefix}{key}"
        if path in ALLOWED_FIELDS:
            if path in flat:
                raise _bad(f"{path}: given twice, once as a dotted key and once nested",
                           f"Give {path} once, either dotted or nested.", path=path)
            flat[path] = _check_value(path, value)
        elif path in _ALLOWED_PREFIXES and isinstance(value, dict):
            _flatten_changes(value, path + ".", flat)
        else:
            raise _bad(f"{path}: this field cannot be changed through this tool",
                       "Change only tier thresholds and require lists, volume.min_trades, "
                       "volume.min_dollars, price.max_spread_pct, gap.move_pct, gap.step_pct, "
                       "gap.rearm_pct or enabled. Anything else is changed by the operator "
                       "by hand.", path=path, allowed=sorted(ALLOWED_FIELDS))
    return flat


def _nest(flat) -> dict:
    """{dotted path: value} back to a nested object, the only shape the engine merges."""
    out = {}
    for path, value in flat.items():
        *parents, leaf = path.split(".")
        node = out
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = copy.deepcopy(value)
    return out


def _same(old, new) -> bool:
    """Equal in value and in kind: 1 and 1.0 are the same threshold, true and 1 are not."""
    return old == new and isinstance(old, bool) == isinstance(new, bool)


def _is_number(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and not (isinstance(value, float) and not math.isfinite(value)))


def _cross_field_faults(watch):
    """The cross-field rules of the engine contract, section 3, on one whole watch.

    Yields (path, what is wrong, what to do). Fields that are absent or of the wrong type
    are left to the field rules in ``check_rules``.
    """
    tiers = watch.get("tiers") if isinstance(watch.get("tiers"), dict) else {}
    early = tiers.get("early") if isinstance(tiers.get("early"), dict) else {}
    firm = tiers.get("firm") if isinstance(tiers.get("firm"), dict) else {}
    for field in ("move_pct", "volume_x"):
        lo, hi = early.get(field), firm.get(field)
        if _is_number(lo) and _is_number(hi) and lo > hi:
            yield (f"tiers.early.{field}", f"{lo} would exceed the firm tier's {hi}",
                   f"Keep tiers.early.{field} at or below tiers.firm.{field}; change both "
                   "in one call if both must move.")
    has_market = isinstance(watch.get("market"), dict)
    for name, tier in (("early", early), ("firm", firm)):
        if "require" not in tier:
            continue
        path, require = f"tiers.{name}.require", tier["require"]
        if (not isinstance(require, list) or not require
                or any(not isinstance(r, str) or r not in REQUIRE_NAMES for r in require)
                or len(set(require)) != len(require)):
            yield (path, "must be a non-empty list drawn from price, volume and market, "
                         "each at most once",
                   f"Call again with {path} such as [\"price\", \"volume\"].")
        elif not {"price", "volume"} & set(require):
            yield (path, "must include price or volume; market alone is refused",
                   f"Add price or volume to {path}.")
        elif "market" in require and not has_market:
            yield (path, "names market, but this watch has no market object",
                   f"Leave market out of {path}; the operator adds a market by hand.")
    gap = watch.get("gap")
    if isinstance(gap, dict):
        rearm, move = gap.get("rearm_pct"), gap.get("move_pct")
        if _is_number(rearm) and _is_number(move) and rearm > move:
            yield ("gap.rearm_pct", f"{rearm} would exceed gap.move_pct {move}",
                   "Keep gap.rearm_pct at or below gap.move_pct; change both in one call if "
                   "both must move.")


def normalise_changes(changes) -> dict:
    """Validate a partial watch and return it as {dotted path: value}, in the given order."""
    if not isinstance(changes, dict) or not changes:
        raise _bad("changes: a non-empty object is required, a partial watch such as "
                   "{\"tiers\": {\"early\": {\"move_pct\": 1.0}}}",
                   "Call again with the fields to change.", path="changes")
    return _flatten_changes(changes)


def plan_changes(watch, changes):
    """Validate a partial watch against one watch. Returns (after, changed paths). Pure."""
    flat = normalise_changes(changes)
    after = copy.deepcopy(watch)
    changed = []
    for path, value in flat.items():
        parts = path.split(".")
        node = after
        for depth, part in enumerate(parts[:-1]):
            child = node.get(part)
            if not isinstance(child, dict):
                where = ".".join(parts[:depth + 1])
                raise _bad(f"{path}: watch {watch['id']!r} has no {where} section",
                           f"Leave {where} out of changes; this watch does not use it.",
                           path=path)
            node = child
        leaf = parts[-1]
        if leaf not in node or not _same(node[leaf], value):
            changed.append(path)
            node[leaf] = value
    for path, problem, next_action in _cross_field_faults(after):
        raise _bad(f"{path}: {problem}", next_action, path=path)
    return after, changed


def _check_overrides(rules, overrides) -> dict:
    """Validate overrides and return them nested, keyed by watch id, for the backtest."""
    if not isinstance(overrides, dict) or not overrides:
        raise _bad("overrides: must be a non-empty object keyed by watch id",
                   "Pass overrides such as {\"example-watch\": {\"tiers\": {\"early\": "
                   "{\"move_pct\": 1.0}}}}, or leave it out.", path="overrides")
    nested = {}
    for watch_id, changes in overrides.items():
        try:
            plan_changes(_find_watch(rules, watch_id), changes)
            nested[watch_id] = _nest(normalise_changes(changes))
        except ToolError as exc:
            path = exc.extra.get("path") or ""
            exc.extra["path"] = f"overrides.{watch_id}.{path}".rstrip(".")
            exc.message = f"overrides.{watch_id}: {exc.message}"
            raise
    return nested


# --------------------------------------------------------------------------
# the whole-file check before a write
# --------------------------------------------------------------------------
# A local copy of the engine contract's section 3 field rules. The engine ignores a rules
# file that breaks any of them, so the writer refuses to produce one. Kept apart from the
# change rules above on purpose: when the two codebases drift, this check is what says so.

_HHMM = re.compile(r"^(\d{2}):(\d{2})$")
_MARKET_KINDS = ("ibkr_basket", "nasdaq_quote", "ibkr_contract")


class _Fault(Exception):
    def __init__(self, field, reason):
        super().__init__(f"{field}: {reason}")
        self.field, self.reason = field, reason


def _num_field(obj, key, where, low, high, required=True, integer=False):
    field = f"{where}.{key}" if where else key
    value = obj.get(key)
    if value is None:
        if required:
            raise _Fault(field, "is required")
        return None
    if not _is_number(value):
        raise _Fault(field, "must be a number")
    if integer and isinstance(value, float) and not value.is_integer():
        raise _Fault(field, "must be a whole number")
    if not low <= value <= high:
        raise _Fault(field, f"must be from {low} to {high}")
    return value


def _obj_field(obj, key, where, required=True):
    field = f"{where}.{key}" if where else key
    value = obj.get(key)
    if value is None:
        if required:
            raise _Fault(field, "is required")
        return None
    if not isinstance(value, dict):
        raise _Fault(field, "must be an object")
    return value


def _str_field(obj, key, where, required=True):
    field = f"{where}.{key}"
    if key not in obj and not required:
        return None
    value = obj.get(key)
    if not isinstance(value, str) or (required and not value.strip()):
        raise _Fault(field, "must be a non-empty string" if required else "must be a string")
    return value


def _contract_field(value, field):
    if not isinstance(value, dict) or not (value.get("symbol") or value.get("con_id")):
        raise _Fault(field, "needs a symbol or a con_id")


def _minutes(value, field):
    match = _HHMM.match(value) if isinstance(value, str) else None
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise _Fault(field, "must be HH:MM")
    return int(match.group(1)) * 60 + int(match.group(2))


def _check_watch(watch, where):
    from zoneinfo import ZoneInfo

    if not isinstance(watch.get("enabled", True), bool):
        raise _Fault(f"{where}.enabled", "must be true or false")
    _str_field(watch, "subject", where)
    for key in ("label", "venue", "context"):
        _str_field(watch, key, where, required=False)
    _num_field(watch, "no_tick_alarm_min", where, 1, 120, required=False, integer=True)
    _contract_field(watch.get("contract"), f"{where}.contract")

    hours = _obj_field(watch, "hours", where)
    tz = _str_field(hours, "tz", f"{where}.hours")
    try:
        ZoneInfo(tz)
    except Exception:
        raise _Fault(f"{where}.hours.tz", "is not a known time zone") from None
    if (_minutes(hours.get("open"), f"{where}.hours.open")
            >= _minutes(hours.get("close"), f"{where}.hours.close")):
        raise _Fault(f"{where}.hours", "open must be before close")

    price = _obj_field(watch, "price", where)
    if price.get("source") not in ("last", "mid"):
        raise _Fault(f"{where}.price.source", "must be last or mid")
    _num_field(price, "lookback_min", f"{where}.price", 1, 30, integer=True)
    _num_field(price, "max_spread_pct", f"{where}.price", 0.05, 10.0, required=False)

    volume = _obj_field(watch, "volume", where)
    window = volume.get("window_min")
    if isinstance(window, bool) or window not in (1, 3):
        raise _Fault(f"{where}.volume.window_min", "must be 1 or 3")
    if volume.get("baseline") not in ("median", "mean"):
        raise _Fault(f"{where}.volume.baseline", "must be median or mean")
    baseline = _num_field(volume, "baseline_min", f"{where}.volume", 1, 90, integer=True)
    if baseline % window:
        raise _Fault(f"{where}.volume.baseline_min", "must be a multiple of window_min")
    _num_field(volume, "min_trades", f"{where}.volume", 0, 1000, required=False, integer=True)
    _num_field(volume, "min_dollars", f"{where}.volume", 0, 100_000_000, required=False)

    market = _obj_field(watch, "market", where, required=False)
    if market is not None:
        kind = market.get("kind")
        if kind not in _MARKET_KINDS:
            raise _Fault(f"{where}.market.kind", "must be " + ", ".join(_MARKET_KINDS))
        if kind == "ibkr_basket":
            symbols = market.get("symbols")
            if not isinstance(symbols, list) or not symbols or not all(
                    isinstance(s, str) and s for s in symbols):
                raise _Fault(f"{where}.market.symbols", "must be a non-empty list of symbols")
            fresh = _num_field(market, "min_fresh", f"{where}.market", 1, 50, integer=True)
            if fresh > len(symbols):
                raise _Fault(f"{where}.market.min_fresh", "is larger than the basket")
            _str_field(market, "exchange", f"{where}.market")
            _str_field(market, "currency", f"{where}.market")
        elif kind == "nasdaq_quote":
            _str_field(market, "symbol", f"{where}.market")
        else:
            _contract_field(market.get("contract"), f"{where}.market.contract")

    tiers = _obj_field(watch, "tiers", where)
    if set(tiers) != {"early", "firm"}:
        raise _Fault(f"{where}.tiers", "must hold exactly the keys early and firm")
    for name in ("early", "firm"):
        tier = _obj_field(tiers, name, f"{where}.tiers")
        _num_field(tier, "move_pct", f"{where}.tiers.{name}", 0.05, 20.0)
        _num_field(tier, "volume_x", f"{where}.tiers.{name}", 1.0, 100.0)

    gap = _obj_field(watch, "gap", where, required=False)
    if gap is not None:
        _str_field(gap, "against", f"{where}.gap")
        _num_field(gap, "ratio", f"{where}.gap", 0.0001, 10000.0)
        _contract_field(gap.get("fx"), f"{where}.gap.fx")
        _num_field(gap, "move_pct", f"{where}.gap", 0.05, 20.0)
        _num_field(gap, "step_pct", f"{where}.gap", 0.1, 10.0)
        _num_field(gap, "rearm_pct", f"{where}.gap", 0.1, 10.0)
        if price.get("source") != "mid":
            raise _Fault(f"{where}.gap", "needs price.source mid")

    for path, problem, _ in _cross_field_faults(watch):
        raise _Fault(f"{where}.{path}", problem)


def _check_rules_file(rules):
    if not isinstance(rules, dict):
        raise _Fault("rules", "must be a JSON object")
    if rules.get("v") != 1 or isinstance(rules.get("v"), bool):
        raise _Fault("v", "must be 1")
    _num_field(rules, "cooldown_min", "", 1, 240, integer=True)
    recipients = _obj_field(rules, "recipients", "")
    email = recipients.get("email", [])
    if not isinstance(email, list) or not all(isinstance(a, str) and "@" in a for a in email):
        raise _Fault("recipients.email", "must be a list of addresses")
    whatsapp = recipients.get("whatsapp", [])
    if not isinstance(whatsapp, list) or not all(
            isinstance(n, str) and (n.isdigit() or n.endswith("@g.us")) for n in whatsapp):
        raise _Fault("recipients.whatsapp", "must hold digits-only numbers or ids ending @g.us")
    watches = rules.get("watches")
    if not isinstance(watches, list):
        raise _Fault("watches", "must be a list")
    ids = []
    for i, watch in enumerate(watches):
        if not isinstance(watch, dict):
            raise _Fault(f"watches.{i}", "must be an object")
        wid = watch.get("id")
        if not isinstance(wid, str) or not WATCH_ID.match(wid):
            raise _Fault(f"watches.{i}.id", "may hold only lower case letters, digits and -")
        if wid in ids:
            raise _Fault(f"watches.{i}.id", "repeats another watch id")
        ids.append(wid)
        _check_watch(watch, f"watches.{wid}")
    for watch in watches:
        gap = watch.get("gap")
        if isinstance(gap, dict) and (gap["against"] not in ids or gap["against"] == watch["id"]):
            raise _Fault(f"watches.{watch['id']}.gap.against", "must name another watch")


def check_rules(rules) -> None:
    """Refuse a whole rules file the engine would ignore. Raises ``bad_argument``."""
    try:
        _check_rules_file(rules)
    except _Fault as fault:
        raise _bad(f"rules: the merged rules break the engine's field rules at "
                   f"{fault.field}: {fault.reason}",
                   "Nothing was written. If the fault is in a field this tool cannot change, "
                   "tell the operator the rules file needs fixing by hand.",
                   path="rules", field=fault.field) from None


def _engine_check(cfg, runner):
    """The engine's own validator on the temp file, when ``LACUNA_PRICE_ALERTS_VALIDATOR``
    names it. Returns a check for ``_atomic_write``, or None when it is not named."""
    if cfg.validator_path is None:
        return None
    if not cfg.validator_path.is_file():
        raise _unavailable(f"the rules validator {cfg.validator_path} does not exist")

    def check(tmp_path):
        argv = [cfg.python, str(cfg.validator_path), "--check", str(tmp_path)]
        failed = ("Nothing was written. Tell the operator the engine's rules check could not "
                  "run on the server.")
        try:
            code, stdout, _ = runner(argv, dict(os.environ), VALIDATOR_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise ToolError("validator_failed", f"the engine's rules check ran past its "
                            f"{VALIDATOR_TIMEOUT_SECONDS} second limit", failed)
        except OSError as exc:
            raise ToolError("validator_failed",
                            f"the engine's rules check could not be started ({exc})", failed)
        if code == 0:
            return
        line = next((ln.strip() for ln in (stdout or "").splitlines() if ln.strip()), "")
        if code == 2:
            raise _bad(f"the engine's own check refused the merged rules: {line}",
                       "Nothing was written. Read the message, fix the change, and call "
                       "again. If the fault is in a field this tool cannot change, tell the "
                       "operator the rules file needs fixing by hand.", path="rules")
        raise ToolError("validator_failed",
                        f"the engine's rules check exited {code} without a verdict", failed,
                        exit_code=code)

    return check


# --------------------------------------------------------------------------
# ibkr_update_price_alert
# --------------------------------------------------------------------------

def update_alert(cfg, args, can_write, runner=None) -> dict:
    if not can_write:
        raise ToolError(
            "write_not_allowed",
            "this connection may read price alerts but may not change them",
            "Tell the user that changing an alert needs a write token, and that they should "
            "ask the operator of this market-data server for one. Do not retry with this "
            "token.")
    watch_id = _need_watch_id(args)
    changes = args.get("changes")

    import fcntl   # here, so importing this module never needs it on a platform without it

    path = cfg.rules_path
    if not path.is_file():
        raise _unavailable(f"the rules file {path} does not exist")
    engine_check = _engine_check(cfg, runner or run_subprocess)
    try:
        lock_fd = os.open(cfg.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise _unavailable(f"the rules lock {cfg.lock_path} could not be opened ({exc})")
    try:
        # Exclusive for the whole read, merge and write, so two updates in flight at once
        # both land: the second reads what the first wrote.
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        rules = read_rules(cfg)
        watch = _find_watch(rules, watch_id)
        before = copy.deepcopy(watch)
        after, changed = plan_changes(watch, changes)
        if changed:
            index = rules["watches"].index(watch)
            rules["watches"][index] = after
            check_rules(rules)
            _atomic_write(path, json.dumps(rules, indent=2, ensure_ascii=False) + "\n",
                          check=engine_check)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    payload = {"watch": watch_id, "before": before, "after": after, "changed": changed}
    if changed:
        payload["note"] = "written; the engine reads the new rules within one minute"
    else:
        payload["note"] = "every value already matched, so the rules file was left untouched"
    return payload


def _atomic_write(path: Path, text: str, check=None) -> None:
    """Temp file beside the real file, fsync, optional check, rename over, fsync the directory.

    A reader sees the old file or the new one, never half of either. The file keeps its mode.
    A symlinked rules file stays a link: the write goes to the file it points at. ``check``
    gets the temp file's path and raises ToolError to stop the rename.
    """
    path = Path(os.path.realpath(path))
    mode = os.stat(path).st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")

    def discard():
        try:
            os.unlink(tmp)
        except OSError:
            pass

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if check is not None:
            check(tmp)
        os.replace(tmp, path)
    except ToolError:
        discard()
        raise
    except OSError as exc:
        discard()
        raise ToolError("write_failed", f"the rules file {path} could not be written ({exc})",
                        "Nothing was changed. Tell the operator the rules file could not be "
                        "written on the server.")
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass   # the rename already happened; a directory fsync is extra durability only


# --------------------------------------------------------------------------
# ibkr_test_price_alert
# --------------------------------------------------------------------------

def run_subprocess(argv, env, timeout):
    """The real runner: no shell, captured output. Returns (exit code, stdout, stderr)."""
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout,
                          shell=False, check=False)
    return done.returncode, done.stdout, done.stderr


def backtest_argv(cfg, day, watch_id=None, overrides=None) -> list[str]:
    argv = [cfg.python, str(cfg.backtest_path), "--json", "--date", day]
    if watch_id:
        argv += ["--watch", watch_id]
    if overrides:
        argv += ["--overrides", json.dumps(overrides, separators=(",", ":"))]
    return argv


def backtest_env(cfg, base=None) -> dict:
    env = dict(os.environ if base is None else base)
    env.update({"PRICE_ALERTS_RULES": str(cfg.rules_path),
                "PRICE_ALERTS_STATE_DIR": str(cfg.state_dir),
                "LACUNA_IBKR_ROOT": cfg.ibkr_root,
                "LACUNA_IBKR_DIR": cfg.ibkr_dir})
    return env


def backtest_alert(cfg, args, runner) -> dict:
    day = args.get("date")
    if not isinstance(day, str) or not ISO_DATE.match(day):
        raise _bad(f"date: must be YYYY-MM-DD, got {day!r}",
                   "Call again with date such as 2026-03-04.", path="date")
    try:
        date.fromisoformat(day)
    except ValueError:
        raise _bad(f"date: {day!r} is not a real date", "Call again with a real date.",
                   path="date")
    watch_id = _need_watch_id(args, required=False)
    rules = read_rules(cfg)
    if watch_id:
        _find_watch(rules, watch_id)
    overrides = args.get("overrides")
    if overrides is not None:
        overrides = _check_overrides(rules, overrides)
    if not cfg.backtest_path.is_file():
        raise _unavailable(f"the backtest script {cfg.backtest_path} does not exist")

    argv = backtest_argv(cfg, day, watch_id, overrides)
    try:
        code, stdout, stderr = runner(argv, backtest_env(cfg), BACKTEST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise ToolError("backtest_timeout",
                        f"the backtest ran past its {BACKTEST_TIMEOUT_SECONDS} second limit",
                        "Narrow it to one watch with the watch argument and call again.",
                        timeout_seconds=BACKTEST_TIMEOUT_SECONDS)
    except OSError as exc:
        raise _unavailable(f"the backtest could not be started ({exc})")

    try:
        parsed = json.loads(stdout or "")
    except ValueError:
        parsed = None
    if code == 0 and isinstance(parsed, dict):
        return parsed
    if code == 2 and isinstance(parsed, dict) and parsed.get("error"):
        raise ToolError("backtest_failed", f"the backtest refused: {parsed['error']}",
                        "Read the message, fix the date, watch or overrides, and call again.")
    tail = (stderr or "").strip()[-400:]
    raise ToolError("backtest_failed",
                    f"the backtest exited {code} without a readable result",
                    "Tell the operator the backtest script failed on the server.",
                    exit_code=code, stderr_tail=tail)
