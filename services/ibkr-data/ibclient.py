"""The only module that talks to IB Gateway.

Thin by design: connect, contract details, historical bars, head timestamp. No
caching, no pacing, no storage. Those live in contracts.py, jobs.py and store.py
so that everything except this file is testable without a gateway.

Never name a module in here ``ibapi.py``. That shadows IBKR's own package import
and the failure is silent.
"""

from __future__ import annotations

import math
import time

HOST = "127.0.0.1"
PORT = 4001            # live gateway; the image maps host 4001 to container 4003
CLIENT_IDS = tuple(range(201, 211))   # this service's reserved band
DELAYED = 3            # reqMarketDataType: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen

LOGIN_URL = "http://127.0.0.1:8642"


class GatewayDown(RuntimeError):
    """The gateway is not answering. The message says who has to act."""


def _down_message(host: str, port: int, detail: str) -> str:
    return (
        f"IB Gateway is not answering on {host}:{port} ({detail}). "
        f"A human must log in at {LOGIN_URL}, or run "
        "./services/ibkr-data/gateway from a shell. Two factor authentication "
        "means no tool can do this for you. Do not retry a login "
        "programmatically: IBKR throttles after a few attempts and then locks "
        "the account."
    )


class Client:
    """One socket session to the gateway.

    Use it as a context manager so the socket always closes::

        with Client() as client:
            details = client.contract_details("MSFT")
    """

    def __init__(self, host: str = HOST, port: int = PORT,
                 client_id: int | None = None, timeout: int = 20,
                 delayed: bool = True):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.timeout = timeout
        self.delayed = delayed
        self.ib = None

    # -- lifecycle ---------------------------------------------------------
    def connect(self):
        try:
            from ib_async import IB, util
        except ImportError as exc:  # pragma: no cover - dependency is installed
            raise GatewayDown(
                "ib_async is not installed in this interpreter. Use "
                ".venv/bin/python."
            ) from exc

        util.logToConsole("ERROR")  # ib_async is chatty at INFO
        ids = [self.client_id] if self.client_id is not None else list(CLIENT_IDS)
        last = None
        for candidate in ids:
            ib = IB()
            try:
                # Market-data service: perform the API handshake without
                # IB.connect's account/order synchronization. That startup
                # sync requests write access on a read-only gateway.
                ib.client.connect(self.host, self.port, clientId=candidate,
                                  timeout=self.timeout)
            except Exception as exc:
                last = exc
                try:
                    ib.disconnect()
                except Exception:
                    pass
                if "already in use" not in str(exc).lower():
                    raise GatewayDown(_down_message(self.host, self.port, str(exc))) from exc
                continue
            self.ib = ib
            self.client_id = candidate
            if self.delayed:
                ib.reqMarketDataType(DELAYED)
            return self
        raise GatewayDown(_down_message(self.host, self.port,
                                        f"no free clientId in {CLIENT_IDS}: {last}"))

    def disconnect(self):
        if self.ib is not None:
            try:
                self.ib.disconnect()
            finally:
                self.ib = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.disconnect()
        return False

    def _require(self):
        if self.ib is None:
            raise GatewayDown("client is not connected; call connect() first")
        return self.ib

    # -- reads -------------------------------------------------------------
    def accounts(self) -> list[str]:
        return list(self._require().managedAccounts())

    def server_version(self) -> int:
        return int(self._require().client.serverVersion())

    def contract_details(self, symbol: str, sec_type: str = "STK",
                         exchange: str = "SMART", currency: str = "USD",
                         primary_exchange: str | None = None) -> list[dict]:
        """``reqContractDetails`` flattened to plain dicts.

        No ib_async object escapes this method, so contracts.py stays free of
        the dependency.
        """
        from ib_async import Contract

        ib = self._require()
        contract = Contract(secType=sec_type, symbol=str(symbol).upper(),
                            exchange=exchange, currency=currency)
        if primary_exchange:
            contract.primaryExchange = primary_exchange
        details = ib.reqContractDetails(contract)
        out = []
        for d in details or []:
            c = d.contract
            out.append({
                "symbol": c.symbol,
                "con_id": int(c.conId),
                "sec_type": c.secType,
                "exchange": c.exchange or exchange,
                "primary_exchange": c.primaryExchange or "",
                "currency": c.currency,
                "long_name": getattr(d, "longName", "") or "",
                "trading_hours": getattr(d, "tradingHours", "") or "",
                "time_zone": getattr(d, "timeZoneId", "") or "",
            })
        return out

    @staticmethod
    def _plain_contract(c, detail=None, fallback_exchange="SMART") -> dict:
        return {
            "symbol": c.symbol,
            "con_id": int(c.conId),
            "sec_type": c.secType,
            "exchange": c.exchange or fallback_exchange,
            "primary_exchange": c.primaryExchange or "",
            "currency": c.currency,
            "long_name": getattr(detail, "longName", "") or "",
            "local_symbol": c.localSymbol or "",
            "expiry": c.lastTradeDateOrContractMonth or "",
            "strike": float(c.strike or 0),
            "right": c.right or "",
            "multiplier": c.multiplier or "",
            "trading_class": c.tradingClass or "",
            "underlying_con_id": int(getattr(detail, "underConId", 0) or 0),
            "time_zone": getattr(detail, "timeZoneId", "") or "",
        }

    def _contract_from_selector(self, spec: dict):
        from ib_async import Contract
        return Contract(
            conId=int(spec.get("con_id") or 0), symbol=spec.get("symbol", ""),
            secType=spec.get("sec_type", ""),
            exchange=spec.get("exchange", "SMART" if not spec.get("con_id") else ""),
            primaryExchange=spec.get("primary_exchange", ""),
            currency=spec.get("currency", "" if spec.get("con_id") else "USD"),
            lastTradeDateOrContractMonth=spec.get("expiry", ""),
            strike=float(spec.get("strike") or 0), right=spec.get("right", ""),
            multiplier=str(spec.get("multiplier") or ""),
            tradingClass=spec.get("trading_class", ""),
            localSymbol=spec.get("local_symbol", ""),
        )

    def resolve_contract(self, spec: dict) -> list[dict]:
        """Qualify a general selector; returns plain, complete contract rows."""
        ib = self._require()
        query = self._contract_from_selector(spec)
        details = ib.reqContractDetails(query)
        return [self._plain_contract(d.contract, d, spec.get("exchange", "SMART"))
                for d in details or []]

    def option_parameters(self, underlying: dict) -> list[dict]:
        """Option-chain parameter sets for an already resolved underlying."""
        ib = self._require()
        rows = ib.reqSecDefOptParams(
            underlying.get("symbol", ""), "",
            underlying.get("sec_type", "STK"), int(underlying["con_id"]))
        return [{"exchange": r.exchange, "underlying_con_id": int(r.underlyingConId),
                 "trading_class": r.tradingClass, "multiplier": r.multiplier,
                 "expirations": sorted(r.expirations),
                 "strikes": sorted(float(x) for x in r.strikes)} for r in rows or []]

    def qualify(self, contract_dict: dict):
        """A CONTRACT dict back into the ib_async object a fetch needs."""
        return self._contract_from_selector(contract_dict)

    @staticmethod
    def _number(value, *, allow_negative=True):
        """JSON-safe IBKR number, removing max-double and missing sentinels."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or abs(number) > 1e100:
            return None
        if number in (-1.0, -2.0) and not allow_negative:
            return None
        return number

    @classmethod
    def _greeks(cls, value):
        if value is None:
            return None
        def greek(name):
            number = cls._number(getattr(value, name, None))
            return None if number == -2.0 else number
        out = {
            "implied_volatility": cls._number(getattr(value, "impliedVol", None), allow_negative=False),
            "delta": greek("delta"), "gamma": greek("gamma"),
            "vega": greek("vega"), "theta": greek("theta"),
            "option_price": cls._number(getattr(value, "optPrice", None), allow_negative=False),
            "underlying_price": cls._number(getattr(value, "undPrice", None), allow_negative=False),
        }
        return out if any(v is not None for v in out.values()) else None

    def market_snapshot(self, contracts: list[dict], timeout: float = 8.0,
                        generic_ticks: str = "100,101,104,106") -> list[dict]:
        """Bounded delayed subscriptions. Partial values survive API warnings."""
        ib = self._require()
        timeout = min(max(float(timeout), 0.25), 20.0)
        tickers = []
        errors = []
        def on_error(req_id, code, message, contract=None, *extra):
            errors.append({"request_id": req_id, "code": code, "message": str(message)})
        ib.errorEvent += on_error
        started = time.time()
        try:
            for item in contracts:
                tickers.append((item, ib.reqMktData(self.qualify(item), generic_ticks,
                                                    snapshot=False, regulatorySnapshot=False)))
            while time.time() - started < timeout:
                ib.sleep(0.2)
                if all((t.marketDataType in (1, 2, 3, 4) and
                        any(self._number(getattr(t, f, None), allow_negative=False) is not None
                            for f in ("bid", "ask", "last", "close")))
                       for _, t in tickers):
                    # Greeks often follow the first price update.
                    if all(i.get("sec_type") != "OPT" or self._greeks(t.modelGreeks)
                           for i, t in tickers):
                        break
            receipt = int(time.time())
            out = []
            for item, t in tickers:
                row = dict(item)
                row.update({
                    "ts": receipt, "collected_ts": receipt,
                    "market_data_type": int(t.marketDataType or 0) or None,
                    "bid": self._number(t.bid, allow_negative=False),
                    "ask": self._number(t.ask, allow_negative=False),
                    "last": self._number(t.last, allow_negative=False),
                    "close": self._number(t.close, allow_negative=False),
                    "bid_size": self._number(t.bidSize, allow_negative=False),
                    "ask_size": self._number(t.askSize, allow_negative=False),
                    "last_size": self._number(t.lastSize, allow_negative=False),
                    "volume": self._number(t.volume, allow_negative=False),
                    "call_open_interest": self._number(getattr(t, "callOpenInterest", None), allow_negative=False),
                    "put_open_interest": self._number(getattr(t, "putOpenInterest", None), allow_negative=False),
                    "model_greeks": self._greeks(t.modelGreeks),
                    "bid_greeks": self._greeks(t.bidGreeks),
                    "ask_greeks": self._greeks(t.askGreeks),
                    "last_greeks": self._greeks(t.lastGreeks),
                    "errors": list(errors),
                })
                row["warnings"] = []
                if item.get("sec_type") == "STK" and row["volume"] is not None and row["volume"] > 1e11:
                    row["warnings"].append(
                        "stock volume was implausibly large and is withheld; IBKR feed units are unverified")
                    row["volume"] = None
                populated = [k for k in ("bid", "ask", "last", "close", "model_greeks",
                                          "call_open_interest", "put_open_interest") if row.get(k) is not None]
                tracked = ("bid", "ask", "last", "close", "bid_size", "ask_size",
                           "last_size", "volume", "call_open_interest", "put_open_interest",
                           "model_greeks", "bid_greeks", "ask_greeks", "last_greeks")
                row["field_status"] = {name: ("available" if row.get(name) is not None else "unavailable")
                                       for name in tracked}
                row["status"] = "complete" if populated and not errors else ("partial" if populated else "unavailable")
                out.append(row)
            return out
        finally:
            for _item, ticker in tickers:
                try:
                    ib.cancelMktData(ticker.contract)
                except Exception:
                    pass
            try:
                ib.errorEvent -= on_error
            except Exception:
                pass

    def head_timestamp(self, contract, what: str = "TRADES",
                       use_rth: bool = False) -> str | None:
        """Earliest moment IBKR holds data for this contract, or None."""
        ib = self._require()
        if isinstance(contract, dict):
            contract = self.qualify(contract)
        try:
            return ib.reqHeadTimeStamp(contract, whatToShow=what, useRTH=use_rth)
        except Exception:
            return None

    def historical_bars(self, contract, end, duration: str, bar_size: str,
                        what: str = "TRADES", use_rth: bool = True):
        """One ``reqHistoricalData`` call. Returns the raw BarData list.

        An empty list is a real and common answer. It means a market holiday, an
        entitlement problem, or a breached pacing limit, and this module cannot
        tell those apart. jobs.py records it as ``empty`` and moves on.
        """
        ib = self._require()
        if isinstance(contract, dict):
            contract = self.qualify(contract)
        current_window = (getattr(contract, "secType", "") == "CONTFUT" or
                          str(what).upper() == "ADJUSTED_LAST")
        request_end = "" if current_window else (end or "")
        return ib.reqHistoricalData(
            contract,
            endDateTime=request_end,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what,
            useRTH=bool(use_rth),
            formatDate=2,
            timeout=min(max(int(self.timeout), 1), 30),
        )
