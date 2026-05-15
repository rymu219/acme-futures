"""Show how close last night's bars came to triggering each fleet keeper.

Pulls 2-min MES bars via ProjectX REST (stateless — does NOT spawn a
SignalR session, so it won't kick the live runner per CLAUDE.md's
single-instance API key rule). Computes the load-bearing trigger
condition for each of the three active keepers and reports the closest
near-miss for last night's overnight window.

  boundary           closest bar-high/bar-low approach to any tracked
                     level (PDH/PDL/ONH/ONL/ORH/ORL), in ticks
  overnight_drift    body of the 15:30-16:00 CT bias bar
                     (needs 0 < body <= weak_body_threshold = 3pt)
  gap_fill           today's 08:30 CT open vs prior 16:00 CT close
                     (needs |gap| >= min_gap_points)

Usage:
  uv run python scripts/check_near_miss.py

The window is auto-selected as "the most recent overnight session":
  yesterday 15:30 CT through today 09:00 CT (covers the bias bar, the
  full overnight period, and today's opening range).
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acme.broker.projectx import ProjectXAdapter  # noqa: E402
from acme.contracts import MES, front_month_code  # noqa: E402
from acme.levels import compute_day_levels  # noqa: E402

CT = ZoneInfo("America/Chicago")

# Strategy thresholds — defaults from each strategy's @dataclass config.
# These match what the live runner uses unless overridden in Supabase.
WEAK_BODY_THRESHOLD_PT = 3.0
MIN_BODY_POINTS = 0.5
MIN_GAP_POINTS = 12.0
LEVEL_BUFFER_TICKS = 4              # boundary.level_buffer_ticks default
ENTRY_HOUR_BLACKLIST_CT = {9, 10, 11, 12, 13}


def _aggregate_to_2min(bars_1m: list) -> list:
    """Aggregate 1-min bars to 2-min bars (the strategies' native timeframe)."""
    from acme.broker.base import Bar
    bars = sorted(bars_1m, key=lambda b: b.t)
    out = []
    cur: list = []
    cur_floor: datetime | None = None
    for b in bars:
        floor = b.t.replace(second=0, microsecond=0)
        floor = floor.replace(minute=(floor.minute // 2) * 2)
        if cur_floor is None or floor != cur_floor:
            if cur:
                out.append(Bar(
                    t=cur_floor,
                    o=cur[0].o, h=max(x.h for x in cur),
                    l=min(x.l for x in cur), c=cur[-1].c,
                    v=sum(x.v for x in cur),
                ))
            cur, cur_floor = [b], floor
        else:
            cur.append(b)
    if cur and cur_floor is not None:
        out.append(Bar(
            t=cur_floor,
            o=cur[0].o, h=max(x.h for x in cur),
            l=min(x.l for x in cur), c=cur[-1].c,
            v=sum(x.v for x in cur),
        ))
    return out


def _nearest_tracked_level(price: float, levels) -> tuple[str, float, float] | None:
    """Like levels.nearest_level_distance but returns the signed delta in
    *points* alongside the ticks distance so we can tell which side."""
    candidates = {
        "PDH": levels.pdh, "PDL": levels.pdl,
        "ONH": levels.onh, "ONL": levels.onl,
        "ORH": levels.orh, "ORL": levels.orl,
    }
    valid = [(n, v) for n, v in candidates.items() if v is not None]
    if not valid:
        return None
    name, lvl = min(valid, key=lambda kv: abs(price - kv[1]))
    return name, lvl, abs(price - lvl)


async def main() -> None:
    load_dotenv(Path.home() / "acme-futures" / ".env")
    if not os.getenv("PROJECTX_API_KEY"):
        sys.exit("PROJECTX_API_KEY not set — load ~/acme-futures/.env first")

    # Window: yesterday 15:30 CT → today 09:00 CT (covers bias bar +
    # full overnight + opening range, ~17.5 hours).
    now_ct = datetime.now(UTC).astimezone(CT)
    today = now_ct.date()
    # If it's before noon CT, "today" is the active trading day. After
    # market close, look at the prior overnight.
    trade_date = today if now_ct.hour < 12 else today + timedelta(days=1)
    prior = trade_date - timedelta(days=1)
    win_start = datetime.combine(prior, datetime.min.time(), tzinfo=CT).replace(hour=15, minute=30)
    win_end   = datetime.combine(trade_date, datetime.min.time(), tzinfo=CT).replace(hour=9, minute=0)
    print(f"Window  : {win_start:%Y-%m-%d %H:%M} CT → {win_end:%Y-%m-%d %H:%M} CT  (trade_date={trade_date})")

    # Let ProjectX resolve the front-month contract id from the bare "MES".
    _ = front_month_code(trade_date)        # imported helper kept for future symbol-massaging

    async with ProjectXAdapter() as px:
        contract_id = await px.resolve_contract("MES")
        print(f"Contract: {contract_id}")
        bars_1m = await px.get_bars(
            contract_id,
            unit=2,             # 2 = minute
            unit_number=1,      # 1-min bars
            start=win_start.astimezone(UTC),
            end=win_end.astimezone(UTC),
            limit=20000,
        )

    if not bars_1m:
        print("\nNo bars returned. Check connectivity / contract id.")
        return
    bars_2m = _aggregate_to_2min(bars_1m)
    print(f"Bars    : {len(bars_1m)} 1-min  →  {len(bars_2m)} 2-min\n")

    levels = compute_day_levels(bars_2m, trade_date)
    print(f"LEVELS  ({trade_date}):")
    for name in ("pdh", "pdl", "onh", "onl", "orh", "orl"):
        v = getattr(levels, name)
        print(f"  {name.upper():4s} = {v}")
    print()

    # ── BOUNDARY ─────────────────────────────────────────────────────
    # Closest bar-extreme approach to any tracked level during the
    # active overnight entry window (17-23 + 03-08 CT, excludes 09-13).
    print("BOUNDARY — closest level approach (overnight window)")
    print("─" * 64)
    nearest_hits = []
    for b in bars_2m:
        bar_ct = b.t.astimezone(CT)
        hr = bar_ct.hour
        # Only score during BOUNDARY's active window (per blacklist).
        if hr in ENTRY_HOUR_BLACKLIST_CT:
            continue
        # Check both extremes.
        for price, side in ((b.h, "high"), (b.l, "low")):
            r = _nearest_tracked_level(price, levels)
            if r is None:
                continue
            name, lvl, dist_pts = r
            ticks = dist_pts / MES.tick_size
            nearest_hits.append((ticks, bar_ct, name, lvl, price, side))
    nearest_hits.sort()
    if not nearest_hits:
        print("  (no bars in the active window with levels defined)")
    else:
        for ticks, bar_ct, name, lvl, price, side in nearest_hits[:5]:
            crossed = "✓ CROSSED" if ticks <= LEVEL_BUFFER_TICKS else " "
            print(f"  {bar_ct:%m-%d %H:%M CT}  bar.{side}={price:>9.2f}  "
                  f"vs {name}={lvl:>9.2f}  → {ticks:5.1f} ticks  {crossed}")
        triggered = sum(1 for t, *_ in nearest_hits if t <= LEVEL_BUFFER_TICKS)
        print(f"\n  Trigger threshold: bar.high/low within {LEVEL_BUFFER_TICKS} ticks of a level")
        print(f"  Bars within trigger range: {triggered}")
        if triggered == 0:
            best = nearest_hits[0][0]
            print(f"  Closest miss: {best:.1f} ticks "
                  f"(would need to be within {LEVEL_BUFFER_TICKS})")
    print()

    # ── OVERNIGHT_DRIFT ──────────────────────────────────────────────
    # The 15:30-16:00 CT bias bar must be weak-bullish: 0 < body <= 3pt.
    print("OVERNIGHT_DRIFT — bias bar body (15:30-16:00 CT yesterday)")
    print("─" * 64)
    bias_open: float | None = None
    bias_close: float | None = None
    for b in bars_2m:
        bar_ct = b.t.astimezone(CT)
        if bar_ct.date() != prior:
            continue
        if not (bar_ct.hour == 15 and 30 <= bar_ct.minute <= 58):
            continue
        if bias_open is None:
            bias_open = b.o
        bias_close = b.c
    if bias_open is None or bias_close is None:
        print("  (no 15:30-16:00 CT bars found)")
    else:
        body = bias_close - bias_open
        print(f"  Open  (15:30)   : {bias_open:.2f}")
        print(f"  Close (15:58)   : {bias_close:.2f}")
        print(f"  Body            : {body:+.2f} pt")
        print(f"  Trigger band    : {MIN_BODY_POINTS:.1f} < body <= "
              f"{WEAK_BODY_THRESHOLD_PT:.1f} (weak bullish)")
        if body <= 0:
            print("  Verdict         : NO TRIGGER (bearish or doji — needed weak bullish)")
        elif body < MIN_BODY_POINTS:
            print(f"  Verdict         : NO TRIGGER (body too small by "
                  f"{MIN_BODY_POINTS - body:.2f} pt)")
        elif body > WEAK_BODY_THRESHOLD_PT:
            print(f"  Verdict         : NO TRIGGER (body too strong by "
                  f"{body - WEAK_BODY_THRESHOLD_PT:.2f} pt — bias bar was conviction-bullish)")
        else:
            print("  Verdict         : WOULD TRIGGER at 17:00 CT")
    print()

    # ── GAP_FILL ─────────────────────────────────────────────────────
    # Gap = today's first bar after 08:30 CT open vs prior day's 16:00 CT close.
    print("GAP_FILL — open gap vs prior close")
    print("─" * 64)
    prior_close: float | None = None
    today_open: float | None = None
    today_open_ts: datetime | None = None
    for b in bars_2m:
        bar_ct = b.t.astimezone(CT)
        if bar_ct.date() == prior and bar_ct.hour == 15 and bar_ct.minute >= 58:
            prior_close = b.c
        if (bar_ct.date() == trade_date and bar_ct.hour == 8
                and bar_ct.minute >= 30 and today_open is None):
            today_open = b.o
            today_open_ts = bar_ct
    if prior_close is None or today_open is None:
        print("  (insufficient bars — need prior 16:00 close and today 08:30 open)")
        if prior_close is None:
            print("    missing: prior day 15:58 close")
        if today_open is None:
            print(f"    missing: today {trade_date} 08:30 CT open "
                  f"(now={now_ct:%H:%M} CT — may not have happened yet)")
    else:
        gap = today_open - prior_close
        print(f"  Prior close (16:00) : {prior_close:.2f}")
        print(f"  Today open ({today_open_ts:%H:%M})    : {today_open:.2f}")
        print(f"  Gap                 : {gap:+.2f} pt")
        print(f"  Trigger threshold   : |gap| >= {MIN_GAP_POINTS:.1f} pt")
        if abs(gap) < MIN_GAP_POINTS:
            print(f"  Verdict             : NO TRIGGER (gap short by "
                  f"{MIN_GAP_POINTS - abs(gap):.2f} pt)")
        else:
            direction = "UP" if gap > 0 else "DOWN"
            print(f"  Verdict             : QUALIFYING GAP {direction} — strategy "
                  f"will look to fade once price retraces toward prior close")


if __name__ == "__main__":
    asyncio.run(main())
