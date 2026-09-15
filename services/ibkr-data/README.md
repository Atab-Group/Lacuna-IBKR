# ibkr-data

Read-only market data from Interactive Brokers, for event studies that free
sources cannot serve. Yahoo gives 1-minute bars for 30 days only, so anything
older than a month coarsens to daily bars. IBKR gives minute bars going back
years, and 30-second bars going back further than its own documentation admits.

Nothing in this service can place an order.

## The data core

`CONTRACT.md` is the interface spec and it is where every number lives. The short
version:

    .venv/bin/python services/ibkr-data/cli.py resolve MSFT
    .venv/bin/python services/ibkr-data/cli.py bars MSFT --interval 1d --start 2026-08-01
    .venv/bin/python services/ibkr-data/cli.py pull MSFT --interval 1m --date 2026-06-25
    .venv/bin/python services/ibkr-data/cli.py coverage MSFT

`bars` and `pull` serve from the parquet store under `shared/data/ibkr/` and fetch
only what is missing, inside the 60-requests-per-10-minutes budget that `jobs.py`
persists in `jobs.db`. Add `--no-fetch` to stay offline. `coverage` never touches
the network.

Tests cover the pure parts and nothing hits IBKR:

    .venv/bin/python -m pytest services/ibkr-data/tests -q

## How the connection works

Nothing talks to IBKR over the internet directly. A local IB Gateway process
holds the authenticated session, and Python connects to it over a socket on
localhost. No gateway, no data.

The intended shape is a read-only secondary username on the account. That has
two useful consequences: orders are impossible, and the one-API-session-per-
username limit never collides with the account owner using TWS.

## Using it

Two front ends drive the same machinery. The browser one exists because the
terminal one must be run by the person typing, which does not work when an
agent is driving:

    .venv/bin/python services/ibkr-data/webui.py    # then open http://127.0.0.1:8642

The page asks for credentials (first run), starts the gateway, and shows a
big code box the moment the gateway wants six digits, with a countdown.
Localhost only. This is the piece that becomes the plugin's login surface.

The terminal flow, for when you are at a shell yourself:

    ./services/ibkr-data/gateway

The first run asks for the IBKR username and password (typed masked, saved to
`deploy/.env` with owner-only permissions, gitignored) and offers to store the
TOTP secret. After that it goes straight to the login: five steps with a live
spinner, and a prompt for the six digit authenticator code at the moment the
gateway is ready for one. That timing matters because TOTP codes expire in 30
seconds and the dialog takes about 40 seconds to appear, so have the
authenticator app open and read a fresh code when asked.

The other commands:

    ./services/ibkr-data/gateway status   # is the session alive?
    ./services/ibkr-data/gateway down     # stop the gateway
    ./services/ibkr-data/gateway creds    # re-enter username and password

`gateway` is a shell wrapper around `gateway.py` because the repo path
contains a space and a shebang cannot express that.

Guard rails: it refuses to start if IBKR throttled the login in the last 15
minutes, which is the state that precedes an account lockout, and it rejects a
code that is not exactly six digits rather than wasting an attempt on it.

With `TOTP_SECRET` stored (the creds prompt offers it), logins generate the
code themselves and never prompt. That is what makes unattended restarts
possible, at the cost of both auth factors living in one file.

Then test what the account can actually see:

       cd ../../..
       .venv/bin/python services/ibkr-data/check_connection.py AAPL MSFT

   It reports the account list, then per symbol: the conId, the earliest
   available data, 10 daily bars and one day of minute bars.

## The watchlist and the nightly update

`universe.json` beside this file is the watchlist: the symbols the nightly
backfill maintains, each with its resolved conId. It ships with two generic
names and is yours to edit. Full detail is in CONTRACT.md under "The
watchlist".

    .venv/bin/python services/ibkr-data/cli.py universe            # list and validate
    .venv/bin/python services/ibkr-data/cli.py universe --resolve  # fill in the conIds
    .venv/bin/python services/ibkr-data/cli.py backfill --start 2019-01-01
    .venv/bin/python services/ibkr-data/cli.py update              # last session or two

A backfill can run for an hour or more, because seven years of daily bars for
thirty names is about 250 requests against a ceiling of 60 per ten minutes. Run
it detached and read the log:

    nohup .venv/bin/python services/ibkr-data/cli.py backfill \
        > services/ibkr-data/backfill.log 2>&1 &
    tail -f services/ibkr-data/backfill.log

Killing it costs nothing. Both walkers decide what to ask for from the coverage
ledger, so a restart picks up at the first session date that never settled and a
finished run costs zero requests.

`update` is what a nightly timer should call. On Linux a systemd user timer
does the job:

    systemctl --user list-timers lacuna-ibkr-update.timer
    systemctl --user start lacuna-ibkr-update.service   # run it now
    tail services/ibkr-data/update.log

## Reading the result

The script answers the question that decides the whole design: whether this
username gets historical bars with no market data subscription. Delayed data
only degrades live streaming quotes; historical bars are the same data either
way. So `EMPTY` on daily bars means a permissions problem, not a delay.

A subscription added under a secondary username bills the account owner. Do not
add one without asking them.

## Facts established by live testing

Tested against a read-only secondary username with no market data
subscriptions.

- Historical bars need no subscriptions. Every symbol tried returned both daily
  and intraday bars on a bare account.
- Daily history reaches back decades. AAPL goes to 1980-12-12. How far back any
  given contract goes varies, and `reqHeadTimeStamp` is the only honest answer
  for a name you have not pulled before.
- ADRs resolve cleanly, as ordinary SMART/USD stocks with an unusual primary
  exchange such as PINK.
- **30-second bars go back at least 3 years**, which contradicts IBKR's
  documented six-month limit for sub-minute bars. Verified by pulling a full
  780-bar session for MSFT on 2023-06-27, and the same at 2 months, 6 months and
  14 months. 1-minute bars returned a full 390-bar session at every one of
  those distances.

That last point is the reason this service exists. Yahoo caps 1-minute data at
30 days, so anything older silently degrades to daily bars. IBKR serves
30-second resolution years back instead.

## How often a code is actually needed

Once a week, in the normal case. There is no API key or token that skips the
login: every IBKR retail session goes through the Secure Login System, and the
partial opt-out that existed around 2019 to 2021 has been withdrawn for TWS and
Gateway logins. Read-only permissions and a secondary username change nothing
about this.

What keeps it to weekly: at `AUTO_RESTART_TIME` the gateway restarts itself
using a security token written to disk at the last full login, with no prompt.
IBKR invalidates those tokens every Sunday at 01:00 US Eastern, and the first
login after that needs a real code. So the cadence is one code per week rather
than one per session, provided the container stays alive across the restart.
A crash or a `docker compose down` loses the token and forces a cold login,
which is why the Jts settings directory is on a named volume.

Full automation is possible because the Mobile Authenticator is standard TOTP:
store the secret, generate the code with `pyotp`, type it into the dialog.
`heshiming/ibga` already does exactly this with `oathtool`, so the approach is
proven. Two things to settle before going that way. The secret is a complete
second factor living beside the password, and on a secondary username the
account owner has to agree. Re-enrolling the authenticator to capture the secret may
also invalidate the owner's existing enrolment on their own phone.

## Traps

- Host port 4001 maps to container port 4003, because the image puts socat in
  front of the gateway. Live is 4001/4003, paper 4002/4004.
- Never name a module in here `ibapi.py`. That shadows IBKR's own package and
  the failure is silent.
- A violated pacing limit returns an empty response rather than an error, so
  "no bars" and "hit the rate limit" look identical. The limit is 60 historical
  requests per rolling 10 minutes, and BID_ASK requests count twice.
- Bars of 30 seconds or finer only go back six months. Minute bars go back
  years. `reqHeadTimeStamp` gives the exact earliest point per contract.
- The gateway restarts itself daily, and IBKR invalidates the session weekly on
  Sunday around 01:00 US Eastern, which needs a fresh 2FA code.
- With a TOTP authenticator app, the gateway shows a dialog reading
  "Enter Mobile Authenticator app code" and waits for six typed digits. There
  is no phone push to approve. The dialog lives 180 seconds.
- Never set `RELOGIN_AFTER_TWOFA_TIMEOUT=yes`. It retries every few minutes
  while nobody is typing a code, and IBKR starts refusing with "Too many failed
  login attempts" after about nine tries, then locks the account. The compose
  file deliberately carries `restart: "no"` and `TWOFA_TIMEOUT_ACTION: exit`
  for the same reason: a missed code stops the container and waits.
- `vncdo` opens a new VNC connection per invocation, several seconds each, so
  chain every action into one call. A loop of single keystrokes takes longer
  than the 30 second TOTP window and the code expires mid-typing.

## VNC

When a login hangs, watch it: connect a VNC client to `127.0.0.1:5901` using
`VNC_SERVER_PASSWORD` from `.env`.
