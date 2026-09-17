---
name: status
description: Use to check IBKR gateway and data pipeline health in one line, triggered by "ibkr status", "is ibkr up", "/ibkr:status", or before relying on market-data tools for something time-sensitive.
---

# IBKR status

Reports one health line: session state, account, pacing budget left, store
size, and time of the last nightly pull.

Three things can be down and they are not interchangeable: the local market
data service, the gateway container, and the IBKR login. From the outside all
three look like "no prices", and only the login needs the person, so always
establish which one before saying anything.

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

3. **If the MCP tool is unavailable, check the local service first.** The
   plugin reaches the market data over a local address, so a stopped service
   and a broken login look identical from the outside: both are "no market
   data". Which one it is decides who fixes it, and telling the person to log
   in again when nothing is listening wastes a login and their time.

   ```bash
   curl -fsS --max-time 2 http://127.0.0.1:8770/healthz && echo " service up" || echo "service down"
   ```

   That route needs no key, so it answers whether or not the caller has one.
   A reply means the service is running and the problem is further in. No
   reply means the service is not running, and it is started the same way it
   was installed:

   ```bash
   systemctl --user status ibkr-http     # where it was installed as a service
   systemctl --user start ibkr-http
   ```

   Where there is no systemd, `./setup.sh` starts it again and is safe to run
   twice. Say it plainly, without naming the machinery:

   > The market data on your computer is not running at the moment. I have
   > started it again.
   > Done: the market data service is back up.
   > Recommended: ask me your question again.

4. **If the service is up and the tool still fails**, check the pieces below
   it instead of guessing:

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

   Report what these checks found in place of the one-line summary, and
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
