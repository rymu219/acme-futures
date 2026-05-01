from datetime import date

from acme.contracts import (
    MES,
    REGISTRY,
    front_month_code,
    resolve_mes_contract_id,
)


def test_mes_registry():
    assert REGISTRY["MES"] is MES
    assert MES.tick_size == 0.25
    assert MES.point_value == 5.0
    assert MES.tick_value == 1.25
    assert MES.exchange == "CME"


def test_front_month_picks_quarterly():
    # Mid-Feb: front month is March
    assert front_month_code(date(2026, 2, 1)) == "H26"
    # Early April: front month is June
    assert front_month_code(date(2026, 4, 1)) == "M26"
    # Late August: front month is September
    assert front_month_code(date(2026, 8, 1)) == "U26"
    # Early November: front month is December
    assert front_month_code(date(2026, 11, 1)) == "Z26"


def test_front_month_rolls_at_8_days_before_third_friday():
    # 3rd Friday of March 2026 = 2026-03-20. Roll = 2026-03-12.
    # On 2026-03-11 we're still on H26.
    assert front_month_code(date(2026, 3, 11)) == "H26"
    # On 2026-03-12 we've rolled to M26.
    assert front_month_code(date(2026, 3, 12)) == "M26"


def test_front_month_year_wrap():
    # Late December: front month is March of next year.
    # 3rd Friday Dec 2026 = 2026-12-18. Roll = 2026-12-10.
    assert front_month_code(date(2026, 12, 9)) == "Z26"
    assert front_month_code(date(2026, 12, 10)) == "H27"


def test_resolve_mes_contract_id_default(monkeypatch):
    monkeypatch.delenv("ACME_MES_OVERRIDE", raising=False)
    cid = resolve_mes_contract_id()
    assert cid.startswith("CON.F.US.MES.")
    assert len(cid.split(".")[-1]) == 3   # e.g. M26


def test_resolve_mes_contract_id_override(monkeypatch):
    monkeypatch.setenv("ACME_MES_OVERRIDE", "CON.F.US.MES.U26")
    assert resolve_mes_contract_id() == "CON.F.US.MES.U26"
