#!/usr/bin/env python3
"""Answer three questions about a fresh IBKR API setup, in order:

1. Can this username open an API session at all?
2. Does it get historical daily bars without any market data subscription?
3. How fine-grained and how far back can it go?

Run it after `docker compose up -d` in deploy/. Everything it does is a read.

    python3 check_connection.py            # default symbol AAPL
    python3 check_connection.py MSFT NSRGY
"""

from __future__ import annotations

import sys

HOST = "127.0.0.1"
PORT = 4001  # live gateway; paper is 4002
CLIENT_ID = 77  # anything unused; the human's TWS is usually 0 or 1

DELAYED = 3  # reqMarketDataType: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen


def main(symbols: list[str]) -> int:
    try:
        from ib_async import IB, Stock, util
    except ImportError:
        print("ib_async is not installed. Run:")
        print("    pip install ib_async pyarrow duckdb")
        return 1

    util.logToConsole("ERROR")  # ib_async is chatty at INFO
    ib = IB()

    print(f"connecting to {HOST}:{PORT} (clientId={CLIENT_ID})")
    try:
        ib.client.connect(HOST, PORT, clientId=CLIENT_ID, timeout=20)
    except Exception as exc:
        print(f"\nFAILED to connect: {exc}\n")
        print("Common causes, in order of likelihood:")
        print("  - gateway container not running, or still at the login screen")
        print("  - login needs a 2FA tap that nobody answered")
        print("  - this username lacks 'Trade Button & TWS access'")
        print("  - API not enabled in the gateway's own settings")
        print("Watch the login with a VNC client on 127.0.0.1:5901.")
        return 1

    print(f"connected. server version {ib.client.serverVersion()}")

    accounts = ib.managedAccounts()
    print(f"accounts visible: {', '.join(accounts) if accounts else 'none'}")

    ib.reqMarketDataType(DELAYED)

    ok = True
    for sym in symbols:
        print(f"\n--- {sym} ---")
        contract = Stock(sym, "SMART", "USD")
        try:
            details = ib.reqContractDetails(contract)
        except Exception as exc:
            print(f"  contract lookup failed: {exc}")
            ok = False
            continue
        if not details:
            print("  no contract found. Check the symbol and exchange.")
            ok = False
            continue

        d = details[0]
        print(f"  conId {d.contract.conId} on {d.contract.primaryExchange}"
              f" ({d.longName})")

        # How far back does IBKR keep data for this contract?
        try:
            head = ib.reqHeadTimeStamp(d.contract, whatToShow="TRADES",
                                       useRTH=False)
            print(f"  earliest data: {head}")
        except Exception as exc:
            print(f"  head timestamp unavailable: {exc}")

        for bar_size, duration in (("1 day", "10 D"), ("1 min", "1 D")):
            try:
                bars = ib.reqHistoricalData(
                    d.contract,
                    endDateTime="",
                    durationStr=duration,
                    barSizeSetting=bar_size,
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                )
            except Exception as exc:
                print(f"  {bar_size:>6}: request raised {exc}")
                ok = False
                continue

            if not bars:
                print(f"  {bar_size:>6}: EMPTY. Either no permission for this"
                      " instrument, or pacing, or a market holiday.")
                ok = False
            else:
                first, last = bars[0], bars[-1]
                print(f"  {bar_size:>6}: {len(bars)} bars,"
                      f" {first.date} to {last.date}, last close {last.close}")

    ib.disconnect()

    print("\n" + ("all checks passed" if ok else "some checks failed, see above"))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["AAPL"]))
