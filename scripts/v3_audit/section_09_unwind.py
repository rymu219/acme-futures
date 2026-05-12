"""Section 9 — synthesis and v3 unwind plan.

Pure markdown synthesis section. No CSV. Pulls in findings from
Sections 0–8 to produce:

  1. The v3 unwind plan (per-variant exit recommendation)
  2. Surviving insights for IGNITION / SESSION / REGIME / BOUNDARY
  3. What to inherit vs what to leave behind
  4. Recommended unwind execution order

This section does NOT execute the unwind.
"""
from __future__ import annotations

from pathlib import Path

try:
    from scripts.v3_audit.db import DOCS_DIR  # type: ignore
    from scripts.v3_audit.trades import append_section  # type: ignore
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.v3_audit.db import DOCS_DIR  # type: ignore  # noqa: E402
    from scripts.v3_audit.trades import append_section  # type: ignore  # noqa: E402


SECTION_9 = """## §9 Synthesis and v3 unwind plan

### What we learned

Restating the audit's six driving questions with the section-level answers.

**Q1. Active operational risks?**
Only one: data integrity. 10 orphaned trade rows from 2026-05-07 22:08
CT sit `exit_ts = NULL` in `ryan_spec_v3_trades` even though their
heartbeats are flat. No real-money risk (dry-run + no actual positions
held); the dashboard's "11+ variants stuck LONG" framing is a read of
those orphaned rows. HALT does not lock positions; the `/200` indicator
is a settled-trade promotion gate, not a contract cap (§0.2). The
runtime is healthy.

**Q2. Verified edge vs tail-driven?**
*Every* variant currently net-positive on 7d is tail-driven — top 5
trades account for between 188% and 8,693% of that variant's net P&L
(§5). Strip out the top-5 outliers and the entire fleet flips negative.
**No variant has verified statistical edge in the 7d window.** This is
the most important downward revision of the v0 exec summary.

**Q3. When does v3 make money?**
The single strongest pattern is hour-of-day (§2). **Best windows:**
03:00–05:00 CT (European session, PF ~2.0), 08:00–09:00 CT (US RTH
open, PF 1.6–1.7), and 17:00 CT (PF 3.47, but only 53 trades). **Worst
windows:** 11:00 CT (PF 0.23), 13:00 CT (PF 0.30 — the single worst
hour; every variant has its worst hour at 13:00 CT), and 15:00 CT
(PF 0.24). The draft SESSION window of 13:30–15:00 CT in the original
plan is the *worst* 2-hour window in the entire fleet.

**Q4. Correlation reality.**
495 cluster events in 7d; **131 of them (26%) involve 11+ variants**
firing the same direction within 5 minutes (§0.6). Those 11+ clusters
alone account for **-$2,317 of P&L** — more than the entire fleet's
net loss (§6). Daily-P&L correlations are extreme: the v3-* family is
internally correlated at >0.9 between every pair; the v3.1-* family
likewise; even the EMA-trend-gate and EMA-trend-flip variants share
r = +0.85 because they read the same classifier. Only one pair shows
real diversification: `v4-overnight-bias ↔ v4-trend-flip` at
r = -0.73. Sixteen "variants" run as **three effective strategies**.

**Q5. 2-bar minimum hold?**
**Dramatic confirmation** (§3). 1-bar `opposite_signal` exits: 2,366
trades, **-$8,858**, WR 16.5%, PF 0.18. ≥2-bar `opposite_signal` exits:
1,617 trades, **+$10,541**, WR 68.3%, PF 6.14. Win rate climbs
monotonically with hold time: 18% (bar 1) → 54% (bar 2) → 83% (bars
4-6) → 94% (bars 7-10). The signal source identifies real moves; the
exit policy throws them away in the first 2 minutes. v3-min2bar
(forced 2-bar minimum) is the only variant that captures this fix in
the current fleet — and it outperforms v3-canon as expected (-$89 vs
-$231).

**Q6. Surviving insights for the new fleet.**

| Strategy | What it should inherit | What it should NOT inherit |
|---|---|---|
| **IGNITION** | (a) Minimum-2-bar hold rule (the single biggest fix in the data, §3). (b) The bar1_fast_fail concept where MAE inverts MFE early (a positive lower-bound filter for false-start trades). | The shared `LiveBarDeltaBuilder` signal source — IGNITION needs PULSE/ECI as a *mechanically different* trigger, not a cum_delta extreme. PULSE/ECI is not in the repo today and is the gating dependency for IGNITION. |
| **SESSION** | The hour bucket data (§2): 03:00–05:00 CT (Europe), 08:00–09:00 CT (RTH open), 17:00 CT (small sample, needs validation). NOT the original plan's 13:30–15:00 CT window. | Any framing that uses "morning" or "afternoon" RTH as the dominant windows. The data says European pre-open and US open are productive; afternoon is a no-trade zone. |
| **REGIME** | The trend-EMA + vol classifier code (`acme.ryan_spec.v4_regime`). v4-trend-gate's negative correlation with v4-overnight-bias (r=-0.73) is the only real diversification in the fleet. The classifiers themselves work; the entry signal they wrap is the problem. | The "compression-fade, expansion-follow" framing in the original plan: 11 of 16 variants take their max-DD in pure chop (§7), so a chop *deadband* (skip) is more justified than a "fade in chop" rule. |
| **BOUNDARY** | Nothing from v3 directly — BOUNDARY is a new mechanic. The audit shows the chop-driven losses (§7) leave room for a level-rejection strategy that only fires near defined levels. | n/a — BOUNDARY is built from scratch. |

### v3 unwind plan

#### Per-variant exit recommendation

The runtime is `--dry-run`, so "exit" means cleaning up trade rows so
the perf surface is honest. Every variant should have its remaining
open trade rows cleaned, then be archived.

| Variant | 7d trades | 7d net P&L | Action |
|---|---:|---:|---|
| v4-overnight-bias | 301 | $+133.05 (tail) | retire; classifier survives in REGIME |
| v4-trend-gate | 250 | $+33.75 (tail) | retire; classifier survives in REGIME |
| v4-vol-regime | 309 | $+22.45 (tail) | retire; classifier survives in REGIME |
| v5-mtf-anchor | 311 | $+2.85 (tail) | retire; multi-TF idea survives in REGIME |
| v3.1-armor | 20 | -$62.75 (low sample) | retire — too few trades to learn from |
| v3.1-min2bar | 163 | -$65.35 | retire; min-2-bar rule survives in IGNITION |
| v3.1-trail | 183 | -$89.35 | retire; bar1_fast_fail survives in IGNITION |
| v3-min2bar | 434 | -$88.95 | retire; min-2-bar rule survives in IGNITION |
| v3.1-pctile | 173 | -$119.85 | retire — pctile filter doesn't help |
| v4-loose-shorts | 276 | -$115.70 | retire; never fired a short anyway (§4) |
| v3.1-canon | 181 | -$139.20 | retire — same edge as v3-canon, smaller sample |
| v3-canon | 627 | -$236.55 | retire — the canonical signal needs a different exit policy |
| v3-pctile | 478 | -$257.25 | retire — pctile filter doesn't add edge |
| v4-trend-flip | 289 | -$258.55 | retire; inversion concept stays in REGIME (but inverting is high-risk) |
| v3-armor | 147 | -$394.30 | retire — armor rule wasn't enough |
| v3-trail | 544 | -$484.70 | retire — trailing stop in 2-min bars churns |

#### Recommended unwind execution order

Do these steps in order. None of them are auto-run by this audit.

1. **Operator cleanup of the 10 orphaned trade rows** (§0.4).
   Manual SQL UPDATE setting `exit_reason = 'manual_cleanup_2026_05_11_audit'`
   and either:
   - (a) compute `exit_price` from a 2026-05-07 22:10 Databento bar
     and write real P&L, or
   - (b) set `exit_price = entry_price`, `pnl_dollars = 0`.
   The audit recommends (b) — these were not real trades; closing at
   entry price avoids fabricating P&L. Match the precedent at exit_reason
   `'manual_cleanup_2026_05_06_signalr_drop'` for the SQL shape.

2. **Stop the launchd agent** at the end of the next RTH session:
   ```
   launchctl unload ~/Library/LaunchAgents/com.acme-futures.v3-runner.plist
   ```
   Do this *after* `v4-trend-flip` (currently SHORT @ $7,425) exits on
   its own stop or opposite signal. Don't force-close mid-position.

3. **Run a final `--once` pass** confirming all heartbeats stable at
   flat, all open trades closed.

4. **Mark `ryan_spec_v3_trades` read-only at the code level.** Strip
   the `insert_ryan_spec_v3_trade` and `update_ryan_spec_v3_trade`
   write paths in `src/acme/db.py`. Leave the `select_ryan_spec_v3_trades`
   read path intact — the audit, dashboard, and any future analysis
   still need to read it.

5. **Archive v3 runtime code** under `archive/v3/` preserving git
   history. Move `src/acme/runner.py` (16-variant loader),
   `src/acme/ryan_spec/v3_runtime.py`, `src/acme/ryan_spec/v3_engine.py`,
   `src/acme/ryan_spec/v3_promotion.py`, and `src/acme/ryan_spec/v4_regime.py`.
   The v4_regime classifiers ARE going to be reused in REGIME, but
   archiving the current wrapper keeps the boundary clean — REGIME
   imports from `acme.classifiers.*`, not from the v3 archive.

6. **Leave `runtime_heartbeats` alone.** The classic conductor and the
   future new fleet will reuse it. Strip only the v3-specific service
   IDs from the watchdog filter after the agent is unloaded.

### Headline finding

If you do nothing else in Part 2 except enforce a **2-bar minimum hold
on `opposite_signal` exits**, the fleet's 7d net P&L flips from
**-$2,086 to +$6,772**. That's the single biggest finding in the audit
and the easiest fix to ship. Everything else — REGIME's classifier,
SESSION's hours, BOUNDARY's levels — is built on top of an
honest-to-the-signal exit policy.

The signal source is finding real moves. The exit policy is destroying
them. Fix the exit first.
"""


def main() -> int:
    append_section(DOCS_DIR / "v3_audit.md", SECTION_9)
    print(SECTION_9)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
