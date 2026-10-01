"""Core FDP and rest calculations.

Regulatory basis
----------------
The rules here follow a deliberately simplified reading of EASA ORO.FTL.225
(maximum FDP) and ORO.FTL.235 (minimum rest). Full EASA FTL is a 40-page
document with dozens of modifiers (augmented crew, in-flight rest, split duty,
time zone differences, delayed reporting, commander's discretion, etc.).
We DO NOT model all of those. We model the unaugmented, un-split,
non-discretionary case, which covers the vast majority of short-haul pairings
and is the case that schedulers most need to sanity-check.

The simplifications, stated plainly so a reviewer can argue with them:

- Local clock time throughout. We do not convert to UTC. The caller is
  responsible for giving us times in the local time of the reporting base.
  Mixing timezones inside one duty is not supported.
- One duty = one FDP. No extension of FDP by in-flight rest, no augmented
  crew tables, no split-duty breaks.
- The daily rest requirement is the greater of (a) a fixed floor of 10 hours
  and (b) the duration of the preceding FDP. This is a conservative reading
  of ORO.FTL.235(e), which in practice allows the preceding FDP to be used as
  the rest target only when it exceeds 10h.
- Late-finish penalty: a duty ending in the WOCL reduces the next day's max
  FDP. We model the WOCL as 02:00-04:59 local. This is a simplification of the
  EASA WOCL definition, which is more nuanced (it's tied to the individual's
  acclimatised local time and runs 02:00-04:59).
- Max FDP table: we use a compact approximation of Table 1 (un-augmented
  crew) keyed on report time band and number of sectors. The bands are the
  canonical EASA report-time bands (05:00-05:59, 06:00-07:59, etc.).

If your operation needs augmented crew, split duty, or timezone conversions,
this library will give you the wrong answer and you should not use it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, List, Optional, Tuple


# ---- Constants ----------------------------------------------------------

#: A clock callable returns the current datetime. Tests inject a fake.
Clock = Callable[[], datetime]

#: Minimum daily rest floor. EASA ORO.FTL.235(e) sets 10h at home base.
MIN_REST_HOURS = 10

#: Window of Circadian Low (simplified). 02:00-04:59 local.
WOCL_START = (2, 0)
WOCL_END_EXCLUSIVE = (5, 0)


# ---- Max FDP table (un-augmented, simplified) ---------------------------
# Keys are (report_hour_start, report_hour_end_exclusive, max_sectors_inclusive).
# Values are max FDP in hours. Derived from EASA ORO.FTL.225 Table 1, collapsed
# to three sector buckets (1, 2, >=3) for compactness. The 2-sector and 3-sector
# buckets use the EASA 2-sector and 4-sector columns respectively; this is
# slightly conservative for exactly 3 sectors, which is acceptable for a
# legality sanity-check tool.
_MAX_FDP_TABLE: List[Tuple[Tuple[int, int, int], int]] = [
    ((0, 5, 1), 11),
    ((0, 5, 2), 10),
    ((0, 5, 3), 9),
    ((5, 6, 1), 13),
    ((5, 6, 2), 12),
    ((5, 6, 3), 11),
    ((6, 8, 1), 14),
    ((6, 8, 2), 13),
    ((6, 8, 3), 12),
    ((8, 13, 1), 13),
    ((8, 13, 2), 12),
    ((8, 13, 3), 11),
    ((13, 15, 1), 12),
    ((13, 15, 2), 11),
    ((13, 15, 3), 10),
    ((15, 18, 1), 11),
    ((15, 18, 2), 10),
    ((15, 18, 3), 9),
    ((18, 24, 1), 12),
    ((18, 24, 2), 11),
    ((18, 24, 3), 10),
]


def _max_fdp(report_hour: int, num_sectors: int) -> int:
    """Look up the maximum FDP in hours for the given report hour and sector count.

    Sectors >=3 collapse into the 3-bucket. If no band matches (should not
    happen for hour 0..23), fall back to the most restrictive value, 9h, so a
    malformed input never inflates a limit.
    """
    sectors = min(num_sectors, 3)
    fallback = 9
    for (start, end, max_sec), limit in _MAX_FDP_TABLE:
        if start <= report_hour < end and sectors <= max_sec:
            return limit
    return fallback


# ---- Time helpers -------------------------------------------------------


def parse_time(value: str) -> datetime:
    """Parse a local datetime string in ISO-8601 form.

    Accepts ``YYYY-MM-DDTHH:MM`` or ``YYYY-MM-DD HH:MM``. Seconds may be
    present but are truncated to whole minutes, because FDP limits are in
    whole minutes and carrying seconds gives a false sense of precision.

    We deliberately do not accept timezone offsets. The library operates in
    local clock time by design; accepting offsets would imply a UTC conversion
    we do not perform.
    """
    if not isinstance(value, str):
        raise TypeError(f"parse_time expects str, got {type(value).__name__}")
    normalised = value.strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise ValueError(f"Could not parse {value!r} as YYYY-MM-DDTHH:MM") from exc
    if dt.tzinfo is not None:
        raise ValueError(
            "Timezone-aware datetimes are not supported; pass local clock time."
        )
    return dt.replace(second=0, microsecond=0)


def _in_wocl(dt: datetime) -> bool:
    """True if ``dt`` falls within the simplified WOCL (02:00-04:59 local)."""
    h, m = dt.hour, dt.minute
    start_minutes = WOCL_START[0] * 60 + WOCL_START[1]
    end_minutes = WOCL_END_EXCLUSIVE[0] * 60 + WOCL_END_EXCLUSIVE[1] - 1  # inclusive end
    cur = h * 60 + m
    return start_minutes <= cur <= end_minutes


def _duty_spans_wocl(start: datetime, end: datetime) -> bool:
    """True if any portion of [start, end] touches the WOCL on the start date.

    We check against the start date's WOCL. A duty starting before 05:00 and
    ending after 02:00 on the same calendar day counts as WOCL-impacted. For
    multi-day duties we conservatively check only the first midnight's WOCL;
    most FDPs under 13h will not span a second one, and the simplified model
    does not pretend to handle ultra-long-haul.
    """
    # WOCL on the start date (02:00 to 04:59 of start.date()).
    day = start.date()
    wocl_start = datetime.combine(day, datetime.min.time()).replace(hour=2)
    wocl_end = datetime.combine(day, datetime.min.time()).replace(hour=5)  # exclusive
    if start < wocl_end and end > wocl_start:
        return True
    # Also consider the WOCL after the first midnight (early hours of day+1).
    next_day = day + timedelta(days=1)
    wocl_start2 = datetime.combine(next_day, datetime.min.time()).replace(hour=2)
    wocl_end2 = datetime.combine(next_day, datetime.min.time()).replace(hour=5)
    if start < wocl_end2 and end > wocl_start2:
        return True
    return False


# ---- Data classes -------------------------------------------------------


@dataclass(frozen=True)
class CrewMember:
    """A crew member with an acclimatisation base date.

    ``acclimatised_as_of`` is the local date on which the member is considered
    acclimatised to their base time zone. A duty whose report date differs by
    more than 2 days from this date is treated as ``acclimatised=False``; under
    EASA, an unacclimatised crew member has different (lower) limits, but the
    simplified model here only surfaces the flag — it does NOT apply separate
    unacclimatised tables. Callers must not use an unacclimatised result as a
    go-ahead.
    """

    name: str
    acclimatised_as_of: date

    def is_acclimatised(self, on_date: date) -> bool:
        delta = abs((on_date - self.acclimatised_as_of).days)
        return delta <= 2


@dataclass(frozen=True)
class Duty:
    """A single unaugmented duty.

    ``report`` and ``debrief_end`` are local clock times; no timezone offsets
    allowed (see parse_time). ``sectors`` is the count of flight sectors in the
    duty; positioning flights count as sectors per EASA ORO.FTL.105.
    """

    report: datetime
    debrief_end: datetime
    sectors: int = 1

    def __post_init__(self) -> None:
        if self.report.tzinfo is not None or self.debrief_end.tzinfo is not None:
            raise ValueError("Duty times must be timezone-naive local clock time.")
        if self.debrief_end <= self.report:
            raise ValueError("debrief_end must be after report.")
        if self.sectors < 1:
            raise ValueError("sectors must be >= 1.")
        # Truncate to whole minutes for consistency with parse_time.
        object.__setattr__(self, "report", self.report.replace(second=0, microsecond=0))
        object.__setattr__(
            self, "debrief_end", self.debrief_end.replace(second=0, microsecond=0)
        )

    @property
    def fdp_start(self) -> datetime:
        # FDP starts at report time per EASA ORO.FTL.105(a).
        return self.report

    @property
    def fdp_end(self) -> datetime:
        # FDP ends at the latest of: blocks-off + sector times, or the specified
        # debrief end. In the simplified model we take debrief_end as the FDP
        # end directly; the caller is expected to set it to the post-flight
        # duty finish. See README for the limitation.
        return self.debrief_end

    @property
    def fdp_duration(self) -> timedelta:
        return self.fdp_end - self.fdp_start

    @property
    def debrief_duration(self) -> timedelta:
        # We assume a 30-minute debrief that is INCLUDED in debrief_end but
        # EXCLUDED from FDP. This is the common European short-haul convention.
        # If the duty is shorter than 30 minutes (degenerate), debrief is
        # clamped to the FDP duration.
        candidate = timedelta(minutes=30)
        if candidate > self.fdp_duration:
            return self.fdp_duration
        return candidate

    @property
    def fdp_without_debrief(self) -> timedelta:
        return self.fdp_duration - self.debrief_duration


@dataclass(frozen=True)
class FDPResult:
    """Outcome of an FDP calculation for a single duty."""

    duty: Duty
    max_fdp: timedelta
    actual_fdp: timedelta
    within_limit: bool
    margin: timedelta  # max_fdp - actual_fdp; negative if over
    spans_wocl: bool
    acclimatised: bool

    @property
    def over_limit(self) -> bool:
        return not self.within_limit


@dataclass(frozen=True)
class RestRequirement:
    """Minimum rest required before the next duty, given the preceding FDP."""

    preceding_fdp: timedelta
    min_rest: timedelta
    basis: str  # human-readable reason


# ---- Public functions ---------------------------------------------------


def calculate_fdp(duty: Duty, crew: Optional[CrewMember] = None) -> FDPResult:
    """Evaluate a single duty against the FDP limit table.

    Returns an FDPResult. Does not mutate inputs. Deterministic given the
    Duty object; the optional crew member only affects the ``acclimatised``
    flag on the result, never the numeric limit (see CrewMember docstring).
    """
    actual = duty.fdp_without_debrief
    max_h = _max_fdp(duty.report.hour, duty.sectors)

    # Late finish / WOCL penalty: if the duty touches the WOCL, reduce the
    # max FDP by 30 minutes, capped at 0. This mirrors the EASA principle
    # that WOCL intrusion costs capacity; the exact 30 minutes is the
    # simplification, not the regulation's own formula.
    spans_wocl = _duty_spans_wocl(duty.report, duty.debrief_end)
    penalty = timedelta(minutes=30) if spans_wocl else timedelta(0)
    max_fdp = max(timedelta(0), timedelta(hours=max_h) - penalty)

    acclimatised = True
    if crew is not None:
        acclimatised = crew.is_acclimatised(duty.report.date())

    return FDPResult(
        duty=duty,
        max_fdp=max_fdp,
        actual_fdp=actual,
        within_limit=actual <= max_fdp,
        margin=max_fdp - actual,
        spans_wocl=spans_wocl,
        acclimatised=acclimatised,
    )


def calculate_rest(
    preceding_duty: Optional[Duty],
    next_report: Optional[datetime] = None,
    now: Optional[datetime] = None,
    clock: Optional[Clock] = None,
) -> RestRequirement:
    """Compute the minimum rest that must be available before the next report.

    Two modes:

    - If ``next_report`` is given, the actual available rest is
      ``next_report - preceding_duty.debrief_end`` and we check it against
      the minimum. The returned ``min_rest`` is the regulatory floor; the
      caller can compare.
    - If ``next_report`` is None, we return the floor only and the caller
      schedules around it.

    ``now``/``clock`` are accepted for API symmetry and future use (e.g. a
    commander's discretion countdown). They are not used in the current
    computation, which is fully time-difference based. Passing them does
    nothing and passing nothing is fine.
    """
    del now, clock  # accepted but unused; documented above.

    if preceding_duty is None:
        return RestRequirement(
            preceding_fdp=timedelta(0),
            min_rest=timedelta(hours=MIN_REST_HOURS),
            basis="no preceding duty; 10h floor applies",
        )

    fdp = preceding_duty.fdp_without_debrief
    floor = timedelta(hours=MIN_REST_HOURS)
    # Rest must be at least the preceding FDP if that exceeds the 10h floor.
    # This is the conservative reading described in the module docstring.
    target = max(floor, fdp)
    basis = (
        f"preceding FDP {int(fdp.total_seconds()//3600)}h exceeds 10h floor; "
        "rest must match FDP"
        if fdp > floor
        else "10h floor (preceding FDP not longer)"
    )

    if next_report is not None:
        available = next_report - preceding_duty.debrief_end
        if available < target:
            basis += (
                f"; available rest {int(available.total_seconds()/60)}m "
                f"< required {int(target.total_seconds()/60)}m"
            )

    return RestRequirement(
        preceding_fdp=fdp,
        min_rest=target,
        basis=basis,
    )
