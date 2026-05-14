#!/bin/bash
# fleet_runner_watchdog.sh — wraps `python -m acme.fleet_runner` in an
# outer loop. Same two-layer supervision pattern as runner_watchdog.sh
# (which wraps the deprecated v3 multi-variant runner).
#
# Detects two zombie patterns:
#   A. SignalR zombie — >50 "Connection closed" lines in last 100 log
#      lines AND <3 fleet_runner_ activity lines.
#   B. Heartbeat staleness — Supabase heartbeats older than threshold
#      via scripts/check_runner_heartbeat.py.
#
# Heartbeat probe filters by the v\d-prefix regex it already uses; the
# new fleet uses strategy names (ignition / session / regime / boundary)
# rather than v\d- service names, so the probe needs the env override
# below to point at the new fleet's service rows.

set -u

cd "$(dirname "$0")/.." || exit 1

RUNNER_LOG="logs/fleet_runner.out.log"
WATCH_LOG="logs/fleet_watchdog.log"
CHECK_INTERVAL_S=60
ZOMBIE_CLOSED_LINES=50
ZOMBIE_REQUIRES_NO_ACTIVITY=3
HEARTBEAT_PROBE="scripts/check_runner_heartbeat.py"

mkdir -p logs

# Load .env (if present) so ACME_LIVE and other user-controlled toggles
# reach this script. Done with `set -a / +a` so each variable defined in
# .env is automatically exported into the child runner's environment.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . .env
    set +a
fi

# Live-mode flag — defaults to dry-run for safety. Set ACME_LIVE=true in
# .env to flip to real-broker execution. The strategies must additionally
# be in PILOT or LIVE state in the registry; SHADOW strategies always go
# phantom regardless of this flag.
if [ "${ACME_LIVE:-false}" = "true" ]; then
    RUNNER_FLAGS=""
    MODE_LABEL="LIVE"
else
    RUNNER_FLAGS="--dry-run"
    MODE_LABEL="dry-run"
fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$WATCH_LOG"; }

while true; do
    log "spawning fleet_runner mode=$MODE_LABEL"
    # PYTHONUNBUFFERED=1 — same rationale as runner_watchdog.sh (line buffering
    # so tail -f stays responsive when stdout is redirected).
    # $RUNNER_FLAGS is intentionally unquoted so an empty value expands to
    # zero args (no --dry-run); a literal "--dry-run" expands to one arg.
    # shellcheck disable=SC2086
    PYTHONUNBUFFERED=1 nohup caffeinate -i uv run python -m acme.fleet_runner $RUNNER_FLAGS \
        >> "$RUNNER_LOG" 2>&1 &
    RUNNER_PID=$!
    log "fleet_runner spawned with caffeinate PID $RUNNER_PID"

    # Grace period before heartbeat-staleness checks start. The new
    # runner doesn't write its first heartbeat until the first 2-min
    # bar completes, which can take up to ~3 min after spawn. Meanwhile
    # the previous runner's heartbeats are aging out, so the probe sees
    # only-stale rows and would false-positive kill. 360s gives the
    # runner enough breathing room to write its first heartbeat before
    # we start probing (2026-05-12 incident: 12-min false-kill cycle).
    grace_until=$(($(date +%s) + 360))
    consecutive_stale=0

    while kill -0 "$RUNNER_PID" 2>/dev/null; do
        sleep "$CHECK_INTERVAL_S"

        # 1) Signalrcore zombie pattern in the log
        if [ -s "$RUNNER_LOG" ]; then
            recent=$(tail -100 "$RUNNER_LOG")
            closed=$(echo "$recent" | grep -c "Connection closed" || true)
            # The classic conductor logs lines tagged "conductor_*" — use those
            # plus "fleet_runner_" for activity detection.
            activity=$(echo "$recent" | grep -cE "conductor_|fleet_runner_" || true)
            if [ "$closed" -gt "$ZOMBIE_CLOSED_LINES" ] && \
               [ "$activity" -lt "$ZOMBIE_REQUIRES_NO_ACTIVITY" ]; then
                log "ZOMBIE-A signalr (closed=$closed activity=$activity); killing"
                pkill -9 -P "$RUNNER_PID" 2>/dev/null || true
                kill -9 "$RUNNER_PID" 2>/dev/null || true
                # Defense in depth — kill ANY surviving python subprocess
                # in the acme.fleet_runner chain. The watchdog's $! is
                # caffeinate's PID; uv run + python subprocesses can
                # outlive a kill of the caffeinate wrapper, accumulating
                # zombie runners that steal the SignalR session
                # (see 2026-05-11 incident: 4 orphan runners → broker
                # session hogged → bars never completed → heartbeats
                # silent → false-positive zombie kill).
                pkill -9 -f "acme.fleet_runner" 2>/dev/null || true
                sleep 2
                break
            fi
        fi

        # 2) Heartbeat staleness probe. The probe's default service regex is
        #    `^v\d+(\.\d+)?-` (v3-canon, v4-vol-regime, etc). The new fleet's
        #    services are named after strategy classes — set
        #    HEARTBEAT_SERVICE_REGEX so the probe looks at the right rows.
        #    Keep this list in sync with fleet_runner._build_keeper_instances().
        if [ "$(date +%s)" -gt "$grace_until" ]; then
            probe_out=$(HEARTBEAT_SERVICE_REGEX='^(boundary|overnight_drift|gap_fill)$' \
                uv run python "$HEARTBEAT_PROBE" 2>&1)
            probe_rc=$?
            if [ "$probe_rc" -eq 1 ]; then
                consecutive_stale=$((consecutive_stale + 1))
                log "heartbeat probe STALE ($consecutive_stale): $probe_out"
                if [ "$consecutive_stale" -ge 2 ]; then
                    log "ZOMBIE-B heartbeat stale 2x; killing PID $RUNNER_PID"
                    pkill -9 -P "$RUNNER_PID" 2>/dev/null || true
                    kill -9 "$RUNNER_PID" 2>/dev/null || true
                    sleep 2
                    break
                fi
            elif [ "$probe_rc" -eq 0 ]; then
                consecutive_stale=0
            else
                log "heartbeat probe transient (rc=$probe_rc): $probe_out"
            fi
        fi
    done

    log "fleet_runner exited (PID $RUNNER_PID); restarting in 5s"
    sleep 5
done
