# Analysis orient — GO/NO-GO and IGNITION-on-PULSE

Phase 0 deliverable. Read-only. Source citations from current `master` parity in worktree `claude/peaceful-knuth-f5d3c6` on 2026-05-14.

---

## 1. The 4 gates of GO/NO-GO — exactly as coded

Source: `src/acme/strategies/go_no_go.py:136-177` (the `update()` method on `GoNoGoEngine`).

Per-bar state is computed from:

```
ef         = EMA(close, ema_fast=9)
es         = EMA(close, ema_slow=14)
vma        = SMA(volume, vr_len=20)
abs_sep    = |ef - es|
slope_fast = ef - ema_fast_history[-1 - slope_lookback]      # slope_lookback=1 default
slope_slow = es - ema_slow_history[-1 - slope_lookback]
vr         = bar.volume / vma   (or 0 if vma<=0)
vr_rising  = vr > previous bar's vr
```

The four gates (all defaults from `GoNoGoConfig`):

| # | Gate                    | Code expression                                                                                              | Default threshold |
|---|-------------------------|--------------------------------------------------------------------------------------------------------------|-------------------|
| 1 | **Separation**          | `sep_ok = abs_sep >= cfg.sep_thr`                                                                            | `sep_thr = 0.35`  |
| 2 | **Volume ratio**        | `vr_ok = (vr >= cfg.vr_thr) and (vr_rising if require_vr_rising else True)`                                  | `vr_thr = 0.85`, `require_vr_rising = True` |
| 3a | **Slope alignment UP**   | `up_aligned = (slope_fast > slope_thr) and (slope_slow > slope_thr)`                                         | `slope_thr = 0.0`, `slope_lookback = 1` |
| 3b | **Slope alignment DOWN** | `down_aligned = (slope_fast < -slope_thr) and (slope_slow < -slope_thr)`                                     | (same)            |
| 4 | **Time window**         | **NOT IN THIS MODULE.** The Pine indicator's 4th gate is a session window; `go_no_go.py` deliberately leaves that to the strategy layer (`go_no_go.py:18-21`). |

Composite signal (`go_no_go.py:167-168`):

```python
signal_long  = sep_ok and vr_ok and up_aligned
signal_short = sep_ok and vr_ok and down_aligned
```

`GoNoGoState.signal` property returns `+1` / `-1` / `0`. WAIT when any gate fails.

**Key observation:** as coded, the 4 listed gates are really **3 gates** (sep + vr + slope). The time-window gate is unimplemented in this module — it's "handled by the strategy layer." IGNITION supplies the time gate itself (or doesn't, by default).

---

## 2. How GO/NO-GO is composed inside IGNITION

Source: `src/acme/strategies/ignition.py`.

The `IgnitionStrategy.on_bar()` flow (`ignition.py:172-228`):

```python
def on_bar(self, bar, *, state, profile, current_position, current_balance_unrealized):
    gng = self._gng.update(bar)              # → GoNoGoState or None (if warming)
    self._atr.update(bar)
    self._update_bars_held(current_position)

    if gng is None or not self._atr.is_warm:
        return None

    in_window = self._in_window(bar.t)
    signal_raw = gng.signal                  # +1 / -1 / 0
    # ... position-held exit branch (skip if flat) ...

    # Entry path: we're flat
    if signal_raw == 0:
        return None
    if not in_window:                        # ← time gate added here
        return None
    if signal_raw == 1 and not self.config.allow_longs:
        return None
    if signal_raw == -1 and not self.config.allow_shorts:
        return None
    side = "buy" if signal_raw == 1 else "sell"
    return self._build_signal(..., reason="gng_entry")
```

So IGNITION = GO/NO-GO **as both the entry filter AND the directional signal source** (Phase 1's spec for the standalone `go_no_go_strategy.py` matches this exactly — when all 4 gates pass, take the trade in the direction the gates indicate). IGNITION adds these layers on top:

- **Time gate** (`_in_window`): default `time_windows = ()` means **no gating — fires any hour**. The `AUDIT_WINDOWS` constant (03-05 + 08-09 CT) exists but is not applied by default.
- **Direction gate**: `allow_longs=True`, `allow_shorts=False` by default (long-only per audit §4).
- **Min-2-bar opposite-exit policy**: when in position, an opposite GO/NO-GO signal only reverses if `_bars_held >= 2` (suppresses 1-bar whipsaws — audit §3).
- **ATR-multiple bracket**: stop = `1.5 × ATR`, target = `2.5 × ATR` (ratio 1.67R reward).
- **Sizing**: `dollars_to_contracts(risk_dollars_per_trade=25, stop_distance, point_value, fee)`.

Stripping IGNITION down to "GO/NO-GO standalone" means dropping the min-2-bar exit policy and any time gating beyond what GO/NO-GO itself defines (i.e. none) — fire entries any hour where all 3 effective gates pass.

---

## 3. PulseFeatureEngine vs GoNoGoEngine — what each computes

Sources: `pulse_features.py:120-219` (PULSE), `go_no_go.py:99-177` (GO/NO-GO).

### Side-by-side feature surface

| Concept             | GO/NO-GO produces                                                          | PULSE produces                                                                          |
|---------------------|----------------------------------------------------------------------------|------------------------------------------------------------------------------------------|
| EMA fast / slow     | `ef = EMA(close, 9)`, `es = EMA(close, 14)`                                 | `ema_fast = EMA(close, 9)`, `ema_slow = EMA(close, 14)`                                  |
| Separation          | `abs_sep = |ef - es|`                                                       | `ema_sep = ef - es` (signed; PULSE keeps direction)                                       |
| Slope               | `slope_fast`, `slope_slow` — 1-bar differences on each EMA                  | `slope = sep - prev_sep` — 1-bar change in the separation itself, **plus 4-bar history** |
| Slope decomposition | Two single-bar slopes (fast, slow) checked independently                    | 4-bar weighted decay (`decay=0.6`) → `mom_w` (direction) and `mag_w` (magnitude)         |
| Volume              | `vr = volume / SMA(volume, 20)`, `vr_rising` flag                           | `rvol = volume / SMA(volume, 20)` (same)                                                  |
| Range / volatility  | (not used)                                                                  | `atr = ATR(close, 4)`                                                                     |
| Composite score     | None — the gates produce booleans directly                                  | `raw_score = w_mom*mom_w + w_mag*tanh(mag_w) + w_rvol*(rvol - 1)` → clipped to ±3        |
| Probability         | None                                                                        | `p_long = 1 / (1 + exp(-2*score))`, `p_short = 1 - p_long`                                |
| Edge / ECI          | None                                                                        | `edge = |p_long - 0.5| * 2` — 0..1 confidence in **either** direction                     |
| Projected move      | None                                                                        | `proj_pts = max(atr * (rvol/rvol_base), min_range_pts)`                                   |
| Output type         | `GoNoGoState` (booleans + raw values for diagnostics)                       | `PulseFeatures` (15 fields including `score`, `p_long`, `p_short`, `edge`, `proj_pts`)    |

### Where they overlap

- **Both use the same EMA pair (9/14).** Same inputs, same defaults.
- **Both compute the same volume-ratio** (`volume / SMA(volume, 20)`). PULSE labels it `rvol`, GO/NO-GO labels it `vr` — same number.
- **Both look at slope of the EMAs.** GO/NO-GO looks at each EMA's 1-bar slope independently and requires both to agree. PULSE looks at the slope of the **separation** and takes 4 bars of history with exponential decay.

### Where they differ — the structurally different part

- **Decision form.** GO/NO-GO is a **hard binary** — three independent boolean gates `AND`'d together. PULSE is a **continuous score** transformed through a logistic function into a probability. There's no equivalent of GO/NO-GO's `sep_thr=0.35` or `vr_thr=0.85` in PULSE; PULSE blends those signals into a single number.
- **Volume role.** GO/NO-GO uses volume as a **necessary participation gate** (must clear `vr_thr` AND be rising). PULSE uses volume as a **weight** in the composite score (one of three weighted terms) AND as a multiplier on the projected-move estimate.
- **Direction.** GO/NO-GO derives direction from the **sign of both EMAs' slopes** (both must move the same way). PULSE derives direction from the **sign of the 4-bar weighted slope of the separation** — a single momentum measure that smooths out 1-bar noise via the decay weights.
- **State.** PULSE produces an **edge / ECI** value (0..1) that's exposed to the strategy layer as continuous confidence. GO/NO-GO produces no equivalent — it's pass or fail. The user's Phase 3 spec ("ECI threshold at its default > 0.5") maps to PULSE's `edge > 0.5`.
- **Missing in PULSE relative to GO/NO-GO.** PULSE has no explicit "slope ≥ threshold" check; the slope is blended into the score. So PULSE will fire on small slopes when other terms are large; GO/NO-GO won't fire unless slope is strictly positive (or negative) on **both** EMAs.

### One-line summary

GO/NO-GO is the **gate stack**: same EMA/volume primitives as PULSE, but as a 3-of-3 boolean filter. PULSE is the **scoring engine**: same primitives, blended into a probabilistic edge. They're two different aggregation strategies sitting on top of nearly identical features.

---

## 4. Backtest harness — location, interface, data range

### Location

`scripts/backtest_new_fleet.py` (528 lines). This is the same harness whose 2-year run produced the filtering verdict that selected BOUNDARY, OVERNIGHT_DRIFT, GAP_FILL (its output CSVs are in `docs/backtest_new_fleet/` — including `ignition.csv` at 840 KB and `session.csv` at 1.08 MB from the prior run).

### Interface

Two callable building blocks for downstream scripts to import:

- `stream_2min_bars(start=None, end=None) -> list[Bar]` — streams 1-min bars from the parquet cache, aggregates correctly to 2-min OHLCV (uses the full 1-min H/L, not just close ticks).
- `backtest_strategy(strategy, bars_2min, *, name, levels_by_date=None) -> list[dict]` — drives the strategy through every bar, tracks phantom positions via `DryRunPosition`/`check_dry_run_exits`, handles per-day `DailyState` reset, force-flat hooks (`wants_force_flat`), end-of-stream cleanup. Returns a list of one dict per closed trade.

Strategy contract:
- Must expose `on_bar(bar, *, state, profile, current_position, current_balance_unrealized) -> Signal | None`.
- Must declare `name: str`, `version: str`, `metadata: StrategyMetadata`.
- Optional: `set_levels(DayLevels)` (BOUNDARY only), `wants_force_flat(bar)` (any strategy with a time-of-day hard close).

CLI:
- `--strategy <name>` to run one only
- `--start YYYY-MM-DD --end YYYY-MM-DD` to bound the window
- Default: runs every name in the module's `STRATEGIES` dict

Per-strategy stats produced by `_strategy_stats()`: `n`, `net_pnl`, `win_rate`, `profit_factor`, `avg_pnl`, `max_win`, `max_loss`, `bar1_n`, `bar1_net`, `bar1_wr`. **Not currently produced: max drawdown, Sharpe.** I'll compute those from the CSV in Phase 2.

### Data range available

Parquet cache: `~/.acme/backtest_cache/glbx-mdp3-20240401-20260430.ohlcv-1m.dbn.front_month.parquet`

- **Span:** 2024-04-01 00:00 UTC → 2026-04-30 16:29 UTC
- **Rows:** 736,039 1-min bars (front-month MES, filtered)
- **Length:** ~25 months
- **Roll handling:** front-month-as-of-each-bar filter applied at cache-build time (see `src/acme/backtest/data.py`)

This is the same 25 months that produced the 2-year backtest verdict for the current fleet. Using `--start 2024-04-01 --end 2026-04-30` gives apples-to-apples.

---

## 5. Gaps that would block subsequent phases

1. **Cache is 14 days stale** for live-replay purposes (ends 2026-04-30; today is 2026-05-14). This **does not block** Phases 1–4 — the question is whether GO/NO-GO and IGNITION-on-PULSE produce edge on the same 25-month window the current fleet was filtered against. The 14-day gap only matters for live-near-miss probes (Phase 0 of yesterday's session), not for this audit.

2. **No live IGNITION history.** IGNITION has never been registered in the `strategies` table and never bound by any runner. `broker_events` contains zero rows with `strategy='ignition'`. The 840 KB `docs/backtest_new_fleet/ignition.csv` from the prior backtest run **is** available as a baseline reference for Phase 1 — but that's backtest output, not live shadow data. Phase 1's audit will find no live history; this needs to be stated plainly in the phase deliverable.

3. **Max drawdown and Sharpe are not computed by `_strategy_stats()`.** Phase 2 and Phase 3 deliverables require these per the spec. I will compute them from the closes-list (running cumulative P&L → peak → trough delta for max DD; per-day P&L → Sharpe). This is a deliverable-side calculation, not a harness change — the harness emits enough data via the close-record list.

4. **GO/NO-GO has no built-in time gate** (`go_no_go.py:18-21` documents this is deliberate). For Phase 2 RUN B (08:00–12:00 CT only), the `go_no_go_strategy.py` will need a `time_windows` field on its config (matching the pattern already used in IGNITION). This is a deliberate add to the new standalone strategy, not a modification of the existing module.

5. **PulseFeatureEngine has no built-in "fire when X" decision rule** (it just emits features). The Phase 3 `ignition_pulse_test.py` will need to implement the entry rule: "enter LONG when `edge > 0.5` and `p_long > p_short`; enter SHORT when `edge > 0.5` and `p_short > p_long`." This is a rule the user specified ("ECI threshold at its default > 0.5 = expansion, entries active") — it sits on top of PULSE, not inside it.

6. **No blocker on the live runner.** The probe scripts in Phase 2 and Phase 3 only read from the parquet cache; they don't touch ProjectX, Supabase writes, or the live runner process (PID 35991). The standing rules are honored by default — no manual care needed.

---

## Status

Phase 0 complete. No code changes, no live runner contact. Ready for explicit go on Phase 1 (extract `go_no_go_strategy.py` and audit historical records).
