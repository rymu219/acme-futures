# Acme Futures runner — launchd setup

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
