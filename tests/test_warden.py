"""Tests for the Warden v1 heartbeat monitor.

The monitor is pure given Supabase fixtures — we hand it a fake client
and verify the events it would emit. No actual Supabase access here.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# warden/ is at repo root, not under src/acme/. Put repo root on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from warden.monitors.heartbeat import (
    FLEET,
    LIVE_MAX_S,
    OFFLINE_MIN_S,
)
from warden.monitors.heartbeat import (
    run as run_heartbeat,
)


def _hb(service: str, age_s: int) -> dict:
    ts = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    return {"service": service, "ts": ts, "last_bar_ts": ts,
            "position_state": "flat", "auth_ok": True, "consecutive_errors": 0,
            "extra": {}}


class _FakeSB:
    """Fake Supabase client just enough for the heartbeat monitor."""

    def __init__(self, heartbeats=None, open_events=None):
        self._heartbeats = heartbeats or []
        self._open_events = open_events or []

    def table(self, name):
        return _FakeQuery(self, name)


class _FakeQuery:
    def __init__(self, parent, name):
        self.parent = parent
        self.name = name

    def select(self, *_a):
        return self
    def in_(self, *_a):
        return self
    def eq(self, *_a):
        return self
    def gte(self, *_a):
        return self
    def order(self, *_a, **_kw):
        return self

    def execute(self):
        if self.name == "runtime_heartbeats":
            return _Resp(self.parent._heartbeats)
        if self.name == "operator_events":
            return _Resp(self.parent._open_events)
        return _Resp([])


class _Resp:
    def __init__(self, data):
        self.data = data


# ════════════ all live → no events ═══════════════════════════════════


def test_all_live_no_events():
    hbs = [_hb(name, age_s=10) for name in FLEET]
    sb = _FakeSB(heartbeats=hbs)
    r = run_heartbeat(sb)
    assert r.events_to_emit == []
    assert r.events_to_resolve == []


# ════════════ stale → warn ══════════════════════════════════════════


def test_stale_strategy_emits_warn():
    hbs = [_hb("ignition", age_s=LIVE_MAX_S + 60)]   # 6 min stale
    hbs += [_hb(n, age_s=10) for n in FLEET if n != "ignition"]
    sb = _FakeSB(heartbeats=hbs)
    r = run_heartbeat(sb)
    stale_events = [e for e in r.events_to_emit if e["kind"] == "heartbeat_stale"]
    assert len(stale_events) == 1
    assert stale_events[0]["strategy"] == "ignition"
    assert stale_events[0]["severity"] == "warn"


# ════════════ offline → error ════════════════════════════════════════


def test_offline_strategy_emits_error():
    hbs = [_hb("session", age_s=OFFLINE_MIN_S + 60)]   # >15 min
    hbs += [_hb(n, age_s=10) for n in FLEET if n != "session"]
    sb = _FakeSB(heartbeats=hbs)
    r = run_heartbeat(sb)
    off_events = [e for e in r.events_to_emit if e["kind"] == "heartbeat_offline"]
    assert len(off_events) == 1
    assert off_events[0]["severity"] == "error"


# ════════════ missing row → unknown warn ════════════════════════════


def test_missing_heartbeat_emits_unknown():
    # 3 strategies have heartbeats, boundary doesn't
    hbs = [_hb(n, age_s=10) for n in FLEET if n != "boundary"]
    sb = _FakeSB(heartbeats=hbs)
    r = run_heartbeat(sb)
    unk = [e for e in r.events_to_emit if e["kind"] == "heartbeat_unknown"]
    assert len(unk) == 1
    assert unk[0]["strategy"] == "boundary"


# ════════════ dedup: existing open event suppresses re-emit ═════════


def test_no_duplicate_stale_event_when_already_open():
    hbs = [_hb("regime", age_s=LIVE_MAX_S + 120)]
    hbs += [_hb(n, age_s=10) for n in FLEET if n != "regime"]
    # An open heartbeat_stale event already exists for regime
    open_ev = [{
        "id": 99, "kind": "heartbeat_stale", "strategy": "regime",
        "resolved_at": None,
    }]
    sb = _FakeSB(heartbeats=hbs, open_events=open_ev)
    r = run_heartbeat(sb)
    stale_events = [e for e in r.events_to_emit if e["kind"] == "heartbeat_stale"]
    assert stale_events == []


# ════════════ recovery: open stale + now live → resolve + recover ═══


def test_recovered_strategy_resolves_open_event():
    # regime is now LIVE again, but there's an open heartbeat_stale event
    hbs = [_hb(n, age_s=10) for n in FLEET]
    open_ev = [{
        "id": 42, "kind": "heartbeat_stale", "strategy": "regime",
        "resolved_at": None,
    }]
    sb = _FakeSB(heartbeats=hbs, open_events=open_ev)
    r = run_heartbeat(sb)
    assert 42 in r.events_to_resolve
    rec = [e for e in r.events_to_emit if e["kind"] == "heartbeat_recovered"]
    assert len(rec) == 1
    assert rec[0]["strategy"] == "regime"
    assert rec[0]["severity"] == "info"
