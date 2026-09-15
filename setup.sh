#!/usr/bin/env bash
# One-time setup for the IBKR market-data plugin bundle.
# Run from the unpacked bundle directory:  ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== IBKR market-data plugin setup =="

# 1. Prerequisites
command -v docker >/dev/null || { echo "docker is required (the IBKR gateway runs in a container). Install it and re-run."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose v2 is required. Install it and re-run."; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required."; exit 1; }
python3 - <<'EOF' || { echo "python 3.10+ is required."; exit 1; }
import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
EOF

# 2. Virtualenv with the pinned dependencies
if [ ! -x .venv/bin/python ]; then
  echo "-- creating .venv"
  python3 -m venv .venv
fi
echo "-- installing dependencies"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# 3. Directories the service expects
mkdir -p shared/data/ibkr .claude/skills

# 4. Skills for Claude Code sessions started in this directory
ln -sfn ../../ibkr-plugin/skills/market-data .claude/skills/ibkr-market-data
ln -sfn ../../ibkr-plugin/skills/login       .claude/skills/ibkr-login
ln -sfn ../../ibkr-plugin/skills/status      .claude/skills/ibkr-status
ln -sfn ../../ibkr-plugin/skills/setup       .claude/skills/ibkr-setup

# 5. Sanity: run the offline test suite
echo "-- running the test suite (nothing touches IBKR)"
.venv/bin/pip install -q pytest
.venv/bin/python -m pytest services/ibkr-data/tests -q

cat <<'DONE'

== Setup complete ==

You need your OWN Interactive Brokers username. One API session exists per
username, and the 2FA prompt goes to the username owner's authenticator, so
sharing someone else's login cannot work. A read-only secondary username on
an existing account is ideal; the account's primary user creates it under
Settings, then User Access Rights.

To log in and start pulling data:

  1.  .venv/bin/python services/ibkr-data/webui.py
  2.  Open http://127.0.0.1:8642 in your browser.
  3.  Enter your IBKR username and password, then the 6 digit code from
      your authenticator app when the page asks (about 40s later).
      Credentials stay in services/ibkr-data/deploy/.env on this machine,
      owner-readable only. The login repeats weekly (Sunday reset).

To use it from Claude Code, start `claude` in this directory. The ibkr MCP
server and three ibkr skills load automatically; approve the server when
prompted. Try: "what did AAPL do yesterday?"

To use it from a shell:

  .venv/bin/python services/ibkr-data/cli.py bars AAPL --interval 1d --start 2026-08-01

Read services/ibkr-data/README.md for the traps (rate limits, the weekly
login, why empty responses are suspicious).
DONE
