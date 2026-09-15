#!/usr/bin/env bash
# Resolve the watchlist, then backfill daily bars for it.
#
# It waits for the gateway session rather than assuming one. The 23:59 auto
# restart can leave the gateway sitting on a login dialog that only a human can
# clear, and this script is normally started detached, so it probes every five
# minutes for up to twelve hours and then gives up. It never attempts a login:
# IBKR throttles and then locks an account after a handful of tries.
#
# Both stages are ledger driven, so running this again after a kill costs only
# what is genuinely missing.
#
#   nohup ./services/ibkr-data/backfill_run.sh > services/ibkr-data/backfill.log 2>&1 &
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
py="$repo/.venv/bin/python"
start="${1:-2019-01-01}"

cd "$repo" || exit 1

for attempt in $(seq 1 144); do
    if "$py" "$here/check_connection.py" >/dev/null 2>&1; then
        echo "gateway answered at $(date -Is), attempt $attempt"
        "$py" "$here/cli.py" --client-id 221 universe --resolve || exit 1
        echo
        exec "$py" "$here/cli.py" --client-id 221 backfill --start "$start"
    fi
    echo "$(date -Is) no API session yet (attempt $attempt of 144). A human logs in at http://127.0.0.1:8642"
    sleep 300
done

echo "gave up after twelve hours without a session"
exit 1
