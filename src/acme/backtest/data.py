"""Historical bar data loader for backtesting.

The Databento DBN file contains every MES contract that traded during the
window — multiple contracts overlap during roll periods. For backtesting we
want a single continuous series, so we filter to the front-month-as-of-each-bar
using the same calendar logic the live runner uses.

Pattern:
  raw DBN (1.14M rows) → filter to front-month → cache as parquet → stream Bars

The parquet cache is the source of truth for subsequent runs. First call is
slow (~10 sec to filter); subsequent calls read the parquet directly (<1 sec).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import structlog

from acme.broker.base import Bar
from acme.contracts import front_month_code

log = structlog.get_logger(__name__)

DEFAULT_DBN_PATH = Path("historical data/glbx-mdp3-20240401-20260430.ohlcv-1m.dbn.zst")
CACHE_DIR = Path.home() / ".acme" / "backtest_cache"


def _expected_symbol(d: date) -> str:
    """Front-month MES symbol for a given date, in Databento's format
    (e.g. 'MESM4' for June 2024). The contracts.py front_month_code returns
    something like 'M24' (single digit year); Databento uses single-digit year too.
    """
    code = front_month_code(d)   # e.g. 'M26'
    letter = code[0]
    year_two_digits = code[1:]    # '26'
    year_single = year_two_digits[-1]  # '6' — Databento uses single-digit year
    return f"MES{letter}{year_single}"


def _load_dbn(dbn_path: Path):
    """Load the raw DBN file and return a pandas DataFrame.
    Imported lazily so test code doesn't pay the import cost.
    """
    import databento as db
    log.info("dbn_load_start", path=str(dbn_path))
    store = db.DBNStore.from_file(dbn_path)
    df = store.to_df()
    log.info("dbn_load_complete", rows=len(df))
    return df


def build_or_load_parquet(
    dbn_path: Path = DEFAULT_DBN_PATH,
    cache_dir: Path = CACHE_DIR,
    *,
    rebuild: bool = False,
) -> Path:
    """Convert the front-month-filtered DBN to parquet (cached).
    Returns the parquet path.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = cache_dir / f"{dbn_path.stem}.front_month.parquet"

    if parquet_path.exists() and not rebuild:
        log.info("parquet_cache_hit", path=str(parquet_path))
        return parquet_path

    df = _load_dbn(dbn_path)

    # Compute the expected front-month symbol for each row's date once
    # (much faster than per-row apply).
    log.info("front_month_filter_start", input_rows=len(df))
    df = df.reset_index()  # ts_event becomes a regular column
    df["bar_date"] = df["ts_event"].dt.date
    unique_dates = df["bar_date"].drop_duplicates()
    expected_by_date = {d: _expected_symbol(d) for d in unique_dates}
    df["expected_symbol"] = df["bar_date"].map(expected_by_date)
    df = df[df["symbol"] == df["expected_symbol"]]
    df = df.drop(columns=["bar_date", "expected_symbol"])
    log.info("front_month_filter_done", output_rows=len(df))

    df.to_parquet(parquet_path, index=False)
    log.info("parquet_written", path=str(parquet_path), size_mb=parquet_path.stat().st_size / 1e6)
    return parquet_path


def iter_bars(
    dbn_path: Path = DEFAULT_DBN_PATH,
    cache_dir: Path = CACHE_DIR,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    rebuild_cache: bool = False,
) -> Iterator[Bar]:
    """Stream Bars from the front-month-filtered parquet, optionally bounded
    to [start, end]. Bars are yielded in chronological order with UTC timestamps.
    """
    parquet_path = build_or_load_parquet(dbn_path, cache_dir, rebuild=rebuild_cache)
    import pandas as pd
    df = pd.read_parquet(parquet_path)
    df = df.sort_values("ts_event")
    if start is not None:
        df = df[df["ts_event"] >= start]
    if end is not None:
        df = df[df["ts_event"] <= end]
    log.info("iter_bars_start", n=len(df))
    for row in df.itertuples(index=False):
        yield Bar(
            t=row.ts_event.to_pydatetime(),
            o=float(row.open),
            h=float(row.high),
            l=float(row.low),
            c=float(row.close),
            v=int(row.volume),
        )


def bar_count(dbn_path: Path = DEFAULT_DBN_PATH, cache_dir: Path = CACHE_DIR) -> int:
    """Cheap row-count without materializing all bars."""
    parquet_path = build_or_load_parquet(dbn_path, cache_dir)
    import pyarrow.parquet as pq
    return pq.read_metadata(parquet_path).num_rows


def env_dbn_path() -> Path:
    """Allow override via env var; otherwise use the default in `historical data/`."""
    return Path(os.getenv("ACME_DBN_PATH", str(DEFAULT_DBN_PATH)))
