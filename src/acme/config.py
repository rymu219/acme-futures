from __future__ import annotations

import os
from dataclasses import dataclass

import structlog
from dotenv import load_dotenv

from acme.risk import (
    EVAL_PROFILE_MAX_AGE_DAYS,
    PROFILES,
    EvalProfile,
    eval_profile_age_days,
    is_eval_profile_stale,
)

load_dotenv()

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Config:
    eval_profile: EvalProfile
    live: bool
    tz: str
    flatten_hh: int
    flatten_mm: int
    trade_window_start: tuple[int, int]
    trade_window_end: tuple[int, int]


def _parse_hhmm(s: str) -> tuple[int, int]:
    hh, mm = s.split(":")
    return int(hh), int(mm)


def load_config() -> Config:
    profile_name = os.getenv("ACME_EVAL_PROFILE", "topstep_50k")
    profile = PROFILES.get(profile_name)
    if not profile:
        raise ValueError(f"unknown ACME_EVAL_PROFILE={profile_name}; known={list(PROFILES)}")
    if is_eval_profile_stale(profile):
        log.warning(
            "eval_profile_stale",
            profile=profile.name,
            snapshot_date=(profile.snapshot_date.isoformat()
                           if profile.snapshot_date else None),
            age_days=eval_profile_age_days(profile),
            max_age_days=EVAL_PROFILE_MAX_AGE_DAYS,
            note="Re-validate against current Topstep docs and bump snapshot_date.",
        )
    flatten_hh, flatten_mm = _parse_hhmm(os.getenv("ACME_FLATTEN_HHMM", "15:55"))
    return Config(
        eval_profile=profile,
        live=os.getenv("ACME_LIVE", "false").lower() == "true",
        tz=os.getenv("ACME_TZ", "America/Chicago"),
        flatten_hh=flatten_hh,
        flatten_mm=flatten_mm,
        trade_window_start=_parse_hhmm(os.getenv("ACME_TRADE_WINDOW_START", "08:30")),
        trade_window_end=_parse_hhmm(os.getenv("ACME_TRADE_WINDOW_END", "15:50")),
    )
