from datetime import UTC, datetime, timedelta

from acme.broker.base import Bar
from acme.risk import TOPSTEP_50K, DailyState
from acme.strategies.ema_cross import EmaCrossConfig, EmaCrossStrategy


def _state(peak: float = 50_000.0, realized: float = 0.0) -> DailyState:
    return DailyState(
        trade_date=datetime.now(UTC).date(),
        starting_balance=50_000.0,
        realized_pnl=realized,
        peak_balance_eod=peak,
        max_loss_limit=peak - TOPSTEP_50K.max_loss_amount,
        daily_loss_limit=TOPSTEP_50K.daily_loss_limit,
    )


def _bars_from_closes(closes: list[float]) -> list[Bar]:
    t0 = datetime(2026, 4, 29, 14, 0, tzinfo=UTC)
    return [
        Bar(t=t0 + timedelta(minutes=i), o=c, h=c, l=c, c=c, v=1)
        for i, c in enumerate(closes)
    ]


def test_warmup_emits_no_signal():
    strat = EmaCrossStrategy(EmaCrossConfig(fast=3, slow=5))
    state = _state()
    closes = [100.0, 100.0, 100.0, 100.0]   # fewer than `slow` bars
    for bar in _bars_from_closes(closes):
        sig = strat.on_bar(
            bar, state=state, profile=TOPSTEP_50K, current_position=0,
            current_balance_unrealized=50_000,
        )
        assert sig is None


def test_emits_buy_on_upward_cross():
    strat = EmaCrossStrategy(EmaCrossConfig(fast=3, slow=5))
    state = _state()
    # Establish a downtrend so fast<slow, then sharp uptrend so fast>slow.
    closes = [100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90,
              92, 95, 99, 105, 112, 120, 130, 140, 150, 160]
    saw_buy = False
    for bar in _bars_from_closes(closes):
        sig = strat.on_bar(
            bar, state=state, profile=TOPSTEP_50K, current_position=0,
            current_balance_unrealized=50_000,
        )
        if sig and sig.size > 0 and sig.side == "buy":
            saw_buy = True
            assert sig.bracket is not None
            assert sig.bracket.stop_loss_offset_ticks == 8
            assert sig.bracket.take_profit_offset_ticks == 16
            break
    assert saw_buy, "expected a buy signal on upward cross"


def test_no_signal_when_already_in_position():
    strat = EmaCrossStrategy(EmaCrossConfig(fast=3, slow=5))
    state = _state()
    closes = [100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90,
              92, 95, 99, 105, 112, 120, 130, 140, 150, 160]
    saw_signal = False
    for bar in _bars_from_closes(closes):
        sig = strat.on_bar(
            bar, state=state, profile=TOPSTEP_50K, current_position=1,   # already long
            current_balance_unrealized=50_000,
        )
        if sig is not None:
            saw_signal = True
    assert not saw_signal, "should suppress signals while already in a position"


def test_blocked_by_trailing_dd_returns_zero_size():
    strat = EmaCrossStrategy(EmaCrossConfig(fast=3, slow=5))
    state = _state(peak=50_000)
    closes = [100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90,
              92, 95, 99, 105, 112, 120, 130, 140, 150, 160]
    blocked = False
    for bar in _bars_from_closes(closes):
        sig = strat.on_bar(
            bar, state=state, profile=TOPSTEP_50K, current_position=0,
            current_balance_unrealized=47_500,   # below MLL of 48_000
        )
        if sig is not None:
            assert sig.size == 0
            assert "blocked" in sig.reason
            blocked = True
            break
    assert blocked, "expected at least one blocked signal"
