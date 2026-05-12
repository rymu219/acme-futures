"""Smoke tests for web/fleet_view.py — the new home page."""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# fleet_view lives under web/, not in the acme package. Add it to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

from fleet_view import (  # noqa: E402
    FLEET,
    _bucket_closes_by_hour,
    _heartbeat_status,
    _render_bars_held,
    _render_header,
    _render_hour_heatmap,
    _render_position_panel,
    _render_promotion_gate,
    _render_recent_trades,
    _render_strategy_cards,
    render_overview,
)


# ════════════ helpers ═══════════════════════════════════════════════


class _FakeSupabase:
    """In-memory mock for the Supabase client surface fleet_view uses."""

    def __init__(self, *, heartbeats=None, strategies=None, snaps=None, closes=None):
        self._heartbeats = heartbeats or []
        self._strategies = strategies or []
        self._snaps = snaps or {}
        self._closes = closes or []

    def table(self, name):
        return _FakeQuery(self, name)


class _FakeQuery:
    def __init__(self, parent, table):
        self.parent = parent
        self.table_name = table
        self._filters = []

    def select(self, *_args):
        return self
    def in_(self, *_args):
        return self
    def eq(self, *_args):
        return self
    def gte(self, *_args):
        return self
    def order(self, *_args, **_kwargs):
        return self
    def limit(self, *_args):
        return self

    def execute(self):
        data = []
        if self.table_name == "runtime_heartbeats":
            data = self.parent._heartbeats
        elif self.table_name == "strategies":
            data = self.parent._strategies
        elif self.table_name == "broker_events":
            data = self.parent._closes
        elif self.table_name == "strategy_perf_snapshot":
            # Pretend the .eq filter narrowed by strategy name. The fake just
            # returns the dict for whatever strategy the test set up.
            data = list(self.parent._snaps.values()) and [list(self.parent._snaps.values())[0]]
        return _FakeResp(data)


class _FakeResp:
    def __init__(self, data):
        self.data = data


def _hb(service, *, state="flat", age_s=10):
    ts = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    return {
        "service": service, "ts": ts, "last_bar_ts": ts,
        "position_state": state, "auth_ok": True,
        "consecutive_errors": 0, "extra": {"contract_id": "CON.F.US.MES.M26"},
    }


def _close(*, strategy="ignition", net_pnl=10.0, hours_ago=1, bars_held_minutes=4,
           outcome="target"):
    ts = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    return {
        "id": 1, "occurred_at": ts, "kind": "dry_run_close",
        "strategy": strategy, "side": "buy", "size": 1, "price": 100.0,
        "raw": {
            "net_pnl": net_pnl, "outcome": outcome,
            "entry_price": 100.0, "exit_price": 100 + net_pnl/5,
            "bars_held_minutes": bars_held_minutes,
        },
    }


# ════════════ helpers — pure ════════════════════════════════════════


def test_heartbeat_status_live():
    label, color, _ = _heartbeat_status(_hb("ignition", age_s=10))
    assert label == "LIVE"
    assert color == "#16a34a"


def test_heartbeat_status_stale():
    label, _, _ = _heartbeat_status(_hb("ignition", age_s=500))
    assert label == "STALE"


def test_heartbeat_status_offline():
    label, _, _ = _heartbeat_status(_hb("ignition", age_s=2000))
    assert label == "OFFLINE"


def test_heartbeat_status_unknown_on_missing():
    label, _, _ = _heartbeat_status(None)
    assert label == "UNKNOWN"


# ════════════ renderers — produce HTML containing key elements ═════


def test_header_shows_all_four_strategies():
    hbs = {name: _hb(name) for name in FLEET}
    html = _render_header(hbs)
    for name in FLEET:
        assert name in html
    assert "last bar" in html


def test_position_panel_empty_when_flat():
    hbs = {name: _hb(name, state="flat") for name in FLEET}
    html = _render_position_panel(hbs)
    assert "no position held" in html


def test_position_panel_shows_position_holder():
    hbs = {name: _hb(name) for name in FLEET}
    hbs["regime"] = _hb("regime", state="long")
    html = _render_position_panel(hbs)
    assert "regime" in html
    assert "LONG" in html


def test_position_panel_warns_on_multiple_holders():
    hbs = {"ignition": _hb("ignition", state="long"),
           "boundary": _hb("boundary", state="short")}
    html = _render_position_panel(hbs)
    assert "state desync" in html or "desync" in html


def test_strategy_cards_include_all_four():
    snaps = {n: {"n_trades": 10, "net_pnl": 25.0, "win_rate": 0.5,
                 "profit_factor": 1.2, "sharpe": 0.5, "max_drawdown": 5}
             for n in FLEET}
    strats = {n: {"name": n, "state": "SHADOW", "score": 0.3, "tier": 2}
              for n in FLEET}
    html = _render_strategy_cards(strats, snaps)
    for name in FLEET:
        assert name in html
    assert "SHADOW" in html


def test_promotion_gate_renders_thresholds():
    strats = {n: {"name": n, "state": "SHADOW", "score": 0.3}
              for n in FLEET}
    html = _render_promotion_gate(strats)
    assert "PILOT" in html
    assert "LIVE" in html
    # Each row should have a score
    for name in FLEET:
        assert name in html


def test_promotion_gate_progress_for_high_score():
    strats = {"ignition": {"name": "ignition", "state": "SHADOW", "score": 0.70}}
    html = _render_promotion_gate(strats)
    assert "LIVE-eligible" in html


def test_hour_heatmap_aggregates_pnl():
    # Three closes at 03:00 CT (08 UTC in CDT) with net=+10 each
    closes = [_close(net_pnl=10, hours_ago=h) for h in (1, 2, 3)]
    buckets = _bucket_closes_by_hour(closes)
    assert sum(v["n"] for v in buckets.values()) == 3
    assert sum(v["net_pnl"] for v in buckets.values()) == 30


def test_hour_heatmap_empty_state():
    html = _render_hour_heatmap([])
    assert "no closed trades" in html


def test_bars_held_renders_distribution():
    closes = [
        _close(bars_held_minutes=2),  # 1 bar
        _close(bars_held_minutes=4),  # 2 bars
        _close(bars_held_minutes=8),  # 4 bars
    ]
    html = _render_bars_held(closes)
    assert "ignition" in html
    # The 33% bar-1 should NOT be flagged (warn fires at >20%)
    # But all 3 trades show, so n=3 must appear
    assert "n=3" in html


def test_bars_held_empty_state():
    html = _render_bars_held([])
    assert "waiting for closed trades" in html


def test_recent_trades_renders():
    closes = [_close(strategy="session", net_pnl=15)]
    html = _render_recent_trades(closes)
    assert "session" in html


def test_recent_trades_empty_state():
    html = _render_recent_trades([])
    assert "waiting" in html


# ════════════ render_overview — end-to-end ═════════════════════════


def test_render_overview_with_empty_data():
    sb = _FakeSupabase()
    html = render_overview(sb, token="t")
    assert "Acme Futures" in html
    assert "v3 archive" in html  # footer link present


def test_render_overview_with_full_data():
    hbs = [_hb(n) for n in FLEET]
    strats = [{"name": n, "state": "SHADOW", "score": 0.3, "tier": 2}
              for n in FLEET]
    snaps = {n: {"strategy": n, "n_trades": 5, "net_pnl": 12.0,
                 "win_rate": 0.4, "profit_factor": 1.1,
                 "sharpe": 0.3, "max_drawdown": 3}
             for n in FLEET}
    closes = [_close(strategy=n) for n in FLEET]
    sb = _FakeSupabase(heartbeats=hbs, strategies=strats, snaps=snaps, closes=closes)
    html = render_overview(sb, token="t")
    assert "<!doctype html>" in html
    assert "Live Position" in html
    assert "Strategy Performance" in html
    assert "Promotion Gate" in html
    assert "Hour-of-day" in html
    assert "Bars-held" in html
    assert "Recent Closes" in html
