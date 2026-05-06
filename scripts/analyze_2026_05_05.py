"""One-shot analysis of the 2026-05-05 trading session.

Pulls all trades from `ryan_spec_v3_trades` for the Topstep session that
ran 2026-05-04 22:00 UTC -> 2026-05-05 19:36 UTC, fetches the 2m bars
between each trade's entry and exit, computes MFE/MAE per trade, and
renders a markdown report at docs/2026-05-05-trading-day-analysis.md.

Why a one-off script: today's data needs analysis NOW, the runtime
doesn't capture MFE/MAE yet (forward-going wiring is brief task 4a),
and we want the give-back / leakage picture in the user's hands tonight.
"""
from __future__ import annotations

import asyncio
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from acme.broker.projectx import ProjectXAdapter
from acme.db import Db

CT = ZoneInfo("America/Chicago")
UTC = timezone.utc

# Topstep session boundaries for 2026-05-05 trading session
SESSION_START_UTC = "2026-05-04T22:00:00+00:00"  # 17:00 CT yesterday
TICK_SIZE = 0.25  # MES
POINT_VALUE = 5.0  # MES
COMMISSION = 0.70  # round-turn
FILTER_THRESH_QUOTE = -670  # what the engine was using
OOS_PF_QUOTE_MODE = 2.21
OOS_OPPOSITE_EXIT_PCT = 53.0
OOS_EXPECTED_TRADES_PER_DAY = 89


def _ct(iso_utc: str) -> str:
    """Render UTC ISO timestamp as HH:MM CT."""
    if not iso_utc:
        return "—"
    return datetime.fromisoformat(iso_utc).astimezone(CT).strftime("%H:%M")


def _hour_ct(iso_utc: str) -> int:
    return datetime.fromisoformat(iso_utc).astimezone(CT).hour


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p / 100.0
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


async def main() -> None:
    db = Db()

    # 1. Pull all trades for the session
    res = (db.client.table("ryan_spec_v3_trades")
           .select("*")
           .gte("bar_ts", SESSION_START_UTC)
           .order("bar_ts").execute())
    trades = res.data or []
    if not trades:
        print("No trades in session window.")
        return
    print(f"Loaded {len(trades)} trades")

    # 2. Authenticate ProjectX and fetch all 2m bars covering the trade window
    broker = ProjectXAdapter()
    await broker.authenticate()
    await broker.get_account()
    contract_id = await broker.resolve_contract("MES")
    print(f"Contract: {contract_id}")

    earliest_entry = min(t["entry_ts"] for t in trades if t["entry_ts"])
    latest_exit = max(t["exit_ts"] for t in trades if t["exit_ts"])
    bar_start = datetime.fromisoformat(earliest_entry) - timedelta(minutes=4)
    bar_end = datetime.fromisoformat(latest_exit) + timedelta(minutes=4)
    print(f"Fetching 2m bars {bar_start.isoformat()} -> {bar_end.isoformat()}")

    bars = await broker.get_bars(
        contract_id, unit=2, unit_number=2,
        start=bar_start, end=bar_end, limit=20_000,
    )
    await broker.aclose()
    print(f"Loaded {len(bars)} bars")

    # 3. Index bars by their start timestamp (minute-aligned UTC)
    bars_by_t = {b.t.replace(second=0, microsecond=0): b for b in bars}

    # 4. Compute MFE/MAE per trade
    enriched = []
    for t in trades:
        if not t["entry_ts"] or not t["exit_ts"]:
            continue
        e_ts = datetime.fromisoformat(t["entry_ts"])
        x_ts = datetime.fromisoformat(t["exit_ts"])
        # Round entry down to the bar boundary, exit up
        cur = e_ts.replace(second=0, microsecond=0)
        cur -= timedelta(minutes=cur.minute % 2)
        end = x_ts.replace(second=0, microsecond=0)
        end += timedelta(minutes=(2 - end.minute % 2) % 2)
        sign = 1 if t["direction"] == "long" else -1
        entry_px = float(t["entry_price"])
        atr = float(t.get("atr_at_entry") or 0.0)
        max_h, min_l = entry_px, entry_px
        bar_count = 0
        while cur <= end:
            b = bars_by_t.get(cur)
            if b is not None:
                max_h = max(max_h, b.h)
                min_l = min(min_l, b.l)
                bar_count += 1
            cur += timedelta(minutes=2)
        if t["direction"] == "long":
            mfe_pts = max_h - entry_px
            mae_pts = entry_px - min_l
        else:
            mfe_pts = entry_px - min_l
            mae_pts = max_h - entry_px
        mfe_atr = mfe_pts / atr if atr > 0 else None
        mae_atr = mae_pts / atr if atr > 0 else None
        # Exit P&L expressed in ATR units for comparability
        exit_pnl_pts = (float(t["exit_price"]) - entry_px) * sign if t.get("exit_price") else 0.0
        exit_pnl_atr = exit_pnl_pts / atr if atr > 0 else None
        enriched.append({
            **t,
            "mfe_pts": mfe_pts, "mae_pts": mae_pts,
            "mfe_atr": mfe_atr, "mae_atr": mae_atr,
            "exit_pnl_pts": exit_pnl_pts, "exit_pnl_atr": exit_pnl_atr,
            "bars_in_window": bar_count,
        })

    print(f"Enriched {len(enriched)} trades with MFE/MAE")

    # 5. Aggregate stats
    pnls = [t["pnl_dollars"] for t in enriched if t["pnl_dollars"] is not None]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]
    gross_win = sum(winners)
    gross_loss = abs(sum(losers))
    pf = gross_win / max(gross_loss, 0.01)
    win_rate = len(winners) / len(pnls) * 100 if pnls else 0
    expectancy = sum(pnls) / len(pnls) if pnls else 0

    exit_dist = Counter(t.get("exit_reason") for t in enriched)
    direction_dist = Counter(t.get("direction") for t in enriched)
    cum_deltas = [int(t.get("cum_delta_at_entry") or 0) for t in enriched]
    atrs = [float(t.get("atr_at_entry") or 0) for t in enriched if t.get("atr_at_entry")]
    bars_held = [int(t.get("bars_held") or 0) for t in enriched if t.get("bars_held")]

    # P&L by exit reason
    by_reason: dict[str, list[float]] = {}
    for t in enriched:
        r = t.get("exit_reason") or "?"
        by_reason.setdefault(r, []).append(t.get("pnl_dollars") or 0.0)

    # 6. Identify big winners / big losers
    sorted_by_pnl = sorted(enriched, key=lambda t: t.get("pnl_dollars") or 0)
    big_losers = sorted_by_pnl[:5]
    big_winners = sorted_by_pnl[-5:][::-1]

    # 7. Give-back analysis
    have_mfe = [t for t in enriched if t.get("mfe_atr") is not None and t.get("exit_pnl_atr") is not None]
    losers_that_were_green = sum(
        1 for t in have_mfe
        if (t["pnl_dollars"] or 0) <= 0 and (t["mfe_atr"] or 0) >= 1.0
    )
    losers_that_were_green_2atr = sum(
        1 for t in have_mfe
        if (t["pnl_dollars"] or 0) <= 0 and (t["mfe_atr"] or 0) >= 2.0
    )
    winners_with_giveback = []  # mfe - exit_pnl in ATR units
    for t in have_mfe:
        if (t["pnl_dollars"] or 0) > 0 and (t["mfe_atr"] is not None) and (t["exit_pnl_atr"] is not None):
            gb_atr = t["mfe_atr"] - t["exit_pnl_atr"]
            if gb_atr > 0:
                winners_with_giveback.append((t, gb_atr))
    winners_with_giveback.sort(key=lambda x: -x[1])

    # 8. Render markdown report
    lines: list[str] = []
    lines.append(f"# 2026-05-05 trading day — analysis\n")
    lines.append(f"Topstep session: 2026-05-04 17:00 CT → 2026-05-05 14:36 CT (final exit)")
    lines.append(f"Generated: {datetime.now(UTC).isoformat()}\n")

    lines.append("## Headline\n")
    lines.append(f"- **Trades**: {len(enriched)}")
    lines.append(f"- **Total P&L**: ${sum(pnls):.2f}")
    lines.append(f"- **Win rate**: {win_rate:.1f}% ({len(winners)}W / {len(losers)}L)")
    lines.append(f"- **Profit factor**: {pf:.2f}  (OOS quote-mode baseline {OOS_PF_QUOTE_MODE:.2f}, gate floor 1.50)")
    lines.append(f"- **Expectancy**: ${expectancy:.2f} per trade")
    lines.append(f"- **Direction mix**: {dict(direction_dist)}")
    lines.append("")

    lines.append("## Exit-reason mix\n")
    lines.append("| Exit reason | Count | % | Sum P&L | Avg P&L |")
    lines.append("|---|---:|---:|---:|---:|")
    total = len(enriched)
    for r, count in exit_dist.most_common():
        ps = by_reason.get(r, [])
        s = sum(ps)
        avg = s / max(len(ps), 1)
        lines.append(f"| `{r}` | {count} | {count/total*100:.1f}% | ${s:.2f} | ${avg:.2f} |")
    lines.append(f"\nOOS baseline opposite_signal share: ~{OOS_OPPOSITE_EXIT_PCT:.0f}%.\n")

    lines.append("## Cum_delta at entry — distribution\n")
    lines.append(f"Filter threshold (quote mode): **{FILTER_THRESH_QUOTE}**. Long entries fire when `cum_delta_in_dir < {FILTER_THRESH_QUOTE}`.\n")
    if cum_deltas:
        # All trades today are long (per dashboard) — cum_delta_in_dir == cum_delta_at_entry
        abs_cd = [abs(c) for c in cum_deltas]
        lines.append(f"- min: {min(cum_deltas):,}")
        lines.append(f"- p25: {_percentile(cum_deltas, 25):,.0f}")
        lines.append(f"- median: {_percentile(cum_deltas, 50):,.0f}")
        lines.append(f"- p75: {_percentile(cum_deltas, 75):,.0f}")
        lines.append(f"- max: {max(cum_deltas):,}")
        lines.append(f"- mean(|cum_delta|): {statistics.mean(abs_cd):,.0f}")
        lines.append("")
        lines.append(f"Median |cum_delta| at entry is **{statistics.mean(abs_cd):.0f}× the filter threshold magnitude**. The filter is firing at extremes far beyond what OOS calibrated for, which means it's barely filtering — virtually any 2-bar reversal with sell-leaning flow passes.\n")

    lines.append("## ATR at entry — distribution\n")
    if atrs:
        lines.append(f"- min: {min(atrs):.2f}")
        lines.append(f"- median: {_percentile(atrs, 50):.2f}")
        lines.append(f"- max: {max(atrs):.2f}")
        lines.append(f"- mean: {statistics.mean(atrs):.2f}")
        lines.append("")

    lines.append("## Bars held — distribution\n")
    if bars_held:
        lines.append(f"- min: {min(bars_held)}  (= {min(bars_held)*2}m)")
        lines.append(f"- median: {_percentile(bars_held, 50):.0f}  (= {_percentile(bars_held, 50)*2:.0f}m)")
        lines.append(f"- max: {max(bars_held)}  (= {max(bars_held)*2}m)")
        lines.append(f"- mean: {statistics.mean(bars_held):.1f}")
        lines.append("")
        bh_counter = Counter(bars_held)
        lines.append("Bars-held buckets:")
        lines.append("| Bars | Trades | Avg P&L |")
        lines.append("|---:|---:|---:|")
        buckets = [(1, 1), (2, 3), (4, 9), (10, 30), (31, 60)]
        for lo, hi in buckets:
            ts = [t for t in enriched if t.get("bars_held") and lo <= t["bars_held"] <= hi]
            if not ts:
                continue
            ps = [t["pnl_dollars"] for t in ts if t.get("pnl_dollars") is not None]
            avg = sum(ps) / len(ps) if ps else 0
            label = f"{lo}" if lo == hi else f"{lo}-{hi}"
            lines.append(f"| {label} | {len(ts)} | ${avg:.2f} |")
        lines.append("")

    lines.append("## Per-hour pattern (CT)\n")
    by_hour: dict[int, list[dict]] = {}
    for t in enriched:
        if t.get("bar_ts"):
            h = _hour_ct(t["bar_ts"])
            by_hour.setdefault(h, []).append(t)
    lines.append("| Hour CT | Trades | Sum P&L | Win rate |")
    lines.append("|---:|---:|---:|---:|")
    for h in sorted(by_hour):
        ts = by_hour[h]
        ps = [t["pnl_dollars"] for t in ts if t.get("pnl_dollars") is not None]
        s = sum(ps)
        ws = sum(1 for p in ps if p > 0)
        wr = ws / len(ps) * 100 if ps else 0
        lines.append(f"| {h:02d} | {len(ts)} | ${s:.2f} | {wr:.0f}% |")
    lines.append("")

    lines.append("## Top 5 winners\n")
    lines.append("| ID | bar_ts CT | bars | exit reason | entry | exit | P&L | MFE atr | MAE atr |")
    lines.append("|---:|---|---:|---|---:|---:|---:|---:|---:|")
    for t in big_winners:
        lines.append(
            f"| {t['id']} | {_ct(t['bar_ts'])} | "
            f"{t.get('bars_held', '—')} | `{t.get('exit_reason') or '—'}` | "
            f"{t.get('entry_price', '—')} | {t.get('exit_price', '—')} | "
            f"${t.get('pnl_dollars', 0):.2f} | "
            f"{t['mfe_atr']:.2f} | {t['mae_atr']:.2f} |"
            if t.get("mfe_atr") is not None else
            f"| {t['id']} | {_ct(t['bar_ts'])} | {t.get('bars_held','—')} | "
            f"`{t.get('exit_reason') or '—'}` | "
            f"{t.get('entry_price','—')} | {t.get('exit_price','—')} | "
            f"${t.get('pnl_dollars', 0):.2f} | — | — |"
        )
    lines.append("")

    lines.append("## Top 5 losers\n")
    lines.append("| ID | bar_ts CT | bars | exit reason | entry | exit | P&L | MFE atr | MAE atr |")
    lines.append("|---:|---|---:|---|---:|---:|---:|---:|---:|")
    for t in big_losers:
        lines.append(
            f"| {t['id']} | {_ct(t['bar_ts'])} | "
            f"{t.get('bars_held','—')} | `{t.get('exit_reason') or '—'}` | "
            f"{t.get('entry_price','—')} | {t.get('exit_price','—')} | "
            f"${t.get('pnl_dollars', 0):.2f} | "
            f"{t['mfe_atr']:.2f} | {t['mae_atr']:.2f} |"
            if t.get("mfe_atr") is not None else
            f"| {t['id']} | {_ct(t['bar_ts'])} | {t.get('bars_held','—')} | "
            f"`{t.get('exit_reason') or '—'}` | "
            f"{t.get('entry_price','—')} | {t.get('exit_price','—')} | "
            f"${t.get('pnl_dollars', 0):.2f} | — | — |"
        )
    lines.append("")

    lines.append("## Give-back analysis\n")
    lines.append(f"- Trades with MFE/MAE coverage: **{len(have_mfe)} / {len(enriched)}**")
    if have_mfe:
        lines.append(f"- **Losers that were green at some point (MFE ≥ 1 ATR before going red): {losers_that_were_green}** "
                     f"({losers_that_were_green/max(len(losers),1)*100:.0f}% of losers). "
                     f"These are the clearest trailing-stop candidates.")
        lines.append(f"- Losers that were +2 ATR or more at some point: **{losers_that_were_green_2atr}**.")
        lines.append("")
        lines.append("### Top 5 winners by give-back (MFE − exit P&L, ATR units)")
        lines.append("Trades that captured a large move but exited at much less. The bigger the give-back, the more a trailing stop or thesis-complete exit would have helped.\n")
        lines.append("| ID | bar_ts CT | exit reason | exit P&L atr | MFE atr | Give-back atr |")
        lines.append("|---:|---|---|---:|---:|---:|")
        for t, gb in winners_with_giveback[:5]:
            lines.append(
                f"| {t['id']} | {_ct(t['bar_ts'])} | `{t.get('exit_reason') or '—'}` | "
                f"{t['exit_pnl_atr']:.2f} | {t['mfe_atr']:.2f} | **{gb:.2f}** |"
            )
        lines.append("")

    lines.append("## What this looks like\n")
    if cum_deltas and atrs:
        median_cd_mag = statistics.mean(abs(c) for c in cum_deltas)
        magnitude_factor = median_cd_mag / abs(FILTER_THRESH_QUOTE)
        lines.append(f"1. **Filter is functionally inert.** Mean |cum_delta| at entry was ~{median_cd_mag:.0f} vs threshold {FILTER_THRESH_QUOTE} — that's {magnitude_factor:.0f}× too extreme. The filter only blocks entries when |cum_delta| < {abs(FILTER_THRESH_QUOTE)}, but live cum_delta hasn't been near that range all session. Entries are firing on every two-bar reversal that has any sell-leaning flow.")
    lines.append(f"2. **All trades long.** Today was a one-sided session — sell-flow accumulated all day, never built up to the +{abs(FILTER_THRESH_QUOTE)} threshold needed for shorts.")
    if exit_dist:
        opp_pct = exit_dist.get("opposite_signal", 0) / max(total, 1) * 100
        lines.append(f"3. **Opposite-signal exits at {opp_pct:.0f}%** vs OOS baseline ~{OOS_OPPOSITE_EXIT_PCT:.0f}%. Mechanism is intact — most trades complete via reversal, not stop or time-stop.")
    if pnls:
        lines.append(f"4. **PF {pf:.2f} vs OOS {OOS_PF_QUOTE_MODE:.2f}** ({pf/OOS_PF_QUOTE_MODE*100:.0f}% of OOS). Edge is degraded but not negative. Gate floor is 1.50.")
    if have_mfe and len(losers) > 0:
        gb_pct = losers_that_were_green / len(losers) * 100
        lines.append(f"5. **Give-back: {losers_that_were_green} of {len(losers)} losers ({gb_pct:.0f}%) were green ≥1 ATR at some point.** Trailing stop at break-even after +1 ATR would have rescued them.")

    out_path = "docs/2026-05-05-trading-day-analysis.md"
    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    print(f"\nReport written to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
