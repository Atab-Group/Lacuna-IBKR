# IBKR market data for AI agents

Read-only market data from Interactive Brokers, wired into Claude Code as an
MCP server plus four skills. Historical bars down to 30-second resolution
going years back, event windows around a dated announcement, and a local
parquet store so the same question costs nothing the second time.

**It never places a trade.** There is no order code in this repository. The
refusal sits at three layers: no order tool exists on the MCP surface, the
gateway runs with `READ_ONLY_API=yes`, and the intended IBKR login is a
read-only secondary username that cannot trade at all.

## What you need

- **Docker**, because the IBKR gateway runs in a container. The image is about
  1 GB and downloads on first login.
- **Python 3.10 or newer.**
- **Your own Interactive Brokers username.** Not a colleague's. IBKR allows one
  API session per username, so a shared login knocks the other person off, and
  the six digit code goes to the phone of whoever the username belongs to. A
  read-only secondary username on an existing account is the intended shape.
  The account's primary user creates one in Client Portal under Settings, then
  User Access Rights.
- **An authenticator app enrolled with IBKR.** The login asks for a six digit
  code, about once a week.

Historical bars need no market data subscription. Nothing here requires a paid
data feed.

## Installing it

**a. As a Claude Code plugin**

```
/plugin marketplace add Atab-Group/Lacuna-IBKR
/plugin install ibkr@lacuna-ibkr
```

That gives you the four skills. The service itself still needs a clone, because
it runs a container and a virtualenv on your machine:

```
git clone https://github.com/Atab-Group/Lacuna-IBKR.git
cd Lacuna-IBKR && ./setup.sh
```

**b. Clone and run the setup script**

```
git clone https://github.com/Atab-Group/Lacuna-IBKR.git
cd Lacuna-IBKR && ./setup.sh
```

`setup.sh` checks for Docker and Python, builds a virtualenv, links the skills
into `.claude/skills/`, and runs the offline test suite. It touches no network
beyond PyPI and it does not log you in. Starting `claude` in that directory
picks up the `ibkr` MCP server and the skills.

## Handing it to an AI

Paste this:

> Read `ibkr-plugin/skills/setup/SKILL.md` in this repository and follow it end
> to end to get my market data working.

The setup skill does every technical step itself, asks the one question that
needs a human, hands you a login page on `127.0.0.1`, proves the data works
with one cheap pull, and then asks which companies you want it to track.

## Logging in

Your credentials never go into a chat. The login is a page on your own machine:

```
.venv/bin/python services/ibkr-data/webui.py
```

Then open `http://127.0.0.1:8642`. Type your username and password, wait about
forty seconds, and enter a fresh six digit code when the page asks. Credentials
are written to `services/ibkr-data/deploy/.env`, owner-readable only and
gitignored. IBKR invalidates the session weekly, on Sunday around 01:00 US
Eastern, so expect to do this about once a week.

Never retry a failed login automatically. IBKR throttles failed attempts and an
account lockout sits directly behind the throttle.

## Using it

From Claude Code, in plain language: "what did Apple do the day of the ruling",
"how did it move in the hour after the open on 3 March", "how far back do we
have prices for this name".

From a shell:

```
.venv/bin/python services/ibkr-data/cli.py bars AAPL --interval 1d --start 2026-08-01
.venv/bin/python services/ibkr-data/cli.py coverage AAPL
```

`services/ibkr-data/CONTRACT.md` is the full interface spec.
`services/ibkr-data/README.md` carries the traps.

## The pacing limit

IBKR allows **60 historical requests per rolling 10 minutes**, and BID_ASK
requests count twice. The service enforces that budget locally and shares it
between every caller. Do not work around it.

A breached budget is the trap that matters: IBKR answers with an empty
response rather than an error, so "no data" and "you asked too fast" look
identical. Every response carries a `coverage` block saying what was asked for
against what actually came back. Read that block, never the row count.

## Limits, honestly

- **It cannot trade, and it has no portfolio view.** No positions, no account
  value, no orders. That is deliberate and permanent.
- **A human logs in roughly weekly.** There is no API key that skips this. IBKR
  withdrew the partial opt-out that existed around 2019 to 2021, and read-only
  permissions change nothing about it. The gateway restarts itself daily using
  a token on disk, which is what keeps the cadence to weekly rather than daily.
- **Docker is not optional.** The gateway is a desktop Java application driven
  through a container; there is no pure-Python path to an IBKR session.
- **60 requests per 10 minutes is a hard ceiling.** A seven-year daily backfill
  across thirty names is roughly 250 requests, so it takes the better part of
  an hour. Run it detached.
- **Live streaming quotes are delayed** on an account with no market data
  subscription, so `ibkr_get_quote` can be minutes stale. Historical bars carry
  no such discount.
- **30-second bars have a shorter reach than minute bars.** Retention varies per
  contract, and `reqHeadTimeStamp` is the only honest answer for a name you have
  not pulled before.
- **Tested on Linux and macOS.** Windows is untried.
- **The watchlist ships nearly empty**, holding AAPL and MSFT. It is yours to
  fill in.

## Licence

MIT. See `LICENSE`.
