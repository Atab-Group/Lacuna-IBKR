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

Clone it and run the setup script. There is only one path, because the plugin
needs a key that only this machine can mint:

```
git clone https://github.com/Atab-Group/Lacuna-IBKR.git
cd Lacuna-IBKR && ./setup.sh
```

`setup.sh` checks for Docker and Python, builds a virtualenv, links the skills
into `.claude/skills/`, runs the offline test suite, then mints a key, starts
the market data service, and prints the two lines that register the plugin with
that key:

```
claude plugin marketplace add Atab-Group/Lacuna-IBKR
claude plugin install ibkr@lacuna-ibkr -y --config ibkr_token=<the key it printed>
```

Run those and the market data answers in every Claude session and in Cowork,
not only in the folder holding the clone. The key is printed once and stored
nowhere else, so a lost one is replaced rather than recovered.

It touches no network beyond PyPI and GitHub, and it does not log you in.
Running it a second time reuses the key and leaves the running service alone.

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

## Reaching it from any folder, and from Cowork

The stdio server in `.mcp.json` is found relative to the folder Claude was
started in, so a session opened anywhere else in the filesystem gets no market
data, and Claude Cowork, which never starts in this clone, cannot reach it at
all. The HTTP front end fixes both: the same nine tools on a local port that a
client reaches by URL and token.

`setup.sh` does all of this for you. By hand it is:

```
.venv/bin/python services/ibkr-data/httpserver.py --add-token nic-laptop
.venv/bin/python services/ibkr-data/httpserver.py
```

The token is printed once and stored nowhere else. It goes into the client
config as a bearer header against `http://127.0.0.1:8770/mcp`. Revoking one is
a two-character edit in `services/ibkr-data/tokens.json`, set `"revoked"` to
`true`, and it takes effect on the next request with no restart.

It binds `127.0.0.1` and nothing else. There is no TLS in front of it, so the
token crosses the wire in clear text and must never leave the loopback
interface. `GET /healthz` is the one route that needs no token, so a watchdog
can ask whether the server is up without holding a credential.

To keep it running, `setup.sh` installs and enables
`services/ibkr-data/deploy/ibkr-http.service` as a systemd **user** unit
wherever `systemctl --user` works, which includes WSL2 with systemd enabled in
`/etc/wsl.conf`. Where there is no systemd it falls back to a detached
background process, and says so, because that one does not come back after a
restart. A user unit dies at logout unless lingering is on, and a Cowork
session runs with nobody logged in at the desktop, so:

```
sudo loginctl enable-linger $USER
```

Check it at any time, with no key needed:

```
curl http://127.0.0.1:8770/healthz
```

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
