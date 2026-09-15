"""Symbol to qualified IBKR contract, cached on disk.

The conId is the only stable identifier IBKR has. A ticker is not: NSRGY is a
SMART/USD ADR whose primary exchange is PINK, and some names share
a ticker with a foreign listing. So every fetch goes through a resolved contract,
and the resolution is cached in ``shared/data/ibkr/contracts.json``.

See CONTRACT.md for the CONTRACT dict shape.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import store

CACHE = "contracts.json"

# Where a US listing normally lives, best first. PINK and VALUE are where the
# ADRs sit, so they rank last without being excluded.
EXCHANGE_PREFERENCE = ("NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "PINK", "VALUE")

FIELDS = ("symbol", "con_id", "sec_type", "exchange", "primary_exchange",
          "currency", "long_name", "resolved_ts")


def cache_path(root_dir=None) -> Path:
    return store.root(root_dir) / CACHE


def load(root_dir=None) -> dict:
    path = cache_path(root_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save(cache: dict, root_dir=None) -> Path:
    path = cache_path(root_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path


def pick(details: list[dict], currency: str = "USD",
         sec_type: str = "STK") -> tuple[dict | None, list[dict]]:
    """Choose one contract from a ``reqContractDetails`` answer.

    Pure. Returns ``(chosen, candidates)``. Candidates are every row that
    survived the currency and security type filter, ordered the way the choice
    was made, so a caller can show a human what else matched.
    """
    pool = [d for d in details or []
            if str(d.get("currency", "")).upper() == currency.upper()
            and str(d.get("sec_type", "")).upper() == sec_type.upper()]
    if not pool:
        return None, list(details or [])

    def rank(row):
        primary = str(row.get("primary_exchange", "")).upper()
        try:
            position = EXCHANGE_PREFERENCE.index(primary)
        except ValueError:
            position = len(EXCHANGE_PREFERENCE)
        return (position, int(row.get("con_id", 0)))

    ordered = sorted(pool, key=rank)
    return ordered[0], ordered


def _to_contract(row: dict, now: int) -> dict:
    return {
        "symbol": str(row["symbol"]).upper(),
        "con_id": int(row["con_id"]),
        "sec_type": row.get("sec_type", "STK"),
        "exchange": row.get("exchange") or "SMART",
        "primary_exchange": row.get("primary_exchange") or "",
        "currency": row.get("currency", "USD"),
        "long_name": row.get("long_name", ""),
        "resolved_ts": int(now),
    }


def resolve(symbol: str, client=None, root_dir=None, refresh: bool = False,
            now: int | None = None) -> dict:
    """A CONTRACT for this symbol, from the cache or from the gateway.

    Raises ``LookupError`` when nothing matches, with the near misses in the
    message so a human can pick. Raises ``ibclient.GatewayDown`` when the cache
    misses and no client was given.
    """
    sym = str(symbol).upper()
    now = int(now if now is not None else time.time())
    cache = load(root_dir)
    if not refresh and sym in cache and cache[sym].get("con_id"):
        return cache[sym]

    if client is None:
        from ibclient import GatewayDown
        raise GatewayDown(
            f"{sym} is not in {cache_path(root_dir)} and no gateway client was "
            "given, so it cannot be resolved offline. Run "
            "'lacuna-ibkr resolve %s' with the gateway up." % sym
        )

    details = client.contract_details(sym)
    chosen, candidates = pick(details)
    if chosen is None:
        near = ", ".join(
            f"{c.get('symbol')} {c.get('sec_type')} {c.get('currency')}"
            f" on {c.get('primary_exchange') or c.get('exchange')}"
            for c in candidates[:6]
        ) or "nothing at all"
        raise LookupError(f"no USD stock matched {sym!r}. IBKR offered: {near}")

    contract = _to_contract(chosen, now)
    cache[sym] = contract
    save(cache, root_dir)
    return contract


def head_timestamp(symbol: str, client, root_dir=None, what: str = "TRADES") -> str | None:
    """Earliest data IBKR holds for a symbol. One gateway call, not paced."""
    contract = resolve(symbol, client=client, root_dir=root_dir)
    return client.head_timestamp(contract, what=what)
