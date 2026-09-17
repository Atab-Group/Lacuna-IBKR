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

# 5. The local market-data service
#
# The plugin reaches the service over a local address rather than by running it
# from the folder Claude started in, because a folder-relative server is invisible
# from every other folder and invisible to Cowork, which never starts in this
# clone. That means the service has to be running before the plugin is any use,
# and it has to survive this shell exiting.
#
# Every part of this step is idempotent. Re-running setup.sh must not mint a
# second token or start a second server: a second server on the same port only
# fails to bind, and a second token leaves a live credential nobody knows about.

CLONE="$(pwd)"
TOKENS_FILE="services/ibkr-data/tokens.json"
# Read the port from the code rather than repeating the number here, so a change
# to the default cannot leave setup.sh pointing at the old one.
PORT="$(.venv/bin/python -c "import sys; sys.path.insert(0, 'services/ibkr-data'); import httpserver; print(httpserver.DEFAULT_PORT)")"
HEALTH="http://127.0.0.1:${PORT}/healthz"

service_is_up() {
  curl -fsS --max-time 2 "$HEALTH" >/dev/null 2>&1
}

live_token() {
  .venv/bin/python - "$TOKENS_FILE" <<'PYTOKEN'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, ValueError):
    raise SystemExit(0)
for entry in (data.get("tokens") or []):
    if isinstance(entry, dict) and entry.get("token") and not entry.get("revoked"):
        print(entry["token"])
        break
PYTOKEN
}

echo "-- market data service"

TOKEN="$(live_token)"
if [ -z "$TOKEN" ]; then
  echo "   minting a key for this computer"
  .venv/bin/python services/ibkr-data/httpserver.py \
    --add-token "$(hostname -s 2>/dev/null || echo local)" >/dev/null
  TOKEN="$(live_token)"
  [ -n "$TOKEN" ] || { echo "could not mint a key into $TOKENS_FILE"; exit 1; }
else
  echo "   reusing the key already in $TOKENS_FILE"
fi

START_ROUTE="already running"
if service_is_up; then
  echo "   already listening on 127.0.0.1:${PORT}, leaving it alone"
else
  # systemctl --user needs a running user manager. On WSL2 that exists only when
  # /etc/wsl.conf turns systemd on, and `systemctl --user show-environment` is
  # the cheapest thing that fails when it is off, so it is the test rather than
  # sniffing for WSL and reading that file.
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    START_ROUTE="systemd user service"
    mkdir -p "$HOME/.config/systemd/user"
    sed "s#CLONE#${CLONE}#g" services/ibkr-data/deploy/ibkr-http.service \
      > "$HOME/.config/systemd/user/ibkr-http.service"
    systemctl --user daemon-reload
    systemctl --user enable --now ibkr-http.service
    echo "   started as a systemd user service (systemctl --user status ibkr-http)"
    # Without lingering the service dies at logout, which defeats the point: a
    # Cowork session runs with nobody logged in at the desktop.
    LINGER="$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)"
    if [ "$LINGER" != "yes" ]; then
      echo "   note: it stops when you log out. To keep it running:"
      echo "     sudo loginctl enable-linger $USER"
    fi
  else
    START_ROUTE="background process"
    # No systemd, so nohup with setsid, which detaches it from this shell's
    # session as well, so closing the terminal does not take it down.
    nohup setsid .venv/bin/python services/ibkr-data/httpserver.py \
      --log services/ibkr-data/http.log >/dev/null 2>&1 &
    echo $! > services/ibkr-data/http.pid
    echo "   no systemd on this machine, so it is running in the background"
    echo "   it will NOT come back by itself after a restart. Start it again with:"
    echo "     ./setup.sh"
  fi

  # Wait for the port rather than sleeping a fixed amount, because a cold import
  # of pyarrow and duckdb is seconds on a laptop and instant on a workstation.
  for _ in $(seq 1 30); do
    service_is_up && break
    sleep 0.5
  done
fi

if service_is_up; then
  echo "   answering on 127.0.0.1:${PORT} (${START_ROUTE})"
else
  echo "the market data service did not come up on 127.0.0.1:${PORT}."
  echo "look at services/ibkr-data/http.log, or: systemctl --user status ibkr-http"
  exit 1
fi

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

To use it from a shell:

  .venv/bin/python services/ibkr-data/cli.py bars AAPL --interval 1d --start 2026-08-01

Read services/ibkr-data/README.md for the traps (rate limits, the weekly
login, why empty responses are suspicious).
DONE

# The key is printed here and nowhere else in this script. It is stored in
# tokens.json and never printed again, so a lost one is replaced rather than
# recovered: mint another with
#   .venv/bin/python services/ibkr-data/httpserver.py --add-token NAME
cat <<REGISTER

To use it from every Claude session, and from Cowork, register the plugin with
the key for this computer. Run these two lines once:

  claude plugin marketplace add Atab-Group/Lacuna-IBKR
  claude plugin install ibkr@lacuna-ibkr -y --config ibkr_token=${TOKEN}

Then, from any folder at all: "what did Apple do yesterday?"
REGISTER
