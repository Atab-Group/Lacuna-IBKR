# Paste this to your AI agent

If you were given the repository link, clone it and work inside it:

    git clone https://github.com/Atab-Group/Lacuna-IBKR.git && cd Lacuna-IBKR

If you were given a bundle file instead, unpack it in this directory:

    tar xzf ibkr-plugin-bundle.tar.gz && cd lacuna-ibkr

Then read `ibkr-plugin/skills/setup/SKILL.md` and follow it end to end. That
skill is the whole setup: it checks what this computer needs, runs
`./setup.sh`, asks the one question a human has to answer, hands the person
the login page, and proves the data works with one cheap pull.

Work autonomously. The only thing the person should ever type is their own
Interactive Brokers username, password and six digit code, and all three go
into the login page on their own computer, never into the chat.

## Standing rules, during setup and for ever after

- **Never ask for the password or the authenticator code in chat, never read
  them from disk, and never attempt or retry the login yourself.** IBKR
  throttles failed logins and an account lockout sits directly behind that
  throttle. The login page at `http://127.0.0.1:8642` is the only sanctioned
  path and a human operates it.
- **The pacing budget is 60 requests per rolling 10 minutes**, enforced
  locally and shared by every caller. Never work around it. A breached budget
  answers with silence rather than an error, so read the `coverage` block on
  every response and never the row count.
- **The gateway session dies weekly**, on Sunday around 01:00 US Eastern. The
  fix is always the person at the login page, never a retry loop.
- **Nothing in this system can place an order.** That is intentional and
  permanent.

Once it is set up, start `claude` inside that directory, approve
the `ibkr` MCP server when prompted, and ask market questions in plain
language. The `market-data`, `login` and `status` skills teach the session the
rest.
