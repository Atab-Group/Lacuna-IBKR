# 0.6.0 - 19 November 2026

- Five optional price-alert tools, listed only when `LACUNA_PRICE_ALERTS_DIR`
  names the state directory of a price-alert engine on the same machine:
  `ibkr_get_live_tape`, `ibkr_list_price_alerts`,
  `ibkr_list_price_alert_history`, `ibkr_test_price_alert` and
  `ibkr_update_price_alert`. Without that variable the tool list is unchanged.
- The live tape gives the last minutes of a watched instrument as they were
  recorded, for the moments when historical bars are still about 15 minutes
  behind. Every reply says how old its newest row is and whether the feed is
  live.
- `ibkr_test_price_alert` replays a stored day through proposed thresholds, and
  `ibkr_update_price_alert` changes them within fixed bounds. Contracts, hours,
  the cooldown and recipients cannot be changed through a tool.
- Tokens gain an optional `"write": true`. Only a write token may change a
  threshold; `--add-token NAME --write` mints one. Over stdio, writing needs
  `LACUNA_PRICE_ALERTS_STDIO_WRITE=1`.
- `ibkr_update_price_alert` checks the whole merged rules file against the
  engine's field and cross-field rules before it writes, and, when
  `LACUNA_PRICE_ALERTS_VALIDATOR` names the engine's `config.py`, runs that
  check on the new file before the rename. A dotted key in `changes` or
  `overrides` is turned into a nested one. A symlinked rules file stays a link.
- No tool returns who the alerts go to: the listing gives counts, and every
  reply, error replies included, is scrubbed of recipient values.
- The market-data skill says when to use the tape in place of bars, and to
  test and read back a change before making it.

# 0.5.0 - 18 September 2026

- Added generalized contract resolution by `conId` or full instrument fields,
  including options. Derivative identity preserves expiry, strike, right,
  multiplier and trading class.
- Added option-chain discovery, bounded delayed option snapshots, model Greeks,
  open interest where available, and explicit account/data capability reporting.
- Added local option-snapshot capture. Captured values remain queryable after
  collection through `ibkr_get_option_snapshots`; IBKR does not supply a
  historical strike-level Greek or IV surface.
- Historical bars now support trade, bid, ask, midpoint, bid/ask, adjusted-last,
  historical-volatility and option-implied-volatility series where IBKR supports
  them for the selected instrument. Option daily bars are rejected because IBKR
  does not serve them directly.
- Bar storage and the request ledger now include contract and data-series identity,
  so two strikes or trade and quote bars cannot overwrite one another. The old
  equity trade cache remains readable; every newly qualified contract writes v2.
- `ibkr_get_quote` now returns a bounded delayed snapshot rather than a placeholder.
  Snapshot responses preserve partial values, per-field status and warnings/errors,
  report the actual market-data type, and normalize IBKR missing-value sentinels without
  discarding valid negative Greeks.
- Updated the market-data skill and operator documentation with the measured limits:
  delayed access depends on account entitlements, sparse option trades are not
  missing quote coverage, underlying IV is not strike-level IV history, and no
  collection schedule is enabled automatically.
- Clarified session semantics: generalized `eth` is the all-hours IBKR request
  mode (`useRTH=false`), so it includes regular hours. Clock-classified legacy
  US-equity event rows keep their existing `rth`/`eth` labels.

# 0.4.0 - 17 September 2026

- The market data now works from every folder, and inside Cowork. The plugin
  used to point at a server started from the folder Claude was opened in, so a
  session opened anywhere else got no market data at all, and Cowork, which
  never starts in the clone, could not reach it under any circumstances. The
  plugin now reaches a local service by address and key instead.
- New `httpserver.py`: the same nine tools over Streamable HTTP on
  `127.0.0.1:8770`. It binds loopback and nothing else, and there is no TLS, so
  the key must never leave the machine. `GET /healthz` is the one route that
  needs no key, so a watchdog can ask whether the service is up without holding
  a credential. The stdio server is untouched and still works.
- New `tokens.py`: named keys in `tokens.json`, mode 600 and gitignored.
  Revoking one is setting `"revoked"` to `true` and it takes effect on the next
  request with no restart. Comparison is constant time over every entry.
- `setup.sh` now finishes the job. It mints a key if there is not already a live
  one, starts the service, installs and enables the systemd user unit where
  systemd is available including WSL2, falls back to a detached background
  process where it is not, says which route it took, and prints the exact two
  lines that register the plugin with that key. Running it twice does not mint a
  second key or start a second service.
- The `setup` skill now runs those two lines itself rather than showing them to
  the person, and tells them the market data works in all of their
  conversations rather than in one folder.
- The `status` skill now separates a stopped local service from a broken login.
  From the outside both look like "no prices", only one of them needs the
  person, and telling someone to log in again when nothing is listening wastes
  a login.
- `deploy/ibkr-http.service` is a systemd user unit template, not a system one:
  the service reaches a gateway container started under the person's own login
  and a store inside their clone, and root has neither.

# 0.3.0 - 15 September 2026

- First public release. The plugin and the service it drives now live in their
  own repository and install with `/plugin marketplace add`, rather than
  travelling as a tarball.
- The shipped watchlist is now two generic large caps, AAPL and MSFT, and it is
  meant to be edited. The `case` field on a watchlist entry is optional
  everywhere it is read, so an entry that is just a symbol is valid.
- New step in the `setup` skill: once the login works, it asks which companies
  the person cares about, resolves the names to tickers, reads them back, and
  saves them to the watchlist on a yes.
- A repository README covering what this needs, both install paths, the pacing
  limit, and an honest limits section.

# 0.2.0 - 15 September 2026

- New `setup` skill. One skill takes a person from the unpacked bundle to
  working market data, doing every technical step itself. The only thing the
  person types is their own Interactive Brokers username, password and six
  digit code, into the login page on their own computer.
- The setup skill asks one question, the only one that genuinely needs a
  human: whether the person has their own Interactive Brokers username with
  the authenticator app set up. It explains why a borrowed login cannot work
  and gives them the exact words to send the account owner if they need one.
- Everything the setup skill says to the person is plain English. No technical
  words, no raw output, and every reply ends with what was done and what is
  recommended.
- `AGENT-PROMPT.md` in the bundle is now two lines of instruction plus the
  standing rules. Unpack, then tell your AI to follow the setup skill.
- `setup.sh` links the setup skill alongside the other three, so a later
  session finds it too.

# 0.1.1 - 14 September 2026

- Use API-only handshakes in the market-data client, login probe and diagnostic script. Avoid requesting order/account synchronization on a read-only gateway.
- Stop the gateway container when a web UI login attempt fails, including exceptions and missing-code timeouts, to contain IBC's independent re-login loop.
- Explain the distinction between local API availability and verified historical-data access in all three skills.
- Verified recovery with live IBKR historical prices; add offline regression tests for connection behavior and failed-login cleanup.

The live gateway is not restarted by installing this update. Authentication remains a human step. The upstream IBC automatic re-login handler itself is unchanged.
