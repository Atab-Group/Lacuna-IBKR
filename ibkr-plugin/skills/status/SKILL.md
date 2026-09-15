---
name: status
description: Use to check IBKR gateway and data pipeline health in one line, triggered by "ibkr status", "is ibkr up", "/ibkr:status", or before relying on market-data tools for something time-sensitive.
---

# IBKR status

Reports one health line: session state, account, pacing budget left, store
size, and time of the last nightly pull.

## Steps

1. **Call the `ibkr_status` MCP tool.** This is the primary path and is
   usually all that is needed. It returns session up/down, the account,
   pacing budget remaining, store size, universe count, and, if the session
   is down, the login URL.

2. **Render one line from the result.** For example:

   > IBKR: session up, account U1234567, budget 54/60 this window, store
   > 1.2 GB, 30 symbols tracked, last nightly pull 2026-08-31 21:05 UTC.

   If the session is down, report that first and give the login URL, and
   point at the `login` skill (`/ibkr:login`) rather than attempting
   anything yourself.

3. **If the MCP tool is unavailable** (the server did not start, or the call
   errors out), fall back to checking the pieces directly instead of
   guessing:

   ```bash
   cd services/ibkr-data/deploy && docker compose ps
   ```

   Confirms whether the gateway container is running at all.

   ```bash
   nc -z 127.0.0.1 4001 && echo "socket open" || echo "socket closed"
   ```

   Confirms whether the API socket is listening. Port 4001 is live trading
   mode; a closed socket with a running container usually means the session
   is logged out and needs the `login` skill.

   Report what these two checks found in place of the one-line summary, and
   say plainly that the MCP path was unavailable so the reader knows this is
   a degraded read that misses part of the picture (`ibkr_status` also reports pacing
   budget and store size, which the fallback checks cannot see).


## Gateway recovery fixes (0.1.1)

A running container, an open API port, or a successful API handshake does
not prove the upstream market-data connection works. Verify recovery with
one small historical-bar request and inspect its returned data and coverage.
Errors 1100/2110 indicate lost upstream connectivity; 2103/2157 identify
broken data/security-definition connections. Do not diagnose every empty
response as a login failure: check contract resolution, permissions, session
filtering and the fetch metadata first.

The data client and login probe use the low-level API handshake, skipping
`IB.connect` account/order synchronization. Never enable write access to
resolve an "API client needs write access" prompt for this service.

IBC has a separate "Re-login is required" dialog handler which can retry
even when the second-factor retry setting is disabled. The web UI stops the
container after failed attempts. If an unattended re-login loop is observed,
stop the container and leave the next login to the human; never automate a
retry. This cleanup is containment, not a change to IBC's internal handler.
