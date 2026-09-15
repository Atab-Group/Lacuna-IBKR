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

COLUMNS = ["ts", "symbol", "interval", "open", "high", "low", "close",
           "volume", "wap", "bar_count", "session", "source", "pulled_ts"]

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
])

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


def glob_pattern(symbol: str | None = None, interval: str | None = None,
                 root_dir=None) -> str:
    """The glob DuckDB and pandas both read the store through."""
    canon = barlib.normalize_interval(interval) if interval else "*"
    sym = str(symbol).upper() if symbol else "*"
    return str(root(root_dir) / "bars" / canon / sym / "*.parquet")


def _table_to_rows(table: pa.Table) -> list[dict]:
    return table.to_pylist()


def _rows_to_table(rows: list[dict]) -> pa.Table:
    cols = {name: [] for name in COLUMNS}
    for row in rows:
        for name in COLUMNS:
            cols[name].append(row.get(name))
    return pa.Table.from_pydict(cols, schema=SCHEMA)


def read_partition(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    return _table_to_rows(pq.read_table(path, schema=SCHEMA))


def write_bars(rows: list[dict], root_dir=None) -> dict[str, int]:
    """Write ROW dicts into their partitions. Returns ``{path: rows_in_file}``."""
    if not rows:
        return {}
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        canon = barlib.normalize_interval(row["interval"])
        key = barlib.partition_key(int(row["ts"]), canon)
        groups.setdefault((str(row["symbol"]).upper(), canon, key), []).append(row)

    written: dict[str, int] = {}
    for (symbol, interval, key), group in groups.items():
        path = partition_path(symbol, interval, key, root_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = {int(r["ts"]): r for r in read_partition(path)}
        for row in group:
            merged[int(row["ts"])] = {name: row.get(name) for name in COLUMNS}
        ordered = [merged[k] for k in sorted(merged)]
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(_rows_to_table(ordered), tmp, compression="zstd")
        os.replace(tmp, path)
        written[str(path)] = len(ordered)
    return written


def partitions(symbol: str, interval: str, root_dir=None) -> list[Path]:
    canon = barlib.normalize_interval(interval)
    directory = root(root_dir) / "bars" / canon / str(symbol).upper()
    if not directory.exists():
        return []
    return sorted(directory.glob("*.parquet"))


def read_bars(symbol: str, interval: str, start=None, end=None,
              session: str | None = None, root_dir=None) -> list[dict]:
    """Every held bar for a symbol and interval inside ``[start, end)``."""
    canon = barlib.normalize_interval(interval)
    start_ts = barlib.to_epoch(start) if start is not None else None
    end_ts = barlib.to_epoch(end) if end is not None else None

    out: list[dict] = []
    for path in partitions(symbol, canon, root_dir):
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
            out.append(row)
    out.sort(key=lambda r: r["ts"])
    return out


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
    symbols = sorted({path.parent.name for path in files})
    intervals = sorted({path.parent.parent.name for path in files})
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
    if not files:
        conn.execute(_EMPTY_VIEW)
        return conn
    pattern = str(base / "*" / "*" / "*.parquet").replace("'", "''")
    conn.execute(
        f"CREATE VIEW bars AS SELECT * FROM read_parquet('{pattern}', union_by_name=true)"
    )
    conn.execute(
        "CREATE VIEW bars_dt AS SELECT *, to_timestamp(ts) AS dt FROM bars"
    )
    return conn


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
