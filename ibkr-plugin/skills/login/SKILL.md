---
name: login
description: Use when the IBKR gateway session is down and a human needs to log in with a 2FA code, typically weekly on or after Sunday, triggered by "log into IBKR", "ibkr login", "/ibkr:login", or an ibkr_status result reporting the session down.
---

# IBKR login

The gateway needs a human once a week: IBKR invalidates the session token
every Sunday around 01:00 US Eastern, and the first login after that needs a
real six-digit code from the authenticator app. Between resets, a prior
login's token survives the gateway's own daily restart with no prompt.

This skill starts the login web UI and hands the human the URL. It never
attempts the login itself, and it never asks for a password or a code in
chat. 2FA is a human step by design; there is no supported way to script
around it, and repeated automated attempts trip IBKR's throttle, which sits
directly in front of an account lockout.

## Steps

1. **Check if the web UI is already up.**

   ```bash
   curl -s 127.0.0.1:8642/state
   ```

   A response means it is running; move to step 3. No response (connection
   refused) means it needs starting.

2. **Start it, in the background, if it is not running.**

   ```bash
   .venv/bin/python services/ibkr-data/webui.py &
   ```

   Wait a few seconds, then confirm with the same `curl` call from step 1.

3. **Give the user the URL and the reminder, then stop talking.**

   Tell the user, plainly:

   > Open http://127.0.0.1:8642 and log in. Have your authenticator app open,
   > you will need a six-digit code.

   Do not proceed to poll immediately. The human needs a moment to open the
   page and reach the code entry step.

4. **Poll `/state` until the outcome is clear.**

   ```bash
   curl -s 127.0.0.1:8642/state
   ```

   Poll every few seconds rather than in a tight loop. Stop polling once the
   state reports the session is up, or reports a failure, or after a few
   minutes with no change, whichever comes first.

5. **Report the outcome plainly.** Session up: say so, one line. Session
   still down after a reasonable wait: say so and suggest the user check the
   browser tab for a stuck dialog, rather than retrying anything yourself.

## What this skill must never do

- Never type, guess, or ask the user for a password in chat.
- Never type, guess, or ask the user for a 2FA / authenticator code in chat.
- Never call any IBKR login endpoint directly, or attempt to automate the
  browser login flow. The web UI at 127.0.0.1:8642 is the only sanctioned
  path, and a human operates it.
- Never retry a failed login automatically. If it failed, report that and
  stop; a human decides whether to try again.


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
