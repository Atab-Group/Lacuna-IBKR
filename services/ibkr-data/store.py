"""Parquet bar store and the DuckDB query surface over it.

Filesystem only. Nothing in here talks to IBKR.

Layout, matching CONTRACT.md:

    shared/data/ibkr/bars/1d/MSFT/2026.parquet          one file per symbol-year
    shared/data/ibkr/bars/1m/MSFT/2026-06-25.parquet    one file per symbol-day

Writes are idempotent partition replacement. A write reads the target partition,
drops every row a new row repeats on ``ts``, concatenates, sorts and rewrites the
whole file. Writing the same rows twice changes nothing. Writing ten days into a
year file keeps the other 240.
"""

from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

import bars as barlib

LEGACY_COLUMNS = ["ts", "symbol", "interval", "open", "high", "low", "close",
                  "volume", "wap", "bar_count", "session", "source", "pulled_ts"]
COLUMNS = ["ts", "symbol", "interval", "open", "high", "low", "close",
           "volume", "wap", "bar_count", "session", "source", "pulled_ts",
           "con_id", "sec_type", "data_type", "exchange", "currency",
           "local_symbol", "expiry", "strike", "right", "multiplier",
           "trading_class", "underlying_con_id", "time_zone", "session_date",
           "date_anchor"]

SCHEMA = pa.schema([
    ("ts", pa.int64()),
    ("symbol", pa.string()),
    ("interval", pa.string()),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.float64()),
    ("wap", pa.float64()),
    ("bar_count", pa.int64()),
    ("session", pa.string()),
    ("source", pa.string()),
    ("pulled_ts", pa.int64()),
    ("con_id", pa.int64()),
    ("sec_type", pa.string()),
    ("data_type", pa.string()),
    ("exchange", pa.string()),
    ("currency", pa.string()),
    ("local_symbol", pa.string()),
    ("expiry", pa.string()),
    ("strike", pa.float64()),
    ("right", pa.string()),
    ("multiplier", pa.string()),
    ("trading_class", pa.string()),
    ("underlying_con_id", pa.int64()),
    ("time_zone", pa.string()),
    ("session_date", pa.string()),
    ("date_anchor", pa.string()),
])
LEGACY_SCHEMA = pa.schema(list(SCHEMA)[:len(LEGACY_COLUMNS)])

# services/ibkr-data/store.py -> repo root -> shared/data/ibkr
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent / "shared" / "data" / "ibkr"


def root(override: str | os.PathLike | None = None) -> Path:
    """Store root. ``LACUNA_IBKR_ROOT`` wins over the repo default."""
    if override is not None:
        return Path(override)
    env = os.environ.get("LACUNA_IBKR_ROOT")
    return Path(env) if env else _DEFAULT_ROOT


def partition_path(symbol: str, interval: str, key: str,
                   root_dir=None) -> Path:
    canon = barlib.normalize_interval(interval)
    return root(root_dir) / "bars" / canon / str(symbol).upper() / f"{key}.parquet"


def dataset_partition_path(con_id: int, interval: str, key: str,
                           data_type: str = "TRADES", session: str = "rth",
                           root_dir=None) -> Path:
    return (root(root_dir) / "bars-v2" / str(data_type).upper() /
            str(session).lower() / barlib.normalize_interval(interval) /
            str(int(con_id)) / f"{key}.parquet")


def glob_pattern(symbol: str | None = None, interval: str | None = None,
                 root_dir=None) -> str:
    """The glob DuckDB and pandas both read the store through."""
    canon = barlib.normalize_interval(interval) if interval else "*"
    sym = str(symbol).upper() if symbol else "*"
    return str(root(root_dir) / "bars" / canon / sym / "*.parquet")


def _table_to_rows(table: pa.Table) -> list[dict]:
    return table.to_pylist()


def _rows_to_table(rows: list[dict], legacy=False) -> pa.Table:
    names = LEGACY_COLUMNS if legacy else COLUMNS
    cols = {name: [] for name in names}
    for row in rows:
        for name in names:
            cols[name].append(row.get(name))
    return pa.Table.from_pydict(cols, schema=LEGACY_SCHEMA if legacy else SCHEMA)


def read_partition(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    rows = _table_to_rows(pq.read_table(path))
    return [{name: row.get(name) for name in COLUMNS} for row in rows]


def write_bars(rows: list[dict], root_dir=None) -> dict[str, int]:
    """Write ROW dicts into their partitions. Returns ``{path: rows_in_file}``."""
    if not rows:
        return {}
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        canon = barlib.normalize_interval(row["interval"])
        key = barlib.partition_key(int(row["ts"]), canon)
        # Every newly qualified dataset is conId-keyed. The ticker layout is
        # read compatibility for pre-v1.1 equity files only.
        v2 = bool(row.get("con_id"))
        if v2:
            group_key = ("v2", int(row["con_id"]), canon, key,
                         str(row.get("data_type") or "TRADES").upper(),
                         str(row.get("session") or "rth").lower())
        else:
            group_key = ("legacy", str(row["symbol"]).upper(), canon, key)
        groups.setdefault(group_key, []).append(row)

    written: dict[str, int] = {}
    for group_key, group in groups.items():
        if group_key[0] == "v2":
            _, con_id, interval, key, data_type, session = group_key
            path = dataset_partition_path(con_id, interval, key, data_type, session, root_dir)
        else:
            _, symbol, interval, key = group_key
            path = partition_path(symbol, interval, key, root_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = {int(r["ts"]): r for r in read_partition(path)}
        for row in group:
            merged[int(row["ts"])] = {name: row.get(name) for name in COLUMNS}
        ordered = [merged[k] for k in sorted(merged)]
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(_rows_to_table(ordered, legacy=group_key[0] == "legacy"),
                       tmp, compression="zstd")
        os.replace(tmp, path)
        written[str(path)] = len(ordered)
    return written


def partitions(symbol: str, interval: str, root_dir=None, con_id: int | None = None,
               data_type: str = "TRADES", session: str | None = None,
               include_legacy: bool | None = None) -> list[Path]:
    canon = barlib.normalize_interval(interval)
    paths = []
    if con_id:
        sessions = [session] if session else ["rth", "eth"]
        for sess in sessions:
            directory = (root(root_dir) / "bars-v2" / str(data_type).upper() /
                         sess / canon / str(int(con_id)))
            if directory.exists():
                paths.extend(directory.glob("*.parquet"))
    elif symbol:
        sessions = [session] if session else ["rth", "eth"]
        for sess in sessions:
            directory = root(root_dir) / "bars-v2" / str(data_type).upper() / sess / canon
            if directory.exists():
                paths.extend(directory.glob("*/*.parquet"))
    # Legacy fallback is opt-in when a conId was supplied, preventing an option
    # from reading the underlying's old ticker-keyed files.
    if include_legacy is None:
        include_legacy = con_id is None
    if include_legacy and str(data_type).upper() == "TRADES" and symbol:
        directory = root(root_dir) / "bars" / canon / str(symbol).upper()
        if directory.exists():
            paths.extend(directory.glob("*.parquet"))
    return sorted(set(paths))


def read_bars(symbol: str, interval: str, start=None, end=None,
              session: str | None = None, root_dir=None, con_id: int | None = None,
              data_type: str = "TRADES", include_legacy: bool | None = None) -> list[dict]:
    """Every held bar for a symbol and interval inside ``[start, end)``."""
    canon = barlib.normalize_interval(interval)
    start_ts = barlib.to_epoch(start) if start is not None else None
    end_ts = barlib.to_epoch(end) if end is not None else None

    out: list[dict] = []
    for path in partitions(symbol, canon, root_dir, con_id, data_type, session,
                           include_legacy):
        # a year or day partition outside the range still has to be opened for
        # daily files, because one file spans a whole year; the cost is small
        for row in read_partition(path):
            ts = int(row["ts"])
            if start_ts is not None and ts < start_ts:
                continue
            if end_ts is not None and ts >= end_ts:
                continue
            if session and row.get("session") != session:
                continue
            if symbol and row.get("symbol") and str(row["symbol"]).upper() != str(symbol).upper():
                continue
            if con_id and row.get("con_id") and int(row["con_id"]) != int(con_id):
                continue
            if not con_id and row.get("sec_type") and str(row["sec_type"]).upper() != "STK":
                continue
            if row.get("data_type") and str(row["data_type"]).upper() != str(data_type).upper():
                continue
            out.append(row)
    if not con_id:
        identities = {int(r["con_id"]) for r in out if r.get("con_id")}
        if len(identities) > 1:
            raise ValueError(
                f"symbol {symbol!r} has multiple qualified contracts {sorted(identities)}; pass con_id")
    # Prefer v2 rows over legacy rows at the same timestamp.
    merged = {int(r["ts"]): r for r in out if not r.get("con_id")}
    merged.update({int(r["ts"]): r for r in out if r.get("con_id")})
    return [merged[k] for k in sorted(merged)]


def read_frame(symbol: str, interval: str, start=None, end=None,
               session: str | None = None, root_dir=None):
    """The same rows as a pandas DataFrame, with a UTC ``dt`` column added."""
    import pandas as pd

    rows = read_bars(symbol, interval, start, end, session, root_dir)
    frame = pd.DataFrame(rows, columns=COLUMNS)
    if not frame.empty:
        frame.insert(1, "dt", pd.to_datetime(frame["ts"], unit="s", utc=True))
    return frame


def store_stats(root_dir=None) -> dict:
    base = root(root_dir) / "bars"
    files = sorted(base.glob("*/*/*.parquet")) if base.exists() else []
    files += sorted((root(root_dir) / "bars-v2").glob("*/*/*/*/*.parquet"))
    symbols = sorted({path.parent.name for path in files if "bars-v2" not in path.parts})
    intervals = sorted({path.parent.parent.name for path in files if "bars-v2" not in path.parts} |
                       {path.parent.parent.name for path in files if "bars-v2" in path.parts})
    return {
        "root": str(root(root_dir)),
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "symbols": symbols,
        "intervals": intervals,
    }


# --------------------------------------------------------------------------
# DuckDB
# --------------------------------------------------------------------------

_EMPTY_VIEW = """
CREATE VIEW bars AS SELECT
  CAST(NULL AS BIGINT)  AS ts,
  CAST(NULL AS VARCHAR) AS symbol,
  CAST(NULL AS VARCHAR) AS interval,
  CAST(NULL AS DOUBLE)  AS open,
  CAST(NULL AS DOUBLE)  AS high,
  CAST(NULL AS DOUBLE)  AS low,
  CAST(NULL AS DOUBLE)  AS close,
  CAST(NULL AS DOUBLE)  AS volume,
  CAST(NULL AS DOUBLE)  AS wap,
  CAST(NULL AS BIGINT)  AS bar_count,
  CAST(NULL AS VARCHAR) AS session,
  CAST(NULL AS VARCHAR) AS source,
  CAST(NULL AS BIGINT)  AS pulled_ts
WHERE FALSE
"""


def connect(root_dir=None):
    """An in-memory DuckDB connection with a ``bars`` view over the parquet glob.

    The connection never writes to the store: it only registers the files. A
    cold store gets an empty typed view, so a query against it returns zero rows
    instead of raising.
    """
    import duckdb

    conn = duckdb.connect(database=":memory:")
    base = root(root_dir) / "bars"
    files = sorted(base.glob("*/*/*.parquet")) if base.exists() else []
    files += sorted((root(root_dir) / "bars-v2").glob("*/*/*/*/*.parquet"))
    if not files:
        conn.execute(_EMPTY_VIEW)
        return conn
    patterns = []
    if sorted(base.glob("*/*/*.parquet")):
        patterns.append(str(base / "*" / "*" / "*.parquet"))
    if sorted((root(root_dir) / "bars-v2").glob("*/*/*/*/*.parquet")):
        patterns.append(str(root(root_dir) / "bars-v2" / "*" / "*" / "*" / "*" / "*.parquet"))
    pattern = [p.replace("'", "''") for p in patterns]
    conn.execute(
        f"CREATE VIEW bars AS SELECT * FROM read_parquet({pattern!r}, union_by_name=true)"
    )
    conn.execute(
        "CREATE VIEW bars_dt AS SELECT *, to_timestamp(ts) AS dt FROM bars"
    )
    return conn


SNAPSHOT_COLUMNS = [
    "ts", "collected_ts", "con_id", "symbol", "local_symbol", "sec_type",
    "exchange", "currency", "expiry", "strike", "right", "multiplier",
    "trading_class", "underlying_con_id", "time_zone", "market_data_type", "status",
    "bid", "ask", "last", "close", "bid_size", "ask_size", "last_size",
    "volume", "call_open_interest", "put_open_interest", "model_greeks",
    "bid_greeks", "ask_greeks", "last_greeks", "errors",
]
_SNAPSHOT_JSON = {"model_greeks", "bid_greeks", "ask_greeks", "last_greeks",
                  "errors", "warnings", "field_status"}
SNAPSHOT_SCHEMA = pa.schema([
    (name, pa.string() if name in _SNAPSHOT_JSON or name in
     {"symbol", "local_symbol", "sec_type", "exchange", "currency", "expiry", "right",
      "multiplier", "trading_class", "time_zone", "status"}
     else pa.int64() if name in {"ts", "collected_ts", "con_id", "underlying_con_id", "market_data_type"}
     else pa.float64()) for name in SNAPSHOT_COLUMNS + ["warnings", "field_status"]
])


def write_snapshots(rows: list[dict], root_dir=None) -> dict[str, int]:
    """Append/idempotently replace snapshots by receipt timestamp and conId."""
    groups = {}
    for row in rows:
        day = __import__("datetime").datetime.fromtimestamp(
            int(row["collected_ts"]), __import__("datetime").timezone.utc).date().isoformat()
        groups.setdefault((int(row["con_id"]), day), []).append(row)
    written = {}
    for (con_id, day), group in groups.items():
        path = root(root_dir) / "snapshots" / str(con_id) / f"{day}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        old = pq.read_table(path).to_pylist() if path.exists() else []
        merged = {(int(r["collected_ts"]), int(r["con_id"])): r for r in old}
        for row in group:
            disk = {name: row.get(name) for name in SNAPSHOT_SCHEMA.names}
            for name in _SNAPSHOT_JSON:
                disk[name] = __import__("json").dumps(disk.get(name), sort_keys=True)
            merged[(int(row["collected_ts"]), int(row["con_id"]))] = disk
        ordered = [merged[k] for k in sorted(merged)]
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(pa.Table.from_pylist(ordered, schema=SNAPSHOT_SCHEMA), tmp, compression="zstd")
        os.replace(tmp, path)
        written[str(path)] = len(ordered)
    return written


def read_snapshots(con_id: int | None = None, start=None, end=None, root_dir=None) -> list[dict]:
    base = root(root_dir) / "snapshots"
    paths = sorted((base / str(int(con_id))).glob("*.parquet")) if con_id else sorted(base.glob("*/*.parquet"))
    start_ts = barlib.to_epoch(start) if start is not None else None
    end_ts = barlib.to_epoch(end) if end is not None else None
    rows = []
    for path in paths:
        for row in pq.read_table(path).to_pylist():
            ts = int(row["collected_ts"])
            if start_ts is not None and ts < start_ts: continue
            if end_ts is not None and ts >= end_ts: continue
            for name in _SNAPSHOT_JSON:
                if row.get(name) is not None:
                    row[name] = __import__("json").loads(row[name])
            rows.append(row)
    return sorted(rows, key=lambda r: (r["collected_ts"], r["con_id"]))


_FORBIDDEN = ("insert", "update", "delete", "drop", "create", "alter", "copy",
              "attach", "install", "load", "pragma", "export", "call")


def query(sql: str, root_dir=None, limit: int = 10000):
    """Run one SELECT against the store. Returns ``(columns, rows)``.

    Refuses anything that is not a single SELECT or WITH statement, and caps the
    row count so a stray cross join cannot fill memory.
    """
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise ValueError("empty query")
    if ";" in text:
        raise ValueError("one statement per query")
    head = text.split(None, 1)[0].lower()
    if head not in ("select", "with"):
        raise ValueError(f"only SELECT is allowed, got {head!r}")
    lowered = text.lower()
    for word in _FORBIDDEN:
        if f" {word} " in f" {lowered} ":
            raise ValueError(f"{word!r} is not allowed in a store query")

    conn = connect(root_dir)
    try:
        result = conn.execute(f"SELECT * FROM ({text}) LIMIT {int(limit)}")
        columns = [d[0] for d in result.description]
        return columns, result.fetchall()
    finally:
        conn.close()
