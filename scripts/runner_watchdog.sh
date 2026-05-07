#!/bin/bash
# runner_watchdog.sh — wrap acme.runner in an outer loop that restarts
# the python process whenever it falls into the signalrcore zombie state
# (process alive but spamming "Connection closed" with no real activity).
#
# Symptom we detect: in the last 100 log lines, > 50 "Connection closed"
# entries AND < 3 v3_runtime_* entries. That signature only appears
# after the SignalR SDK has lost its socket and can't recover.
#
# Usage (from the project root):
#     nohup ./scripts/runner_watchdog.sh > logs/watchdog.out 2>&1 &
#
# To stop the loop entirely:
#     pkill -f runner_watchdog.sh   # kills this loop
#     pkill -f acme.runner          # kills the runner it spawned
set -u

cd "$(dirname "$0")/.." || exit 1

RUNNER_LOG="logs/runner.out.log"
WATCH_LOG="logs/watchdog.log"
CHECK_INTERVAL_S=60         # poll cadence
ZOMBIE_CLOSED_LINES=50      # > this many "Connection closed" in last 100 lines
ZOMBIE_REQUIRES_NO_ACTIVITY=3  # AND fewer than this many v3_runtime_ lines
HEARTBEAT_PROBE="scripts/check_runner_heartbeat.py"

mkdir -p logs

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$WATCH_LOG"; }

while true; do
    log "spawning runner"
    nohup caffeinate -i uv run python -m acme.runner --dry-run \
        >> "$RUNNER_LOG" 2>&1 &
    RUNNER_PID=$!
    log "runner spawned with caffeinate PID $RUNNER_PID"

    grace_until=$(($(date +%s) + 180))  # 3 min warmup before we check heartbeats
    consecutive_stale=0

    while kill -0 "$RUNNER_PID" 2>/dev/null; do
        sleep "$CHECK_INTERVAL_S"

        # 1) Signalrcore zombie pattern in the log
        if [ -s "$RUNNER_LOG" ]; then
            recent=$(tail -100 "$RUNNER_LOG")
            closed=$(echo "$recent" | grep -c "Connection closed" || true)
            activity=$(echo "$recent" | grep -c "v3_runtime_" || true)
            if [ "$closed" -gt "$ZOMBIE_CLOSED_LINES" ] && \
               [ "$activity" -lt "$ZOMBIE_REQUIRES_NO_ACTIVITY" ]; then
                log "ZOMBIE-A signalr (closed=$closed activity=$activity); killing"
                pkill -9 -P "$RUNNER_PID" 2>/dev/null || true
                kill -9 "$RUNNER_PID" 2>/dev/null || true
                sleep 2
                break
            fi
        fi

        # 2) Heartbeat staleness via Supabase (catches Mac-sleep / generic hang
        #    where the log isn't spamming but no bars are being processed).
        if [ "$(date +%s)" -gt "$grace_until" ]; then
            probe_out=$(uv run python "$HEARTBEAT_PROBE" 2>&1)
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
                # rc=2 means transient (config / supabase blip). Don't kill.
                log "heartbeat probe transient (rc=$probe_rc): $probe_out"
            fi
        fi
    done

    log "runner exited (PID $RUNNER_PID); restarting in 5s"
    sleep 5
done
