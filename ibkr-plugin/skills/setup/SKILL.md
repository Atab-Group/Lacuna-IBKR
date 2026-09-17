---
name: setup
description: "Use when someone has just installed the IBKR market-data plugin, or unpacked its bundle, and wants equities, options or other supported market data working for the first time. Takes them from a fresh copy to real data, doing every technical step itself, and asking the person for nothing except their own login at the local login page."
---

# Setting up market data

This skill takes one person from a fresh copy of this repository to working
market data.
You do every step. The only thing the person ever types is their own
Interactive Brokers username, password and six digit code, and they type all
three into a login page on their own computer, never into this conversation.

The person may never have opened a terminal. This file keeps two things
apart. **What you do** is technical and is for you. **What you say** is the
only part that reaches them, and it is plain.

## What you say, always

- No technical words at all. Not server, container, image, package, library,
  key, token, header, address, port, folder, path, script, terminal, shell,
  command, test suite, virtual environment, plugin file. Say "the market data
  service", "your computer", "the login page", "the checks".
- Never show raw output, a command, an error trace, or a file name. Say what
  it means for them.
- Never use the em dash character, and never the antithesis pattern. Short
  plain sentences. Name real things: company names, dates, prices, minutes.
- **Every reply ends with two lines.** The only exception is a question you
  are waiting on, which ends with the question, and the two lines come after
  they answer.

  > Done: <what happened, in one sentence>.
  > Recommended: <the one thing to do next>.

- An error is one or two plain sentences plus one thing to do about it.
  Never give a cause the person cannot act on.
- Say how long something takes before it takes it. Silence while a person
  waits is what makes them think it broke.

## Step 0. What you do: find the working directory

Every command below runs from the top of the repository, the directory holding
`setup.sh` and `services/`. If the person installed this as a Claude Code
plugin and there is no `setup.sh` in the current directory, they still need a
clone of the repository for the service itself, so say this and stop:

> This needs a copy of the market data project on your computer before it can
> run.
> Done: nothing is set up yet.
> Recommended: ask whoever sent you this for the one line that downloads it.

## Step 1. What you do: check what this computer has

Work from that directory. Check three things:

```bash
command -v docker >/dev/null && docker compose version >/dev/null 2>&1 && echo docker-ok || echo docker-missing
python3 -c 'import sys; print("py-ok" if sys.version_info >= (3,10) else "py-old")' 2>/dev/null || echo py-missing
```

If both say ok, go to step 2 and say nothing about this step.

If something is missing, decide whether installing it here is safe and
reversible on this platform, and do it yourself where it is:

- **macOS with Homebrew present** (`command -v brew`): `brew install --cask docker`
  for Docker Desktop, `brew install python@3.12` for Python. Both are
  reversible with `brew uninstall`. Docker Desktop still needs the person to
  open the app once and accept its licence, so say that in plain words.
- **Linux with apt and passwordless sudo**: `sudo apt-get install -y python3`
  is safe. Installing Docker is not, because it adds a package source and a
  group that changes what the person's account can do, so do not do it
  unasked.
- **Windows, or no package manager, or sudo would prompt for a password**: do
  not install anything.

Where you are not installing it, say one plain sentence and stop:

> Your computer needs Docker Desktop before the market data can run. It is
> free and it comes from docker.com.
> Done: nothing is set up yet.
> Recommended: install Docker Desktop, open it once, then come back and say
> "carry on with the market data setup".

**What you say** if you did install something:

> I installed the two pieces your computer was missing. That is done.

## Step 2. What you do: run the setup script

Tell the person first, then run it:

> Setting up now. This takes a few minutes, mostly downloading. I will tell
> you when it is finished.

```bash
./setup.sh
```

It creates the working environment, installs what the service needs, links
the skills, runs the offline checks, then starts the market data service and
prints a key for this computer. Read the last lines of the output. The checks
must report that every one passed, and the last block gives you the key you
need in step 3. Nothing in this step touches IBKR and nothing needs a login
yet.

Running it a second time is safe: it reuses the key already on this computer
and leaves the running service alone.

If `setup.sh` exits non-zero, or the checks report a failure, stop. Do not
try to repair it and do not run it again more than once.

> Something went wrong while setting up the market data on your computer.
> Done: the setup did not finish, and nothing was left running.
> Recommended: ask whoever sent you this, and tell them the setup did not
> finish.

If it succeeded:

> That is installed and all the checks passed.

## Step 3. What you do: make it work everywhere, not just here

Run this yourself. Do not show these lines to the person and do not ask them
to run anything.

The last thing `setup.sh` printed is a key for this computer and the two lines
that use it. Read the key out of that output, then run both lines, putting the
key where the example has it:

```bash
claude plugin marketplace add Atab-Group/Lacuna-IBKR
claude plugin install ibkr@lacuna-ibkr -y --config ibkr_token=THE_KEY_SETUP_PRINTED
```

Check it took:

```bash
claude mcp list
```

The `ibkr` entry must say connected. Until this step is done the market data
only answers inside this one folder, which is the thing that made it useless
to most people. After it, it answers in every session and in Cowork.

If the install or the check fails, do not retry it more than once and do not
work around it. The rest of the setup still works here, so carry on to step 4
and say one plain sentence at the end instead:

> One part did not finish, so the market data works here but not yet in your
> other conversations.

**What you say** when it worked:

> The market data now works in all of your conversations, not only in this
> one, and in Cowork too. You will not have to set it up again.

## Step 4. The one question a person has to answer

This is the only question in the whole setup, and it cannot be worked out by
looking. Ask it before anything touches IBKR.

**What you say:**

> One question before we go further. Do you have your own Interactive Brokers
> username, with the Interactive Brokers authenticator app set up on your
> phone?
>
> It has to be your own. Interactive Brokers allows one connection per
> username, so using someone else's would knock them off, and the six digit
> code goes to the phone of whoever the username belongs to, so you would
> have to ask them for a code every week.

Wait. Do not guess and do not go on.

**On a yes:** go to step 5.

**On a no, or "I use my colleague's":** stop and give them the exact words to
send, then wait. Do not go to step 5 until they confirm it is done.

> You need your own username first, and the person who owns the account can
> create one for you in a few minutes. Send them this:
>
> "Please could you create a secondary username for me on the account, with
> read only permissions. In Client Portal it is under Settings, then User
> Access Rights. Once it exists, please enrol it in the IB Key authenticator
> app so I can get my own login codes."
>
> Done: the market data is installed and waiting for your own username.
> Recommended: send them that message, and tell me when you have your
> username and the app is set up.

## Step 5. The login, which only the person can do

**Never ask for the password or the six digit code in this conversation.
Never read them from a file. Never attempt the login yourself and never retry
a failed one.** Interactive Brokers throttles failed logins and an account
lockout sits directly behind that throttle. The login page is the only
sanctioned path and a human operates it.

**What you do.** Start the login page in the background, then confirm it is
up:

```bash
.venv/bin/python services/ibkr-data/webui.py >/dev/null 2>&1 &
sleep 3
curl -s 127.0.0.1:8642/state
```

**What you say**, once it answers:

> The login page is ready. Open this in your browser:
>
> http://127.0.0.1:8642
>
> Type your Interactive Brokers username and password there. Then wait. About
> forty seconds later the page asks for a six digit code, so have your
> authenticator app open and read a fresh code when it asks, because the codes
> expire after thirty seconds.
>
> I will wait here and tell you when it is connected. It usually takes a
> couple of minutes.

Then stop talking and wait. Do not repeat the instruction, and do not ask
whether they have done it yet.

**What you do: poll patiently.** Check every ten seconds or so, never in a
tight loop, for up to about ten minutes:

```bash
curl -s 127.0.0.1:8642/state
```

The `phase` field is the answer. Read it like this, and say nothing to the
person while it is moving through the middle ones:

| `phase` | What it means | What you do |
|---|---|---|
| `needs_creds` | They have not typed anything yet | Keep waiting, say nothing |
| `starting`, `waiting`, `opening` | It is working | Keep waiting, say nothing |
| `need_code` | The page is asking for the six digits | Keep waiting. Only if it sits here over two minutes, say one line: "The page is asking for your six digit code now." |
| `typing` | The code went in | Keep waiting, say nothing |
| `logged_in` | Done | Go to step 6 |
| `failed` | It did not work | Stop. See below. |

On `failed`, say one or two plain sentences and stop. Do not restart the
page, do not clear anything, and above all do not try the login again:

> That login did not go through. It is worth opening the page again and
> trying once yourself, slowly, with a fresh code.
> Done: the market data is installed but not connected yet.
> Recommended: try the login page once more, and if it fails again, ask
> whoever sent you this.

If ten minutes pass with `needs_creds` unchanged, ask once whether they got
to the page, and keep waiting after they answer.

## Step 6. What you do: prove it actually works

One cheap pull, well inside the pacing budget:

```bash
.venv/bin/python services/ibkr-data/cli.py bars AAPL --interval 1d \
  --start "$(date -u -d '14 days ago' +%F 2>/dev/null || date -u -v-14d +%F)"
```

Read the `coverage` block before you read the rows. If bars came back, take
the last close and its date.

**If it comes back empty, do not call that a failure yet.** An empty response
here has several meanings and they look identical:

- `fetched: denied` in coverage means the pacing budget was spent, not that
  data is missing. Wait a few minutes and pull once more.
- `empty` means IBKR was asked and genuinely had nothing, which a market
  holiday or a delisted name explains.
- Read the Traps section of `services/ibkr-data/README.md` before concluding
  anything else. Empty on daily bars for a large listed name points at
  permissions on the username rather than at this setup.

Only after reading both the coverage block and that section, say in plain
words what you found:

> The market data is connected, but it came back with nothing for a test on
> Apple. That usually means the username needs a permission it does not have
> yet.
> Done: the market data is installed and logged in, but not returning prices.
> Recommended: ask whoever sent you this, and tell them the test came back
> empty.

**On success:**

> Connected. Apple last closed at 231.44 on 12 September, and that came from
> real market data rather than anything cached.

This equity pull proves the connection and that one historical path. It does
not prove every exchange or option field is entitled. The capabilities probe
can confirm gateway connectivity and the implemented interface, but it is not
an entitlement test. When the person first asks for options, FX, futures,
indexes or delayed snapshot fields, make a small bounded request for the exact
contract and report partial availability as returned. Never purchase or change
an entitlement.

## Step 7. The companies they care about

Do this straight after the first successful pull, before anything else. A
watchlist the person chose is what makes the next question fast, and an empty
one makes every question start from scratch.

**What you say:**

> One more thing and then you are set. Which companies do you want me to keep
> prices for? Give me the names, in any form. "Apple", "the big supermarkets",
> "Barclays" all work, and you can add more any time.
>
> If you would rather skip this, say skip, and I will just use Apple and
> Microsoft for now.

Wait for the answer. Do not guess names for them.

**What you do.** Turn each name into a ticker. Where a name is ambiguous, or an
ADR, resolve it rather than guessing:

```bash
.venv/bin/python services/ibkr-data/cli.py resolve TICKER
```

Read back every name and ticker you worked out, in plain words, and get a yes
before writing anything:

> I have these. Apple as AAPL, Microsoft as MSFT, Tesco as TSCDY, which is the
> London listing traded in America. Shall I save those?

On a yes, add them to the watchlist. Read the file, add only the entries that
are not already there, and write it back:

```bash
.venv/bin/python - <<'EOF'
import json, pathlib
path = pathlib.Path("services/ibkr-data/universe.json")
data = json.loads(path.read_text())
have = {e["symbol"] for e in data["symbols"]}
for sym in ["AAPL", "MSFT"]:          # replace with the tickers they confirmed
    if sym not in have:
        data["symbols"].append({"symbol": sym, "exchange": "SMART",
                                "currency": "USD", "primary_exchange": None,
                                "con_id": None, "long_name": None,
                                "intervals": ["1d"]})
path.write_text(json.dumps(data, indent=2) + "\n")
EOF
.venv/bin/python services/ibkr-data/cli.py universe
```

The `universe` command validates what you wrote and exits non-zero if anything
is wrong. If it complains, fix the file rather than telling the person about it.

Do not run a backfill now. Seven years of daily bars for thirty names is about
250 requests against a ceiling of 60 per ten minutes, so it belongs in a
deliberate overnight run, not in a setup the person is waiting on.

> Saved. I will keep daily prices for those from now on.
> Done: your companies are on the list.
> Recommended: ask me what one of them did last week.

## Step 8. What you say: what they can now ask for

Close with examples rather than an explanation. Use real company names they
would recognise.

> You can now ask me things like:
>
> - "what did Acme do the day that ruling came out"
> - "how did Acme move in the hour after the market opened on 3 March"
> - "compare Acme against the rest of its sector over the last month"
> - "how far back do we have prices for Acme"
>
> Two things worth knowing. Interactive Brokers signs you out once a week,
> usually on a Sunday night, so about once a week I will ask you to open the
> login page again for one code. And this can only read prices. It cannot buy
> or sell anything, ever.
>
> Done: market data is set up and working on your computer, in every
> conversation.
> Recommended: try "what did Apple do last Tuesday" to see the shape of it.

## Standing rules, for you, for ever after

- The pacing budget is 60 requests per rolling 10 minutes, shared by every
  caller, enforced locally. Never work around it. A breached budget answers
  with silence rather than an error, so read the coverage block and never the
  row count.
- The session dies weekly, on Sunday around 01:00 US Eastern. The fix is
  always the person at the login page. Never a retry loop, ever.
- Nothing in this system can place an order. That is deliberate and
  permanent, and it is worth saying to the person once.
