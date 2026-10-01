"""Flight Duty Period calculator.

A small, dependency-free library for computing flight crew Flight Duty Period
(FDP) limits and minimum rest requirements under a common interpretation of
EASA ORO.FTL.105/225. The intent is to give schedulers and crew a single place
to ask "is this pairing legal, and how much rest must follow?".

See core.py for the implementation; this module re-exports the public surface.
"""

from .core import (
    CrewMember,
    Duty,
    FDPResult,
    RestRequirement,
    calculate_fdp,
    calculate_rest,
    parse_time,
)

__all__ = [
    "CrewMember",
    "Duty",
    "FDPResult",
    "RestRequirement",
    "calculate_fdp",
    "calculate_rest",
    "parse_time",
]

__version__ = "0.1.0"
