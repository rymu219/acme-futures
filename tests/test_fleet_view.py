"""Smoke tests for web/fleet_view.py — the Ghost Dog Capital dashboard."""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# fleet_view lives under web/, not in the acme package. Add it to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

from fleet_view import (  # noqa: E402
    FLEET,
    LIVE_THRESHOLD,
    PILOT_THRESHOLD,
    STRATEGY_META,
    _heartbeat_status,
    _render_active_trade,
    _render_footer,
    _render_hour_grid,
    _render_recent_closes,
    _render_strategy_cards,
    _render_topbar,
    _strategy_metrics,
    render_overview,
)

# ════════════ FakeSupabase ═════════════════════════════════════════


class _FakeSupabase:
    """In-memory mock for the Supabase client surface fleet_view uses."""

    def __init__(self, *, heartbeats=None, strategies=None, snaps=None,
                 closes=None, kill_switch_events=None):
        self._heartbeats = heartbeats or []
        self._strategies = strategies or []
        self._snaps = snaps or {}
        self._closes = closes or []
        self._kill_switch_events = kill_switch_events or []

    def table(self, name):
        return _FakeQuery(self, name)


class _FakeQuery:
    def __init__(self, parent, table):
        self.parent = parent
        self.table_name = table
        self._eq_filters: dict = {}

    def select(self, *_args):
        return self

    def in_(self, *_args):
        return self

    def eq(self, col, value):
        self._eq_filters[col] = value
        return self

    def gte(self, *_args):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        data: list = []
        if self.table_name == "runtime_heartbeats":
            data = self.parent._heartbeats
        elif self.table_name == "strategies":
            data = self.parent._strategies
        elif self.table_name == "broker_events":
            data = self.parent._closes
        elif self.table_name == "strategy_perf_snapshot":
            # _fetch_perf_snapshots issues one query per strategy with
            # .eq("strategy", name) — honor that filter.
            wanted = self._eq_filters.get("strategy")
            if wanted and wanted in self.parent._snaps:
                data = [self.parent._snaps[wanted]]
        elif self.table_name == "operator_events":
            data = self.parent._kill_switch_events
        return _FakeResp(data)


class _FakeResp:
    def __init__(self, data):
        self.data = data


# ════════════ fixtures ═════════════════════════════════════════════


def _hb(service, *, state="flat", age_s=10, contract="CON.F.US.MES.M26"):
    ts = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    return {
        "service": service, "ts": ts, "last_bar_ts": ts,
        "position_state": state, "auth_ok": True,
        "consecutive_errors": 0, "extra": {"contract_id": contract},
    }


def _close(*, strategy="boundary", net_pnl=10.0, hours_ago=1,
           bars_held_minutes=4, outcome="target"):
    ts = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    return {
        "id": 1, "occurred_at": ts, "kind": "dry_run_close",
        "strategy": strategy, "side": "buy", "size": 1, "price": 100.0,
        "raw": {
            "net_pnl": net_pnl, "outcome": outcome,
            "entry_price": 100.0, "exit_price": 100 + net_pnl / 5,
            "bars_held_minutes": bars_held_minutes,
        },
    }


# ════════════ pure helpers ═════════════════════════════════════════


def test_fleet_lists_keepers():
    # Post-eval-log dashboard update: fourth keeper go_no_go_levels joined
    # the displayed fleet alongside the original three.
    assert FLEET == ["boundary", "overnight_drift", "gap_fill", "go_no_go_levels"]
    # Every fleet member has a display-meta entry — otherwise renderers
    # silently fall back to .upper() which loses the racing-silk styling.
    for name in FLEET:
        assert name in STRATEGY_META


def test_heartbeat_status_live():
    label, color, _sub = _heartbeat_status(_hb("boundary", age_s=10))
    assert label == "LIVE"
    assert "green" in color  # CSS-var "var(--green2)"


def test_heartbeat_status_stale():
    label, _color, _sub = _heartbeat_status(_hb("boundary", age_s=500))
    assert label == "STALE"


def test_heartbeat_status_offline():
    label, _color, _sub = _heartbeat_status(_hb("boundary", age_s=2000))
    assert label == "OFFLINE"


def test_heartbeat_status_unknown_on_missing():
    label, _color, _sub = _heartbeat_status(None)
    assert label == "UNKNOWN"


def test_strategy_metrics_combines_snap_and_strategy():
    strategies = {"boundary": {"name": "boundary", "state": "PILOT",
                                "tier": 1, "score": 0.62}}
    snaps = {"boundary": {"strategy": "boundary", "n_trades": 20,
                          "net_pnl": 150.0, "win_rate": 0.55,
                          "profit_factor": 1.4, "sharpe": 0.8,
                          "max_drawdown": -25}}
    m = _strategy_metrics("boundary", strategies, snaps)
    assert m["n_trades"] == 20
    assert m["net_pnl"] == 150.0
    assert m["pf"] == 1.4
    assert m["state"] == "PILOT"
    # avg_pnl derived when not supplied: 150 / 20 = 7.5
    assert m["avg_pnl"] == 7.5


def test_strategy_metrics_empty_safe():
    m = _strategy_metrics("boundary", {}, {})
    assert m["n_trades"] == 0
    assert m["net_pnl"] == 0
    assert m["pf"] is None


def test_promotion_thresholds_well_ordered():
    # PILOT must promote at a lower score than LIVE — guards against a
    # config-style edit that accidentally inverts the ladder.
    assert PILOT_THRESHOLD < LIVE_THRESHOLD


# ════════════ section renderers ════════════════════════════════════


def test_topbar_shows_brand_and_last_bar():
    hbs = {name: _hb(name) for name in FLEET}
    html = _render_topbar(hbs)
    assert "Ghost Dog Capital" in html
    assert "LAST BAR" in html
    # Topbar text mentions the 50K eval context
    assert "$50K EVAL" in html


def test_topbar_renders_with_empty_heartbeats():
    html = _render_topbar({})
    assert "LAST BAR" in html
    assert "—" in html  # last-bar fallback


def test_active_trade_empty_when_all_flat():
    hbs = {name: _hb(name, state="flat") for name in FLEET}
    html = _render_active_trade(hbs)
    assert "NO ACTIVE POSITION" in html


def test_active_trade_shows_open_position():
    hbs = {name: _hb(name, state="flat") for name in FLEET}
    hbs["boundary"] = _hb("boundary", state="long")
    html = _render_active_trade(hbs)
    assert "ACTIVE TRADE" in html
    assert "LONG" in html
    assert "BOUNDARY" in html  # STRATEGY_META label


def test_active_trade_warns_on_multiple_holders():
    hbs = {
        "boundary":        _hb("boundary", state="long"),
        "overnight_drift": _hb("overnight_drift", state="short"),
        "gap_fill":        _hb("gap_fill", state="flat"),
    }
    html = _render_active_trade(hbs)
    assert "non-flat" in html


def test_strategy_cards_render_all_fleet_members():
    heartbeats = {name: _hb(name) for name in FLEET}
    strategies = {n: {"name": n, "state": "SHADOW", "score": 0.3, "tier": 2}
                  for n in FLEET}
    snaps = {n: {"strategy": n, "n_trades": 10, "net_pnl": 25.0,
                 "win_rate": 0.5, "profit_factor": 1.2, "sharpe": 0.4,
                 "max_drawdown": -5}
             for n in FLEET}
    html = _render_strategy_cards(heartbeats, strategies, snaps)
    for name in FLEET:
        assert STRATEGY_META[name]["label"] in html
    # Score below PILOT_THRESHOLD → SHADOW badge for all cards
    assert "SHADOW" in html


def test_strategy_cards_pilot_badge_at_threshold():
    heartbeats = {name: _hb(name) for name in FLEET}
    # Boundary clears the PILOT bar; others stay in SHADOW
    strategies = {
        "boundary":        {"name": "boundary",        "state": "PILOT",
                            "score": PILOT_THRESHOLD + 0.01, "tier": 1},
        "overnight_drift": {"name": "overnight_drift", "state": "SHADOW",
                            "score": 0.10, "tier": 2},
        "gap_fill":        {"name": "gap_fill",        "state": "SHADOW",
                            "score": 0.10, "tier": 2},
    }
    snaps: dict = {}
    html = _render_strategy_cards(heartbeats, strategies, snaps)
    assert "PILOT" in html
    assert "SHADOW" in html


def test_hour_grid_renders_24_cells_when_empty():
    html = _render_hour_grid([])
    assert "HOUR-OF-DAY" in html
    # Two rows of 12 hour-cells = 24 hour labels (00..23). Count "hh-cell".
    assert html.count("hh-cell") == 24


def test_hour_grid_aggregates_pnl_when_populated():
    closes = [_close(net_pnl=5, hours_ago=h) for h in (1, 2, 3, 4)]
    html = _render_hour_grid(closes)
    assert "HOUR-OF-DAY" in html
    # Cells with trades use the hh-pos class for positive aggregate P&L.
    assert "hh-pos" in html


def test_recent_closes_empty_state():
    html = _render_recent_closes([], {})
    assert "waiting for first close" in html


def test_recent_closes_renders_fleet_member():
    closes = [_close(strategy="boundary", net_pnl=15)]
    html = _render_recent_closes(closes, {})
    # Tag uses STRATEGY_META label, not the raw key
    assert "BOUNDARY" in html


def test_recent_closes_surfaces_open_position():
    hbs = {name: _hb(name, state="flat") for name in FLEET}
    hbs["gap_fill"] = _hb("gap_fill", state="long")
    html = _render_recent_closes([], hbs)
    # Open row should appear even when there are no closes yet
    assert "GAP FILL" in html
    assert "open" in html


def test_footer_carries_brand_and_refresh_hint():
    html = _render_footer()
    assert "Ghost Dog Capital" in html
    assert "auto-refresh" in html


# ════════════ render_overview — end-to-end ═════════════════════════


def test_render_overview_with_empty_data():
    sb = _FakeSupabase()
    html = render_overview(sb, token="t")
    assert "<!doctype html>" in html
    assert "Ghost Dog Capital" in html
    # The page must render even when every fetcher returns nothing.
    assert "NO ACTIVE POSITION" in html
    assert "HOUR-OF-DAY" in html


def test_render_overview_with_full_data():
    hbs = [_hb(n) for n in FLEET]
    strats = [{"name": n, "state": "SHADOW", "score": 0.3, "tier": 2}
              for n in FLEET]
    snaps = {n: {"strategy": n, "n_trades": 5, "net_pnl": 12.0,
                 "win_rate": 0.4, "profit_factor": 1.1, "sharpe": 0.3,
                 "max_drawdown": -3}
             for n in FLEET}
    closes = [_close(strategy=n) for n in FLEET]
    sb = _FakeSupabase(heartbeats=hbs, strategies=strats,
                       snaps=snaps, closes=closes)
    html = render_overview(sb, token="t")
    assert "<!doctype html>" in html
    for name in FLEET:
        assert STRATEGY_META[name]["label"] in html


def test_render_overview_accepts_legacy_bucket_kwarg():
    # render_overview keeps the `bucket` kwarg for backwards compatibility
    # with callers from before the Ghost Dog redesign — passing it must
    # not raise even though the value is currently unused.
    sb = _FakeSupabase()
    html = render_overview(sb, token="t", bucket="audit_winners")
    assert "<!doctype html>" in html
