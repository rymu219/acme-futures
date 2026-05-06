"""Tests for the Ryan-Spec v3 paper-week promotion gate.

Pins the four gate criteria (settled-count, profit-factor floor, slippage cap,
opposite-signal share), their boundary semantics, the precedence order between
them, and the monthly-PF / win-rate side outputs.
"""
from __future__ import annotations

import pandas as pd

from acme.ryan_spec.v3_promotion import (
    MAX_AVG_SLIPPAGE_TICKS,
    MIN_OPPOSITE_EXIT_PCT,
    MIN_PF_FLOOR,
    MIN_SETTLED_TRADES,
    evaluate_paper_promotion,
    evaluate_paper_promotion_per_strategy,
)

# --- helpers ------------------------------------------------------------

def _trades(
    pnl: list[float],
    *,
    exit_reasons: list[str | None] | None = None,
    slippage: list[float | None] | None = None,
    bar_ts: list[str] | None = None,
    opposite_share: float = 0.55,
) -> pd.DataFrame:
    """Build a trades DataFrame for the gate.

    By default every row is settled. ``opposite_share`` controls the fraction
    of settled rows whose exit_reason is "opposite_signal"; the rest get
    "session_end". Override ``exit_reasons`` for full control (use None for
    unsettled rows).
    """
    n = len(pnl)
    if exit_reasons is None:
        n_opp = int(round(n * opposite_share))
        exit_reasons = ["opposite_signal"] * n_opp + ["session_end"] * (n - n_opp)
    cols = {"exit_reason": exit_reasons, "pnl_dollars": pnl}
    if slippage is not None:
        cols["slippage_ticks"] = slippage
    if bar_ts is not None:
        cols["bar_ts"] = bar_ts
    return pd.DataFrame(cols)


def _balanced(n: int, win: float, loss: float) -> list[float]:
    half = n // 2
    return [win] * half + [loss] * (n - half)


def _passing_pnl(n: int = MIN_SETTLED_TRADES) -> list[float]:
    """PF = 2.0, well above the 1.5 floor."""
    return _balanced(n, win=30.0, loss=-15.0)


# --- settled-count gate -------------------------------------------------

def test_extend_paper_when_below_min_settled():
    df = _trades(_passing_pnl(MIN_SETTLED_TRADES - 1))
    e = evaluate_paper_promotion(df)
    assert e.verdict == "EXTEND_PAPER"
    assert e.settled == MIN_SETTLED_TRADES - 1
    assert e.pf is None
    assert e.win_rate_pct is None
    assert e.avg_slippage_ticks is None
    assert e.exit_distribution == {}
    assert e.monthly_pf is None


def test_extend_paper_ignores_unsettled_trades():
    """Unsettled rows (exit_reason=None) must not count toward MIN_SETTLED_TRADES."""
    settled_pnl = _passing_pnl(MIN_SETTLED_TRADES - 1)
    settled_reasons: list[str | None] = ["opposite_signal"] * len(settled_pnl)
    unsettled_pnl = [0.0] * 50
    unsettled_reasons: list[str | None] = [None] * 50
    df = _trades(
        settled_pnl + unsettled_pnl,
        exit_reasons=settled_reasons + unsettled_reasons,
    )
    e = evaluate_paper_promotion(df)
    assert e.verdict == "EXTEND_PAPER"
    assert e.settled == MIN_SETTLED_TRADES - 1


def test_settled_count_at_threshold_proceeds_past_min():
    """n == 200 is NOT < 200 — boundary inclusive on the 'pass' side."""
    df = _trades(_passing_pnl(MIN_SETTLED_TRADES))
    e = evaluate_paper_promotion(df)
    assert e.verdict == "PROMOTE_LIVE"
    assert e.settled == MIN_SETTLED_TRADES


# --- PF gate ------------------------------------------------------------

def test_halt_when_pf_below_floor():
    df = _trades(_balanced(MIN_SETTLED_TRADES, win=30.0, loss=-30.0))  # PF = 1.0
    e = evaluate_paper_promotion(df)
    assert e.verdict == "HALT"
    assert "PF" in e.reason
    assert e.pf == 1.0


def test_pf_exactly_at_floor_does_not_halt():
    """PF == 1.5 is NOT < 1.5 — boundary inclusive on the 'pass' side."""
    df = _trades(_balanced(MIN_SETTLED_TRADES, win=30.0, loss=-20.0))  # PF = 1.5
    e = evaluate_paper_promotion(df)
    assert e.verdict == "PROMOTE_LIVE"
    assert e.pf == MIN_PF_FLOOR


def test_pf_with_zero_losses_clamps_via_epsilon():
    """No losing trades → gross_loss=0 → PF = gross_win / 0.01 (huge), passes."""
    df = _trades([10.0] * MIN_SETTLED_TRADES)
    e = evaluate_paper_promotion(df)
    assert e.verdict == "PROMOTE_LIVE"
    assert e.pf is not None and e.pf > 1000


# --- slippage gate ------------------------------------------------------

def test_halt_when_avg_slippage_above_max():
    df = _trades(_passing_pnl(), slippage=[2.0] * MIN_SETTLED_TRADES)
    e = evaluate_paper_promotion(df)
    assert e.verdict == "HALT"
    assert "lippage" in e.reason
    assert e.avg_slippage_ticks == 2.0


def test_slippage_exactly_at_max_does_not_halt():
    """avg_slip == 1.5 is NOT > 1.5 — boundary inclusive on the 'pass' side."""
    df = _trades(
        _passing_pnl(),
        slippage=[MAX_AVG_SLIPPAGE_TICKS] * MIN_SETTLED_TRADES,
    )
    e = evaluate_paper_promotion(df)
    assert e.verdict == "PROMOTE_LIVE"
    assert e.avg_slippage_ticks == MAX_AVG_SLIPPAGE_TICKS


def test_missing_slippage_column_does_not_halt():
    """Schema may omit slippage_ticks during early paper trading."""
    df = _trades(_passing_pnl())
    e = evaluate_paper_promotion(df)
    assert e.verdict == "PROMOTE_LIVE"
    assert e.avg_slippage_ticks is None


def test_all_nan_slippage_does_not_halt():
    """Column present but no measured fills yet → treat as 'unknown', not 'fail'."""
    df = _trades(_passing_pnl(), slippage=[None] * MIN_SETTLED_TRADES)
    e = evaluate_paper_promotion(df)
    assert e.verdict == "PROMOTE_LIVE"
    assert e.avg_slippage_ticks is None


def test_partial_nan_slippage_uses_non_nan_mean():
    """Mean ignores NaN — half measured at 1.0, half NaN → avg = 1.0."""
    n = MIN_SETTLED_TRADES
    half = n // 2
    slippage: list[float | None] = [None] * half + [1.0] * (n - half)
    df = _trades(_passing_pnl(n), slippage=slippage)
    e = evaluate_paper_promotion(df)
    assert e.verdict == "PROMOTE_LIVE"
    assert e.avg_slippage_ticks == 1.0


# --- opposite-signal gate ----------------------------------------------

def test_investigate_when_opposite_share_below_floor():
    df = _trades(_passing_pnl(), opposite_share=0.30)
    e = evaluate_paper_promotion(df)
    assert e.verdict == "INVESTIGATE"
    assert "Opposite" in e.reason


def test_opposite_share_exactly_at_floor_passes():
    """opposite_pct == 0.40 is NOT < 0.40 — boundary inclusive on the 'pass' side."""
    df = _trades(_passing_pnl(), opposite_share=MIN_OPPOSITE_EXIT_PCT)
    e = evaluate_paper_promotion(df)
    assert e.verdict == "PROMOTE_LIVE"
    assert e.exit_distribution["opposite_signal"] == MIN_OPPOSITE_EXIT_PCT


def test_no_opposite_signal_exits_investigates():
    """If 'opposite_signal' is absent from exit_distribution, treat as 0%."""
    df = _trades(
        _passing_pnl(),
        exit_reasons=["session_end"] * MIN_SETTLED_TRADES,
    )
    e = evaluate_paper_promotion(df)
    assert e.verdict == "INVESTIGATE"
    assert e.exit_distribution.get("opposite_signal", 0.0) == 0.0


# --- happy path ---------------------------------------------------------

def test_promote_live_when_all_gates_pass():
    df = _trades(
        _passing_pnl(),
        slippage=[1.0] * MIN_SETTLED_TRADES,
        opposite_share=0.55,
    )
    e = evaluate_paper_promotion(df)
    assert e.verdict == "PROMOTE_LIVE"
    assert e.pf == 2.0
    assert e.avg_slippage_ticks == 1.0
    assert e.exit_distribution["opposite_signal"] == 0.55
    assert "all gates pass" in e.reason


# --- precedence (gate order matters) ------------------------------------

def test_pf_failure_takes_precedence_over_slippage():
    df = _trades(
        _balanced(MIN_SETTLED_TRADES, win=30.0, loss=-30.0),  # PF=1.0
        slippage=[2.0] * MIN_SETTLED_TRADES,                  # also fails
    )
    e = evaluate_paper_promotion(df)
    assert e.verdict == "HALT"
    assert "PF" in e.reason and "lippage" not in e.reason


def test_slippage_failure_takes_precedence_over_opposite_exit():
    df = _trades(
        _passing_pnl(),                          # PF OK
        slippage=[2.0] * MIN_SETTLED_TRADES,     # fails
        opposite_share=0.10,                     # would also fail
    )
    e = evaluate_paper_promotion(df)
    assert e.verdict == "HALT"
    assert "lippage" in e.reason


# --- monthly PF ---------------------------------------------------------

def test_monthly_pf_populated_when_bar_ts_present():
    n = MIN_SETTLED_TRADES
    half = n // 2
    bar_ts = (
        ["2026-04-15T10:00:00"] * half
        + ["2026-05-15T10:00:00"] * (n - half)
    )
    df = _trades(_passing_pnl(n), bar_ts=bar_ts)
    e = evaluate_paper_promotion(df)
    assert e.monthly_pf is not None
    assert "2026-04" in e.monthly_pf
    assert "2026-05" in e.monthly_pf


def test_monthly_pf_empty_when_bar_ts_missing():
    df = _trades(_passing_pnl())
    e = evaluate_paper_promotion(df)
    assert e.monthly_pf == {}


# --- win rate -----------------------------------------------------------

def test_win_rate_computed_from_settled_only():
    df = _trades([30.0] * 120 + [-15.0] * 80)  # 120 wins / 200 = 60%
    e = evaluate_paper_promotion(df)
    assert e.win_rate_pct == 60.0


# --- per-strategy gate --------------------------------------------------

def _strategies(spec: dict[str, list[float]]) -> pd.DataFrame:
    """Build a multi-strategy DataFrame. spec maps strategy_id -> pnl list."""
    frames = []
    for sid, pnl in spec.items():
        df = _trades(pnl, opposite_share=0.55)
        df["strategy_id"] = sid
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def test_per_strategy_returns_one_verdict_per_strategy():
    """Two strategies, distinct PnL profiles, distinct verdicts."""
    df = _strategies({
        "v3-canon": _passing_pnl(MIN_SETTLED_TRADES),                  # PF 2.0
        "v3-bad":   _balanced(MIN_SETTLED_TRADES, win=30.0, loss=-30.0),  # PF 1.0
    })
    out = evaluate_paper_promotion_per_strategy(df)
    assert set(out.keys()) == {"v3-canon", "v3-bad"}
    assert out["v3-canon"].verdict == "PROMOTE_LIVE"
    assert out["v3-bad"].verdict == "HALT"
    assert out["v3-canon"].pf == 2.0
    assert out["v3-bad"].pf == 1.0


def test_per_strategy_extends_paper_when_volume_low_per_variant():
    """Per-strategy verdicts use each variant's own settled count, not the
    combined total. Two variants with 100 trades each — each gets EXTEND_PAPER
    even though their combined total is 200."""
    df = _strategies({
        "v3-canon": _passing_pnl(100),
        "v3-trail": _passing_pnl(100),
    })
    out = evaluate_paper_promotion_per_strategy(df)
    assert out["v3-canon"].verdict == "EXTEND_PAPER"
    assert out["v3-trail"].verdict == "EXTEND_PAPER"


def test_per_strategy_empty_input_returns_empty_dict():
    df = pd.DataFrame()
    assert evaluate_paper_promotion_per_strategy(df) == {}


def test_per_strategy_groups_null_strategy_id_under_unknown():
    """Legacy rows without strategy_id (pre-multi-variant migration) get
    bucketed under 'unknown' rather than crashing."""
    df = _trades(_passing_pnl(MIN_SETTLED_TRADES))
    df["strategy_id"] = None  # all rows null
    out = evaluate_paper_promotion_per_strategy(df)
    assert "unknown" in out
    assert out["unknown"].verdict == "PROMOTE_LIVE"
