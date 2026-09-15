"""The only module that talks to IB Gateway.

Thin by design: connect, contract details, historical bars, head timestamp. No
caching, no pacing, no storage. Those live in contracts.py, jobs.py and store.py
so that everything except this file is testable without a gateway.

Never name a module in here ``ibapi.py``. That shadows IBKR's own package import
and the failure is silent.
"""

from __future__ import annotations

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

    def qualify(self, contract_dict: dict):
        """A CONTRACT dict back into the ib_async object a fetch needs."""
        from ib_async import Contract

        return Contract(
            conId=int(contract_dict["con_id"]),
            symbol=contract_dict["symbol"],
            secType=contract_dict.get("sec_type", "STK"),
            exchange=contract_dict.get("exchange", "SMART"),
            primaryExchange=contract_dict.get("primary_exchange") or "",
            currency=contract_dict.get("currency", "USD"),
        )

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
        return ib.reqHistoricalData(
            contract,
            endDateTime=end or "",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what,
            useRTH=bool(use_rth),
            formatDate=1,
        )
