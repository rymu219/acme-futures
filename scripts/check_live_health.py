"""Pre-overnight health check for the live runner pipeline.

Single-shot read-only probe — no broker calls, no writes. Verifies the
chain end-to-end so you can go to bed confident the runner will fire
overnight. Six sections:

  1. Broker session    — is the runner authed? Latest account_snapshot age
                         and balance from the conductor's _snapshot_loop.
  2. Heartbeats        — per-strategy bar processing freshness from
                         runtime_heartbeats. Same data the watchdog uses,
                         broken out per strategy with position state.
  3. Active fleet      — which strategies the runner loaded this generation.
  4. Signal activity   — dry_run_signal + dry_run_close counts, last 24h.
                         Confirms entry/exit logic is running, not just
                         heartbeats spinning silently.
  5. Suppression       — risk_block / calendar_block counts. A sudden
                         storm here means signals are being generated but
                         blocked (eval profile breach, daily-loss-limit
                         hit, etc).
  6. Verdict           — GO / WAIT / NOT-READY summary.

Usage:
    cd ~/acme-futures
    uv run python scripts/check_live_health.py
    uv run python scripts/check_live_health.py --hours 12
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Cheap .env loader so this works without `uv run` shenanigans too.
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from supabase import create_client  # noqa: E402

CT = ZoneInfo("America/Chicago")

# Thresholds
SNAPSHOT_FRESH_SEC = 120       # green if <2 min, yellow <10 min, red >10
SNAPSHOT_OK_SEC = 600
HEARTBEAT_FRESH_SEC = 300      # matches scripts/check_runner_heartbeat.py default

# Expected fleet — kept in sync with fleet_runner._build_keeper_instances().
EXPECTED_STRATEGIES = {"boundary", "overnight_drift", "gap_fill"}

# ANSI colors. Disable if not a TTY (avoids garbled logs).
_USE_COLOR = sys.stdout.isatty()
def _c(code: str, s: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if _USE_COLOR else s
GREEN  = lambda s: _c("32", s)   # noqa: E731
YELLOW = lambda s: _c("33", s)   # noqa: E731
RED    = lambda s: _c("31", s)   # noqa: E731
DIM    = lambda s: _c("2", s)    # noqa: E731


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 60: return f"{s}s"
    if s < 3600: return f"{s // 60}m"
    if s < 86400: return f"{s // 3600}h"
    return f"{s // 86400}d"


def _parse_ts(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _print_header(label: str) -> None:
    print(f"\n{label}")
    print("─" * 60)


# ───────────────────────── checks ─────────────────────────


def check_broker_session(sb, now: datetime) -> tuple[str, dict]:
    """Latest account_snapshot from broker_events. Returns (verdict, info).
    Verdict: 'go' / 'wait' / 'fail'."""
    res = (sb.table("broker_events")
           .select("occurred_at, raw")
           .eq("kind", "account_snapshot")
           .order("occurred_at", desc=True)
           .limit(1)
           .execute())
    rows = res.data or []
    if not rows:
        print(RED("  ✗ no account_snapshot events found at all — runner has never authed"))
        return "fail", {}
    row = rows[0]
    ts = _parse_ts(row.get("occurred_at"))
    if ts is None:
        print(RED("  ✗ latest account_snapshot has unparseable timestamp"))
        return "fail", {}
    age = (now - ts).total_seconds()
    raw = row.get("raw") or {}
    bal = raw.get("balance")
    can_trade = raw.get("can_trade", "?")
    net_pos = raw.get("net_position", "?")
    if age <= SNAPSHOT_FRESH_SEC:
        verdict, color = "go", GREEN
    elif age <= SNAPSHOT_OK_SEC:
        verdict, color = "wait", YELLOW
    else:
        verdict, color = "fail", RED
    print(f"  {color('●')} last snapshot {_ago(age)} ago  "
          f"bal=${bal:,.2f}  can_trade={can_trade}  net_pos={net_pos}"
          if isinstance(bal, (int, float)) else
          f"  {color('●')} last snapshot {_ago(age)} ago  bal={bal}  "
          f"can_trade={can_trade}  net_pos={net_pos}")
    return verdict, {"age_s": age, "balance": bal}


def check_heartbeats(sb, now: datetime) -> tuple[str, set[str]]:
    """Per-strategy heartbeat freshness from runtime_heartbeats.

    Returns (verdict, set of fresh strategy names).

    Only fails the verdict on stale CURRENT-fleet strategies. Rows for
    retired strategies (e.g. the v3 runtime's `session`/`regime`/`ignition`,
    or any `v3-*`/`v4-*` variant) sit in the table forever after their
    runner generation stops. They're displayed for visibility but treated
    as informational, not failure conditions.
    """
    res = sb.table("runtime_heartbeats").select("*").execute()
    rows = res.data or []
    if not rows:
        print(RED("  ✗ runtime_heartbeats table empty — runner has never written a heartbeat"))
        return "fail", set()
    fresh: set[str] = set()
    by_service = sorted(rows, key=lambda r: r.get("service") or "")
    keeper_stale = False
    for hb in by_service:
        service = hb.get("service") or "?"
        ts = _parse_ts(hb.get("ts"))
        if ts is None:
            print(f"  {DIM('●')} {service:<18} no/bad timestamp")
            if service in EXPECTED_STRATEGIES:
                keeper_stale = True
            continue
        age = (now - ts).total_seconds()
        pos_state = hb.get("position_state") or "?"
        auth_ok = hb.get("auth_ok", "?")
        errs = hb.get("consecutive_errors", "?")
        is_keeper = service in EXPECTED_STRATEGIES
        if age <= HEARTBEAT_FRESH_SEC:
            fresh.add(service)
            color = GREEN
        elif is_keeper:
            color = RED
            keeper_stale = True
        else:
            # Retired strategy with stale heartbeat — informational only.
            color = DIM
        print(f"  {color('●')} {service:<18} hb {_ago(age):>5} ago  "
              f"pos={pos_state:<5} auth_ok={auth_ok} errs={errs}"
              + ("" if is_keeper else DIM("  (retired — informational)")))
    return ("fail" if keeper_stale else "go"), fresh


def check_active_fleet(fresh_services: set[str]) -> str:
    """Cross-check fresh-heartbeat services against the expected fleet."""
    missing = EXPECTED_STRATEGIES - fresh_services
    extra = fresh_services - EXPECTED_STRATEGIES
    if missing:
        print(RED(f"  ✗ missing keeper(s): {sorted(missing)}  "
                  "(expected boundary + overnight_drift + gap_fill)"))
    else:
        print(GREEN(f"  ✓ all keepers fresh: {sorted(EXPECTED_STRATEGIES)}"))
    if extra:
        # Extra services aren't necessarily bad — could be legacy v3 services
        # still receiving heartbeats from a stale source. Surface but don't fail.
        print(DIM(f"    (also fresh: {sorted(extra)})"))
    return "fail" if missing else "go"


def check_signal_activity(sb, now: datetime, hours: int) -> str:
    """Counts of dry_run_signal + dry_run_close per strategy in the
    last `hours`. Counts of zero are informational, not necessarily a
    failure — markets may have been quiet — but a long stretch of
    zero on a per-strategy basis combined with fresh heartbeats means
    the entry logic isn't generating signals (worth noticing)."""
    cutoff = (now - timedelta(hours=hours)).isoformat()
    res = (sb.table("broker_events")
           .select("kind, strategy")
           .gte("occurred_at", cutoff)
           .in_("kind", ["dry_run_signal", "dry_run_close"])
           .execute())
    rows = res.data or []
    sig_by_strat: Counter = Counter()
    close_by_strat: Counter = Counter()
    for r in rows:
        if r.get("kind") == "dry_run_signal":
            sig_by_strat[r.get("strategy") or "?"] += 1
        elif r.get("kind") == "dry_run_close":
            close_by_strat[r.get("strategy") or "?"] += 1
    if not sig_by_strat and not close_by_strat:
        print(YELLOW(f"  ○ zero signals + zero closes in last {hours}h"))
        print(DIM(f"    not a failure — market might have been quiet — but worth noting"))
        return "wait"
    for strat in sorted(set(sig_by_strat) | set(close_by_strat) | EXPECTED_STRATEGIES):
        sigs = sig_by_strat.get(strat, 0)
        closes = close_by_strat.get(strat, 0)
        marker = GREEN("●") if (sigs or closes) else DIM("○")
        print(f"  {marker} {strat:<18} signals={sigs:>3}  closes={closes:>3}  "
              f"(last {hours}h)")
    return "go"


def check_suppression(sb, now: datetime, hours: int) -> str:
    """Counts of risk_block + calendar_block in the last `hours`.
    Some of these are normal (calendar blocks fire daily for after-hours
    bars). A sudden storm — e.g. >50 risk_blocks in an hour — usually
    means signals are being generated but the eval-profile or daily-loss
    gates are killing them."""
    cutoff = (now - timedelta(hours=hours)).isoformat()
    res = (sb.table("broker_events")
           .select("kind, raw, occurred_at")
           .gte("occurred_at", cutoff)
           .in_("kind", ["risk_block", "calendar_block"])
           .execute())
    rows = res.data or []
    by_kind: Counter = Counter(r.get("kind") for r in rows)
    if not rows:
        print(GREEN(f"  ✓ zero blocks in last {hours}h"))
        return "go"
    for kind, n in sorted(by_kind.items()):
        marker = YELLOW("●") if n > 50 else DIM("○")
        print(f"  {marker} {kind:<18} {n:>4}  (last {hours}h)")
    # A reasonable amount of calendar_block is normal — only flag risk_blocks
    # as a yellow alert.
    return "wait" if by_kind.get("risk_block", 0) > 50 else "go"


def print_verdict(parts: dict[str, str], now_ct: datetime) -> int:
    """Roll up section verdicts into an exit code (0 go, 1 wait, 2 fail)."""
    _print_header("Verdict")
    fail = [k for k, v in parts.items() if v == "fail"]
    wait = [k for k, v in parts.items() if v == "wait"]
    print(f"  Now: {now_ct.strftime('%a %Y-%m-%d %H:%M:%S CT')}")
    if fail:
        print(RED(f"  NOT READY — failing: {fail}"))
        return 2
    if wait:
        print(YELLOW(f"  CAUTION — yellow signals on: {wait}"))
        return 1
    print(GREEN("  GO — all sections green. Runner is wired and emitting."))
    return 0


# ───────────────────────── main ───────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hours", type=int, default=24,
                   help="Lookback window for signal/suppression activity. "
                        "Default 24h. Use 12 to focus on the most recent session.")
    args = p.parse_args()

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print(RED("error: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set"),
              file=sys.stderr)
        return 2
    sb = create_client(url, key)
    now = datetime.now(UTC)
    now_ct = now.astimezone(CT)

    parts: dict[str, str] = {}

    _print_header("1. Broker session  (account_snapshot freshness)")
    parts["broker"], _ = check_broker_session(sb, now)

    _print_header("2. Heartbeats  (runtime_heartbeats per strategy)")
    parts["heartbeats"], fresh = check_heartbeats(sb, now)

    _print_header("3. Active fleet  (vs expected keepers)")
    parts["fleet"] = check_active_fleet(fresh)

    _print_header(f"4. Signal activity  (last {args.hours}h)")
    parts["signals"] = check_signal_activity(sb, now, args.hours)

    _print_header(f"5. Suppression  (risk_block / calendar_block, last {args.hours}h)")
    parts["suppression"] = check_suppression(sb, now, args.hours)

    return print_verdict(parts, now_ct)


if __name__ == "__main__":
    raise SystemExit(main())
