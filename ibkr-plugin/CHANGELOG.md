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
