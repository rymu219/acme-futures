# Eval-log build — Phase 4 dashboard reader

`web/fleet_view.py` now reads from `eval_log`, surfaces the data in two places — per-strategy charge meters on the strategy cards, and a FLEET ACTIVITY panel below them — and fixes the PILOT badge bug. FLEET updated to include `go_no_go_levels`. 551/551 tests passing, ruff clean. **Live runner not restarted.**

---

## 1. Queries added (matches the existing reader pattern)

All four queries land as functions in `fleet_view.py` following the same shape as the existing `_fetch_*` helpers (try/except wrapper, return-empty-on-failure, called from `render_overview`).

### `_fetch_eval_log_counts(sb) -> dict[str, int]`

```python
midnight_ct = now_ct.replace(hour=0, minute=0, second=0, microsecond=0)
floor_iso = midnight_ct.astimezone(UTC).isoformat()
sb.table("eval_log").select("id", count="exact").gte("bar_ts", floor_iso).limit(1).execute()
sb.table("eval_log").select("id", count="exact").eq("near_miss", True).gte("bar_ts", floor_iso).limit(1).execute()
```

Returns `{"evals_today": int, "near_misses_today": int}`. CT midnight floor matches the user's spec (`current_date AT TIME ZONE 'America/Chicago'`). PostgREST count works via `count="exact"` header.

### `_fetch_eval_log_activity(sb, *, limit=12) -> list[dict]`

```python
sb.table("eval_log").select(
    "bar_ts,strategy,outcome,near_miss,gate_failed,signal_side,reason,gate_values"
).order("bar_ts", desc=True).limit(limit).execute()
```

Returns rows in newest-first order. Empty list on failure.

### `_fetch_charge_pct_by_strategy(sb) -> dict[str, float | None]`

PostgREST doesn't support `DISTINCT ON`, so this issues one query per `FLEET` member (matches the existing `_fetch_perf_snapshots` pattern):

```python
for name in FLEET:
    sb.table("eval_log").select("gate_values").eq("strategy", name)
      .order("bar_ts", desc=True).limit(1).execute()
```

Returns `{"boundary": float | None, "overnight_drift": ..., "gap_fill": ..., "go_no_go_levels": ...}`. None when the strategy has no eval_log row yet.

## 2. Exposure on the existing endpoint (HTML, the existing response shape)

`web/fleet_view.py` already serves the dashboard as a single rendered HTML page from `render_overview(sb, *, token, bucket) -> str`. No new endpoint, no JSON API — the data lands inline in the HTML, matching the existing pattern verbatim. The new render sections are:

1. **`_render_eval_activity(counts, rows)`** — the FLEET ACTIVITY panel: counts in the header, 12-row table below.
2. **Charge meter inside each strategy card** — `_render_strategy_cards` was extended to accept a `charge_by_strategy: dict[str, float | None]` parameter; renders a small bar between the stats and the foot.

`render_overview` now wires both:

```python
heartbeats  = _fetch_heartbeats(sb)
strategies  = _fetch_strategies(sb)
snaps       = _fetch_perf_snapshots(sb)
closes_all  = _fetch_recent_closes(sb)
ks_state    = _fetch_kill_switch_state(sb)
eval_counts = _fetch_eval_log_counts(sb)            # NEW
eval_rows   = _fetch_eval_log_activity(sb, limit=12)  # NEW
charge_by   = _fetch_charge_pct_by_strategy(sb)     # NEW

body = (
    _render_topbar(heartbeats)
    + '<div class="page">'
    + _render_ticker_bar(closes_all, heartbeats, ks_state, token)
    + _render_mll_tracker(closes_all)
    + _render_active_trade(heartbeats)
    + _render_strategy_cards(heartbeats, strategies, snaps, charge_by)   # +charge_by
    + _render_eval_activity(eval_counts, eval_rows)                       # NEW
    + _render_hour_grid(closes_all)
    + _render_recent_closes(closes_all, heartbeats)
    + '</div>'
    + _render_footer()
)
```

### Sample served data — confirmed via live insert-render-cleanup probe

Inserted three eval_log rows (one PASS, one PASS-gate-failed, one NEAR), re-rendered, then deleted. Captured:

**Fetcher returns (post-insert):**
```
counts:   {'evals_today': 3, 'near_misses_today': 1}
activity: 3 rows in newest-first order
charges:  {'boundary': 0.45, 'overnight_drift': 0.0,
           'gap_fill': None, 'go_no_go_levels': 0.94}
```

**HTML render confirms:** every reason string round-trips into the table; the `★` glyph (near-miss highlight) appears in the rendered output; the `FLEET ACTIVITY` heading + per-strategy charge meters all render.

**Cleanup confirmed:** `counts.evals_today` returned 0 after delete. No residue in the table.

## 3. FLEET constant updated

```python
FLEET = ["boundary", "overnight_drift", "gap_fill", "go_no_go_levels"]
```

`STRATEGY_META["go_no_go_levels"]` added with a steel-blue accent (`#5B7FB8`), `silk-blue` silk class, and `"08:00 → 12:00 CT"` window label. A `silk-blue` CSS rule is now in `_CSS`. `tests/test_fleet_view.py::test_fleet_lists_three_keepers` was renamed to `test_fleet_lists_keepers` and updated to assert the 4-member fleet.

## 4. PILOT badge bug — fixed

Old behaviour (`fleet_view.py:_render_strategy_cards`, pre-Phase 4):

```python
if m["score"] >= LIVE_THRESHOLD:        # 0.65
    badge_label = "LIVE-ELIGIBLE"
elif m["score"] >= PILOT_THRESHOLD:     # 0.55
    badge_label = "PILOT"
else:
    badge_label = "SHADOW"
```

This ignored `strategies.state` entirely. All four keepers have `state="PILOT"` (the three originals) or `state="SHADOW"` (`go_no_go_levels`) in Supabase, but the dashboard always read SHADOW because `score=0.0` for every keeper (PerfTracker hasn't accumulated promotion-ladder data yet).

New behaviour (`_badge_for(state, score)`):

```python
_BADGE_BG_BY_STATE = {
    "LIVE": "var(--green)", "PILOT": "var(--gold)",
    "SHADOW": "var(--surface2)", "BENCH": "var(--surface2)",
    "RETIRED": "var(--surface2)", "BACKTEST": "var(--surface2)",
    "REPLAY": "var(--surface2)",
}

def _badge_for(state, score):
    if state and state in _BADGE_BG_BY_STATE:
        return state, _BADGE_BG_BY_STATE[state]
    # Fallback: score-derived lifecycle estimate.
    if score >= LIVE_THRESHOLD:   return "LIVE-ELIGIBLE", "var(--green)"
    if score >= PILOT_THRESHOLD:  return "PILOT", "var(--gold)"
    return "SHADOW", "var(--surface2)"
```

Behaviour:
- `state="PILOT"` → badge **PILOT** (gold)
- `state="SHADOW"` → badge **SHADOW** (neutral surface)
- `state="LIVE"` → badge **LIVE** (green)
- `state=null` or unknown → falls back to the legacy score-derived display (`LIVE-ELIGIBLE` / `PILOT` / `SHADOW`)

Confirmed: with the current strategies table (`boundary`, `overnight_drift`, `gap_fill` all PILOT; `go_no_go_levels` SHADOW), the dashboard now shows three PILOT badges and one SHADOW. The "they all say SHADOW even though I set them PILOT" cosmetic bug is gone.

## 5. Tests, lint, runner

- `uv run pytest -q` → **551 passed**
- `uv run ruff check` → clean
- Live runner: **NOT TOUCHED**. PID 72979 still running pre-Phase-2 code. The activation chain for the eval_log build is now complete in source; the runner restart that flips it all on is your call.

## What the full chain looks like (Phases 1–4)

1. ✅ Phase 1: `eval_log` table exists in Supabase (you applied the migration manually)
2. ✅ Phase 2: All four strategies return `EvalResult` (with `charge_pct`) at every non-warmup non-fire site
3. ✅ Phase 3: Conductor dispatches `EvalResult` → `Db.write_eval_log`; emits ENTRY rows on Signal fires
4. ✅ Phase 4: Dashboard reads `eval_log`, surfaces counts + activity feed + per-strategy charge meter; PILOT badge fixed

**To activate end-to-end on the live system:** merge → `cd ~/acme-futures && git pull origin master` → `kill <python-runner-PID>`. Watchdog respawns, runner imports the Phase 2 + 3 code, starts writing eval_log rows. Dashboard surfaces them on next refresh (5s).

## Status

Phase 4 complete. The full eval-log chain is end-to-end in source, end-to-end verified via probes, end-to-end safe for the live runner restart whenever you're ready.
