# Acme Futures launchd setup

Two daemons, both auto-started on login, both auto-restarted on crash:

- **`com.acme-futures.runner`** — the trading bot (dry-run by default)
- **`com.acme-futures.streamlit`** — the local Streamlit dashboard (Backtest / Inspector / Regime tabs)

# Runner

Auto-starts the runner on login and restarts it if it crashes. Survives Mac reboots.

## Install (one-time)

```bash
# 1. Copy the plist to your LaunchAgents directory
cp "/Users/ryanmurphy/Desktop/Acme Futures/launchd/com.acme-futures.runner.plist" \
   ~/Library/LaunchAgents/

# 2. Stop any runner you have running by hand (Ctrl-C in its terminal)

# 3. Load the agent — starts it now AND on every future login
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.acme-futures.runner.plist

# 4. Verify it's running
launchctl list | grep acme-futures
# Should show:  -  0  com.acme-futures.runner
```

## What it does

- Runs `caffeinate -dimsu uv run python -m acme.runner --dry-run` from the project directory
- Restarts automatically if the process crashes (with a 60s throttle to prevent crash loops)
- Logs stdout to `logs/runner.out.log`, stderr to `logs/runner.err.log`
- Caffeinate keeps the Mac awake (`-dimsu` = no display sleep, no idle sleep, no disk sleep, system stays awake, simulates user activity)

## Useful commands

```bash
# Tail the live logs
tail -f "/Users/ryanmurphy/Desktop/Acme Futures/logs/runner.out.log"

# Stop the runner (won't restart until you bootstrap again or reboot)
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.acme-futures.runner.plist

# Stop AND restart (after editing the plist)
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.acme-futures.runner.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.acme-futures.runner.plist

# Check if it's currently running (PID column will show a number if alive)
launchctl list com.acme-futures.runner
```

## Going live (when you're ready)

Edit the plist and remove the `<string>--dry-run</string>` line in `ProgramArguments`:

```xml
        <string>acme.runner</string>
        <!-- delete the next line to enable real orders -->
        <string>--dry-run</string>
```

Then reload:

```bash
cp "/Users/ryanmurphy/Desktop/Acme Futures/launchd/com.acme-futures.runner.plist" \
   ~/Library/LaunchAgents/
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.acme-futures.runner.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.acme-futures.runner.plist
```

## Uninstall

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.acme-futures.runner.plist
rm ~/Library/LaunchAgents/com.acme-futures.runner.plist
```

---

# Streamlit

Same caffeinate-keep-alive treatment as the runner. Streamlit listens on `0.0.0.0:8501` so you can hit it from this Mac at `http://localhost:8501` and from your phone on the same wifi at `http://<mac-lan-ip>:8501`. No auth — local/LAN only.

## Install (one-time)

```bash
# 1. Copy the plist
cp "/Users/ryanmurphy/Desktop/Acme Futures/launchd/com.acme-futures.streamlit.plist" \
   ~/Library/LaunchAgents/

# 2. If you have a Streamlit running by hand, kill it (Ctrl-C in its terminal)

# 3. Load it
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.acme-futures.streamlit.plist

# 4. Verify
launchctl list | grep acme-futures
# Two rows: runner + streamlit

# 5. Find your Mac's LAN IP for phone access
ipconfig getifaddr en0
# Then on phone: http://<that-ip>:8501
```

## What it does

- Runs `caffeinate -dimsu uv run streamlit run web/backtest_ui.py --server.port=8501 --server.address=0.0.0.0 --server.headless=true`
- Headless mode prevents trying to open a browser on the Mac mini
- Logs stdout to `logs/streamlit.out.log`, stderr to `logs/streamlit.err.log`
- 60s throttle between restarts (same as runner)

## Useful commands

```bash
# Tail logs
tail -f "/Users/ryanmurphy/Desktop/Acme Futures/logs/streamlit.out.log"

# Force restart (e.g., after editing the plist or pulling new code)
launchctl kickstart -k "gui/$(id -u)/com.acme-futures.streamlit"

# Stop without auto-restart
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.acme-futures.streamlit.plist

# Reload after editing the plist
cp "/Users/ryanmurphy/Desktop/Acme Futures/launchd/com.acme-futures.streamlit.plist" \
   ~/Library/LaunchAgents/
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.acme-futures.streamlit.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.acme-futures.streamlit.plist
```

## Uninstall

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.acme-futures.streamlit.plist
rm ~/Library/LaunchAgents/com.acme-futures.streamlit.plist
```

## Note on code updates

The plist runs from `/Users/ryanmurphy/Desktop/Acme Futures` (master branch). Code changes in worktree branches won't take effect until merged to master and the streamlit process is kicked: `launchctl kickstart -k "gui/$(id -u)/com.acme-futures.streamlit"`.
