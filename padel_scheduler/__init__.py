"""Generator jadwal meet padel: Americano, tiered, Mexicano, team, dan
format bersegmen (putra / putri / mixed).

Pemakaian singkat:

    from padel_scheduler import Player, Config, Segment, build_schedule

    players = [Player(id=i, name=f"P{i}", rating=3.0) for i in range(8)]
    cfg = Config(courts=2, duration_minutes=120, mode="americano")
    sch = build_schedule(players, cfg)
"""

from .capacity import CapacityReport, Issue, analyze, suggest_setup
from .economics import Economics, Option, compare, evaluate, upgrade_analysis
from .models import (
    COURT_NAME_MAX,
    COURT_PREFERENCES,
    MODES,
    SEGMENT_RULES,
    Config,
    Match,
    PairStat,
    Player,
    PreferenceViolation,
    Round,
    Schedule,
    ScheduleStats,
    Segment,
)
from .presets import PRESETS, preset_segments
from .report import from_dict, to_csv, to_dict, to_text
from .scheduler import ScheduleError, build_schedule

__all__ = [
    "COURT_NAME_MAX",
    "COURT_PREFERENCES",
    "MODES",
    "SEGMENT_RULES",
    "PRESETS",
    "CapacityReport",
    "Config",
    "Economics",
    "Issue",
    "Match",
    "Option",
    "PairStat",
    "Player",
    "PreferenceViolation",
    "Round",
    "Schedule",
    "ScheduleError",
    "ScheduleStats",
    "Segment",
    "analyze",
    "build_schedule",
    "compare",
    "evaluate",
    "from_dict",
    "preset_segments",
    "suggest_setup",
    "to_csv",
    "to_dict",
    "to_text",
    "upgrade_analysis",
]

__version__ = "1.0.0"
